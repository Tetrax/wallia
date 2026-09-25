"""Sérialisation API (formes stables, aucun secret)."""
from __future__ import annotations

import datetime as dt
from typing import Any

from .models import Attachment, Conversation, Document, IngestionJob, Message, User


def _iso(value: dt.datetime | None) -> str | None:
    return value.isoformat() if value else None


def user_out(user: User) -> dict[str, Any]:
    return {
        "id": str(user.id),
        "email": user.email,
        "is_admin": user.is_admin,
        "created_at": _iso(user.created_at),
        "password_changed_at": _iso(user.password_changed_at),
    }


def conversation_out(conv: Conversation, *, last_message: Message | None = None, message_count: int | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": str(conv.id),
        "title": conv.title,
        "case_state": conv.case_state,
        "created_at": _iso(conv.created_at),
        "updated_at": _iso(conv.updated_at),
    }
    if last_message is not None:
        payload["last_message"] = {
            "role": last_message.role,
            "excerpt": (last_message.content or "")[:160],
            "status": last_message.status,
            "created_at": _iso(last_message.created_at),
        }
    if message_count is not None:
        payload["message_count"] = message_count
    return payload


def message_out(message: Message) -> dict[str, Any]:
    return {
        "id": str(message.id),
        "conversation_id": str(message.conversation_id),
        "seq": message.seq,
        "role": message.role,
        "content": message.content,
        "status": message.status,
        "error": message.error,
        "model": message.model,
        "demo": message.demo,
        "sources": message.sources,
        "created_at": _iso(message.created_at),
        "updated_at": _iso(message.updated_at),
    }


def attachment_out(att: Attachment) -> dict[str, Any]:
    return {
        "id": str(att.id),
        "conversation_id": str(att.conversation_id),
        "message_id": str(att.message_id) if att.message_id else None,
        "filename": att.filename_original,
        "content_type": att.content_type,
        "kind": att.kind,
        "size_bytes": att.size_bytes,
        "pages": att.pages,
        "width": att.width,
        "height": att.height,
        "has_extracted_text": bool(att.extracted_text),
        "created_at": _iso(att.created_at),
    }


def document_out(doc: Document, *, last_job: IngestionJob | None = None, chunks: int | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": str(doc.id),
        "title": doc.title,
        "origin": doc.origin,
        "product": doc.product,
        "versions": list(doc.versions or []),
        "language": doc.language,
        "document_date": doc.document_date.isoformat() if doc.document_date else None,
        "checksum_sha256": doc.checksum_sha256,
        "demo": doc.demo,
        "scope": doc.scope,
        "status": doc.status,
        "current_generation": doc.current_generation,
        "embedding_model": doc.embedding_model,
        "embedding_revision": doc.embedding_revision,
        "embedding_dim": doc.embedding_dim,
        "original_filename": doc.original_filename,
        "content_type": doc.content_type,
        "size_bytes": doc.size_bytes,
        "page_count": doc.page_count,
        "error": doc.error,
        "created_at": _iso(doc.created_at),
        "updated_at": _iso(doc.updated_at),
    }
    if last_job is not None:
        from .jobs import job_to_dict

        payload["last_job"] = job_to_dict(last_job)
    if chunks is not None:
        payload["chunks_current"] = chunks
    return payload
