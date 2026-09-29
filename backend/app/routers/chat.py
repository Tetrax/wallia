"""Chat : récupération documentaire, streaming SSE, stop, retry, erreurs.

Le message utilisateur et le message assistant sont persistés avant le streaming.
L'annulation est explicite au niveau transport : un veilleur interroge l'état
serveur (stop demandé, conversation supprimée, délai total) et ferme réellement
l'appel fournisseur via `ProviderCancellation`, même avant le premier delta ou
si le fournisseur est silencieux. La déconnexion cliente est détectée au niveau
ASGI (`http.disconnect`) : elle ferme le transport amont et garantit la
persistance du partiel, sans dépendre d'une progression du flux, du ramasse-
miettes ni d'un delta suivant. Une génération à la fois par conversation,
plafond global borné réservé par verrou transactionnel court.
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
from ..app_settings import effective_provider, provider_config
from ..config import Settings, get_settings, vision_availability
from ..db import get_sessionmaker
from ..deps import AuthContext, csrf_guard, require_user
from ..embeddings import EmbeddingsUnavailable, get_embedding_service
from ..llm import ProviderCancelled, ProviderCancellation, ProviderError
from ..models import Attachment, Conversation, Message
from ..retrieval import hybrid_search
from ..schemas import ChatIn, normalize_case_state
from ..security import TokenBucket, utcnow
from ..serializers import message_out
from ..web import web_fallback_for_chat
from .conversations import get_owned_conversation, next_seq

log = logging.getLogger("wallia.chat")

router = APIRouter(prefix="/api", tags=["chat"])

RESOURCES_DIR = Path(__file__).resolve().parent.parent.parent / "resources"

# Borne des pièces jointes reprises dans le contexte (cas courant, jamais un autre).
CONTEXT_ATTACHMENTS_LIMIT = 10
# Clé du verrou consultatif transactionnel global (réservation de slot).
GLOBAL_GENERATION_LOCK_KEY = 20260926

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


class SseStreamingResponse(StreamingResponse):
    """StreamingResponse qui traite réellement la déconnexion cliente.

    Starlette annule la tâche d'envoi sur `http.disconnect`, mais le générateur
    synchrone (threadpool) resterait suspendu : son `finally` (persistance du
    partiel, fermeture du transport amont) ne s'exécuterait jamais. Ici, la
    déconnexion déclenche explicitement l'annulation du fournisseur et la
    fermeture du générateur — constatées côté amont, jamais supposées.
    """

    def __init__(self, content: Iterator[str], *, on_disconnect=None, **kwargs: Any) -> None:
        super().__init__(content, **kwargs)
        self._on_disconnect = on_disconnect

    async def __call__(self, scope, receive, send) -> None:  # type: ignore[override]
        if scope["type"] != "http":
            await super().__call__(scope, receive, send)
            return
        import anyio

        disconnected = False
        async with anyio.create_task_group() as task_group:

            async def run_stream() -> None:
                try:
                    await self.stream_response(send)
                finally:
                    task_group.cancel_scope.cancel()

            task_group.start_soon(run_stream)
            try:
                await self.listen_for_disconnect(receive)
                disconnected = True
            finally:
                callback = self._on_disconnect if disconnected else None
                if callback is not None:
                    try:
                        callback()
                    except Exception:  # noqa: BLE001 - fermeture best effort
                        log.warning("fermeture de génération sur déconnexion: échec du callback")
                task_group.cancel_scope.cancel()
        if self.background is not None:
            await self.background()


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


def _conversation_attachments(db: Session, conversation_id: uuid.UUID) -> list[Attachment]:
    """Pièces jointes du cas, bornées (les plus récentes), jamais d'un autre cas."""
    rows = db.execute(
        select(Attachment)
        .where(Attachment.conversation_id == conversation_id)
        .order_by(Attachment.created_at.desc())
        .limit(CONTEXT_ATTACHMENTS_LIMIT)
    ).scalars().all()
    return list(reversed(rows))


def _active_generation_exists(db: Session, conversation_id: uuid.UUID, window_seconds: int) -> bool:
    row = db.execute(
        text(
            "SELECT 1 FROM messages WHERE conversation_id = :c AND status = 'streaming'"
            " AND updated_at > now() - make_interval(secs => :window) LIMIT 1"
        ),
        {"c": str(conversation_id), "window": window_seconds},
    ).fetchone()
    return row is not None


