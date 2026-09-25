"""Paramètres applicatifs persistés (table settings) et résolution fournisseur."""
from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy.orm import Session

from .config import Settings
from .llm import Provider
from .models import Setting
from .security import utcnow

PROVIDER_KEY = "provider"
RETRIEVAL_KEY = "retrieval"
PROVIDER_LAST_TEST_KEY = "provider_last_test"
WORKER_HEARTBEAT_KEY = "worker_heartbeat"

DEFAULT_PROVIDER: dict[str, Any] = {
    "endpoint": None,  # None → valeur d'environnement
    "model": None,
    "timeout_s": None,
}

DEFAULT_RETRIEVAL: dict[str, Any] = {
    "min_cosine": None,  # None → valeur d'environnement
}


def get_row(db: Session, key: str) -> dict[str, Any] | None:
    row = db.get(Setting, key)
    return dict(row.value) if row is not None else None


def set_row(db: Session, key: str, value: dict[str, Any], *, commit: bool = True) -> None:
    row = db.get(Setting, key)
    if row is None:
        db.add(Setting(key=key, value=value))
    else:
        row.value = value
        row.updated_at = utcnow()
    if commit:
        db.commit()


def provider_config(db: Session, settings: Settings) -> dict[str, Any]:
    row = get_row(db, PROVIDER_KEY) or {}
    merged = dict(DEFAULT_PROVIDER)
    merged.update({k: v for k, v in row.items() if v is not None})
    config = {
        "endpoint": merged["endpoint"] or settings.provider_endpoint,
        "model": merged["model"] or settings.provider_model,
        "timeout_s": float(merged["timeout_s"]) if merged["timeout_s"] else settings.provider_total_timeout,
        "key_configured": settings.provider_api_key() is not None,
        "vision_enabled": settings.vision_enabled,
        "adjustable": True,
    }
    return config


def effective_provider(db: Session, settings: Settings) -> Provider:
    config = provider_config(db, settings)
    return Provider(
        settings,
        endpoint=config["endpoint"],
        model=config["model"],
        timeout_s=config["timeout_s"],
    )


def retrieval_config(db: Session, settings: Settings) -> dict[str, Any]:
    row = get_row(db, RETRIEVAL_KEY) or {}
    min_cosine = row.get("min_cosine")
    return {
        "min_cosine": float(min_cosine) if min_cosine is not None else settings.retrieval_min_cosine,
        "top_k": settings.retrieval_top_k,
    }


def last_provider_test(db: Session) -> dict[str, Any] | None:
    return get_row(db, PROVIDER_LAST_TEST_KEY)


def record_provider_test(db: Session, result: dict[str, Any]) -> None:
    payload = dict(result)
    payload["at"] = utcnow().isoformat()
    set_row(db, PROVIDER_LAST_TEST_KEY, payload)


def worker_heartbeat(db: Session) -> dict[str, Any] | None:
    return get_row(db, WORKER_HEARTBEAT_KEY)


def worker_alive(heartbeat: dict[str, Any] | None, max_age_seconds: int = 90) -> bool:
    if not heartbeat or not heartbeat.get("at"):
        return False
    try:
        at = dt.datetime.fromisoformat(str(heartbeat["at"]))
    except ValueError:
        return False
    return (utcnow() - at).total_seconds() <= max_age_seconds
