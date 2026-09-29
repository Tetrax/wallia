"""File d'attente durable des jobs d'ingestion (SQL, SKIP LOCKED + leases)."""
from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from .config import Settings
from .models import IngestionJob
from .security import utcnow

_BACKOFF_BASE_SECONDS = 20
_BACKOFF_MAX_SECONDS = 600
# Verrou consultatif transactionnel des réclamations (un job documentaire à la fois).
_CLAIM_LOCK_KEY = 20260927


def enqueue_job(db: Session, *, kind: str, document_id, max_attempts: int = 3) -> IngestionJob:
    job = IngestionJob(
        kind=kind,
        document_id=document_id,
        status="queued",
        max_attempts=max_attempts,
        available_at=utcnow(),
        progress={},
    )
    db.add(job)
    db.flush()
    return job


_CLAIM_SQL = text(
    """
    UPDATE ingestion_jobs
       SET status = 'running',
           attempts = attempts + 1,
           locked_by = :worker,
           lease_until = now() + make_interval(secs => :lease),
           started_at = COALESCE(started_at, now()),
           updated_at = now()
     WHERE id = (
         SELECT id FROM ingestion_jobs
          WHERE status = 'queued' AND available_at <= now()
          ORDER BY available_at ASC
          FOR UPDATE SKIP LOCKED
          LIMIT 1
     )
    RETURNING id
    """
)


def claim_job(db: Session, worker_id: str, lease_seconds: int) -> IngestionJob | None:
    """Réclame un job — un seul job documentaire réellement en cours à la fois.

    Le verrou transactionnel sérialise les réclamations concurrentes puis la
    garde interroge l'état réel : si un job tourne ENCORE avec un bail valide,
    aucun nouveau job n'est réclamé (deux workers ne traitent pas deux jobs en
    parallèle). Un bail expiré ne bloque pas la reprise.
    """
    db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _CLAIM_LOCK_KEY})
    busy = db.execute(
        text(
            "SELECT 1 FROM ingestion_jobs"
            " WHERE status = 'running' AND lease_until IS NOT NULL AND lease_until > now()"
            " LIMIT 1"
        )
    ).fetchone()
    if busy is not None:
        db.commit()
        return None
    row = db.execute(_CLAIM_SQL, {"worker": worker_id, "lease": lease_seconds}).fetchone()
    if row is None:
        db.commit()
        return None
    db.commit()
    job = db.get(IngestionJob, row[0])
    return job


def renew_lease(db: Session, job_id, worker_id: str, lease_seconds: int) -> bool:
    """Renouvelle le bail — uniquement s'il est ENCORE VALIDE.

    Un bail expiré n'est jamais ressuscité : le job pourrait déjà être repris
    par un autre worker, on ne prolonge pas une propriété perdue.
    """
    result = db.execute(
        text(
            """
            UPDATE ingestion_jobs
               SET lease_until = now() + make_interval(secs => :lease), updated_at = now()
             WHERE id = :id AND locked_by = :worker AND status = 'running'
               AND lease_until IS NOT NULL AND lease_until > now()
            """
        ),
        {"id": str(job_id), "worker": worker_id, "lease": lease_seconds},
    )
    db.commit()
    return result.rowcount > 0


def set_progress(db: Session, job_id, progress: dict[str, Any], *, worker_id: str | None = None) -> bool:
    """Progression du job ; l'écriture est gardée par la propriété d'un bail valide."""
    query = "UPDATE ingestion_jobs SET progress = CAST(:progress AS jsonb), updated_at = now() WHERE id = :id"
    params: dict[str, Any] = {
        "id": str(job_id),
        "progress": __import__("json").dumps(progress, ensure_ascii=False),
    }
    if worker_id is not None:
        query += (
            " AND locked_by = :worker AND status = 'running'"
            " AND lease_until IS NOT NULL AND lease_until > now()"
        )
        params["worker"] = worker_id
    result = db.execute(text(query), params)
    db.commit()
    return result.rowcount > 0


def job_owned_by(db: Session, job_id, worker_id: str, *, renew: bool = True, lease_seconds: int = 300) -> bool:
    """Propriété du bail vérifiée (bail ENCORE VALIDE exigé) et renouvelée si demandé.

    Renvoie False si le job n'appartient plus au worker (bail perdu ou expiré,
    repris ailleurs, supprimé) : le worker ne doit alors jamais publier.
    """
    if renew:
        return renew_lease(db, job_id, worker_id, lease_seconds)
    row = db.execute(
        text(
            "SELECT 1 FROM ingestion_jobs WHERE id = :id AND locked_by = :worker"
            " AND status = 'running' AND lease_until IS NOT NULL AND lease_until > now()"
        ),
        {"id": str(job_id), "worker": worker_id},
    ).fetchone()
    return row is not None


def has_active_job(db: Session, document_id, kind: str) -> bool:
    row = db.execute(
        text(
            "SELECT 1 FROM ingestion_jobs WHERE document_id = :doc AND kind = :kind"
            " AND status IN ('queued', 'running') LIMIT 1"
        ),
        {"doc": str(document_id), "kind": kind},
    ).fetchone()
    return row is not None


def finish_success(db: Session, job_id, worker_id: str | None = None) -> bool:
    """Clôture en succès ; si `worker_id` est fourni, exige encore la propriété du bail."""
    query = (
        "UPDATE ingestion_jobs SET status = 'succeeded', finished_at = now(), updated_at = now(),"
        " lease_until = NULL WHERE id = :id"
    )
    params: dict[str, Any] = {"id": str(job_id)}
    if worker_id is not None:
        query += " AND locked_by = :worker AND status = 'running'"
        params["worker"] = worker_id
    result = db.execute(text(query), params)
    db.commit()
    return result.rowcount > 0


