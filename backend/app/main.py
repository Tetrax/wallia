"""Application FastAPI Wallia : assemblage, middlewares, statiques, démarrage."""
from __future__ import annotations

import json
import logging
import threading
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.datastructures import MutableHeaders

from . import __version__
from .config import get_settings
from .db import session_scope
from .jobs import recover_stale_streams
from .routers import (
    attachments,
    auth,
    chat,
    conversations,
    documents,
    internal,
    search,
    settings_api,
    status as status_router,
    web_api,
)
from .security import TokenBucket, client_ip

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("wallia.app")

APP_TITLE = "Wallia"

_BASE_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: blob:; font-src 'self' data:; connect-src 'self'; "
        "object-src 'none'; base-uri 'self'; frame-ancestors 'none'; form-action 'self'"
    ),
}


class SecurityHeadersMiddleware:
    """En-têtes de sécurité sur toutes les réponses (ASGI pur : pas de buffering SSE)."""

    def __init__(self, app, production: bool) -> None:
        self.app = app
        self.extra = (
            {"Strict-Transport-Security": "max-age=31536000; includeSubDomains"} if production else {}
        )

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for key, value in {**_BASE_HEADERS, **self.extra}.items():
                    headers.setdefault(key, value)
            await send(message)

        return await self.app(scope, receive, send_wrapper)


class ApiRateLimitMiddleware:
    """Garde-fou global en mémoire (défense anti-spoof, en plus du rate-limit login durable)."""

    def __init__(self, app, rate_per_minute: int) -> None:
        self.app = app
        self.bucket = TokenBucket(rate_per_minute)

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        path = scope.get("path", "")
        if path.startswith("/api") or path.startswith("/internal"):
            request = Request(scope, receive)
            settings = get_settings()
            ip = client_ip(request, settings)
            if not self.bucket.allow(f"api:{ip}"):
                response = JSONResponse(
                    {"detail": "trop de requêtes, réessayez plus tard"}, status_code=429,
                    headers={"Retry-After": "60"},
                )
                return await response(scope, receive, send)
        return await self.app(scope, receive, send)


