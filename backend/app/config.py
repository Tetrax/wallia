"""Configuration centralisée Wallia.

Toutes les valeurs proviennent de variables d'environnement ou de fichiers de
secrets dédiés (runtime/secrets/, montés en lecture dans les conteneurs).
Aucune valeur secrète n'est journalisée.
"""
from __future__ import annotations

import ipaddress
import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

APP_VERSION = os.environ.get("WALLIA_APP_VERSION", "0.1.0")
GIT_SHA = os.environ.get("WALLIA_GIT_SHA", "")


class ConfigError(RuntimeError):
    """Configuration invalide : le démarrage doit échouer explicitement."""


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    if value is None or value == "":
        return default
    return value


def _bool_env(name: str, default: bool) -> bool:
    raw = _env(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _int_env(name: str, default: int) -> int:
    raw = _env(name)
    return int(raw) if raw is not None else default


def _float_env(name: str, default: float) -> float:
    raw = _env(name)
    return float(raw) if raw is not None else default


def _csv_env(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    raw = _env(name)
    if raw is None:
        return default
    return tuple(part.strip() for part in raw.split(",") if part.strip())


def _read_secret_file(path: Path) -> str | None:
    try:
        content = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    value = content.strip()
    return value or None


@dataclass(frozen=True)
class Settings:
    env: str
    app_version: str
    git_sha: str
    data_dir: Path
    secrets_dir: Path
    frontend_dir: Path
    model_dir: Path
    docling_models_dir: Path

    db_host: str
    db_port: str
    db_name: str
    db_user: str
    db_password_file: Path
    _db_url_override: str | None

    embedding_backend: str
    embedding_model: str
    embedding_revision: str
    embedding_dim: int
    embedding_batch_size: int

    allowed_origins: tuple[str, ...]
    trusted_proxy_cidrs: tuple[str, ...]
    trusted_proxy_networks: tuple[ipaddress._BaseNetwork, ...]
    cookie_secure: bool
    session_ttl_seconds: int
    login_max_failures_per_ip: int
    login_max_failures_per_email: int
    login_window_seconds: int
    api_rate_per_minute: int
    login_rate_per_minute: int

    provider_endpoint: str
    provider_model: str
    provider_allowed_domains: tuple[str, ...]
    provider_connect_timeout: float
    provider_read_timeout: float
    provider_total_timeout: float
    provider_max_tokens: int
    vision_enabled: bool
    web_enabled: bool

    retrieval_top_k: int
    retrieval_candidates: int
    retrieval_rrf_k: int
    retrieval_min_cosine: float

    chat_max_seconds: int
    chat_max_history_messages: int
    chat_max_chars_per_message: int
    chat_max_context_chars: int

    upload_pdf_max_bytes: int
    upload_pdf_max_pages: int
    upload_text_max_bytes: int
    upload_image_max_bytes: int
    upload_image_max_pixels: int

    worker_poll_seconds: float
    worker_lease_seconds: int
    worker_job_timeout_seconds: int
    worker_docling_timeout_seconds: int
    worker_manual_retry_max: int

    @property
    def is_production(self) -> bool:
        return self.env == "production"

    @property
    def db_url(self) -> str:
        """URL base construite à la demande (le mot de passe n'est lu qu'à l'usage)."""
        if self._db_url_override:
            return self._db_url_override
        password = _read_secret_file(self.db_password_file)
        if not password:
            raise ConfigError(f"mot de passe base introuvable ({self.db_password_file})")
        from urllib.parse import quote

        return (
            f"postgresql+psycopg://{self.db_user}:{quote(password, safe='')}"
            f"@{self.db_host}:{self.db_port}/{self.db_name}"
        )

    @property
    def db_endpoint(self) -> str:
        return f"{self.db_host}:{self.db_port}/{self.db_name}"

    @property
    def uploads_dir(self) -> Path:
        return self.data_dir / "uploads"

    @property
    def documents_dir(self) -> Path:
        return self.data_dir / "documents"

    @property
    def quarantine_dir(self) -> Path:
        return self.data_dir / "quarantine"

    def secret_path(self, name: str) -> Path:
        return self.secrets_dir / name

    def read_secret(self, name: str) -> str | None:
        return _read_secret_file(self.secret_path(name))

    def worker_token(self) -> str:
        token = self.read_secret("worker_token")
        if not token:
            raise ConfigError("secret worker_token manquant (runtime/secrets/worker_token)")
        return token

    def session_secret(self) -> str:
        secret = self.read_secret("session_secret")
        if not secret:
            raise ConfigError("secret session_secret manquant (runtime/secrets/session_secret)")
        return secret

    def provider_api_key(self) -> str | None:
        key = self.read_secret("provider_api_key")
        if key and key.startswith("#"):
            return None
        return key


def _build_db_parts(secrets_dir: Path) -> dict:
    return {
        "db_host": _env("WALLIA_DB_HOST", "db") or "db",
        "db_port": _env("WALLIA_DB_PORT", "5432") or "5432",
        "db_name": _env("WALLIA_DB_NAME", "wallia") or "wallia",
        "db_user": _env("WALLIA_DB_USER", "wallia") or "wallia",
        "db_password_file": Path(_env("WALLIA_DB_PASSWORD_FILE", str(secrets_dir / "db_password")) or str(secrets_dir / "db_password")),
        "_db_url_override": _env("WALLIA_DB_URL"),
    }


def load_settings() -> Settings:
    env = _env("WALLIA_ENV", "local") or "local"
    secrets_dir = Path(_env("WALLIA_SECRETS_DIR", "/secrets") or "/secrets")
    is_prod = env == "production"

    default_origins = (
        ("https://wallia.valdev.me",)
        if is_prod
        else (
            "http://localhost:13745",
            "http://127.0.0.1:13745",
        )
    )
    origins = _csv_env("WALLIA_ALLOWED_ORIGINS", default_origins)

    trusted = _csv_env("WALLIA_TRUSTED_PROXY_CIDRS", ("172.31.245.0/24", "127.0.0.1/32", "::1/128"))
    networks: list[ipaddress._BaseNetwork] = []
    for cidr in trusted:
        try:
            networks.append(ipaddress.ip_network(cidr, strict=False))
        except ValueError as exc:  # pragma: no cover - config invalide
            raise ConfigError(f"CIDR de proxy de confiance invalide: {cidr}") from exc

    embedding_backend = _env("WALLIA_EMBEDDING_BACKEND", "e5") or "e5"
    if is_prod and embedding_backend != "e5":
        raise ConfigError("WALLIA_EMBEDDING_BACKEND=fixture interdit en production")

    settings = Settings(
        env=env,
        app_version=APP_VERSION,
        git_sha=GIT_SHA,
        data_dir=Path(_env("WALLIA_DATA_DIR", "/data") or "/data"),
        secrets_dir=secrets_dir,
        frontend_dir=Path(_env("WALLIA_FRONTEND_DIR", "/app/static") or "/app/static"),
        model_dir=Path(_env("WALLIA_MODEL_DIR", "/opt/models/e5-small") or "/opt/models/e5-small"),
        docling_models_dir=Path(_env("WALLIA_DOCLING_MODELS", "/opt/docling-models") or "/opt/docling-models"),
        **_build_db_parts(secrets_dir),
        embedding_backend=embedding_backend,
        embedding_model=_env("WALLIA_EMBEDDING_MODEL", "intfloat/multilingual-e5-small") or "",
        embedding_revision=_env(
            "WALLIA_EMBEDDING_REVISION", "614241f622f53c4eeff9890bdc4f31cfecc418b3"
        )
        or "",
        embedding_dim=_int_env("WALLIA_EMBEDDING_DIM", 384),
        embedding_batch_size=_int_env("WALLIA_EMBEDDING_BATCH_SIZE", 16),
        allowed_origins=origins,
        trusted_proxy_cidrs=trusted,
        trusted_proxy_networks=tuple(networks),
        cookie_secure=_bool_env("WALLIA_COOKIE_SECURE", is_prod),
        session_ttl_seconds=_int_env("WALLIA_SESSION_TTL_SECONDS", 7 * 24 * 3600),
        login_max_failures_per_ip=_int_env("WALLIA_LOGIN_MAX_FAILURES_PER_IP", 5),
        login_max_failures_per_email=_int_env("WALLIA_LOGIN_MAX_FAILURES_PER_EMAIL", 10),
        login_window_seconds=_int_env("WALLIA_LOGIN_WINDOW_SECONDS", 900),
        api_rate_per_minute=_int_env("WALLIA_API_RATE_PER_MINUTE", 600),
        login_rate_per_minute=_int_env("WALLIA_LOGIN_RATE_PER_MINUTE", 20),
        provider_endpoint=_env("WALLIA_PROVIDER_ENDPOINT", "https://api.deepseek.com/v1") or "",
        provider_model=_env("WALLIA_PROVIDER_MODEL", "deepseek-flash") or "",
        provider_allowed_domains=_csv_env("WALLIA_PROVIDER_ALLOWED_DOMAINS", ("api.deepseek.com",)),
        provider_connect_timeout=_float_env("WALLIA_PROVIDER_CONNECT_TIMEOUT", 10.0),
        provider_read_timeout=_float_env("WALLIA_PROVIDER_READ_TIMEOUT", 60.0),
        provider_total_timeout=_float_env("WALLIA_PROVIDER_TOTAL_TIMEOUT", 240.0),
        provider_max_tokens=_int_env("WALLIA_PROVIDER_MAX_TOKENS", 1400),
        vision_enabled=_bool_env("WALLIA_VISION_ENABLED", False),
        web_enabled=_bool_env("WALLIA_WEB_ENABLED", False),
        retrieval_top_k=_int_env("WALLIA_RETRIEVAL_TOP_K", 6),
        retrieval_candidates=_int_env("WALLIA_RETRIEVAL_CANDIDATES", 30),
        retrieval_rrf_k=_int_env("WALLIA_RETRIEVAL_RRF_K", 60),
        retrieval_min_cosine=_float_env("WALLIA_RETRIEVAL_MIN_COSINE", 0.84),
        chat_max_seconds=_int_env("WALLIA_CHAT_MAX_SECONDS", 240),
        chat_max_history_messages=_int_env("WALLIA_CHAT_MAX_HISTORY_MESSAGES", 20),
        chat_max_chars_per_message=_int_env("WALLIA_CHAT_MAX_CHARS_PER_MESSAGE", 6000),
        chat_max_context_chars=_int_env("WALLIA_CHAT_MAX_CONTEXT_CHARS", 24000),
        upload_pdf_max_bytes=_int_env("WALLIA_UPLOAD_PDF_MAX_BYTES", 20 * 1024 * 1024),
        upload_pdf_max_pages=_int_env("WALLIA_UPLOAD_PDF_MAX_PAGES", 100),
        upload_text_max_bytes=_int_env("WALLIA_UPLOAD_TEXT_MAX_BYTES", 1024 * 1024),
        upload_image_max_bytes=_int_env("WALLIA_UPLOAD_IMAGE_MAX_BYTES", 10 * 1024 * 1024),
        upload_image_max_pixels=_int_env("WALLIA_UPLOAD_IMAGE_MAX_PIXELS", 40_000_000),
        worker_poll_seconds=_float_env("WALLIA_WORKER_POLL_SECONDS", 2.0),
        worker_lease_seconds=_int_env("WALLIA_WORKER_LEASE_SECONDS", 300),
        worker_job_timeout_seconds=_int_env("WALLIA_WORKER_JOB_TIMEOUT_SECONDS", 1800),
        worker_docling_timeout_seconds=_int_env("WALLIA_WORKER_DOCLING_TIMEOUT_SECONDS", 1200),
        worker_manual_retry_max=_int_env("WALLIA_WORKER_MANUAL_RETRY_MAX", 3),
    )
    if settings.is_production and not settings.cookie_secure:
        raise ConfigError("WALLIA_COOKIE_SECURE=0 interdit en production")
    return settings


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return load_settings()
