"""Chat : récupération documentaire, streaming SSE, stop, retry, erreurs.

Le message utilisateur et le message assistant sont persistés avant le streaming.
La fermeture cliente (GeneratorExit) ferme l'appel fournisseur et persiste l'état
partiel (`cancelled`). Une génération à la fois par conversation.
"""
from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Iterator

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from .. import prompts
from ..app_settings import effective_provider, provider_config, retrieval_config
from ..config import Settings, get_settings
from ..db import get_sessionmaker
from ..deps import AuthContext, csrf_guard, require_user
from ..embeddings import EmbeddingsUnavailable, get_embedding_service
from ..llm import ProviderCancelled, ProviderError
from ..models import Attachment, Conversation, Message
from ..retrieval import hybrid_search
from ..schemas import ChatIn
from ..security import TokenBucket, utcnow
from ..serializers import message_out
from .conversations import get_owned_conversation, next_seq

log = logging.getLogger("wallia.chat")

router = APIRouter(prefix="/api", tags=["chat"])

RESOURCES_DIR = Path(__file__).resolve().parent.parent.parent / "resources"

_generation_bucket: TokenBucket | None = None
_conversation_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def generation_bucket(settings: Settings) -> TokenBucket:
    global _generation_bucket
    if _generation_bucket is None:
        _generation_bucket = TokenBucket(30)
    return _generation_bucket


def reset_generation_state() -> None:
    global _generation_bucket, _conversation_locks
    _generation_bucket = None
    with _locks_guard:
        _conversation_locks = {}


def _conversation_lock(conversation_id: uuid.UUID) -> threading.Lock:
    key = str(conversation_id)
    with _locks_guard:
        lock = _conversation_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _conversation_locks[key] = lock
        return lock


def _sse(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _split_for_stream(text: str, size: int = 48) -> Iterator[str]:
    for index in range(0, len(text), size):
        yield text[index : index + size]


def _load_attachments(db: Session, conversation_id: uuid.UUID, ids: list[uuid.UUID]) -> list[Attachment]:
    if not ids:
        return []
    rows = db.execute(select(Attachment).where(Attachment.id.in_(ids))).scalars().all()
    found = {row.id: row for row in rows}
    for item_id in ids:
        row = found.get(item_id)
        if row is None or row.conversation_id != conversation_id:
            # Jamais d'acceptation croisée entre cas, même avec un seul administrateur.
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="pièce jointe invalide pour cette conversation")
    return [found[item_id] for item_id in ids]


def _active_generation_exists(db: Session, conversation_id: uuid.UUID) -> bool:
    row = db.execute(
        text(
            "SELECT 1 FROM messages WHERE conversation_id = :c AND status = 'streaming'"
            " AND updated_at > now() - interval '10 minutes' LIMIT 1"
        ),
        {"c": str(conversation_id)},
    ).fetchone()
    return row is not None


def _ready_corpus_exists(db: Session) -> bool:
    return db.execute(text("SELECT 1 FROM documents WHERE status = 'ready' LIMIT 1")).fetchone() is not None


def _retrieve(db: Session, settings: Settings, conversation: Conversation, query: str) -> dict[str, Any]:
    """Recherche documentaire réelle ; en cas d'indisponibilité, état explicite."""
    if not _ready_corpus_exists(db):
        return {"status": "empty_corpus", "sources": [], "diagnostics": {"reason": "corpus vide"}}
    case_state = conversation.case_state or {}
    product = case_state.get("product") or None
    version = case_state.get("version") or None
    try:
        vector = get_embedding_service().encode([query], kind="query")[0]
    except EmbeddingsUnavailable as exc:
        return {"status": "embeddings_unavailable", "sources": [], "diagnostics": {"reason": str(exc)}}
    result = hybrid_search(
        db,
        settings,
        query=query,
        query_vector=vector,
        product=product,
        version=version,
        scope="all",
    )
    return result


