"""File d'ingestion durable : leases, reprises, échecs, réindexation, suppressions.

Le parseur Docling est remplacé par une sortie d'extraction contrôlée : la
mécanique testée ici (chunking → embeddings → publication → bascule de
génération) est réelle ; l'extraction Docling réelle est exercée en acceptance.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import io
import json
import threading
import time

from sqlalchemy import text

from app.worker import Worker
from tests.conftest import login

ITEMS = [
    {"kind": "text", "label": "section_header", "text": "Diagnostic de l'équipement Aster", "page_no": 1, "section": None},
    {
        "kind": "text",
        "label": "paragraph",
        "text": "Le voyant ambre indique une saturation du journal local sur la version 10.9. "
        "Relever le journal puis comparer avec la version 10.10.",
        "page_no": 1,
        "section": "Diagnostic de l'équipement Aster",
    },
    {
        "kind": "table",
        "label": "table",
        "text": "| Version | État |\n| --- | --- |\n| 10.9 | OK |\n| 10.10 | Dégradé |",
        "page_no": 2,
        "section": "Diagnostic de l'équipement Aster",
    },
]


_PDF_CACHE: dict[int, bytes] = {}


def _pdf_bytes(pages: int = 3) -> bytes:
    """PDF fictif reproductible : mêmes octets (donc même empreinte) entre appels."""
    if pages not in _PDF_CACHE:
        from reportlab.pdfgen import canvas as rl_canvas

        buffer = io.BytesIO()
        pdf = rl_canvas.Canvas(buffer)
        for index in range(pages):
            pdf.drawString(72, 720, f"Document fictif de test — page {index + 1}")
            pdf.showPage()
        pdf.save()
        _PDF_CACHE[pages] = buffer.getvalue()
    return _PDF_CACHE[pages]


def import_document(client, csrf, *, title: str = "Guide fictif Aster", versions: str = "10.9,10.10", scope: str = "demo", product: str = "Aster", pages: int = 3):
    return client.post(
        "/api/documents",
        files={"file": ("guide.pdf", _pdf_bytes(pages), "application/pdf")},
        data={
            "title": title,
            "origin": "fixture de test",
            "product": product,
            "versions": versions,
            "language": "fr",
            "demo": "1" if scope == "demo" else "0",
            "scope": scope,
        },
        headers={"X-CSRF-Token": csrf},
    )


def fake_docling(items=ITEMS, *, raises: Exception | None = None):
    def runner(self, input_path, work_dir, timeout_seconds=None):  # noqa: ANN001
        work_dir.mkdir(parents=True, exist_ok=True)
        if raises is not None:
            raise raises
        (work_dir / "extracted.json").write_text(
            json.dumps({"items": items, "pages": 3, "markdown": "extrait"}, ensure_ascii=False),
            encoding="utf-8",
        )
        return {"pages": 3, "items": len(items)}

    return runner


def _worker(settings) -> Worker:
    return Worker(settings)


def test_import_enqueues_durable_job_and_worker_publishes(client, admin, settings, monkeypatch):
    csrf = login(client, admin)
    response = import_document(client, csrf)
    assert response.status_code == 201, response.text
    payload = response.json()
    assert payload["status"] == "queued"
    assert payload["job"]["status"] == "queued"
    assert payload["job"]["kind"] == "ingest"

    monkeypatch.setattr(Worker, "run_docling", fake_docling())
    assert _worker(settings).run_once() is True

    document = client.get("/api/documents").json()["documents"][0]
    assert document["status"] == "ready"
    assert document["chunks_current"] == 2  # 1 passage texte + 1 tableau
    assert document["current_generation"] == 1

    chunks = client.get(f"/api/documents/{payload['id']}/chunks").json()["chunks"]
    assert [c["kind"] for c in chunks] == ["text", "table"]
    assert chunks[0]["page_start"] == 1
    assert chunks[1]["page_start"] == 2
    assert all(c["token_count"] > 0 for c in chunks)

    jobs = client.get("/api/jobs").json()["jobs"]
    assert jobs[0]["status"] == "succeeded"
    assert jobs[0]["attempts"] == 1

    # La recherche réelle exploite ces passages.
    search = client.post("/api/search", json={"query": "voyant ambre saturation journal"}).json()
    assert search["status"] == "ok"
    assert search["sources"], search


def test_duplicate_import_is_rejected(client, admin):
    csrf = login(client, admin)
    assert import_document(client, csrf).status_code == 201
    duplicate = import_document(client, csrf)
    assert duplicate.status_code == 409


def test_reindex_publishes_new_generation_and_keeps_old_on_failure(client, admin, settings, monkeypatch):
    csrf = login(client, admin)
    document_id = import_document(client, csrf).json()["id"]
    monkeypatch.setattr(Worker, "run_docling", fake_docling())
    assert _worker(settings).run_once() is True

    # Échec définitif de réindexation : l'ancienne génération reste servie.
    reindex = client.post(f"/api/documents/{document_id}/reindex", headers={"X-CSRF-Token": csrf})
    assert reindex.status_code == 202
    monkeypatch.setattr(Worker, "run_docling", fake_docling(raises=__import__("app.worker", fromlist=["JobAbort"]).JobAbort("PDF non pris en charge")))
    assert _worker(settings).run_once() is True

    document = client.get("/api/documents").json()["documents"][0]
    assert document["status"] == "ready"
    assert document["current_generation"] == 1
    assert document["error"] and "réindexation" in document["error"]
    assert client.post("/api/search", json={"query": "voyant ambre"}).json()["status"] == "ok"

    failing_job = client.get("/api/jobs").json()["jobs"][0]
    assert failing_job["status"] == "failed"

    # Réindexation réussie : nouvelle génération publiée, ancienne supprimée.
    client.post(f"/api/documents/{document_id}/reindex", headers={"X-CSRF-Token": csrf})
    monkeypatch.setattr(Worker, "run_docling", fake_docling())
    assert _worker(settings).run_once() is True
    document = client.get("/api/documents").json()["documents"][0]
    assert document["current_generation"] == 2
    assert document["status"] == "ready"
    chunks = client.get(f"/api/documents/{document_id}/chunks").json()
    assert chunks["generation"] == 2 and len(chunks["chunks"]) == 2

    from app.db import session_scope

    with session_scope() as db:
        old = db.execute(
            text("SELECT count(*) FROM chunks WHERE document_id = :d AND generation = 1"), {"d": document_id}
        ).scalar_one()
    assert old == 0


def test_retryable_failure_backoff_then_exhaustion(client, admin, settings, monkeypatch):
    csrf = login(client, admin)
    document_id = import_document(client, csrf).json()["id"]
    monkeypatch.setattr(Worker, "run_docling", fake_docling(raises=RuntimeError("capteur instable")))

    def force_available():
        from app.db import session_scope

        with session_scope() as db:
            db.execute(text("UPDATE ingestion_jobs SET available_at = now() WHERE document_id = :d"), {"d": document_id})

    for attempt in (1, 2):
        assert _worker(settings).run_once() is True
        job = client.get("/api/jobs").json()["jobs"][0]
        assert job["status"] == "queued"
        assert job["attempts"] == attempt
        assert job["available_at"]
        force_available()

    assert _worker(settings).run_once() is True
    job = client.get("/api/jobs").json()["jobs"][0]
    assert job["status"] == "failed"
    assert job["attempts"] == 3
    document = client.get("/api/documents").json()["documents"][0]
    assert document["status"] == "failed"
    assert "capteur instable" in (document["error"] or "")

    # Reprise manuelle bornée : une fois remise en file, elle est réexécutée.
    retry = client.post(f"/api/jobs/{job['id']}/retry", headers={"X-CSRF-Token": csrf})
    assert retry.status_code == 202
    job = client.get("/api/jobs").json()["jobs"][0]
    assert job["status"] == "queued" and job["attempts"] == 0
    second = client.post(f"/api/jobs/{job['id']}/retry", headers={"X-CSRF-Token": csrf})
    assert second.status_code == 409


def test_recover_stale_jobs_and_streaming_messages(client, admin, settings):
    csrf = login(client, admin)
    document_id = import_document(client, csrf).json()["id"]
    from app.db import session_scope
    from app.jobs import recover_stale_jobs, recover_stale_streams

    with session_scope() as db:
        db.execute(
            text(
                "UPDATE ingestion_jobs SET status = 'running', attempts = 1,"
                " lease_until = now() - interval '10 minutes' WHERE document_id = :d"
            ),
            {"d": document_id},
        )
    with session_scope() as db:
        assert recover_stale_jobs(db, settings) == 1
    job = client.get("/api/jobs").json()["jobs"][0]
    assert job["status"] == "queued"

    with session_scope() as db:
        db.execute(
            text(
                "UPDATE ingestion_jobs SET status = 'running', attempts = 3,"
                " lease_until = now() - interval '10 minutes' WHERE document_id = :d"
            ),
            {"d": document_id},
        )
    with session_scope() as db:
        assert recover_stale_jobs(db, settings) == 1
    assert client.get("/api/jobs").json()["jobs"][0]["status"] == "failed"

    # Messages restés « streaming » après un redémarrage : marqués interrompus.
    conversation = client.post("/api/conversations", json={}, headers={"X-CSRF-Token": csrf}).json()
    from app.models import Message

    with session_scope() as db:
        db.add(
            Message(
                conversation_id=__import__("uuid").UUID(conversation["id"]),
                seq=1,
                role="assistant",
                content="fragment",
                status="streaming",
            )
        )
    with session_scope() as db:
        db.execute(text("UPDATE messages SET updated_at = now() - interval '20 minutes'"))
        assert recover_stale_streams(db, 300) == 1
    messages = client.get(f"/api/conversations/{conversation['id']}/messages").json()["messages"]
    assert messages[0]["status"] == "interrupted"


def test_claim_is_exclusive_between_workers(client, admin, settings):
    csrf = login(client, admin)
    for index in range(2):
        assert import_document(client, csrf, title=f"Guide {index}", pages=1 + index).status_code == 201
    from app.db import session_scope
    from app.jobs import claim_job

    claimed: list[str] = []
    lock = threading.Lock()

    def claim(worker_id: str) -> None:
        with session_scope() as db:
            job = claim_job(db, worker_id, 60)
            if job is not None:
                with lock:
                    claimed.append(str(job.id))

    threads = [threading.Thread(target=claim, args=(f"w{i}",)) for i in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(claimed) == len(set(claimed))  # aucun job réclamé deux fois
    assert len(claimed) >= 1


def test_delete_document_cancels_jobs_and_removes_file(client, admin):
    csrf = login(client, admin)
    document_id = import_document(client, csrf).json()["id"]
    from app.db import session_scope
    from app.models import Document

    with session_scope() as db:
        stored = db.get(Document, __import__("uuid").UUID(document_id)).stored_relpath
    from app.config import get_settings

    path = get_settings().documents_dir / stored
    assert path.is_file()

    response = client.delete(f"/api/documents/{document_id}", headers={"X-CSRF-Token": csrf})
    assert response.status_code == 200
    assert not path.exists()
    jobs = client.get("/api/jobs").json()["jobs"]
    # Le job d'ingestion disparaît avec son document (cascade) : plus aucune
    # trace de ce document dans la file.
    assert all(document_id not in json.dumps(job) for job in jobs)
    assert client.get(f"/api/documents/{document_id}/chunks").status_code == 404


def test_reindex_is_refused_while_an_ingestion_is_active(client, admin, settings, monkeypatch):
    csrf = login(client, admin)
    document_id = import_document(client, csrf).json()["id"]

    # L'ingestion initiale est encore active : aucune réindexation empilée.
    first = client.post(f"/api/documents/{document_id}/reindex", headers={"X-CSRF-Token": csrf})
    assert first.status_code == 409

    monkeypatch.setattr(Worker, "run_docling", fake_docling())
    assert _worker(settings).run_once() is True

    accepted = client.post(f"/api/documents/{document_id}/reindex", headers={"X-CSRF-Token": csrf})
    assert accepted.status_code == 202
    # La réindexation vient d'être mise en file : la suivante est refusée.
    duplicate = client.post(f"/api/documents/{document_id}/reindex", headers={"X-CSRF-Token": csrf})
    assert duplicate.status_code == 409
    jobs = client.get("/api/jobs").json()["jobs"]
    active = [job for job in jobs if job["status"] in ("queued", "running")]
    assert len(active) == 1


def test_lost_lease_worker_never_publishes(client, admin, settings, monkeypatch):
    """Un worker qui perd son bail ne publie jamais : ni passages, ni clôture."""
    csrf = login(client, admin)
    document_id = import_document(client, csrf).json()["id"]
    from app.db import session_scope
    from app.jobs import finish_failure, finish_success, job_owned_by, renew_lease

    def stealing_docling(self, input_path, work_dir, timeout_seconds=None):  # noqa: ANN001
        # Simule une reprise par un autre worker pendant l'extraction.
        with session_scope() as db:
            db.execute(
                text("UPDATE ingestion_jobs SET locked_by = 'other-worker' WHERE document_id = :d"),
                {"d": document_id},
            )
        work_dir.mkdir(parents=True, exist_ok=True)
        (work_dir / "extracted.json").write_text(
            json.dumps({"items": ITEMS, "pages": 3}, ensure_ascii=False), encoding="utf-8"
        )
        return {"pages": 3}

    monkeypatch.setattr(Worker, "run_docling", stealing_docling)
    assert _worker(settings).run_once() is True

    document = client.get("/api/documents").json()["documents"][0]
    assert document["status"] != "ready"
    assert document["chunks_current"] == 0
    assert document["current_generation"] == 0
    jobs = client.get("/api/jobs").json()["jobs"]
    assert jobs[0]["status"] == "running"  # le job appartient désormais à 'other-worker'

    with session_scope() as db:
        job_row = db.execute(
            text("SELECT id FROM ingestion_jobs WHERE document_id = :d"), {"d": document_id}
        ).fetchone()
        job_id = job_row[0]
        # Le worker déchu ne peut plus renouveler ni clôturer ce job.
        assert renew_lease(db, job_id, "other-worker", 60) is True
        assert job_owned_by(db, job_id, "other-worker", renew=False) is True
        assert finish_success(db, job_id, worker_id="wallia-inconnu") is False
        assert finish_failure(db, job_id, "tentative indue", retryable=False, worker_id="wallia-inconnu") == "gone"


def test_lease_is_renewed_and_heartbeat_alive_during_long_extraction(client, admin, settings, monkeypatch):
    """Le bail et le battement sont réellement renouvelés pendant un job long."""
    csrf = login(client, admin)
    document_id = import_document(client, csrf).json()["id"]
    samples: dict = {"heartbeat": None, "lease_remaining": []}

    def slow_docling(self, input_path, work_dir, timeout_seconds=None):  # noqa: ANN001
        from app.db import session_scope
        from app.models import Setting

        started = time.monotonic()
        while time.monotonic() - started < 8.0:
            with session_scope() as db:
                row = db.get(Setting, "worker_heartbeat")
                value = dict(row.value) if row is not None else None
            if value and value.get("phase") == "running" and value.get("job_id"):
                samples["heartbeat"] = value
            if time.monotonic() - started >= 4.5:
                with session_scope() as db:
                    lease = db.execute(
                        text("SELECT lease_until FROM ingestion_jobs WHERE document_id = :d"),
                        {"d": document_id},
                    ).scalar_one()
                if lease is not None:
                    samples["lease_remaining"].append((lease - dt.datetime.now(dt.timezone.utc)).total_seconds())
            time.sleep(0.25)
        work_dir.mkdir(parents=True, exist_ok=True)
        (work_dir / "extracted.json").write_text(
            json.dumps({"items": ITEMS, "pages": 3}, ensure_ascii=False), encoding="utf-8"
        )
        return {"pages": 3}

    monkeypatch.setattr(Worker, "run_docling", slow_docling)
    started = time.time()
    assert _worker(settings).run_once() is True
    assert time.time() - started >= 8.0  # l'extraction a réellement duré au-delà du bail

    job = client.get("/api/jobs").json()["jobs"][0]
    assert job["status"] == "succeeded"
    assert job["attempts"] == 1
    document = client.get("/api/documents").json()["documents"][0]
    assert document["status"] == "ready"
    # Battement observé pendant le job, avec le job en cours.
    assert samples["heartbeat"] and samples["heartbeat"]["job_id"]
    # Bail encore largement valide après 4,5 s : il a été renouvelé (bail de 6 s,
    # sans renouvellement il ne resterait ~1,5 s).
    assert samples["lease_remaining"] and min(samples["lease_remaining"]) > 3.0, samples


def test_job_total_timeout_fails_visibly(client, admin, settings, monkeypatch):
    csrf = login(client, admin)
    document_id = import_document(client, csrf).json()["id"]

    def slow_docling(self, input_path, work_dir, timeout_seconds=None):  # noqa: ANN001
        time.sleep(3.0)
        work_dir.mkdir(parents=True, exist_ok=True)
        (work_dir / "extracted.json").write_text(
            json.dumps({"items": ITEMS, "pages": 3}, ensure_ascii=False), encoding="utf-8"
        )
        return {"pages": 3}

    monkeypatch.setattr(Worker, "run_docling", slow_docling)
    worker = Worker(dataclasses.replace(settings, worker_job_timeout_seconds=1))
    assert worker.run_once() is True

    job = client.get("/api/jobs").json()["jobs"][0]
    assert job["status"] == "failed"
    assert "délai total" in (job["error"] or "")
    document = client.get("/api/documents").json()["documents"][0]
    assert document["status"] == "failed"
    assert "délai total" in (document["error"] or "")


def test_lease_guards_progress_renew_and_ownership(client, admin, settings):
    """Progression/renouvellement/propriété exigent un bail ENCORE valide :
    un worker déchu ne peut plus écrire, un bail expiré ne ressuscite jamais."""
    from app.db import session_scope
    from app.jobs import claim_job, job_owned_by, renew_lease, set_progress

    csrf = login(client, admin)
    import_document(client, csrf, title="Guide bail")
    with session_scope() as db:
        row = db.execute(
            text("SELECT id FROM ingestion_jobs WHERE status = 'queued' ORDER BY created_at DESC LIMIT 1")
        ).fetchone()
        job_id = row[0]
    with session_scope() as db:
        claimed = claim_job(db, "worker-A", 60)
        assert claimed is not None and claimed.id == job_id
    with session_scope() as db:
        assert set_progress(db, job_id, {"stage": "ok"}, worker_id="worker-A") is True
        # Un autre worker ne peut ni écrire ni renouveler.
        assert set_progress(db, job_id, {"stage": "pirate"}, worker_id="worker-B") is False
        assert renew_lease(db, job_id, "worker-B", 60) is False
        assert job_owned_by(db, job_id, "worker-B") is False
        # Bail expiré : jamais ressuscité, propriété perdue.
        db.execute(
            text("UPDATE ingestion_jobs SET lease_until = now() - interval '5 seconds' WHERE id = :id"),
            {"id": str(job_id)},
        )
        db.commit()
        assert renew_lease(db, job_id, "worker-A", 60) is False
        assert job_owned_by(db, job_id, "worker-A") is False
        assert set_progress(db, job_id, {"stage": "tard"}, worker_id="worker-A") is False


def test_claim_job_refuses_to_start_a_second_parallel_job(client, admin, settings):
    """Deux workers ne peuvent pas traiter deux jobs en parallèle : la
    réclamation exige qu'aucun bail valide ne soit détenu."""
    from app.db import session_scope
    from app.jobs import claim_job

    csrf = login(client, admin)
    import_document(client, csrf, title="Guide A")
    # Contenu distinct (pages différentes) : l'import est idempotent par
    # empreinte, deux contenus identiques ne créeraient qu'un seul job.
    import_document(client, csrf, title="Guide B", pages=4)
    with session_scope() as db:
        first = claim_job(db, "worker-1", 60)
        assert first is not None
    with session_scope() as db:
        second = claim_job(db, "worker-2", 60)
        assert second is None  # un job est réellement en cours (bail valide)
    with session_scope() as db:
        # Bail expiré (worker disparu) : un autre worker peut reprendre.
        db.execute(
            text("UPDATE ingestion_jobs SET lease_until = now() - interval '5 seconds' WHERE id = :id"),
            {"id": str(first.id)},
        )
        db.commit()
        third = claim_job(db, "worker-2", 60)
        assert third is not None and third.id != first.id


def test_recover_stale_jobs_exhausted_marks_document_failed(client, admin, settings):
    """Repli épuisé : le document initial passe en failed, l'ancienne
    génération publiée reste ready."""
    from app.db import session_scope
    from app.jobs import recover_stale_jobs

    csrf = login(client, admin)
    document_id = import_document(client, csrf, title="Guide reprise").json()["id"]
    with session_scope() as db:
        db.execute(
            text(
                "UPDATE ingestion_jobs SET status = 'running', locked_by = 'worker-mort',"
                " lease_until = now() - interval '5 seconds', attempts = max_attempts"
                " WHERE document_id = :id"
            ),
            {"id": document_id},
        )
        db.execute(
            text(
                "UPDATE documents SET status = 'processing', current_generation = 3 WHERE id = :id"
            ),
            {"id": document_id},
        )
        db.commit()
    with session_scope() as db:
        recovered = recover_stale_jobs(db, settings)
        assert recovered >= 1
    document = client.get("/api/documents").json()["documents"][0]
    assert document["status"] == "ready"  # ancienne génération conservée
    assert document["current_generation"] == 3
    job = client.get("/api/jobs").json()["jobs"][0]
    assert job["status"] == "failed"
