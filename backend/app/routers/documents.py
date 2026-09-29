"""Bibliothèque documentaire (administration) : import, réindexation, suppression,
inspection des passages et des jobs."""
from __future__ import annotations

import hashlib
import io
import json
import shutil
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile, status
from fastapi.responses import FileResponse
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from ..config import Settings, get_settings
from ..deps import AuthContext, csrf_guard, require_admin, require_user
from ..jobs import cancel_queued_jobs_for_document, enqueue_job, has_active_job, job_to_dict, retry_failed_job
from ..models import Chunk, Document, IngestionJob
from ..schemas import DocumentPatch, validate_version
from ..security import sanitize_text
from ..serializers import document_out
from .attachments import PDF_MAGIC, parse_file, read_upload_bounded, sanitize_filename

router = APIRouter(prefix="/api", tags=["documents"])


def _last_job(db: Session, document_id: uuid.UUID) -> IngestionJob | None:
    return db.execute(
        select(IngestionJob)
        .where(IngestionJob.document_id == document_id)
        .order_by(IngestionJob.created_at.desc())
        .limit(1)
    ).scalar_one_or_none()


def _chunks_count(db: Session, document: Document) -> int:
    return int(
        db.execute(
            select(func.count(Chunk.id)).where(
                Chunk.document_id == document.id, Chunk.generation == document.current_generation
            )
        ).scalar_one()
    )


@router.get("/documents")
def list_documents(
    scope: str | None = Query(default=None, pattern="^(demo|official)$"),
    doc_status: str | None = Query(default=None, alias="status"),
    demo: int | None = Query(default=None, ge=0, le=1),
    q: str | None = Query(default=None, max_length=120),
    auth: AuthContext = Depends(require_admin),
):
    stmt = select(Document).order_by(Document.created_at.desc()).limit(300)
    if scope:
        stmt = stmt.where(Document.scope == scope)
    if doc_status:
        stmt = stmt.where(Document.status == doc_status)
    if demo is not None:
        stmt = stmt.where(Document.demo == bool(demo))
    if q:
        stmt = stmt.where(func.lower(Document.title).like(f"%{q.lower()}%"))
    docs = auth.db.execute(stmt).scalars().all()
    return {
        "documents": [
            document_out(d, last_job=_last_job(auth.db, d.id), chunks=_chunks_count(auth.db, d)) for d in docs
        ]
    }


@router.post("/documents", status_code=status.HTTP_201_CREATED)
async def import_document(
    file: UploadFile = File(...),
    title: str = Form(...),
    origin: str = Form(default="demo"),
    product: str = Form(default=""),
    versions: str = Form(default="[]"),
    language: str = Form(default="fr"),
    document_date: str = Form(default=""),
    demo: int = Form(default=1),
    scope: str = Form(default="demo"),
    auth: AuthContext = Depends(csrf_guard),
):
    settings = auth.settings
    if not auth.user.is_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="accès administrateur requis")
    # Lecture réellement bornée (jamais le corps entier chargé) puis contrôle
    # du nombre de pages AVANT extraction, hors de la boucle web.
    try:
        data = await read_upload_bounded(file, settings.upload_pdf_max_bytes)
    except HTTPException as exc:
        if exc.status_code == status.HTTP_413_REQUEST_ENTITY_TOO_LARGE:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail="PDF trop volumineux (max 20 Mio)"
            )
        raise
    if not data or not data.startswith(PDF_MAGIC):
        raise HTTPException(status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, detail="seuls les PDF sont acceptés pour le corpus")

    import pypdf  # noqa: F401 - conservé pour compatibilité d'import des tests

    try:
        # Analyse dans un sous-processus borné (réellement terminable).
        pages = int((await parse_file("pdf_pages", data, auth.settings))["pages"])
    except HTTPException:
        raise
    except Exception:  # noqa: BLE001
        raise HTTPException(status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, detail="PDF illisible")
    if pages > settings.upload_pdf_max_pages:
        raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail="PDF trop long (max 100 pages)")

    cleaned_title = sanitize_text(title, 300)
    if not cleaned_title:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="titre requis")
    if scope not in ("demo", "official"):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="périmètre invalide")
    # Cohérence démo/périmètre imposée à l'égalité : demo == (scope == "demo").
    if bool(demo) != (scope == "demo"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="cohérence démo/périmètre : un document de démonstration est « demo », sinon « official »",
        )
    try:
        version_list = json.loads(versions) if versions.strip().startswith("[") else [
            v.strip() for v in versions.split(",") if v.strip()
        ]
        version_list = [validate_version(str(v)) for v in version_list]
    except (ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=f"versions invalides: {exc}")
    parsed_date = None
    if document_date.strip():
        import datetime as dt

        try:
            parsed_date = dt.date.fromisoformat(document_date.strip())
        except ValueError:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="date invalide (AAAA-MM-JJ)")

    checksum = hashlib.sha256(data).hexdigest()
    # Déduplication sérialisée : deux imports simultanés du même contenu ne
    # peuvent pas créer deux documents.
    auth.db.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(CAST(:c AS text), 0))"),
        {"c": f"import:{scope}:{checksum}"},
    )
    duplicate = auth.db.execute(
        select(Document).where(Document.checksum_sha256 == checksum, Document.scope == scope)
    ).scalar_one_or_none()
    if duplicate is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"document identique déjà importé (id {duplicate.id})",
        )

    document = Document(
        title=cleaned_title,
        origin=sanitize_text(origin, 200) or "demo",
        product=sanitize_text(product, 200) or None,
        versions=version_list,
        language=sanitize_text(language, 10) or "fr",
        document_date=parsed_date,
        checksum_sha256=checksum,
        demo=bool(demo),
        scope=scope,
        status="queued",
        stored_relpath="",
        original_filename=sanitize_filename(file.filename or "document.pdf"),
        content_type="application/pdf",
        size_bytes=len(data),
        page_count=pages,
        created_by=auth.user.id,
    )
    auth.db.add(document)
    auth.db.flush()

    rel_dir = str(document.id)
    stored_name = f"{sanitize_filename(file.filename or 'document.pdf')}"
    target_dir = settings.documents_dir / rel_dir
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / stored_name
    target.write_bytes(data)
    target.chmod(0o600)
    document.stored_relpath = f"{rel_dir}/{stored_name}"

    job = enqueue_job(auth.db, kind="ingest", document_id=document.id, max_attempts=3)
    auth.db.commit()
    return {**document_out(document, last_job=job), "job": job_to_dict(job)}


