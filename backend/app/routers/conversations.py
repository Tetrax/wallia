"""Conversations, messages et état de cas (scopés utilisateur)."""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from ..deps import AuthContext, csrf_guard, require_user
from ..models import Attachment, Conversation, Message
from ..schemas import CaseStatePatch, ConversationIn, ConversationPatch, case_state_patch_to_raw, normalize_case_state
from ..security import sanitize_text
from ..serializers import attachment_out, conversation_out, message_out

router = APIRouter(prefix="/api/conversations", tags=["conversations"])


def get_owned_conversation(db: Session, auth: AuthContext, conversation_id: uuid.UUID) -> Conversation:
    conv = db.execute(
        select(Conversation).where(
            Conversation.id == conversation_id, Conversation.user_id == auth.user.id
        )
    ).scalar_one_or_none()
    if conv is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="conversation introuvable")
    return conv


def next_seq(db: Session, conversation_id: uuid.UUID) -> int:
    """Séquence du prochain message, sérialisée par verrou consultatif par conversation."""
    db.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(CAST(:c AS text), 0))"), {"c": str(conversation_id)})
    row = db.execute(
        text("SELECT COALESCE(MAX(seq), 0) + 1 FROM messages WHERE conversation_id = :c"),
        {"c": str(conversation_id)},
    ).fetchone()
    return int(row[0])


@router.get("")
def list_conversations(auth: AuthContext = Depends(require_user)):
    rows = auth.db.execute(
        select(Conversation)
        .where(Conversation.user_id == auth.user.id)
        .order_by(Conversation.updated_at.desc())
        .limit(200)
    ).scalars().all()
    out = []
    for conv in rows:
        last = auth.db.execute(
            select(Message).where(Message.conversation_id == conv.id).order_by(Message.seq.desc()).limit(1)
        ).scalar_one_or_none()
        count = auth.db.execute(
            select(func.count(Message.id)).where(Message.conversation_id == conv.id)
        ).scalar_one()
        out.append(conversation_out(conv, last_message=last, message_count=int(count)))
    return {"conversations": out}


@router.post("", status_code=status.HTTP_201_CREATED)
def create_conversation(body: ConversationIn, auth: AuthContext = Depends(csrf_guard)):
    title = sanitize_text(body.title, 200) if body.title else "Nouvelle conversation"
    # État de cas initial canonique (même structure que le défaut SQL) : la
    # réponse ne doit jamais exposer un objet partiel.
    conv = Conversation(
        user_id=auth.user.id, title=title or "Nouvelle conversation", case_state=normalize_case_state({})
    )
    auth.db.add(conv)
    auth.db.commit()
    return conversation_out(conv)


@router.get("/{conversation_id}")
def get_conversation(conversation_id: uuid.UUID, auth: AuthContext = Depends(require_user)):
    conv = get_owned_conversation(auth.db, auth, conversation_id)
    messages = auth.db.execute(
        select(Message).where(Message.conversation_id == conv.id).order_by(Message.seq.asc())
    ).scalars().all()
    attachments = auth.db.execute(
        select(Attachment).where(Attachment.conversation_id == conv.id).order_by(Attachment.created_at.asc())
    ).scalars().all()
    return {
        **conversation_out(conv, message_count=len(messages)),
        "messages": [message_out(m) for m in messages],
        "attachments": [attachment_out(a) for a in attachments],
    }


@router.patch("/{conversation_id}")
def rename_conversation(conversation_id: uuid.UUID, body: ConversationPatch, auth: AuthContext = Depends(csrf_guard)):
    conv = get_owned_conversation(auth.db, auth, conversation_id)
    title = sanitize_text(body.title, 200)
    if not title:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="titre vide")
    conv.title = title
    conv.updated_at = func.now()
    auth.db.commit()
    return conversation_out(conv)


@router.delete("/{conversation_id}")
def delete_conversation(conversation_id: uuid.UUID, auth: AuthContext = Depends(csrf_guard)):
    conv = get_owned_conversation(auth.db, auth, conversation_id)
    files = [a.stored_relpath for a in auth.db.execute(
        select(Attachment).where(Attachment.conversation_id == conv.id)
    ).scalars().all()]
    auth.db.delete(conv)
    auth.db.commit()
    from pathlib import Path

    from ..config import get_settings

    settings = get_settings()
    removed = 0
    for rel in files:
        path = settings.uploads_dir / rel
        try:
            path.unlink()
            removed += 1
        except OSError:
            pass
    return {"ok": True, "attachments_removed": removed}


@router.get("/{conversation_id}/messages")
def list_messages(conversation_id: uuid.UUID, auth: AuthContext = Depends(require_user)):
    conv = get_owned_conversation(auth.db, auth, conversation_id)
    messages = auth.db.execute(
        select(Message).where(Message.conversation_id == conv.id).order_by(Message.seq.asc())
    ).scalars().all()
    return {"messages": [message_out(m) for m in messages]}


@router.patch("/{conversation_id}/case_state")
def patch_case_state(conversation_id: uuid.UUID, body: CaseStatePatch, auth: AuthContext = Depends(csrf_guard)):
    conv = get_owned_conversation(auth.db, auth, conversation_id)
    existing = normalize_case_state(conv.case_state or {})
    incoming = normalize_case_state(case_state_patch_to_raw(body))
    # Conservation de la provenance d'origine pour les items existants (jamais de
    # promotion implicite d'une hypothèse ou d'un contrôle proposé).
    existing_by_id = {
        item["id"]: item
        for key in ("facts", "hypotheses", "proposed_checks", "performed_checks", "results", "missing_info")
        for item in existing[key]
    }
    for key in ("facts", "hypotheses", "proposed_checks", "performed_checks", "results", "missing_info"):
        for item in incoming[key]:
            previous = existing_by_id.get(item["id"])
            if previous is not None:
                item["origin"] = previous["origin"]
                item["created_at"] = previous["created_at"]
                item["message_id"] = item.get("message_id") or previous.get("message_id")
    conv.case_state = incoming
    conv.updated_at = func.now()
    auth.db.commit()
    return {"case_state": conv.case_state}
