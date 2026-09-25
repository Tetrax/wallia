"""Primitives de sécurité : mots de passe Argon2id, sessions opaques, CSRF,
rate-limit, IP client de confiance, contrôle d'origine."""
from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import ipaddress
import secrets
import threading
import time
from typing import Any

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from fastapi import HTTPException, Request, status

from .config import Settings

COOKIE_NAME = "wallia_session"

_hasher = PasswordHasher(
    time_cost=3,
    memory_cost=65536,
    parallelism=2,
    hash_len=32,
    salt_len=16,
)


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def needs_rehash(password_hash: str) -> bool:
    try:
        return _hasher.check_needs_rehash(password_hash)
    except InvalidHashError:
        return True


def new_token(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


def hmac_token(token: str, secret: str) -> str:
    return hmac.new(secret.encode("utf-8"), token.encode("utf-8"), hashlib.sha256).hexdigest()


def constant_time_eq(left: str, right: str) -> bool:
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


class TokenBucket:
    """Limiteur en mémoire (fenêtre glissante simple par compteur de jetons).

    Volontairement simple : l'API tourne en un seul processus ; le rate-limit
    durable des connexions vit en base (table login_attempts).
    """

    def __init__(self, rate_per_minute: int) -> None:
        self.rate = max(1, rate_per_minute)
        self._lock = threading.Lock()
        self._state: dict[str, tuple[float, float]] = {}

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            tokens, last = self._state.get(key, (float(self.rate), now))
            elapsed = max(0.0, now - last)
            tokens = min(float(self.rate), tokens + elapsed * (self.rate / 60.0))
            if tokens < 1.0:
                self._state[key] = (tokens, now)
                return False
            self._state[key] = (tokens - 1.0, now)
            if len(self._state) > 20000:  # borne mémoire
                self._state = {k: v for k, v in self._state.items() if now - v[1] < 600}
            return True


def client_ip(request: Request, settings: Settings) -> str:
    """IP client fiable.

    N'accepte X-Forwarded-For que si la connexion provient d'un proxy de confiance
    (réseau dédié). Nginx écrase cet en-tête ; on ne lit que la première valeur.
    """
    peer = request.client.host if request.client else "unknown"
    try:
        peer_ip = ipaddress.ip_address(peer)
    except ValueError:
        return peer
    trusted = any(peer_ip in net for net in settings.trusted_proxy_networks)
    if trusted:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            candidate = forwarded.split(",")[0].strip()
            try:
                ipaddress.ip_address(candidate)
                return candidate
            except ValueError:
                return peer
    return peer


def check_origin(request: Request, settings: Settings) -> None:
    """Contrôle d'origine strict sur les mutations.

    En production, l'en-tête Origin est exigé et doit être dans la liste blanche.
    Hors production, l'absence d'Origin est tolérée (outils locaux), mais une
    origine présente et inconnue est toujours refusée.
    """
    origin = request.headers.get("origin")
    if origin is None:
        if settings.is_production:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="origine requise")
        return
    if origin not in settings.allowed_origins:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="origine refusée")


def require_csrf_match(session_csrf: str, header_value: str | None) -> None:
    if not header_value or not constant_time_eq(session_csrf, header_value):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="jeton CSRF invalide")


def durable_login_blocked(db, settings: Settings, ip: str, email: str) -> int:
    """Retourne le nombre de secondes de blocage restantes, 0 si autorisé."""
    from sqlalchemy import text

    window = settings.login_window_seconds
    row = db.execute(
        text(
            """
            SELECT
              (SELECT count(*) FROM login_attempts
                WHERE ip = :ip AND success = false
                  AND created_at > now() - make_interval(secs => :window)) AS failures_ip,
              (SELECT count(*) FROM login_attempts
                WHERE lower(email) = lower(:email) AND success = false
                  AND created_at > now() - make_interval(secs => :window)) AS failures_email,
              (SELECT max(created_at) FROM login_attempts
                WHERE ip = :ip AND success = false
                  AND created_at > now() - make_interval(secs => :window)) AS last_fail
            """
        ),
        {"ip": ip, "email": email, "window": window},
    ).fetchone()
    failures_ip = int(row[0] or 0)
    failures_email = int(row[1] or 0)
    if failures_ip < settings.login_max_failures_per_ip and failures_email < settings.login_max_failures_per_email:
        return 0
    last_fail = row[2]
    if last_fail is None:
        return window
    import datetime as _dt

    if isinstance(last_fail, str):  # pragma: no cover - défensif
        return window
    elapsed = (_dt.datetime.now(_dt.timezone.utc) - last_fail).total_seconds()
    return max(1, int(window - elapsed))


def record_login_attempt(db, ip: str, email: str | None, success: bool) -> None:
    from .models import LoginAttempt

    db.add(LoginAttempt(ip=ip, email=email, success=success))
    db.flush()


def sanitize_text(value: Any, max_len: int = 1000) -> str:
    """Réduit un texte utilisateur à une forme sûre et bornée (une ligne)."""
    text = str(value)
    text = "".join(ch for ch in text if ch == "\n" or ch == "\t" or ch.isprintable())
    text = text.replace("\x00", "")
    return text.strip()[:max_len]