@router.patch("/documents/{document_id}")
def patch_document(document_id: uuid.UUID, body: DocumentPatch, auth: AuthContext = Depends(csrf_guard)):
    if not auth.user.is_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="accès administrateur requis")
    document = auth.db.get(Document, document_id)
    if document is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="document introuvable")
    data = body.model_dump(exclude_unset=True)
    if "scope" in data and data["scope"] != document.scope:
        duplicate = auth.db.execute(
            select(Document).where(
                Document.checksum_sha256 == document.checksum_sha256,
                Document.scope == data["scope"],
                Document.id != document.id,
            )
        ).scalar_one_or_none()
        if duplicate is not None:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="conflit de périmètre avec un document identique")
    # Cohérence démo/périmètre imposée aussi à la modification, à l'égalité :
    # demo == (scope == "demo"), dans les deux sens.
    resulting_demo = bool(data.get("demo", document.demo))
    resulting_scope = data.get("scope", document.scope)
    if resulting_demo != (resulting_scope == "demo"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="cohérence démo/périmètre : un document de démonstration est « demo », sinon « official »",
        )
    for field in ("title", "origin", "product", "language", "document_date", "demo", "scope"):
        if field in data:
            value = data[field]
            if field in ("title", "origin", "product", "language") and value is not None:
                value = sanitize_text(value, 300) or None
            setattr(document, field, value)
    if "versions" in data and data["versions"] is not None:
        document.versions = data["versions"]
    document.updated_at = func.now()
    auth.db.commit()
    return document_out(document, last_job=_last_job(auth.db, document.id))


@router.post("/documents/{document_id}/reindex", status_code=status.HTTP_202_ACCEPTED)
def reindex_document(document_id: uuid.UUID, auth: AuthContext = Depends(csrf_guard)):
    if not auth.user.is_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="accès administrateur requis")
    document = auth.db.get(Document, document_id)
    if document is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="document introuvable")
    if document.status == "deleting":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="document en cours de suppression")
    # Contrôle et insertion sérialisés par verrou transactionnel : deux appels
    # de réindexation simultanés ne peuvent pas empiler deux jobs.
    auth.db.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(CAST(:c AS text), 0))"),
        {"c": f"reindex:{document.id}"},
    )
    if has_active_job(auth.db, document.id, "ingest") or has_active_job(auth.db, document.id, "reindex"):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="une ingestion est déjà en cours pour ce document")
    job = enqueue_job(auth.db, kind="reindex", document_id=document.id, max_attempts=3)
    auth.db.commit()
    return {"job": job_to_dict(job), "document": document_out(document)}