def finish_failure(db: Session, job_id, error: str, *, retryable: bool = True, worker_id: str | None = None) -> str:
    """Retourne le statut final du job : 'queued' (nouvelle tentative) ou 'failed'."""
    row = db.execute(
        text("SELECT attempts, max_attempts FROM ingestion_jobs WHERE id = :id"), {"id": str(job_id)}
    ).fetchone()
    if row is None:
        return "gone"
    attempts, max_attempts = int(row[0]), int(row[1])
    safe_error = " ".join(str(error).split())[:500]
    ownership = " AND locked_by = :worker AND status = 'running'" if worker_id is not None else ""
    extra_params = {"worker": worker_id} if worker_id is not None else {}
    if retryable and attempts < max_attempts:
        delay = min(_BACKOFF_MAX_SECONDS, _BACKOFF_BASE_SECONDS * (2 ** max(0, attempts - 1)))
        result = db.execute(
            text(
                f"""
                UPDATE ingestion_jobs
                   SET status = 'queued', error = :error, locked_by = NULL, lease_until = NULL,
                       available_at = now() + make_interval(secs => :delay), updated_at = now()
                 WHERE id = :id{ownership}
                """
            ),
            {"id": str(job_id), "error": safe_error, "delay": delay, **extra_params},
        )
        db.commit()
        return "queued" if result.rowcount else "gone"
    result = db.execute(
        text(
            f"""
            UPDATE ingestion_jobs
               SET status = 'failed', error = :error, finished_at = now(), locked_by = NULL,
                   lease_until = NULL, updated_at = now()
             WHERE id = :id{ownership}
            """
        ),
        {"id": str(job_id), "error": safe_error, **extra_params},
    )
    db.commit()
    return "failed" if result.rowcount else "gone"


def cancel_queued_jobs_for_document(db: Session, document_id) -> int:
    result = db.execute(
        text(
            "UPDATE ingestion_jobs SET status = 'cancelled', finished_at = now(), updated_at = now()"
            " WHERE document_id = :id AND status = 'queued'"
        ),
        {"id": str(document_id)},
    )
    db.flush()
    return result.rowcount


def recover_stale_jobs(db: Session, settings: Settings) -> int:
    """Reprise des jobs dont le bail a expiré (worker disparu).

    Replanifiés s'il reste des tentatives, échoués sinon. Ne touche jamais un job
    encore valide (lease_until dans le futur). Un job épuisé marque aussi son
    document : « failed » pour une ingestion initiale, l'ancienne génération
    restant servie (« ready ») pour une réindexation.
    """
    requeued = db.execute(
        text(
            """
            UPDATE ingestion_jobs
               SET status = 'queued', locked_by = NULL, lease_until = NULL,
                   error = COALESCE(error, 'bail expiré — reprise automatique'),
                   available_at = now(), updated_at = now()
             WHERE status = 'running' AND lease_until IS NOT NULL AND lease_until < now()
               AND attempts < max_attempts
            """
        )
    )
    failed = db.execute(
        text(
            """
            UPDATE ingestion_jobs
               SET status = 'failed', finished_at = now(), locked_by = NULL, lease_until = NULL,
                   error = 'bail expiré — tentatives épuisées', updated_at = now()
             WHERE status = 'running' AND lease_until IS NOT NULL AND lease_until < now()
               AND attempts >= max_attempts
            RETURNING document_id
            """
        )
    )
    failed_document_ids = [row[0] for row in failed.fetchall() if row[0] is not None]
    for document_id in failed_document_ids:
        db.execute(
            text(
                """
                UPDATE documents
                   SET status = CASE WHEN current_generation > 0 THEN 'ready' ELSE 'failed' END,
                       error = 'bail expiré — reprise épuisée, document non publié',
                       updated_at = now()
                 WHERE id = :doc AND status <> 'deleting'
                """
            ),
            {"doc": str(document_id)},
        )
    db.commit()
    return int(requeued.rowcount or 0) + len(failed_document_ids)


def recover_stale_streams(db: Session, older_than_seconds: int = 300) -> int:
    """Messages laissés 'streaming' par un arrêt brutal → 'interrupted' (rejouable)."""
    result = db.execute(
        text(
            """
            UPDATE messages
               SET status = 'interrupted',
                   error = COALESCE(error, 'génération interrompue (redémarrage)'),
                   updated_at = now()
             WHERE status = 'streaming' AND updated_at < now() - make_interval(secs => :seconds)
            """
        ),
        {"seconds": older_than_seconds},
    )
    db.commit()
    return int(result.rowcount or 0)


def retry_failed_job(db: Session, job: IngestionJob, settings: Settings) -> bool:
    if job.status != "failed":
        return False
    if job.manual_retries >= settings.worker_manual_retry_max:
        return False
    job.status = "queued"
    job.attempts = 0
    job.manual_retries += 1
    job.error = None
    job.available_at = utcnow()
    job.lease_until = None
    job.locked_by = None
    job.finished_at = None
    db.flush()
    return True


def job_to_dict(job: IngestionJob) -> dict[str, Any]:
    return {
        "id": str(job.id),
        "document_id": str(job.document_id) if job.document_id else None,
        "kind": job.kind,
        "status": job.status,
        "attempts": job.attempts,
        "max_attempts": job.max_attempts,
        "manual_retries": job.manual_retries,
        "progress": job.progress or {},
        "error": job.error,
        "available_at": _iso(job.available_at),
        "lease_until": _iso(job.lease_until),
        "created_at": _iso(job.created_at),
        "started_at": _iso(job.started_at),
        "finished_at": _iso(job.finished_at),
    }


def _iso(value: dt.datetime | None) -> str | None:
    return value.isoformat() if value else None