def _active_generation_count(db: Session, window_seconds: int) -> int:
    row = db.execute(
        text(
            "SELECT count(*) FROM messages WHERE status = 'streaming'"
            " AND updated_at > now() - make_interval(secs => :window)"
        ),
        {"window": window_seconds},
    ).fetchone()
    return int(row[0]) if row else 0


def _reserve_generation_slot(db: Session, conversation: Conversation, settings: Settings) -> None:
    """Réserve le droit d'insérer une génération, atomiquement.

    Un verrou consultatif transactionnel GLOBAL court sérialise le contrôle et
    l'insertion : deux requêtes concurrentes — même sur des conversations
    différentes — ne peuvent pas franchir le plafond global, et deux requêtes
    sur la même conversation ne peuvent pas créer deux générations actives.
    Le verrou est libéré au COMMIT de l'insertion, jamais tenu pendant la
    préparation (embeddings) ni pendant le streaming.
    """
    window = settings.stream_recovery_window_seconds
    db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": GLOBAL_GENERATION_LOCK_KEY})
    if _active_generation_exists(db, conversation.id, window):
        # Refus : la transaction est fermée immédiatement (le verrou global et
        # les verrous de lecture sont libérés sans attendre la fin de requête).
        db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="une génération est déjà en cours dans cette conversation")
    if _active_generation_count(db, window) >= settings.chat_max_concurrent:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="trop de générations simultanées, patientez un instant")


def _ready_corpus_exists(db: Session) -> bool:
    return db.execute(text("SELECT 1 FROM documents WHERE status = 'ready' LIMIT 1")).fetchone() is not None


def _retrieve(db: Session, settings: Settings, case_state: dict[str, Any], query: str) -> dict[str, Any]:
    """Recherche documentaire réelle (SQL + reclassement) ; état explicite si indisponible.

    Le reclassement est le cross-encoder réel configuré (aucun réglage
    opérateur) ; son indisponibilité remonte telle quelle (`retrieval_unavailable`
    avec sources vides), sans jamais retomber sur une ancienne barrière.
    """
    if not _ready_corpus_exists(db):
        return {"status": "empty_corpus", "sources": [], "diagnostics": {"reason": "corpus vide"}}
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


def _known_products(db: Session) -> list[str]:
    rows = db.execute(
        text("SELECT DISTINCT product FROM documents WHERE product IS NOT NULL AND product <> '' LIMIT 50")
    ).fetchall()
    return [str(row[0]) for row in rows]


def _scan_declarations(db: Session, user_text: str) -> dict[str, Any]:
    """Extraction EXPLICITE produit/version du tour — PURE, sans effet de bord.

    Aucune écriture ni verrou ici : le résultat sert à construire l'état
    effectif du tour AVANT le retrieval (embeddings compris)."""
    from ..declarations import extract_explicit_declarations, is_explicit_correction

    return {
        "found": extract_explicit_declarations(user_text, known_products=_known_products(db)),
        "correction": is_explicit_correction(user_text),
    }


def _merge_declared_facts(state: dict[str, Any], scan: dict[str, Any]) -> bool:
    """Applique les déclarations explicites à un état de cas NORMALISÉ (copie
    de travail) : provenance conservée (message renseigné à la persistance,
    dans la transaction d'insertion), jamais de déduction ni de promotion
    d'hypothèse. Retourne True si l'état a changé."""
    found = scan["found"]
    if not found["product"] and not found["version"]:
        return False
    changed = False
    for field, label in (("product", "produit"), ("version", "version")):
        for value in found[field]:
            fact_text = f"{label} explicitement déclaré(e) : {value}"
            if not any(item.get("text") == fact_text for item in state["facts"]):
                state["facts"].append(
                    {
                        "id": str(uuid.uuid4()),
                        "text": fact_text,
                        "status": "confirmed",
                        "origin": "user_message",
                        "message_id": None,
                        "created_at": utcnow().isoformat(),
                    }
                )
                changed = True
            current = state.get(field)
            if not current or (scan["correction"] and current != value):
                if current != value:
                    state[field] = value
                    changed = True
    return changed


def _declarations_effective_state(
    db: Session, conversation: Conversation, user_text: str
) -> tuple[dict[str, Any], bool]:
    """État effectif du TOUR courant : les déclarations explicites du message
    s'appliquent immédiatement (produit/version dès ce retrieval), pas au tour
    suivant. L'état n'est PAS persisté ici : la persistance (avec l'ID du
    message comme provenance) rejoint la transaction d'insertion."""
    state = normalize_case_state(conversation.case_state or {})
    scan = _scan_declarations(db, user_text)
    changed = _merge_declared_facts(state, scan)
    return state, changed