def _demo_reply(sources: list[dict[str, Any]], retrieval_status: str, attachment_notes: list[str]) -> str:
    lines = [
        "**Mode démonstration — aucun modèle connecté.**",
        "",
        "Cette réponse n'est pas produite par un modèle de langage : aucun fournisseur n'est "
        "configuré (Administration → Connexion modèle). La recherche documentaire ci-dessous, "
        "en revanche, a réellement été exécutée sur le corpus indexé.",
        "",
    ]
    if sources:
        lines.append("Passages retrouvés dans le corpus (leur présence ne prouve pas qu'ils répondent à la question) :")
        lines.append("")
        for index, source in enumerate(sources[:5], start=1):
            pages = f"p. {source.get('page_start')}" if source.get("page_start") else "page inconnue"
            versions = ", ".join(source.get("versions") or []) or "version non précisée"
            lines.append(f"- [{index}] *{source.get('title')}* — {pages} — versions : {versions}")
            excerpt = " ".join(str(source.get("text") or "").split())[:220]
            lines.append(f"  > {excerpt}…")
        lines.append("")
        lines.append("Pour obtenir une réponse rédigée, connectez un fournisseur de modèle.")
    elif retrieval_status == "no_relevant_source":
        lines.append(
            "Aucun passage pertinent trouvé dans le corpus pour cette question "
            "(barrière de pertinence non franchie). Aucune citation n'est produite."
        )
    else:
        lines.append("Le corpus documentaire ne contient aucun document indexé pour le moment.")
    if attachment_notes:
        lines.append("")
        lines.append("Pièces jointes :")
        for note in attachment_notes:
            lines.append(f"- {note}")
    return "\n".join(lines)


def _persist_message(
    message_id: uuid.UUID,
    *,
    content: str,
    status_value: str,
    error: str | None,
    sources: list[dict[str, Any]] | None,
    model: str | None,
    demo: bool,
    conversation_id: uuid.UUID,
) -> None:
    session = get_sessionmaker()()
    try:
        message = session.get(Message, message_id)
        if message is None:
            return
        message.content = content
        message.status = status_value
        message.error = error
        message.sources = sources
        message.model = model
        message.demo = demo
        message.updated_at = utcnow()
        session.execute(
            text("UPDATE conversations SET updated_at = now() WHERE id = :c"), {"c": str(conversation_id)}
        )
        session.commit()
    except Exception:  # noqa: BLE001 - ne jamais masquer un streaming par une erreur de persistance
        session.rollback()
        log.exception("persistance du message %s impossible", message_id)
    finally:
        session.close()


def _stop_requested(message_id: uuid.UUID) -> bool:
    session = get_sessionmaker()()
    try:
        row = session.execute(
            text("SELECT stop_requested FROM messages WHERE id = :id"), {"id": str(message_id)}
        ).fetchone()
        return bool(row and row[0])
    finally:
        session.close()


