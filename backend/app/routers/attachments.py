"""Pièces jointes : upload validé, extraction bornée, téléchargement authentifié.

Isolation stricte par conversation : aucune pièce jointe n'est accessible ou
acceptée en dehors de sa conversation, même pour l'administrateur.
"""
from __future__ import annotations

import hashlib
import io
import re
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from ..config import get_settings
from ..deps import AuthContext, csrf_guard, require_user
from ..models import Attachment, Conversation
from ..security import sanitize_text
from ..serializers import attachment_out
from .conversations import get_owned_conversation

router = APIRouter(prefix="/api", tags=["attachments"])

PDF_MAGIC = b"%PDF-"
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
JPEG_MAGIC = b"\xff\xd8\xff"
TEXT_EXTENSIONS = {".txt", ".log"}
IMAGE_EXTENSIONS = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}
CONTENT_TYPES = {
    "pdf": "application/pdf",
    "png": "image/png",
    "jpeg": "image/jpeg",
    "webp": "image/webp",
    "text": "text/plain; charset=utf-8",
}

_SAFE_NAME = re.compile(r"[^\w.\- ()]+", re.UNICODE)


def sanitize_filename(name: str) -> str:
    base = Path(name or "fichier").name
    base = _SAFE_NAME.sub("_", base).strip().strip(".") or "fichier"
    return base[:180]


def detect_kind(data: bytes, filename: str) -> str | None:
    lowered = filename.lower()
    if data.startswith(PDF_MAGIC) and lowered.endswith(".pdf"):
        return "pdf"
    if data.startswith(PNG_MAGIC):
        return "png"
    if data.startswith(JPEG_MAGIC):
        return "jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    suffix = Path(lowered).suffix
    if suffix in TEXT_EXTENSIONS and b"\x00" not in data[:4096]:
        return "text"
    return None


def _extract_pdf_text(data: bytes, max_chars: int = 200_000) -> tuple[int, str]:
    import pypdf

    reader = pypdf.PdfReader(io.BytesIO(data))
    pages = len(reader.pages)
    parts: list[str] = []
    total = 0
    for page in reader.pages:
        try:
            text = page.extract_text() or ""
        except Exception:  # noqa: BLE001 - PDF exotique : texte partiel acceptable
            text = ""
        if text:
            parts.append(text)
            total += len(text)
        if total >= max_chars:
            break
    return pages, ("\n\n".join(parts))[:max_chars]


def _extract_image_meta(data: bytes) -> tuple[int, int]:
    from PIL import Image

    with Image.open(io.BytesIO(data)) as image:
        image.verify()
    with Image.open(io.BytesIO(data)) as image:
        width, height = image.size
    return width, height