class BodySizeLimitMiddleware:
    """Borne ASGI cumulée du corps HTTP, AVANT parsing/spool multipart.

    - `Content-Length` annoncé au-dessus de la limite → 413 immédiat, sans lire.
    - Corps réellement reçu au-dessus de la limite → 413 dès le franchissement :
      le corps n'est jamais mis en mémoire ni spoolé en entier.
    """

    def __init__(self, app, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        for name, value in scope.get("headers") or []:
            if name.lower() == b"content-length":
                try:
                    declared = int(value.decode("latin-1"))
                except (ValueError, UnicodeDecodeError):
                    declared = None
                if declared is not None and declared > self.max_bytes:
                    response = JSONResponse(
                        {"detail": "corps de requête trop volumineux"}, status_code=413
                    )
                    return await response(scope, receive, send)
        total = 0

        async def bounded_receive():
            nonlocal total
            message = await receive()
            if message["type"] == "http.request":
                total += len(message.get("body") or b"")
                if total > self.max_bytes:
                    raise HTTPException(status_code=413, detail="corps de requête trop volumineux")
            return message

        return await self.app(scope, bounded_receive, send)


def _startup_recovery() -> None:
    """Recovery de démarrage : aucun stream de CE processus n'est vivant avant
    le premier appel, donc tout message resté « streaming » est orphelin
    (redémarrage, processus tué) et redevient immédiatement rejouable.

    Le runtime documenté est mono-processus : cette interruption est sûre ;
    la recovery périodique utilise, elle, la fenêtre réelle + marge.
    """
    try:
        with session_scope() as db:
            count = recover_stale_streams(db, older_than_seconds=0)
        if count:
            log.warning("%s message(s) 'streaming' orphelin(s) marqué(s) 'interrupted' au démarrage", count)
    except Exception as exc:  # noqa: BLE001 - base pas encore migrée : non bloquant
        log.warning("recovery démarrage impossible: %s", exc)


STREAM_RECOVERY_INTERVAL_SECONDS = 60


def _stream_recovery_loop(stop_event: threading.Event) -> None:
    """Recovery périodique : un état `streaming` expiré ne survit pas durablement.

    Complète la recovery de démarrage et celle du worker : même sans redémarrage,
    les messages abandonnés (processus tué, conversation orpheline) repassent en
    `interrupted` au bout de la fenêtre RÉELLE maximale (+ marge).
    """
    while not stop_event.wait(STREAM_RECOVERY_INTERVAL_SECONDS):
        try:
            window = get_settings().stream_recovery_window_seconds
            with session_scope() as db:
                count = recover_stale_streams(db, older_than_seconds=window)
            if count:
                log.warning("%s message(s) 'streaming' abandonné(s) repris en 'interrupted'", count)
        except Exception as exc:  # noqa: BLE001 - jamais bloquant
            log.warning("recovery périodique impossible: %s", exc.__class__.__name__)


def _warmup() -> None:
    try:
        from .embeddings import get_embedding_service

        get_embedding_service().warmup()
        log.info("modèle d'embeddings prêt (backend=%s)", get_settings().embedding_backend)
    except Exception as exc:  # noqa: BLE001
        log.warning("préchauffage embeddings impossible: %s", exc)
    # Reclassement : chargement local protégé ; un échec n'est jamais fatal
    # (l'état réel est exposé par /api/status, les recherches répondent
    # `retrieval_unavailable` sans repli silencieux).
    try:
        from .reranking import get_reranker_service

        get_reranker_service().warmup()
        log.info("reclassement documentaire prêt (backend=%s)", get_settings().reranker_backend)
    except Exception as exc:  # noqa: BLE001
        log.warning("préchauffage reclassement impossible: %s", exc)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    log.info(
        "Wallia %s démarre (env=%s, embeddings=%s, provider_configuré=%s)",
        settings.app_version,
        settings.env,
        settings.embedding_backend,
        settings.provider_api_key() is not None,
    )
    _startup_recovery()
    recovery_stop = threading.Event()
    threading.Thread(
        target=_stream_recovery_loop, args=(recovery_stop,), daemon=True, name="wallia-stream-recovery"
    ).start()
    threading.Thread(target=_warmup, daemon=True, name="wallia-warmup").start()
    yield
    recovery_stop.set()
    log.info("Wallia s'arrête")


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title=APP_TITLE,
        version=__version__,
        docs_url=None,
        redoc_url=None,
        openapi_url=None if settings.is_production else "/api/openapi.json",
        lifespan=lifespan,
    )
    app.add_middleware(ApiRateLimitMiddleware, rate_per_minute=settings.api_rate_per_minute)
    app.add_middleware(SecurityHeadersMiddleware, production=settings.is_production)
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=settings.upload_body_max_bytes)

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(request: Request, exc: RequestValidationError):
        """Erreurs de validation stables, sans jamais réexposer la valeur soumise.

        Les erreurs Pydantic incluent `input` (valeur reçue) : inacceptable pour
        un champ secret (clé API). On ne renvoie que localisation/message/type.
        """
        errors = [
            {
                "loc": [str(part) for part in error.get("loc", [])],
                "msg": str(error.get("msg", "valeur invalide")),
                "type": str(error.get("type", "value_error")),
            }
            for error in exc.errors()
        ]
        return JSONResponse({"detail": errors}, status_code=422)

    @app.get("/healthz", include_in_schema=False)
    def healthz():
        return {"status": "ok", "version": __version__}

    app.include_router(auth.router)
    app.include_router(conversations.router)
    app.include_router(chat.router)
    app.include_router(attachments.router)
    app.include_router(documents.router)
    app.include_router(search.router)
    app.include_router(settings_api.router)
    app.include_router(status_router.router)
    app.include_router(web_api.router)
    app.include_router(internal.router)

    frontend = settings.frontend_dir
    if (frontend / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=frontend / "assets"), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    def spa(full_path: str):
        if full_path.startswith(("api/", "internal/", "healthz")):
            return JSONResponse({"detail": "introuvable"}, status_code=404)
        candidate = (frontend / full_path).resolve()
        if full_path and candidate.is_file() and candidate.is_relative_to(frontend.resolve()):
            return FileResponse(candidate)
        index = frontend / "index.html"
        if index.is_file():
            return FileResponse(index)
        return JSONResponse(
            {"detail": "frontend non construit — exécutez npm run build (frontend/)"},
            status_code=503,
        )

    return app


app = create_app()