def _stream(
    *,
    settings: Settings,
    conversation_id: uuid.UUID,
    assistant_id: uuid.UUID,
    model: str | None,
    demo: bool,
    provider_available: bool,
    messages: list[dict[str, Any]] | None,
    sources_payload: dict[str, Any] | None,
    demo_text: str | None,
) -> Iterator[str]:
    started = time.monotonic()
    content = ""
    final_status = "complete"
    error: str | None = None
    stop_poll_at = 0.0
    cancelled = False

    yield _sse(
        "meta",
        {
            "message_id": str(assistant_id),
            "conversation_id": str(conversation_id),
            "model": model,
            "demo": demo,
        },
    )
    if sources_payload is not None:
        yield _sse("sources", sources_payload)

    try:
        if not provider_available:
            yield _sse("status", {"state": "demo", "label": "Mode démonstration (aucun modèle connecté)"})
            assert demo_text is not None
            for piece in _split_for_stream(demo_text):
                content += piece
                yield _sse("delta", {"text": piece})
                time.sleep(0.02)
        else:
            yield _sse("status", {"state": "generation", "label": "Rédaction de la réponse"})
            assert messages is not None
            provider = None
            session = get_sessionmaker()()
            try:
                from ..app_settings import effective_provider as _eff

                provider = _eff(session, settings)
            finally:
                session.close()
            assert provider is not None

            def _cancel() -> bool:
                nonlocal stop_poll_at, cancelled
                now = time.monotonic()
                if now - stop_poll_at < 0.4:
                    return cancelled
                stop_poll_at = now
                cancelled = _stop_requested(assistant_id)
                return cancelled

            for event in provider.stream_chat(messages, cancel_check=_cancel):
                if time.monotonic() - started > settings.chat_max_seconds:
                    raise ProviderError("timeout", "délai de génération dépassé")
                if event["type"] == "delta":
                    content += event["text"]
                    yield _sse("delta", {"text": event["text"]})
            final_status = "complete"
    except ProviderCancelled:
        final_status = "cancelled"
    except ProviderError as exc:
        final_status = "error"
        error = exc.safe_message
        yield _sse("error", {"message": exc.safe_message, "retryable": True, "message_id": str(assistant_id)})
    except GeneratorExit:
        final_status = "cancelled"
        _persist_message(
            assistant_id,
            content=content,
            status_value=final_status,
            error=None,
            sources=(sources_payload or {}).get("sources") if sources_payload else None,
            model=model,
            demo=demo,
            conversation_id=conversation_id,
        )
        raise
    except Exception as exc:  # noqa: BLE001 - jamais laisser un message en streaming
        log.exception("streaming en erreur")
        final_status = "error"
        error = f"erreur interne ({exc.__class__.__name__})"
        yield _sse("error", {"message": "erreur interne pendant la génération", "retryable": True, "message_id": str(assistant_id)})

    _persist_message(
        assistant_id,
        content=content,
        status_value=final_status,
        error=error,
        sources=(sources_payload or {}).get("sources") if sources_payload else None,
        model=model,
        demo=demo,
        conversation_id=conversation_id,
    )
    if final_status == "complete":
        yield _sse(
            "done",
            {
                "message_id": str(assistant_id),
                "status": final_status,
                "model": model,
                "demo": demo,
                "sources_count": len((sources_payload or {}).get("sources") or []),
            },
        )
    elif final_status == "cancelled":
        yield _sse("done", {"message_id": str(assistant_id), "status": "cancelled", "model": model, "demo": demo})


def _prepare(
    db: Session,
    settings: Settings,
    conversation: Conversation,
    user_text: str,
    attachments: list[Attachment],
) -> dict[str, Any]:
    retrieval = _retrieve(db, settings, conversation, user_text)
    sources = retrieval.get("sources") or []
    history = [
        {"role": m.role, "content": m.content}
        for m in db.execute(
            select(Message)
            .where(
                Message.conversation_id == conversation.id,
                Message.role.in_(["user", "assistant"]),
                Message.status.in_(["complete", "cancelled"]),
                Message.content != "",
            )
            .order_by(Message.seq.asc())
        ).scalars().all()
    ]
    system_prompt = prompts.build_system_prompt(settings, RESOURCES_DIR)
    att_dicts = [
        {
            "kind": a.kind,
            "filename_original": a.filename_original,
            "extracted_text": a.extracted_text,
        }
        for a in attachments
    ]
    messages, notes = prompts.build_provider_messages(
        settings,
        system_prompt=system_prompt,
        history=history,
        user_text=user_text,
        sources=sources,
        attachments=att_dicts,
        case_state=conversation.case_state or {},
    )
    notes = list(notes)
    if retrieval.get("status") == "no_relevant_source":
        notes.append("Aucune source pertinente : ne cite aucun document, ne fabrique aucune procédure.")
    elif retrieval.get("status") == "empty_corpus":
        notes.append("Le corpus documentaire est vide : réponds sans citation documentaire.")
    return {"retrieval": retrieval, "sources": sources, "messages": messages, "notes": notes, "attachments": att_dicts}