@router.post("/conversations/{conversation_id}/attachments", status_code=status.HTTP_201_CREATED)
async def upload_attachment(
    conversation_id: uuid.UUID,
    file: UploadFile = File(...),
    auth: AuthContext = Depends(csrf_guard),
):
    settings = auth.settings
    conv = get_owned_conversation(auth.db, auth, conversation_id)
    data = await file.read()
    if not data:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="fichier vide")

    kind = detect_kind(data, file.filename or "")
    if kind is None:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="type non supporté (PDF, txt, log, PNG, JPEG, WebP uniquement)",
        )

    size = len(data)
    pages: int | None = None
    width: int | None = None
    height: int | None = None
    extracted: str | None = None
    stored_kind = kind
    extension = Path(sanitize_filename(file.filename or "")).suffix.lower()

    if kind == "pdf":
        if size > settings.upload_pdf_max_bytes:
            raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail="PDF trop volumineux (max 20 Mio)")
        try:
            pages, extracted = _extract_pdf_text(data)
        except Exception:  # noqa: BLE001 - PDF corrompu/protégé : refus explicite, jamais un 500
            raise HTTPException(status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, detail="PDF illisible (fichier corrompu ou protégé)")
        if pages > settings.upload_pdf_max_pages:
            raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail="PDF trop long (max 100 pages)")
        extracted = extracted or None
        extension = ".pdf"
    elif kind in ("png", "jpeg", "webp"):
        if size > settings.upload_image_max_bytes:
            raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail="image trop volumineuse (max 10 Mio)")
        try:
            width, height = _extract_image_meta(data)
        except Exception:  # noqa: BLE001
            raise HTTPException(status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, detail="image illisible")
        if width * height > settings.upload_image_max_pixels:
            raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail="image aux dimensions excessives")
        extension = f".{kind}"
    else:
        if size > settings.upload_text_max_bytes:
            raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail="fichier texte trop volumineux (max 1 Mio)")
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            text = data.decode("latin-1", errors="replace")
        extracted = text[:200_000]
        extension = extension if extension in TEXT_EXTENSIONS else ".txt"

    stored_name = f"{uuid.uuid4()}{extension}"
    relpath = f"{conv.id}/{stored_name}"
    target = settings.uploads_dir / relpath
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    target.chmod(0o600)

    attachment = Attachment(
        conversation_id=conv.id,
        filename_original=sanitize_filename(file.filename or stored_name),
        stored_relpath=relpath,
        content_type=CONTENT_TYPES[stored_kind if stored_kind != "text" else "text"],
        kind="image" if stored_kind in ("png", "jpeg", "webp") else ("pdf" if stored_kind == "pdf" else "text"),
        size_bytes=size,
        sha256=hashlib.sha256(data).hexdigest(),
        pages=pages,
        width=width,
        height=height,
        extracted_text=extracted,
        uploaded_by=auth.user.id,
    )
    auth.db.add(attachment)
    auth.db.commit()
    return attachment_out(attachment)


@router.get("/conversations/{conversation_id}/attachments")
def list_attachments(conversation_id: uuid.UUID, auth: AuthContext = Depends(require_user)):
    conv = get_owned_conversation(auth.db, auth, conversation_id)
    from sqlalchemy import select

    rows = auth.db.execute(
        select(Attachment).where(Attachment.conversation_id == conv.id).order_by(Attachment.created_at.asc())
    ).scalars().all()
    return {"attachments": [attachment_out(a) for a in rows]}


def _get_owned_attachment(db: Session, auth: AuthContext, attachment_id: uuid.UUID) -> Attachment:
    attachment = db.get(Attachment, attachment_id)
    if attachment is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="pièce jointe introuvable")
    conv = db.get(Conversation, attachment.conversation_id)
    if conv is None or conv.user_id != auth.user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="pièce jointe introuvable")
    return attachment


@router.get("/attachments/{attachment_id}/content")
def download_attachment(
    attachment_id: uuid.UUID,
    inline: int = Query(default=0, ge=0, le=1),
    auth: AuthContext = Depends(require_user),
):
    settings = get_settings()
    attachment = _get_owned_attachment(auth.db, auth, attachment_id)
    base = settings.uploads_dir.resolve()
    path = (settings.uploads_dir / attachment.stored_relpath).resolve()
    if not str(path).startswith(str(base)) or not path.is_file():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="fichier introuvable")
    disposition = "inline" if (inline and attachment.kind in ("image", "pdf")) else "attachment"
    return FileResponse(
        path,
        media_type=attachment.content_type,
        filename=attachment.filename_original,
        content_disposition_type=disposition,
        headers={
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "private, max-age=0, no-store",
        },
    )


@router.delete("/attachments/{attachment_id}")
def delete_attachment(attachment_id: uuid.UUID, auth: AuthContext = Depends(csrf_guard)):
    attachment = _get_owned_attachment(auth.db, auth, attachment_id)
    settings = get_settings()
    path = settings.uploads_dir / attachment.stored_relpath
    auth.db.delete(attachment)
    auth.db.commit()
    try:
        path.unlink()
    except OSError:
        pass
    return {"ok": True}
