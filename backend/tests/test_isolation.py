"""Isolation des tests : répertoires DÉDIÉS, jamais /secrets ni /data réels."""
from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest

from tests import conftest as wallia_conftest
from tests.conftest import _TEST_ROOT, login


def test_settings_use_dedicated_test_directories(settings):
    secrets_dir = Path(settings.secrets_dir).resolve()
    data_dir = Path(settings.data_dir).resolve()
    for path in (secrets_dir, data_dir):
        assert str(path) != "/secrets"
        assert str(path) != "/data"
        assert not str(path).startswith("/secrets")
        assert not str(path).startswith("/data")
        assert path.is_relative_to(_TEST_ROOT)
    # La clé fournisseur des tests vit dans le répertoire de secrets dédié.
    key = Path(settings.secret_path("provider_api_key")).resolve()
    assert key.parent == secrets_dir
    assert str(key) != "/secrets/provider_api_key"
    # Les variables d'environnement visibles par l'application sont dédiées.
    assert os.environ["WALLIA_SECRETS_DIR"] == str(settings.secrets_dir)
    assert os.environ["WALLIA_DATA_DIR"] == str(settings.data_dir)


def test_uploaded_files_land_in_test_data_dir(client, admin, settings):
    csrf = login(client, admin)
    conversation_id = client.post("/api/conversations", json={}, headers={"X-CSRF-Token": csrf}).json()["id"]
    upload = client.post(
        f"/api/conversations/{conversation_id}/attachments",
        files={"file": ("note.txt", b"contenu de test", "text/plain")},
        headers={"X-CSRF-Token": csrf},
    )
    assert upload.status_code == 201
    attachment_id = upload.json()["id"]
    from app.db import session_scope
    from app.models import Attachment

    with session_scope() as db:
        stored = db.get(Attachment, uuid.UUID(attachment_id)).stored_relpath
    path = Path(settings.uploads_dir) / stored
    assert path.is_file()
    assert path.resolve().is_relative_to(Path(settings.data_dir).resolve())


def test_assert_isolated_refuses_data_and_secrets_descendants(monkeypatch):
    """Revue lot3 (#3) : les DESCENDANTS de /data et /secrets sont refusés,
    même quand WALLIA_TEST_TMP est configuré à l'intérieur d'eux."""
    # Contrôle positif d'abord : sous la racine dédiée normale, l'assertion passe.
    wallia_conftest._assert_isolated(_TEST_ROOT / "secrets")
    with pytest.raises(RuntimeError):
        wallia_conftest._assert_isolated(Path("/data/sous-dossier-tests"))
    with pytest.raises(RuntimeError):
        wallia_conftest._assert_isolated(Path("/secrets/sous-dossier-tests"))
    # TEST_TMP configuré DANS /data : les chemins restent refusés.
    monkeypatch.setattr(wallia_conftest, "_TEST_ROOT", Path("/data/tmp-tests").resolve())
    with pytest.raises(RuntimeError):
        wallia_conftest._assert_isolated(Path("/data/tmp-tests/secrets"))
    with pytest.raises(RuntimeError):
        wallia_conftest._assert_isolated(Path("/data/tmp-tests/data"))
    with pytest.raises(RuntimeError):
        wallia_conftest._assert_isolated(Path("/tmp/wallia-tests/secrets"))


def test_delivery_container_guard_is_not_bypassed_by_isolated_flag(monkeypatch, tmp_path):
    """Revue lot3 (#3) : WALLIA_TEST_RUNNER=isolated ne court-circuite JAMAIS
    la détection production / secrets réels."""
    monkeypatch.setenv("WALLIA_TEST_RUNNER", "isolated")
    monkeypatch.setenv("WALLIA_ENV", "production")
    with pytest.raises(RuntimeError):
        wallia_conftest._refuse_delivery_container()

    monkeypatch.setenv("WALLIA_ENV", "test")
    marker = tmp_path / "db_password"
    marker.write_text("x", encoding="utf-8")
    monkeypatch.setattr(wallia_conftest, "_DELIVERY_MARKERS", (str(marker),))
    with pytest.raises(RuntimeError):
        wallia_conftest._refuse_delivery_container()

    # Sans production ni marqueur, le harnais isolé est accepté (aucune
    # dérogation au-delà).
    monkeypatch.setattr(wallia_conftest, "_DELIVERY_MARKERS", ())
    wallia_conftest._refuse_delivery_container()