def _persist_declarations(
    conversation: Conversation, state: dict[str, Any], changed: bool, message_id: uuid.UUID
) -> None:
    """Persiste les déclarations du tour : les faits créés par CE message
    reçoivent leur provenance (message ID) puis l'état est rattaché à l'objet
    de conversation — le COMMIT reste celui de la transaction d'insertion."""
    if not changed:
        return
    for fact in state["facts"]:
        if fact.get("origin") == "user_message" and not fact.get("message_id"):
            fact["message_id"] = str(message_id)
    conversation.case_state = state


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
        lines.append("Sources retrouvées (leur présence ne prouve pas qu'elles répondent à la question) :")
        lines.append("")
        for index, source in enumerate(sources[:5], start=1):
            if source.get("source_type") == "web":
                lines.append(
                    f"- [{index}] *{source.get('title')}* — {source.get('domain')} "
                    "(web public, version non vérifiée)"
                )
                excerpt = " ".join(str(source.get("text") or "").split())[:220]
                lines.append(f"  > {excerpt}…")
                continue
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
            "(aucun passage n'atteint le seuil de reclassement). Aucune citation n'est produite."
        )
    elif retrieval_status == "retrieval_unavailable":
        lines.append(
            "Recherche documentaire momentanément indisponible (reclassement des passages) : "
            "aucune citation n'est produite. Réessayez dans un instant."
        )
    elif retrieval_status == "embeddings_unavailable":
        lines.append(
            "Recherche documentaire momentanément indisponible (service d'embeddings) : "
            "aucune citation n'est produite. Réessayez dans un instant."
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
        # Mise à jour directe (aucun chargement ORM) : pas de transaction de
        # lecture ouverte inutilement, aucun autoflush ambigu.
        session.execute(
            text(
                "UPDATE messages SET content = :content, status = :status, error = :error,"
                " sources = CAST(:sources AS jsonb), model = :model, demo = :demo,"
                " updated_at = now() WHERE id = :id"
            ),
            {
                "content": content,
                "status": status_value,
                "error": error,
                "sources": json.dumps(sources, ensure_ascii=False) if sources is not None else None,
                "model": model,
                "demo": demo,
                "id": str(message_id),
            },
        )
        session.execute(
            text("UPDATE conversations SET updated_at = now() WHERE id = :c"), {"c": str(conversation_id)}
        )
        session.commit()
    except Exception as exc:  # noqa: BLE001 - ne jamais masquer un streaming par une erreur de persistance
        session.rollback()
        # Jamais de traceback : une erreur SQL pourrait écho le texte utilisateur
        # ou des paramètres. Seule la classe d'erreur est journalisée.
        log.error("persistance du message %s impossible (%s)", message_id, exc.__class__.__name__)
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


def _liveness(message_id: uuid.UUID) -> str:
    """État serveur du message : alive / stop / gone (conversation supprimée)."""
    session = get_sessionmaker()()
    try:
        row = session.execute(
            text("SELECT stop_requested FROM messages WHERE id = :id"), {"id": str(message_id)}
        ).fetchone()
        if row is None:
            return "gone"
        return "stop" if row[0] else "alive"
    finally:
        session.close()


class _StopWatch:
    """Veilleur : stop demandé, conversation supprimée, délai total.

    Il ferme réellement le transport via `ProviderCancellation.cancel()` — même
    si le fournisseur est silencieux et n'a envoyé aucun octet.
    """

    def __init__(self, message_id: uuid.UUID, deadline: float, cancel: ProviderCancellation) -> None:
        self._message_id = message_id
        self._deadline = deadline
        self._cancel = cancel
        self._stop = threading.Event()
        self._started = False
        self._thread = threading.Thread(target=self._run, daemon=True, name=f"wallia-stopwatch-{message_id}")

    def start(self) -> None:
        self._started = True
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._started:
            self._thread.join(timeout=1.0)

    def _run(self) -> None:
        last_check = 0.0
        while not self._stop.is_set():
            now = time.monotonic()
            if now >= self._deadline:
                self._cancel.cancel("timeout")
                return
            if now - last_check >= 0.3:
                last_check = now
                try:
                    state = _liveness(self._message_id)
                except Exception:  # noqa: BLE001 - base momentanément indisponible : on réessaie
                    state = "alive"
                if state == "gone":
                    self._cancel.cancel("deleted")
                    return
                if state == "stop":
                    self._cancel.cancel("stop")
                    return
            self._stop.wait(0.1)


def _on_client_disconnect(cancel: ProviderCancellation, generator: Iterator[str] | None) -> None:
    """Déconnexion cliente constatée : ferme le transport amont et le générateur.

    - Si le générateur est suspendu sur un `yield`, `close()` déclenche son
      `finally` (persistance du partiel).
    - S'il s'exécute dans le threadpool (lecture amont bloquée), `close()` lève
      `ValueError` : la fermeture du transport ci-dessus débloque la lecture et
      le `finally` s'exécute dans ce thread.
    """
    cancel.cancel("disconnect")
    if generator is None:
        return
    try:
        generator.close()
    except (ValueError, RuntimeError):
        pass


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
    cancel: ProviderCancellation,
) -> Iterator[str]:
    started = time.monotonic()
    content = ""
    final_status = "complete"
    error: str | None = None
    deadline = started + settings.chat_max_seconds
    watch = _StopWatch(assistant_id, deadline, cancel)
    generation: Any = None
    persisted = False

    def persist_once() -> None:
        nonlocal persisted
        if persisted:
            return
        persisted = True
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

    # TOUT le corps est dans le try/finally : même une déconnexion juste après
    # `meta` (ou avant toute émission) déclenche la persistance du partiel.
    try:
        watch.start()
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
        if not provider_available:
            yield _sse("status", {"state": "demo", "label": "Mode démonstration (aucun modèle connecté)"})
            assert demo_text is not None
            for index, piece in enumerate(_split_for_stream(demo_text, 32)):
                if cancel.cancelled:
                    raise ProviderCancelled(cancel.reason)
                if index % 3 == 0 and _stop_requested(assistant_id):
                    cancel.cancel("stop")
                    raise ProviderCancelled("stop")
                content += piece
                yield _sse("delta", {"text": piece})
                time.sleep(0.045)
        else:
            yield _sse("status", {"state": "generation", "label": "Rédaction de la réponse"})
            assert messages is not None
            session = get_sessionmaker()()
            try:
                provider = effective_provider(session, settings)
            finally:
                session.close()
            generation = provider.stream_chat(messages, cancellation=cancel)
            try:
                for event in generation:
                    if cancel.cancelled:
                        raise ProviderCancelled(cancel.reason)
                    if time.monotonic() > deadline:
                        cancel.cancel("timeout")
                        raise ProviderCancelled("timeout")
                    if event["type"] == "delta":
                        content += event["text"]
                        yield _sse("delta", {"text": event["text"]})
            finally:
                # Fermeture explicite du générateur fournisseur : le transport
                # est libéré par le finally de stream_chat, sans dépendre du GC.
                generation.close()
        final_status = "complete"
    except ProviderCancelled as exc:
        if exc.reason == "timeout":
            final_status = "error"
            error = "délai de génération dépassé"
            yield _sse("error", {"message": error, "retryable": True, "message_id": str(assistant_id)})
        else:
            final_status = "cancelled"
    except ProviderError as exc:
        final_status = "error"
        error = exc.safe_message
        yield _sse("error", {"message": exc.safe_message, "retryable": True, "message_id": str(assistant_id)})
    except GeneratorExit:
        # Fermeture du générateur (déconnexion cliente constatée) : jamais une
        # réponse « complete » alors que le flux n'est pas terminé.
        final_status = "cancelled"
        cancel.cancel("disconnect")
        raise
    except Exception as exc:  # noqa: BLE001 - jamais laisser un message en streaming
        # Aucun traceback : il pourrait écho le texte utilisateur ou des paramètres.
        log.error("streaming en erreur (%s)", exc.__class__.__name__)
        final_status = "error"
        error = f"erreur interne ({exc.__class__.__name__})"
        yield _sse("error", {"message": "erreur interne pendant la génération", "retryable": True, "message_id": str(assistant_id)})
    finally:
        # Persistance garantie du partiel, y compris annulation après `meta`.
        watch.stop()
        try:
            if generation is not None:
                generation.close()
        except Exception:  # noqa: BLE001
            pass
        cancel.cancel("closed")
        persist_once()

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
    *,
    case_state: dict[str, Any],
) -> dict[str, Any]:
    """Prépare retrieval + repli web borné + messages fournisseur pour le tour.

    `case_state` est l'état EFFECTIF du tour (déclarations du message courant
    déjà appliquées) : le retrieval et le prompt ne voient jamais l'ancien état.
    Le repli web ne s'active JAMAIS à la place du corpus (statuts conservés
    indépendamment) et jamais pour masquer une indisponibilité technique.
    Les notes d'honnêteté (corpus ET web) sont construites AVANT
    `build_provider_messages` : elles partent réellement au fournisseur, dans
    le message de données.
    """
    retrieval = _retrieve(db, settings, case_state, user_text)
    corpus_sources = retrieval.get("sources") or []
    web_info = web_fallback_for_chat(
        db,
        settings,
        retrieval_status=str(retrieval.get("status") or ""),
        user_text=user_text,
        case_state=case_state,
    )
    sources = list(corpus_sources) + list(web_info.get("sources") or [])
    retrieval_status = str(retrieval.get("status") or "")
    web_state = str(web_info.get("status") or "")

    # Notes d'honnêteté corpus ET web construites AVANT le prompt : elles sont
    # réellement transmises au fournisseur (message de données), jamais
    # ajoutées après coup. « Pas de citation documentaire corpus » ne nie
    # jamais la possibilité de citer SÉPARÉMENT un extrait web public identifié.
    honesty_notes: list[str] = []
    if web_state == "ok":
        honesty_notes.append(
            "Des extraits web publics du constructeur (version non vérifiée) complètent le corpus : "
            "présente-les comme un complément NON qualifié, jamais comme une procédure constructeur validée."
        )
    elif web_state == "unavailable":
        honesty_notes.append("Recherche web complémentaire indisponible : ne la présente pas comme active.")
    elif web_state == "no_results":
        honesty_notes.append("Aucun résultat web public complémentaire pour cette question.")
    if retrieval_status == "no_relevant_source":
        if web_state == "ok":
            honesty_notes.append(
                "Aucune source pertinente dans le corpus : ne cite aucun document du corpus, ne fabrique "
                "ni citation documentaire ni procédure. Des extraits web publics identifiés séparément "
                "peuvent en revanche être cités avec leur marqueur [n], explicitement comme résultat de "
                "recherche non qualifié (version non vérifiée)."
            )
        else:
            honesty_notes.append("Aucune source pertinente : ne cite aucun document, ne fabrique ni citation ni procédure.")
    elif retrieval_status == "empty_corpus":
        if web_state == "ok":
            honesty_notes.append(
                "Le corpus documentaire est vide : réponds sans citation documentaire ; des extraits web "
                "publics identifiés séparément peuvent être cités avec leur marqueur [n], explicitement "
                "comme résultat de recherche non qualifié (version non vérifiée)."
            )
        else:
            honesty_notes.append("Le corpus documentaire est vide : réponds sans citation documentaire.")
    elif retrieval_status in ("retrieval_unavailable", "embeddings_unavailable"):
        honesty_notes.append(
            "Recherche documentaire temporairement indisponible : ne cite aucun document, ne fabrique "
            "ni citation ni procédure, et signale cette indisponibilité sans la présenter comme une absence de source."
        )
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
    # Continuité du cas : TOUTES les pièces jointes déjà liées à cette
    # conversation (bornées) restent dans le contexte, jamais celles d'un autre cas.
    context_attachments = _conversation_attachments(db, conversation.id)
    att_dicts = [
        {
            "kind": a.kind,
            "filename_original": a.filename_original,
            "extracted_text": a.extracted_text,
        }
        for a in context_attachments
    ]
    current_ids = {a.id for a in attachments}
    notes_extra: list[str] = []
    for a in context_attachments:
        if a.id not in current_ids and a.kind == "pdf" and not a.extracted_text:
            notes_extra.append(f"PDF antérieur « {a.filename_original} » sans texte extrait disponible.")
    vision_effective, _ = vision_availability(settings)
    messages, notes = prompts.build_provider_messages(
        settings,
        system_prompt=system_prompt,
        history=history,
        user_text=user_text,
        sources=sources,
        attachments=att_dicts,
        case_state=case_state,
        vision_effective=vision_effective,
        extra_notes=honesty_notes,
    )
    # Les notes transmises au fournisseur ont été construites AVANT l'appel ;
    # seul l'ajout local des notes de pièces jointes (non transmises) suit.
    notes = list(notes) + notes_extra
    return {
        "retrieval": retrieval,
        "sources": sources,
        "web": web_info,
        "messages": messages,
        "notes": notes,
        "attachments": att_dicts,
    }