@router.delete("/documents/{document_id}")
def delete_document(document_id: uuid.UUID, auth: AuthContext = Depends(csrf_guard)):
    if not auth.user.is_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="accès administrateur requis")
    document = auth.db.get(Document, document_id)
    if document is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="document introuvable")
    settings = get_settings()
    document.status = "deleting"
    auth.db.flush()
    cancel_queued_jobs_for_document(auth.db, document.id)
    chunks_removed = int(
        auth.db.execute(select(func.count(Chunk.id)).where(Chunk.document_id == document.id)).scalar_one()
    )
    base = settings.documents_dir.resolve()
    doc_file = (settings.documents_dir / document.stored_relpath).resolve()
    auth.db.delete(document)  # cascade : messages/chunks/jobs liés au document
    auth.db.commit()
    if doc_file.is_relative_to(base):
        try:
            doc_file.unlink()
        except OSError:
            pass
    work_dir = settings.data_dir / "ingestion" / str(document_id)
    if work_dir.is_dir():
        shutil.rmtree(work_dir, ignore_errors=True)
    return {"ok": True, "chunks_removed": chunks_removed}


@router.get("/documents/{document_id}/original")
def download_original(document_id: uuid.UUID, auth: AuthContext = Depends(require_user)):
    settings = get_settings()
    document = auth.db.get(Document, document_id)
    if document is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="document introuvable")
    base = settings.documents_dir.resolve()
    path = (settings.documents_dir / document.stored_relpath).resolve()
    # Appartenance réelle au répertoire (jamais un simple préfixe de chaîne).
    if not path.is_relative_to(base) or not path.is_file():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="fichier introuvable")
    return FileResponse(
        path,
        media_type="application/pdf",
        filename=document.original_filename,
        content_disposition_type="inline",
        headers={
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "private, max-age=0, no-store",
        },
    )


@router.get("/documents/{document_id}/chunks")
def list_chunks(
    document_id: uuid.UUID,
    generation: int | None = Query(default=None, ge=1),
    auth: AuthContext = Depends(require_admin),
):
    document = auth.db.get(Document, document_id)
    if document is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="document introuvable")
    gen = generation or document.current_generation
    rows = auth.db.execute(
        select(Chunk)
        .where(Chunk.document_id == document.id, Chunk.generation == gen)
        .order_by(Chunk.seq.asc())
        .limit(1000)
    ).scalars().all()
    return {
        "document_id": str(document.id),
        "generation": gen,
        "current_generation": document.current_generation,
        "chunks": [
            {
                "id": str(c.id),
                "seq": c.seq,
                "kind": c.kind,
                "section": c.section,
                "page_start": c.page_start,
                "page_end": c.page_end,
                "token_count": c.token_count,
                "text": c.text,
            }
            for c in rows
        ],
    }


@router.get("/documents/{document_id}/jobs")
def document_jobs(document_id: uuid.UUID, auth: AuthContext = Depends(require_admin)):
    rows = auth.db.execute(
        select(IngestionJob)
        .where(IngestionJob.document_id == document_id)
        .order_by(IngestionJob.created_at.desc())
        .limit(50)
    ).scalars().all()
    return {"jobs": [job_to_dict(j) for j in rows]}


@router.get("/jobs")
def list_jobs(
    job_status: str | None = Query(default=None, alias="status"),
    limit: int = Query(default=50, ge=1, le=200),
    auth: AuthContext = Depends(require_admin),
):
    stmt = (
        select(IngestionJob, Document.title)
        .join(Document, Document.id == IngestionJob.document_id, isouter=True)
        .order_by(IngestionJob.created_at.desc())
        .limit(limit)
    )
    if job_status:
        stmt = stmt.where(IngestionJob.status == job_status)
    rows = auth.db.execute(stmt).all()
    return {
        "jobs": [
            {**job_to_dict(job), "document_title": title} for job, title in rows
        ]
    }


@router.post("/jobs/{job_id}/retry", status_code=status.HTTP_202_ACCEPTED)
def retry_job(job_id: uuid.UUID, auth: AuthContext = Depends(csrf_guard)):
    if not auth.user.is_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="accès administrateur requis")
    job = auth.db.get(IngestionJob, job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="job introuvable")
    if not retry_failed_job(auth.db, job, auth.settings):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="job non relançable (statut ou nombre de relances manuelles atteint)",
        )
    auth.db.commit()
    return {"job": job_to_dict(job)}
