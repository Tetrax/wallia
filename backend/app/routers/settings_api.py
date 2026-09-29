"""Réglages applicatifs exposés à l'administration (aucun secret en sortie)."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, status

from ..app_settings import (
    PROVIDER_KEY,
    effective_provider,
    get_row,
    last_provider_test,
    provider_config,
    record_provider_test,
    set_row,
)
from ..deps import AuthContext, csrf_guard, require_admin
from ..llm import validate_endpoint
from ..schemas import SettingsPatch

router = APIRouter(prefix="/api/settings", tags=["settings"])


def _public_settings(auth: AuthContext) -> dict:
    config = provider_config(auth.db, auth.settings)
    return {
        "provider": {
            "endpoint": config["endpoint"],
            "model": config["model"],
            "timeout_s": config["timeout_s"],
            "key_configured": config["key_configured"],
            "vision_enabled": config["vision_enabled"],
            "allowed_domains": list(auth.settings.provider_allowed_domains),
        },
        # Le seuil de reclassement est GELÉ (code, docs/reranker-probe.md) :
        # aucun réglage opérateur n'est exposé ici.
        "retrieval": {
            "top_k": auth.settings.retrieval_top_k,
        },
        "last_provider_test": last_provider_test(auth.db),
        "env": auth.settings.env,
    }


@router.get("")
def get_settings_api(auth: AuthContext = Depends(require_admin)):
    return _public_settings(auth)


def _write_provider_key(path: Path, key: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".provider_key.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(key.strip() + "\n")
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


@router.put("")
def put_settings(body: SettingsPatch, auth: AuthContext = Depends(csrf_guard)):
    if not auth.user.is_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="accès administrateur requis")
    db = auth.db
    current = get_row(db, PROVIDER_KEY) or {}
    updated = dict(current)

    if body.provider_endpoint is not None:
        endpoint = body.provider_endpoint.strip()
        ok, reason = validate_endpoint(endpoint, auth.settings)
        if not ok:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=f"endpoint refusé: {reason}")
        updated["endpoint"] = endpoint
    if body.provider_model is not None:
        model = body.provider_model.strip()
        if not model or len(model) > 100 or any(ch.isspace() for ch in model):
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="modèle invalide")
        updated["model"] = model
    if body.provider_timeout_s is not None:
        updated["timeout_s"] = float(body.provider_timeout_s)

    if body.api_key:
        _write_provider_key(auth.settings.secret_path("provider_api_key"), body.api_key)
    if body.clear_api_key:
        try:
            auth.settings.secret_path("provider_api_key").unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"suppression impossible: {exc}")

    set_row(db, PROVIDER_KEY, updated, commit=False)
    db.commit()
    return _public_settings(auth)


@router.post("/test")
def test_provider(auth: AuthContext = Depends(csrf_guard)):
    if not auth.user.is_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="accès administrateur requis")
    provider = effective_provider(auth.db, auth.settings)
    result = provider.test_connection()
    record_provider_test(auth.db, result)
    return result
