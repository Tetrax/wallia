"""Authentification : login, logout, me, changement de mot de passe."""
from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from ..config import Settings
from ..db import get_db
from ..deps import AuthContext, csrf_guard, origin_only, require_user
from ..models import User, UserSession
from ..schemas import LoginIn, PasswordChangeIn
from ..security import (
    COOKIE_NAME,
    TokenBucket,
    client_ip,
    durable_login_blocked,
    hash_password,
    hmac_token,
    needs_rehash,
    new_token,
    record_login_attempt,
    utcnow,
    verify_password,
)
from ..serializers import user_out

router = APIRouter(prefix="/api/auth", tags=["auth"])

_login_bucket: TokenBucket | None = None


def login_bucket(settings: Settings) -> TokenBucket:
    global _login_bucket
    if _login_bucket is None:
        _login_bucket = TokenBucket(settings.login_rate_per_minute)
    return _login_bucket


def reset_login_bucket() -> None:
    global _login_bucket
    _login_bucket = None


def _create_session(db: Session, user: User, request: Request, settings: Settings, ip: str) -> tuple[str, UserSession]:
    token = new_token(32)
    session = UserSession(
        user_id=user.id,
        token_hash=hmac_token(token, settings.session_secret()),
        csrf_token=new_token(24),
        user_agent=(request.headers.get("user-agent") or "")[:400],
        ip=ip,
        expires_at=utcnow() + dt.timedelta(seconds=settings.session_ttl_seconds),
    )
    db.add(session)
    db.flush()
    return token, session


def _set_cookie(response: Response, token: str, settings: Settings) -> None:
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=settings.session_ttl_seconds,
        httponly=True,
        samesite="lax",
        secure=settings.cookie_secure,
        path="/",
    )


def _clear_cookie(response: Response) -> None:
    response.delete_cookie(COOKIE_NAME, path="/")


@router.post("/login")
def login(body: LoginIn, request: Request, settings: Settings = Depends(origin_only), db: Session = Depends(get_db)):
    ip = client_ip(request, settings)
    if not login_bucket(settings).allow(f"login:{ip}"):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="trop de tentatives, réessayez plus tard",
            headers={"Retry-After": "60"},
        )
    blocked_seconds = durable_login_blocked(db, settings, ip, body.email)
    if blocked_seconds > 0:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="trop de tentatives échouées, réessayez plus tard",
            headers={"Retry-After": str(blocked_seconds)},
        )

    email = body.email.strip().lower()
    user = db.execute(select(User).where(func.lower(User.email) == email)).scalar_one_or_none()
    ok = bool(user) and verify_password(user.password_hash, body.password)
    record_login_attempt(db, ip, email, ok)
    if not ok:
        db.commit()
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="identifiants invalides")

    assert user is not None
    if needs_rehash(user.password_hash):
        user.password_hash = hash_password(body.password)
        user.updated_at = utcnow()
    token, session = _create_session(db, user, request, settings, ip)
    db.commit()
    response = JSONResponse({"user": user_out(user), "csrf_token": session.csrf_token})
    _set_cookie(response, token, settings)
    return response


@router.post("/logout")
def logout(auth: AuthContext = Depends(csrf_guard)):
    auth.session.revoked_at = utcnow()
    auth.db.commit()
    response = JSONResponse({"ok": True})
    _clear_cookie(response)
    return response


@router.get("/me")
def me(auth: AuthContext = Depends(require_user)):
    return {"user": user_out(auth.user), "csrf_token": auth.session.csrf_token}


@router.post("/password")
def change_password(body: PasswordChangeIn, auth: AuthContext = Depends(csrf_guard)):
    if not verify_password(auth.user.password_hash, body.current_password):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="mot de passe actuel invalide")
    auth.user.password_hash = hash_password(body.new_password)
    auth.user.password_changed_at = utcnow()
    auth.user.updated_at = utcnow()
    # Révocation de toutes les autres sessions ; la session courante reste valide
    # mais son jeton CSRF est renouvelé.
    auth.db.execute(
        text(
            "UPDATE sessions SET revoked_at = now() WHERE user_id = :uid AND id <> :sid AND revoked_at IS NULL"
        ),
        {"uid": str(auth.user.id), "sid": str(auth.session.id)},
    )
    auth.session.csrf_token = new_token(24)
    auth.db.commit()
    return {"ok": True, "csrf_token": auth.session.csrf_token}
