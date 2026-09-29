"""Pièces jointes : upload validé, extraction bornée, téléchargement authentifié.

Isolation stricte par conversation : aucune pièce jointe n'est accessible ou
acceptée en dehors de sa conversation, même pour l'administrateur.

Bornes réelles : lecture HTTP plafonnée (jamais de corps entier chargé), taille,
pages, pixels, et durée de parsing ; les parsers synchrones tournent hors de la
boucle d'événements.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from ..config import Settings, get_settings
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

READ_CHUNK_BYTES = 1024 * 1024

_SAFE_NAME = re.compile(r"[^\w.\- ()]+", re.UNICODE)


class ParseFailed(RuntimeError):
    """Le sous-processus d'analyse a échoué : jamais un détail brut exposé."""


def sanitize_filename(name: str) -> str:
    base = Path(name or "fichier").name
    base = _SAFE_NAME.sub("_", base).strip().strip(".") or "fichier"
    return base[:180]


async def read_upload_bounded(file: UploadFile, max_bytes: int) -> bytes:
    """Lit un upload en le bornant réellement (refus dès dépassement).

    Aucune limite `await file.read()` sans borne : un corps énorme est refusé
    après `max_bytes` lus, jamais après chargement complet en mémoire.
    """
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await file.read(READ_CHUNK_BYTES)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail="fichier trop volumineux",
            )
        chunks.append(chunk)
    return b"".join(chunks)


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


# -- analyse en sous-processus borné -----------------------------------------

_PARSE_SEMAPHORES: dict[int, threading.BoundedSemaphore] = {}
_PARSE_SEMAPHORE_GUARD = threading.Lock()


def _parse_semaphore(limit: int) -> threading.BoundedSemaphore:
    """Bornage de la concurrence des parseurs (une analyse lourde à la fois)."""
    with _PARSE_SEMAPHORE_GUARD:
        semaphore = _PARSE_SEMAPHORES.get(limit)
        if semaphore is None:
            semaphore = threading.BoundedSemaphore(max(1, limit))
            _PARSE_SEMAPHORES[limit] = semaphore
        return semaphore


def _parser_env(memory_bytes: int | None = None) -> dict[str, str]:
    env = {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "PYTHONPATH": str(Path(__file__).resolve().parents[1]),
        "PYTHONDONTWRITEBYTECODE": "1",
        "OMP_NUM_THREADS": "1",
        "MKL_NUM_THREADS": "1",
        "TOKENIZERS_PARALLELISM": "false",
    }
    if memory_bytes:
        # Borne mémoire du sous-processus d'analyse (RLIMIT_AS appliqué par le
        # parseur lui-même) : jamais héritée d'un environnement externe.
        env["WALLIA_PARSE_MAX_MEMORY_BYTES"] = str(int(memory_bytes))
    home = os.environ.get("HOME")
    if home:
        env["HOME"] = home
    return env


def _execute_parser(
    cmd: list[str], payload: bytes, timeout_seconds: float, *, memory_bytes: int | None = None
) -> subprocess.CompletedProcess:
    """Exécute un parseur externe borné : TOUJOURS terminé en cas de dépassement.

    `kill()` + `communicate()` garantissent la fin réelle du processus (reapé,
    plus aucun thread orphelin qui continuerait à consommer du CPU).
    """
    process = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=_parser_env(memory_bytes),
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(payload, timeout=max(0.5, timeout_seconds))
    except subprocess.TimeoutExpired:
        process.kill()
        process.communicate()
        raise
    return subprocess.CompletedProcess(cmd, process.returncode, stdout, stderr)


def _parse_in_subprocess(kind: str, data: bytes, settings: Settings) -> dict:
    workdir = settings.quarantine_dir / "parse"
    workdir.mkdir(parents=True, exist_ok=True)
    payload_path = workdir / f"{uuid.uuid4().hex}.bin"
    payload_path.write_bytes(data)
    try:
        spec = json.dumps({"kind": kind, "path": str(payload_path)}).encode("utf-8")
        cmd = [sys.executable, "-m", "app.parse_runner"]
        with _parse_semaphore(settings.upload_parse_concurrency):
            try:
                completed = _execute_parser(
                    cmd,
                    spec,
                    float(settings.upload_parse_timeout_seconds),
                    memory_bytes=settings.upload_parse_max_memory_bytes,
                )
            except subprocess.TimeoutExpired:
                raise HTTPException(
                    status_code=status.HTTP_408_REQUEST_TIMEOUT,
                    detail="analyse du fichier trop longue",
                ) from None
        if completed.returncode != 0:
            raise ParseFailed(f"parseur en échec (code {completed.returncode})")
        try:
            result = json.loads((completed.stdout or b"").decode("utf-8").strip().splitlines()[-1])
        except (ValueError, IndexError):
            raise ParseFailed("sortie d'analyse illisible") from None
        if not isinstance(result, dict):
            raise ParseFailed("sortie d'analyse illisible")
        return result
    finally:
        try:
            payload_path.unlink()
        except OSError:
            pass


async def parse_file(kind: str, data: bytes, settings: Settings) -> dict:
    """Analyse un fichier potentiellement hostile, hors boucle web et dans un
    sous-processus réellement terminable."""
    return await run_in_threadpool(_parse_in_subprocess, kind, data, settings)


@router.post("/conversations/{conversation_id}/attachments", status_code=status.HTTP_201_CREATED)
async def upload_attachment(
    conversation_id: uuid.UUID,
    file: UploadFile = File(...),
    auth: AuthContext = Depends(csrf_guard),
):
    settings = auth.settings
    conv = get_owned_conversation(auth.db, auth, conversation_id)
    hard_cap = max(settings.upload_pdf_max_bytes, settings.upload_text_max_bytes, settings.upload_image_max_bytes)
    data = await read_upload_bounded(file, hard_cap)
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
            # Contrôle du nombre de pages AVANT extraction complète ; l'analyse
            # tourne dans un sous-processus réellement terminable.
            pages = int((await parse_file("pdf_pages", data, settings))["pages"])
        except HTTPException:
            raise
        except (ParseFailed, KeyError, TypeError, ValueError):  # noqa: BLE001 - PDF corrompu/protégé : refus explicite, jamais un 500
            raise HTTPException(status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, detail="PDF illisible (fichier corrompu ou protégé)")
        if pages > settings.upload_pdf_max_pages:
            raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail="PDF trop long (max 100 pages)")
        try:
            parsed_pdf = await parse_file("pdf_text", data, settings)
            extracted = str(parsed_pdf.get("text") or "")
        except HTTPException:
            raise
        except (ParseFailed, KeyError, TypeError, ValueError):  # noqa: BLE001
            extracted = ""
        extracted = extracted or None
        extension = ".pdf"
    elif kind in ("png", "jpeg", "webp"):
        if size > settings.upload_image_max_bytes:
            raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail="image trop volumineuse (max 10 Mio)")
        try:
            meta = await parse_file("image_meta", data, settings)
            width, height = int(meta["width"]), int(meta["height"])
        except HTTPException:
            raise
        except (ParseFailed, KeyError, TypeError, ValueError):  # noqa: BLE001
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
    # Appartenance réelle au répertoire (jamais un simple préfixe de chaîne).
    if not path.is_relative_to(base) or not path.is_file():
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