def _start_generation(
    auth: AuthContext,
    conversation: Conversation,
    user_text: str,
    attachments: list[Attachment],
    *,
    persist_user: bool = True,
) -> StreamingResponse:
    """Prépare, réserve le slot (atomique) et persiste user + assistant, puis stream.

    `persist_user=False` (relance) : le tour utilisateur EXISTE déjà — il n'est
    jamais dupliqué, seul le nouveau message assistant est créé.
    """
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

    # Déclarations du tour AVANT le retrieval : l'état effectif (produit/version
    # immédiatement applicables) est construit en mémoire, sans verrou et sans
    # écriture ; le retrieval (embeddings compris) reste hors de tout verrou
    # global. La PERSISTANCE rejoint la transaction d'insertion du message.
    case_state = conversation.case_state or {}
    declarations_changed = False
    if persist_user:
        case_state, declarations_changed = _declarations_effective_state(db, conversation, user_text)

    # Préparation en lecture seule (embeddings compris) AVANT les verrous :
    # aucun verrou global n'est tenu pendant un calcul lent.
    prep = _prepare(
        db, settings, conversation, user_text, attachments, case_state=case_state
    )

    # Réservation du slot + insertion dans la MÊME transaction : le plafond
    # global ne peut pas être franchi par des insertions concurrentes.
    _reserve_generation_slot(db, conversation, settings)
    if persist_user:
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
        # Provenance des déclarations du tour + état, dans la MÊME transaction
        # que l'insertion (aucune écriture partielle possible).
        _persist_declarations(conversation, case_state, declarations_changed, user_message.id)
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
    # Pas de `refresh()` : il rouvrirait une transaction de lecture dans la
    # session de requête pendant tout le streaming (verrous ACCESS SHARE qui
    # bloqueraient la maintenance). L'identifiant est déjà chargé.
    assistant_id = assistant.id

    demo_text = None
    if not provider_available:
        demo_text = _demo_reply(prep["sources"], prep["retrieval"]["status"], prep["notes"])

    sources_payload = {
        "status": prep["retrieval"]["status"],
        "sources": prep["sources"],
        "diagnostics": prep["retrieval"].get("diagnostics") or {},
        # Métadonnée web : statut INDÉPENDANT du statut corpus
        # (not_needed/disabled/unavailable/no_results/ok), jamais un repli
        # silencieux ; la requête publique n'utilise que le vocabulaire fermé.
        "web": {
            "status": prep["web"].get("status"),
            "reason": prep["web"].get("reason"),
            "query": prep["web"].get("query"),
        },
    }
    cancel = ProviderCancellation()
    holder: dict[str, Iterator[str] | None] = {"generator": None}
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
        cancel=cancel,
    )
    holder["generator"] = generator
    return SseStreamingResponse(
        generator,
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-store",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
        on_disconnect=lambda: _on_client_disconnect(cancel, holder["generator"]),
    )


