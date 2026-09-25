"""État applicatif détaillé (authentifié) et journal des capacités effectives."""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import func, select, text

from ..app_settings import last_provider_test, provider_config, retrieval_config, worker_alive, worker_heartbeat
from ..deps import AuthContext, require_user
from ..models import Chunk, Document, IngestionJob
from ..serializers import user_out

router = APIRouter(prefix="/api", tags=["status"])


@router.get("/status")
def app_status(auth: AuthContext = Depends(require_user)):
    db = auth.db
    settings = auth.settings
    provider = provider_config(db, settings)

    doc_counts = dict(
        db.execute(select(Document.status, func.count(Document.id)).group_by(Document.status)).all()
    )
    job_counts = dict(
        db.execute(select(IngestionJob.status, func.count(IngestionJob.id)).group_by(IngestionJob.status)).all()
    )
    chunk_rows = db.execute(
        text(
            "SELECT count(*) FROM chunks c JOIN documents d ON d.id = c.document_id"
            " WHERE c.generation = d.current_generation AND d.status = 'ready'"
        )
    ).fetchone()
    embedding = None
    try:
        from ..embeddings import get_embedding_service

        embedding = get_embedding_service().info
    except Exception:  # noqa: BLE001 - jamais bloquant
        embedding = {"backend": settings.embedding_backend, "model": settings.embedding_model,
                     "revision": settings.embedding_revision, "dim": settings.embedding_dim,
                     "note": "service non initialisé"}

    heartbeat = worker_heartbeat(db)
    retrieval = retrieval_config(db, settings)
    return {
        "app": {
            "version": settings.app_version,
            "git_sha": settings.git_sha or None,
            "env": settings.env,
            "demo_mode": not provider["key_configured"],
        },
        "user": user_out(auth.user),
        "embedding": embedding,
        "provider": {
            "endpoint": provider["endpoint"],
            "model": provider["model"],
            "key_configured": provider["key_configured"],
            "vision_enabled": provider["vision_enabled"],
            "last_test": last_provider_test(db),
        },
        "web": {"available": settings.web_enabled, "reason": None if settings.web_enabled else "désactivé (intégration réservée après recette RAG)"},
        "vision": {"available": settings.vision_enabled, "reason": None if settings.vision_enabled else "analyse d'image inactive"},
        "retrieval": retrieval,
        "corpus": {
            "documents_total": int(sum(doc_counts.values())) if doc_counts else 0,
            "documents_by_status": {k: int(v) for k, v in doc_counts.items()},
            "chunks_serving": int(chunk_rows[0]) if chunk_rows else 0,
        },
        "jobs": {
            "by_status": {k: int(v) for k, v in job_counts.items()},
            "worker": {"alive": worker_alive(heartbeat), **({"heartbeat": heartbeat} if heartbeat else {})},
        },
    }
