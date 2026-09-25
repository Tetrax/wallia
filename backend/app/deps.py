"""Dépendances FastAPI : authentification de session, CSRF, helpers."""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import Settings, get_settings
from .db import get_db
from .models import User, UserSession
from .security import COOKIE_NAME, check_origin, client_ip, hmac_token, require_csrf_match, utcnow


@dataclass
class AuthContext:
    user: User
    session: UserSession
    settings: Settings
    db: Session
    ip: str


def settings_dep() -> Settings:
    return get_settings()


def _resolve_session(db: Session, settings: Settings, token: str | None) -> UserSession | None:
    if not token:
        return None
    token_hash = hmac_token(token, settings.session_secret())
    row = db.execute(select(UserSession).where(UserSession.token_hash == token_hash)).scalar_one_or_none()
    if row is None:
        return None
    now = utcnow()
    if row.revoked_at is not None or row.expires_at <= now:
        return None
    if row.last_seen_at < now - dt.timedelta(seconds=60):
        row.last_seen_at = now
        db.flush()
    return row


def get_auth_optional(request: Request, db: Session = Depends(get_db)) -> AuthContext | None:
    settings = get_settings()
    token = request.cookies.get(COOKIE_NAME)
    session = _resolve_session(db, settings, token)
    if session is None:
        return None
    user = db.get(User, session.user_id)
    if user is None:
        return None
    return AuthContext(user=user, session=session, settings=settings, db=db, ip=client_ip(request, settings))


def require_user(auth: AuthContext | None = Depends(get_auth_optional)) -> AuthContext:
    if auth is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="authentification requise")
    return auth


def require_admin(auth: AuthContext = Depends(require_user)) -> AuthContext:
    if not auth.user.is_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="accès administrateur requis")
    return auth


def csrf_guard(request: Request, auth: AuthContext = Depends(require_user)) -> AuthContext:
    """Contrôle Origin + CSRF pour toute mutation authentifiée."""
    check_origin(request, auth.settings)
    require_csrf_match(auth.session.csrf_token, request.headers.get("x-csrf-token"))
    return auth


def origin_only(request: Request, settings: Settings = Depends(settings_dep)) -> Settings:
    """Contrôle Origin sans session (login)."""
    check_origin(request, settings)
    return settings
