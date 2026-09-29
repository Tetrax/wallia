"""Worker d'ingestion documentaire (Docling CPU + embeddings via l'API).

Un seul job documentaire simultané. Survit à une conversion invalide :
Docling tourne dans un sous-processus borné (timeout, limites locales).

Durabilité : le bail (lease) est renouvelé pendant tout le traitement par un
fil dédié (battement compris) ; le délai total du job est borné ; la propriété
du bail est revérifiée avant la publication et avant la clôture. Un worker qui
a perdu son bail ne public JAMAIS (l'ancienne génération éventuelle reste
servie, la reprise reprend le job).
"""
from __future__ import annotations

import json
import logging
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx
from sqlalchemy import text
from sqlalchemy.orm import Session

from . import chunking
from .config import Settings, get_settings
from .db import get_engine, session_scope
from .jobs import (
    claim_job,
    enqueue_job,
    finish_failure,
    finish_success,
    recover_stale_jobs,
    renew_lease,
    set_progress,
)
from .models import Document, Setting
from .security import utcnow

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s worker %(message)s")
log = logging.getLogger("wallia.worker")

WORKER_ID = f"{socket.gethostname()}"

# Verrou consultatif de SESSION : un seul worker traite la file documentaire.
WORKER_SESSION_LOCK_KEY = 20260928


class JobAbort(RuntimeError):
    """Échec non réessayable (document supprimé, entrée invalide, bail perdu)."""


def safe_error_text(error: object) -> str:
    """Message d'erreur stockable : jamais de paramètres SQL ni de dump brut.

    Les erreurs SQLAlchemy incluent souvent les paramètres liés (contenu
    utilisateur) dans leur rendu : on ne conserve que la classe d'erreur.
    """
    import sqlalchemy.exc

    if isinstance(error, sqlalchemy.exc.SQLAlchemyError):
        return f"{error.__class__.__name__} (détail SQL masqué)"
    text_value = " ".join(str(error).split())
    return text_value[:300]


class _LeaseKeeper(threading.Thread):
    """Renouvelle le bail et le battement pendant le traitement d'un job."""

    def __init__(self, worker: "Worker", job_id, lease_seconds: int) -> None:
        super().__init__(daemon=True, name=f"wallia-lease-{job_id}")
        self.worker = worker
        self.job_id = job_id
        self.lease_seconds = lease_seconds
        self.interval = max(2.0, lease_seconds / 3.0)
        self.lost = False
        self._done = threading.Event()

    def run(self) -> None:
        while not self._done.wait(self.interval):
            try:
                with session_scope() as db:
                    if not renew_lease(db, self.job_id, self.worker.worker_id, self.lease_seconds):
                        self.lost = True
                        return
                    self.worker.heartbeat(db, phase="running", job_id=str(self.job_id))
            except Exception as exc:  # noqa: BLE001 - base momentanément indisponible
                log.warning("renouvellement de bail différé: %s", exc.__class__.__name__)

    def stop(self) -> None:
        self._done.set()
        if self.is_alive():
            self.join(timeout=2)