def _start_generation(auth: AuthContext, conversation: Conversation, user_text: str, attachments: list[Attachment]) -> StreamingResponse:
    settings = auth.settings
    db = auth.db
    provider_conf = provider_config(db, settings)
    provider_ok = provider_conf["key_configured"] and provider_conf["endpoint"]
    provider_available = False
    model = provider_conf["model"]
    if provider_ok:
        probe = effective_provider(db, settings)
        status_obj = probe.status()
        provider_available = status_obj.available
        if not provider_available:
            log.warning("fournisseur configuré mais indisponible: %s", status_obj.reason)

    prep = _prepare(db, settings, conversation, user_text, attachments)
    assistant = Message(
        conversation_id=conversation.id,
        seq=next_seq(db, conversation.id),
        role="assistant",
        content="",
        status="streaming",
        model=model if provider_available else None,
        demo=not provider_available,
        sources=prep["sources"] or None,
    )
    db.add(assistant)
    db.commit()
    db.refresh(assistant)
    assistant_id = assistant.id

    demo_text = None
    if not provider_available:
        demo_text = _demo_reply(prep["sources"], prep["retrieval"]["status"], prep["notes"])

    sources_payload = {
        "status": prep["retrieval"]["status"],
        "sources": prep["sources"],
        "diagnostics": prep["retrieval"].get("diagnostics") or {},
    }
    generator = _stream(
        settings=settings,
        conversation_id=conversation.id,
        assistant_id=assistant_id,
        model=model if provider_available else None,
        demo=not provider_available,
        provider_available=provider_available,
        messages=prep["messages"] if provider_available else None,
        sources_payload=sources_payload,
        demo_text=demo_text,
    )
    return StreamingResponse(
        generator,
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-store",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@router.post("/conversations/{conversation_id}/chat")
def chat(conversation_id: uuid.UUID, body: ChatIn, request: Request, auth: AuthContext = Depends(csrf_guard)):
    settings = auth.settings
    db = auth.db
    conversation = get_owned_conversation(db, auth, conversation_id)
    if _active_generation_exists(db, conversation.id):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="une génération est déjà en cours dans cette conversation")
    if not generation_bucket(settings).allow(f"gen:{auth.user.id}"):
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="trop de générations, patientez un instant")
    lock = _conversation_lock(conversation.id)
    if not lock.acquire(blocking=False):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="une génération est déjà en cours dans cette conversation")

    try:
        attachments = _load_attachments(db, conversation.id, body.attachment_ids)
        user_text = body.text.strip()
        user_message = Message(
            conversation_id=conversation.id,
            seq=next_seq(db, conversation.id),
            role="user",
            content=user_text,
            status="complete",
        )
        db.add(user_message)
        db.flush()
        for attachment in attachments:
            attachment.message_id = user_message.id
        response = _start_generation(auth, conversation, user_text, attachments)
    finally:
        lock.release()
    return response


@router.post("/messages/{message_id}/stop")
def stop_generation(message_id: uuid.UUID, auth: AuthContext = Depends(csrf_guard)):
    message = auth.db.get(Message, message_id)
    if message is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="message introuvable")
    conversation = get_owned_conversation(auth.db, auth, message.conversation_id)
    if message.role != "assistant" or message.status != "streaming":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="aucune génération en cours pour ce message")
    message.stop_requested = True
    auth.db.execute(
        text("UPDATE conversations SET updated_at = now() WHERE id = :c"), {"c": str(conversation.id)}
    )
    auth.db.commit()
    return {"ok": True, "message_id": str(message.id)}


@router.post("/messages/{message_id}/retry")
def retry_generation(message_id: uuid.UUID, auth: AuthContext = Depends(csrf_guard)):
    db = auth.db
    source = db.get(Message, message_id)
    if source is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="message introuvable")
    conversation = get_owned_conversation(db, auth, source.conversation_id)
    if source.role != "assistant" or source.status not in ("error", "cancelled", "interrupted"):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="ce message ne peut pas être relancé")
    if _active_generation_exists(db, conversation.id):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="une génération est déjà en cours dans cette conversation")
    previous_user = db.execute(
        select(Message)
        .where(Message.conversation_id == conversation.id, Message.role == "user", Message.seq < source.seq)
        .order_by(Message.seq.desc())
        .limit(1)
    ).scalar_one_or_none()
    if previous_user is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="aucun message utilisateur à relancer")
    attachments = db.execute(
        select(Attachment).where(Attachment.message_id == previous_user.id)
    ).scalars().all()
    lock = _conversation_lock(conversation.id)
    if not lock.acquire(blocking=False):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="une génération est déjà en cours dans cette conversation")
    try:
        response = _start_generation(auth, conversation, previous_user.content, list(attachments))
    finally:
        lock.release()
    return response