@router.post("/conversations/{conversation_id}/chat")
def chat(conversation_id: uuid.UUID, body: ChatIn, request: Request, auth: AuthContext = Depends(csrf_guard)):
    settings = auth.settings
    db = auth.db
    conversation = get_owned_conversation(db, auth, conversation_id)
    window = settings.stream_recovery_window_seconds
    if _active_generation_exists(db, conversation.id, window):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="une génération est déjà en cours dans cette conversation")
    if not generation_bucket(settings).allow(f"gen:{auth.user.id}"):
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="trop de générations, patientez un instant")
    lock = _conversation_lock(conversation.id)
    if not lock.acquire(blocking=False):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="une génération est déjà en cours dans cette conversation")

    try:
        attachments = _load_attachments(db, conversation.id, body.attachment_ids)
        user_text = body.text.strip()
        response = _start_generation(auth, conversation, user_text, attachments)
        # Aucune transaction de requête ne reste ouverte pendant le streaming.
        db.commit()
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
    window = auth.settings.stream_recovery_window_seconds
    if _active_generation_exists(db, conversation.id, window):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="une génération est déjà en cours dans cette conversation")
    if not generation_bucket(auth.settings).allow(f"gen:{auth.user.id}"):
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="trop de générations, patientez un instant")
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
        # Mêmes limites pour une relance : réservation atomique globale, sans
        # dupliquer le tour utilisateur existant.
        response = _start_generation(auth, conversation, previous_user.content, list(attachments), persist_user=False)
        db.commit()
    finally:
        lock.release()
    return response
