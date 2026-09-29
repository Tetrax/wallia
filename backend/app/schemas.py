"""Schémas Pydantic (requêtes) et validation des structures dérivées."""
from __future__ import annotations

import datetime as dt
import re
import uuid
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

VERSION_RE = re.compile(r"^[0-9][0-9A-Za-z._+-]{0,31}$")

CASE_STATE_LIST_KEYS = (
    "facts",
    "hypotheses",
    "proposed_checks",
    "performed_checks",
    "results",
    "missing_info",
)
CASE_STATE_TEXT_KEYS = ("product", "version", "symptom")
ITEM_STATUSES = ("proposed", "confirmed", "refuted", "missing")
ITEM_ORIGINS = ("user_message", "user_explicit", "assistant", "unknown")

DEFAULT_CASE_STATE: dict[str, Any] = {
    "product": None,
    "version": None,
    "symptom": None,
    "facts": [],
    "hypotheses": [],
    "proposed_checks": [],
    "performed_checks": [],
    "results": [],
    "missing_info": [],
}


def validate_version(value: str) -> str:
    value = value.strip()
    if not VERSION_RE.match(value):
        raise ValueError(f"version invalide: {value!r}")
    return value


class LoginIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=1, max_length=512)


class PasswordChangeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    current_password: str = Field(min_length=1, max_length=512)
    new_password: str = Field(min_length=12, max_length=512)


class ConversationIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str | None = Field(default=None, max_length=200)


class ConversationPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=200)


class CaseItemIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str | None = None
    text: str = Field(max_length=4000)
    status: Literal["proposed", "confirmed", "refuted", "missing"] = "proposed"
    origin: Literal["user_message", "user_explicit", "assistant", "unknown"] = "user_explicit"
    message_id: str | None = None


class CaseStatePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    product: str | None = Field(default=None, max_length=200)
    version: str | None = Field(default=None, max_length=64)
    symptom: str | None = Field(default=None, max_length=4000)
    facts: list[CaseItemIn] = Field(default_factory=list)
    hypotheses: list[CaseItemIn] = Field(default_factory=list)
    proposed_checks: list[CaseItemIn] = Field(default_factory=list)
    performed_checks: list[CaseItemIn] = Field(default_factory=list)
    results: list[CaseItemIn] = Field(default_factory=list)
    missing_info: list[CaseItemIn] = Field(default_factory=list)

    @field_validator("version")
    @classmethod
    def _check_version(cls, value: str | None) -> str | None:
        if value is None or value == "":
            return None
        return validate_version(value)


class ChatIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=20000)
    attachment_ids: list[uuid.UUID] = Field(default_factory=list, max_length=10)


class SearchIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(min_length=1, max_length=2000)
    product: str | None = Field(default=None, max_length=200)
    version: str | None = Field(default=None, max_length=64)
    scope: Literal["demo", "official", "all"] = "all"
    top_k: int = Field(default=6, ge=1, le=20)

    @field_validator("version")
    @classmethod
    def _check_version(cls, value: str | None) -> str | None:
        if value is None or value == "":
            return None
        return validate_version(value)


class SettingsPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider_endpoint: str | None = Field(default=None, max_length=300)
    provider_model: str | None = Field(default=None, max_length=100)
    provider_timeout_s: float | None = Field(default=None, ge=5, le=600)
    api_key: str | None = Field(default=None, max_length=400)
    clear_api_key: bool = False


class DocumentMetaIn(BaseModel):
    """Métadonnées d'import (multipart : champs texte du formulaire)."""

    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=300)
    origin: str = Field(default="demo", max_length=200)
    product: str | None = Field(default=None, max_length=200)
    language: str = Field(default="fr", max_length=10)
    document_date: dt.date | None = None
    demo: bool = True
    scope: Literal["demo", "official"] = "demo"


class DocumentPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str | None = Field(default=None, min_length=1, max_length=300)
    origin: str | None = Field(default=None, max_length=200)
    product: str | None = Field(default=None, max_length=200)
    language: str | None = Field(default=None, max_length=10)
    document_date: dt.date | None = None
    demo: bool | None = None
    scope: Literal["demo", "official"] | None = None
    versions: list[str] | None = None

    @field_validator("versions")
    @classmethod
    def _check_versions(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        cleaned = []
        for item in value:
            cleaned.append(validate_version(item))
        if len(set(cleaned)) != len(cleaned):
            raise ValueError("versions en doublon")
        return cleaned


def normalize_case_state(raw: dict[str, Any]) -> dict[str, Any]:
    """Normalise/borne un état de cas (structure serveur, jamais de promotion implicite)."""
    state = dict(DEFAULT_CASE_STATE)
    for key in CASE_STATE_TEXT_KEYS:
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            state[key] = value.strip()[:4000]
    for key in CASE_STATE_LIST_KEYS:
        items = raw.get(key) or []
        out = []
        for item in items:
            if not isinstance(item, dict):
                continue
            text = str(item.get("text", "")).strip()
            if not text:
                continue
            status = item.get("status") if item.get("status") in ITEM_STATUSES else "proposed"
            origin = item.get("origin") if item.get("origin") in ITEM_ORIGINS else "unknown"
            out.append(
                {
                    "id": str(item.get("id") or uuid.uuid4()),
                    "text": text[:4000],
                    "status": status,
                    "origin": origin,
                    "message_id": str(item["message_id"]) if item.get("message_id") else None,
                    "created_at": str(item.get("created_at") or dt.datetime.now(dt.timezone.utc).isoformat()),
                }
            )
        state[key] = out[:200]
    return state


def case_state_patch_to_raw(patch: CaseStatePatch) -> dict[str, Any]:
    raw: dict[str, Any] = {
        "product": patch.product,
        "version": patch.version,
        "symptom": patch.symptom,
    }
    for key in CASE_STATE_LIST_KEYS:
        raw[key] = [item.model_dump() for item in getattr(patch, key)]
    return raw