class Worker:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.worker_id = WORKER_ID
        self._token_counter = None

    # -- heartbeat ---------------------------------------------------------
    def heartbeat(self, db: Session, *, phase: str, job_id: str | None = None) -> None:
        row = db.get(Setting, "worker_heartbeat")
        payload = {
            "worker_id": self.worker_id,
            "at": utcnow().isoformat(),
            "phase": phase,
            "job_id": job_id,
        }
        if row is None:
            db.add(Setting(key="worker_heartbeat", value=payload))
        else:
            row.value = payload
            row.updated_at = utcnow()
        db.commit()

    # -- tokenizer ---------------------------------------------------------
    def token_counter(self):
        if self._token_counter is None:
            allow_approx = self.settings.env == "test"
            self._token_counter = chunking.make_token_counter(self.settings.model_dir, allow_approx)
        return self._token_counter

    # -- embeddings via l'API (une seule copie du modèle) ------------------
    def embed_texts(self, texts: list[str], kind: str, deadline: float | None = None) -> list[list[float]]:
        """Embeddings par l'API interne, bornés par le délai RESTANT du job.

        Chaque tentative et chaque backoff consomment au plus le temps restant :
        un job presque terminé n'attend jamais 3 × 300 s.
        """
        url = os.environ.get("WALLIA_API_BASE_URL", "http://api:8000").rstrip("/") + "/internal/embeddings"
        payload = {"kind": kind, "texts": texts}
        headers = {"X-Internal-Token": self.settings.worker_token()}
        last_error: Exception | None = None
        for attempt in range(3):
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 1.0:
                raise RuntimeError("service d'embeddings indisponible: délai total du job épuisé")
            read_timeout = 300.0 if remaining is None else max(1.0, min(300.0, remaining))
            try:
                with httpx.Client(
                    timeout=httpx.Timeout(
                        connect=min(10.0, read_timeout),
                        read=read_timeout,
                        write=min(60.0, read_timeout),
                        pool=min(10.0, read_timeout),
                    )
                ) as client:
                    response = client.post(url, json=payload, headers=headers)
                if response.status_code == 200:
                    data = response.json()
                    vectors = data.get("vectors")
                    if not isinstance(vectors, list) or len(vectors) != len(texts):
                        raise RuntimeError("réponse d'embeddings incohérente")
                    return vectors
                raise RuntimeError(f"embeddings HTTP {response.status_code}")
            except Exception as exc:  # noqa: BLE001 - reprise bornée
                last_error = exc
                backoff = 2.0 * (attempt + 1)
                if deadline is not None:
                    remaining = deadline - time.monotonic()
                    if remaining <= backoff + 1.0:
                        break  # plus de temps utile pour une nouvelle tentative
                    backoff = min(backoff, remaining - 1.0)
                time.sleep(backoff)
        raise RuntimeError(f"service d'embeddings indisponible: {last_error.__class__.__name__ if last_error else 'inconnu'}")

    # -- extraction --------------------------------------------------------
    def run_docling(self, input_path: Path, work_dir: Path, timeout_seconds: float | None = None) -> dict:
        work_dir.mkdir(parents=True, exist_ok=True)
        env = dict(os.environ)
        env.update(
            {
                "OMP_NUM_THREADS": "2",
                "MKL_NUM_THREADS": "2",
                "TOKENIZERS_PARALLELISM": "false",
                "HOME": str(work_dir / "home"),
            }
        )
        (work_dir / "home").mkdir(exist_ok=True)
        # Le sous-processus tourne dans le dossier de travail : sans PYTHONPATH
        # explicite, `python -m app.docling_runner` ne trouverait plus le paquet.
        app_root = str(Path(__file__).resolve().parents[1])
        current = env.get("PYTHONPATH")
        env["PYTHONPATH"] = app_root if not current else f"{app_root}{os.pathsep}{current}"
        cmd = [
            sys.executable,
            "-m",
            "app.docling_runner",
            "--input",
            str(input_path),
            "--output",
            str(work_dir),
            "--models",
            str(self.settings.docling_models_dir),
        ]
        timeout = timeout_seconds if timeout_seconds is not None else float(self.settings.worker_docling_timeout_seconds)
        started = time.monotonic()
        proc = subprocess.run(
            cmd,
            cwd=str(work_dir),
            env=env,
            capture_output=True,
            text=True,
            timeout=max(1.0, timeout),
        )
        took = time.monotonic() - started
        (work_dir / "docling_stdout.log").write_text(proc.stdout or "", encoding="utf-8")
        (work_dir / "docling_stderr.log").write_text(proc.stderr or "", encoding="utf-8")
        if proc.returncode != 0:
            # Aucun stderr brut exposé : le détail reste dans les journaux locaux
            # du dossier de travail, jamais dans l'erreur publique du job.
            raise JobAbort(f"extraction Docling échouée (code {proc.returncode})")
        try:
            summary = json.loads((proc.stdout or "").strip().splitlines()[-1])
        except (ValueError, IndexError):
            summary = {}
        summary["duration_s"] = round(took, 2)
        log.info("Docling OK en %.1fs (%s pages)", took, summary.get("pages"))
        return summary

    # -- job principal -----------------------------------------------------
    def handle_ingest(self, db: Session, job, keeper: _LeaseKeeper, job_deadline: float) -> None:
        def ensure_lease() -> None:
            """Propriété du bail vérifiée de façon synchrone (et délai total)."""
            if keeper.lost:
                raise JobAbort("bail perdu — publication annulée, reprise par un autre worker")
            if time.monotonic() > job_deadline:
                raise JobAbort(f"délai total du job dépassé ({self.settings.worker_job_timeout_seconds}s)")
            with session_scope() as lease_db:
                if not renew_lease(lease_db, job.id, self.worker_id, self.settings.worker_lease_seconds):
                    keeper.lost = True
                    raise JobAbort("bail perdu — publication annulée, reprise par un autre worker")

        document = db.get(Document, job.document_id)
        if document is None:
            raise JobAbort("document supprimé")
        if document.status == "deleting":
            raise JobAbort("document en cours de suppression")
        doc_path = self.settings.documents_dir / document.stored_relpath
        if not doc_path.is_file():
            raise JobAbort("fichier source introuvable")

        generation = document.current_generation + 1
        work_dir = self.settings.data_dir / "ingestion" / str(document.id) / f"gen{generation}"

        set_progress(db, job.id, {"stage": "extraction_docling", "generation": generation}, worker_id=self.worker_id)
        # Réindexation : l'ancienne génération reste servie pendant le traitement.
        document.status = "processing" if document.current_generation == 0 else "ready"
        db.commit()
        ensure_lease()

        remaining = max(1.0, job_deadline - time.monotonic())
        summary = self.run_docling(doc_path, work_dir, timeout_seconds=min(float(self.settings.worker_docling_timeout_seconds), remaining))
        ensure_lease()

        extracted_path = work_dir / "extracted.json"
        if not extracted_path.is_file():
            raise JobAbort("sortie d'extraction absente")
        extracted = json.loads(extracted_path.read_text(encoding="utf-8"))
        items = extracted.get("items") or []
        if not items:
            raise JobAbort("aucun contenu exploitable extrait du document")

        set_progress(db, job.id, {"stage": "chunking", "items": len(items), "generation": generation}, worker_id=self.worker_id)
        counter = self.token_counter()
        chunks = chunking.chunk_items(items, counter)
        if not chunks:
            raise JobAbort("aucun passage généré")
        set_progress(db, job.id, {"stage": "embeddings", "chunks": len(chunks), "done": 0}, worker_id=self.worker_id)

        vectors: list[list[float]] = []
        batch = self.settings.embedding_batch_size
        for start in range(0, len(chunks), batch):
            window = chunks[start : start + batch]
            vectors.extend(self.embed_texts([c["text"] for c in window], kind="passage", deadline=job_deadline))
            set_progress(
                db,
                job.id,
                {"stage": "embeddings", "chunks": len(chunks), "done": len(vectors)},
                worker_id=self.worker_id,
            )
            ensure_lease()

        # Publication : propriété du bail exigée JUSTE AVANT l'insertion.
        ensure_lease()
        set_progress(db, job.id, {"stage": "publication", "chunks": len(chunks)}, worker_id=self.worker_id)
        # Publication atomique : nouvelle génération insérée puis basculée ;
        # l'ancienne génération n'est supprimée qu'en cas de succès.
        db.execute(
            text("DELETE FROM chunks WHERE document_id = :doc AND generation = :gen"),
            {"doc": str(document.id), "gen": generation},
        )
        for index, chunk in enumerate(chunks):
            db.execute(
                text(
                    """
                    INSERT INTO chunks (document_id, generation, seq, text, page_start, page_end, section, kind, token_count, embedding)
                    VALUES (:doc, :gen, :seq, :text, :ps, :pe, :section, :kind, :tokens, CAST(:embedding AS vector))
                    """
                ),
                {
                    "doc": str(document.id),
                    "gen": generation,
                    "seq": chunk["seq"],
                    "text": chunk["text"],
                    "ps": chunk["page_start"],
                    "pe": chunk["page_end"],
                    "section": chunk["section"],
                    "kind": chunk["kind"],
                    "tokens": chunk["token_count"],
                    "embedding": "[" + ",".join(f"{x:.7f}" for x in vectors[index]) + "]",
                },
            )
        document = db.get(Document, job.document_id)
        if document is None or document.status == "deleting":
            raise JobAbort("document supprimé pendant l'ingestion")
        document.status = "ready"
        document.current_generation = generation
        document.embedding_model = self.settings.embedding_model
        document.embedding_revision = self.settings.embedding_revision
        document.embedding_dim = self.settings.embedding_dim
        document.page_count = int(extracted.get("page_count") or 0) or document.page_count
        document.error = None
        db.execute(
            text("DELETE FROM chunks WHERE document_id = :doc AND generation < :gen"),
            {"doc": str(document.id), "gen": generation},
        )
        # FENCE DE PROPRIÉTÉ ATOMIQUE : la propriété (locked_by, statut, bail
        # ENCORE valide) est revérifiée DANS la transaction de publication,
        # sous verrou de ligne — jamais dans une transaction séparée.
        fence = db.execute(
            text("SELECT locked_by, status, lease_until FROM ingestion_jobs WHERE id = :id FOR UPDATE"),
            {"id": str(job.id)},
        ).fetchone()
        if (
            fence is None
            or fence[0] != self.worker_id
            or fence[1] != "running"
            or fence[2] is None
            or fence[2] <= utcnow()
        ):
            raise JobAbort("bail perdu — publication annulée, reprise par un autre worker")
        db.commit()
        log.info("document %s publié (génération %s, %s passages)", document.id, generation, len(chunks))

    def _processing_lock(self):
        """Verrou consultatif de SESSION sur une connexion dédiée.

        Un seul worker traite la file documentaire : si un autre worker le
        détient, `run_once` ne réclame rien. La connexion est fermée (ou
        invalidée) à la libération, donc le verrou ne survit jamais à un crash.
        """
        conn = get_engine().connect()
        try:
            acquired = bool(
                conn.execute(text("SELECT pg_try_advisory_lock(:key)"), {"key": WORKER_SESSION_LOCK_KEY}).scalar()
            )
        except Exception:
            conn.close()
            raise
        if not acquired:
            conn.close()
            return None
        return conn

    @staticmethod
    def _release_processing_lock(conn) -> None:
        try:
            conn.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": WORKER_SESSION_LOCK_KEY})
            conn.commit()
            conn.close()
        except Exception:
            # Invalidation : la coupure réelle de la connexion libère le verrou.
            conn.invalidate()

    def run_once(self) -> bool:
        lock_conn = self._processing_lock()
        if lock_conn is None:
            return False
        try:
            return self._run_once_locked()
        finally:
            self._release_processing_lock(lock_conn)

    def _run_once_locked(self) -> bool:
        with session_scope() as db:
            job = claim_job(db, self.worker_id, self.settings.worker_lease_seconds)
            if job is None:
                return False
            job_id = job.id
            job_deadline = time.monotonic() + float(self.settings.worker_job_timeout_seconds)
            keeper = _LeaseKeeper(self, job_id, self.settings.worker_lease_seconds)
            log.info("job %s (%s) démarré", job_id, job.kind)
            try:
                self.heartbeat(db, phase="running", job_id=str(job_id))
                keeper.start()
                if job.kind in ("ingest", "reindex"):
                    self.handle_ingest(db, job, keeper, job_deadline)
                else:
                    raise JobAbort(f"type de job inconnu: {job.kind}")
            except JobAbort as exc:
                log.warning("job %s abandonné: %s", job_id, exc)
                try:
                    db.rollback()
                    status = finish_failure(db, job_id, safe_error_text(exc), retryable=False, worker_id=self.worker_id)
                    if status != "gone":
                        self._mark_document_failed(db, job_id, safe_error_text(exc))
                except Exception as inner:  # noqa: BLE001
                    log.error("échec de clôture du job %s: %s", job_id, inner.__class__.__name__)
                return True
            except subprocess.TimeoutExpired:
                db.rollback()
                status = finish_failure(db, job_id, "délai d'extraction dépassé", retryable=True, worker_id=self.worker_id)
                if status != "gone":
                    self._mark_document_failed(db, job_id, "délai d'extraction dépassé")
                return True
            except Exception as exc:  # noqa: BLE001 - le worker doit survivre
                # Jamais de traceback : il pourrait écho des paramètres SQL ou du
                # contenu utilisateur. Seule la classe d'erreur est journalisée.
                log.error("job %s en erreur (%s)", job_id, exc.__class__.__name__)
                try:
                    db.rollback()
                    status = finish_failure(db, job_id, safe_error_text(exc), retryable=True, worker_id=self.worker_id)
                    if status != "gone":
                        self._mark_document_failed(db, job_id, safe_error_text(exc))
                except Exception as inner:  # noqa: BLE001
                    log.error("échec de clôture du job %s: %s", job_id, inner.__class__.__name__)
                return True
            finally:
                keeper.stop()
            try:
                if not finish_success(db, job_id, worker_id=self.worker_id):
                    log.warning("job %s non clôturé en succès (bail perdu)", job_id)
            except Exception as exc:  # noqa: BLE001
                log.error("fin de job %s non persistée: %s", job_id, exc.__class__.__name__)
            return True

    def _mark_document_failed(self, db: Session, job_id, error: str) -> None:
        """Le document reste consultable avec son ancienne génération si elle existe."""
        row = db.execute(
            text("SELECT document_id FROM ingestion_jobs WHERE id = :id"), {"id": str(job_id)}
        ).fetchone()
        if row is None or row[0] is None:
            return
        document = db.get(Document, row[0])
        if document is None or document.status == "deleting":
            return
        safe = safe_error_text(error)
        if document.current_generation > 0:
            document.status = "ready"  # l'indexation précédente reste servie
            document.error = f"dernière réindexation échouée: {safe}"
        else:
            document.status = "failed"
            document.error = safe
        db.commit()

    def run_forever(self) -> None:
        log.info("worker %s démarré (poll %.1fs)", self.worker_id, self.settings.worker_poll_seconds)
        last_recovery = 0.0
        while True:
            try:
                worked = self.run_once()
                now = time.monotonic()
                if now - last_recovery > 60:
                    with session_scope() as db:
                        count = recover_stale_jobs(db, self.settings)
                        if count:
                            log.warning("%s job(s) repris après bail expiré", count)
                        self.heartbeat(db, phase="idle")
                    last_recovery = now
                if not worked:
                    time.sleep(self.settings.worker_poll_seconds)
            except KeyboardInterrupt:
                log.info("arrêt demandé")
                return
            except Exception as exc:  # noqa: BLE001
                log.error("boucle worker: %s", exc.__class__.__name__)
                time.sleep(5)


def main() -> int:
    settings = get_settings()
    worker = Worker(settings)
    worker.run_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
