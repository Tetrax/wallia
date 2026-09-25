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
    row = db.execute(_CLAIM_SQL, {"worker": worker_id, "lease": lease_seconds}).fetchone()
    if row is None:
        return None
    db.commit()
    job = db.get(IngestionJob, row[0])
    return job


def renew_lease(db: Session, job_id, worker_id: str, lease_seconds: int) -> bool:
    result = db.execute(
        text(
            """
            UPDATE ingestion_jobs
               SET lease_until = now() + make_interval(secs => :lease), updated_at = now()
             WHERE id = :id AND locked_by = :worker AND status = 'running'
            """
        ),
        {"id": str(job_id), "worker": worker_id, "lease": lease_seconds},
    )
    db.commit()
    return result.rowcount > 0


def set_progress(db: Session, job_id, progress: dict[str, Any]) -> None:
    db.execute(
        text("UPDATE ingestion_jobs SET progress = CAST(:progress AS jsonb), updated_at = now() WHERE id = :id"),
        {"id": str(job_id), "progress": __import__("json").dumps(progress, ensure_ascii=False)},
    )
    db.commit()


def finish_success(db: Session, job_id) -> None:
    db.execute(
        text(
            "UPDATE ingestion_jobs SET status = 'succeeded', finished_at = now(), updated_at = now(),"
            " lease_until = NULL WHERE id = :id"
        ),
        {"id": str(job_id)},
    )
    db.commit()


def finish_failure(db: Session, job_id, error: str, *, retryable: bool = True) -> str:
    """Retourne le statut final du job : 'queued' (nouvelle tentative) ou 'failed'."""
    row = db.execute(
        text("SELECT attempts, max_attempts FROM ingestion_jobs WHERE id = :id"), {"id": str(job_id)}
    ).fetchone()
    if row is None:
        return "gone"
    attempts, max_attempts = int(row[0]), int(row[1])
    safe_error = " ".join(str(error).split())[:500]
    if retryable and attempts < max_attempts:
        delay = min(_BACKOFF_MAX_SECONDS, _BACKOFF_BASE_SECONDS * (2 ** max(0, attempts - 1)))
        db.execute(
            text(
                """
                UPDATE ingestion_jobs
                   SET status = 'queued', error = :error, locked_by = NULL, lease_until = NULL,
                       available_at = now() + make_interval(secs => :delay), updated_at = now()
                 WHERE id = :id
                """
            ),
            {"id": str(job_id), "error": safe_error, "delay": delay},
        )
        db.commit()
        return "queued"
    db.execute(
        text(
            """
            UPDATE ingestion_jobs
               SET status = 'failed', error = :error, finished_at = now(), locked_by = NULL,
                   lease_until = NULL, updated_at = now()
             WHERE id = :id
            """
        ),
        {"id": str(job_id), "error": safe_error},
    )
    db.commit()
    return "failed"


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
    encore valide (lease_until dans le futur).
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
            """
        )
    )
    db.commit()
    return int(requeued.rowcount or 0) + int(failed.rowcount or 0)


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
