"""Authentification, sessions, CSRF, rate-limit, autorisations."""
from __future__ import annotations

import dataclasses

import pytest
from fastapi import HTTPException

from app.config import get_settings
from tests.conftest import login


class _FakeRequest:
    def __init__(self, origin: str | None) -> None:
        self.headers = {"origin": origin} if origin else {}


def test_origin_strict_in_production():
    from app.security import check_origin

    settings = dataclasses.replace(
        get_settings(), env="production", allowed_origins=("https://wallia.valdev.me",)
    )
    check_origin(_FakeRequest("https://wallia.valdev.me"), settings)
    with pytest.raises(HTTPException):
        check_origin(_FakeRequest(None), settings)
    with pytest.raises(HTTPException):
        check_origin(_FakeRequest("https://attaquant.example"), settings)


def test_login_logout_me_flow(client, admin):
    csrf = login(client, admin)
    me = client.get("/api/auth/me")
    assert me.status_code == 200
    assert me.json()["user"]["email"] == admin["email"]
    assert me.json()["csrf_token"] == csrf

    logout = client.post("/api/auth/logout", headers={"X-CSRF-Token": csrf})
    assert logout.status_code == 200
    assert client.get("/api/auth/me").status_code == 401


def test_login_cookie_flags(client, admin, settings):
    login(client, admin)
    cookie = client.cookies.get("wallia_session")
    assert cookie
    set_cookie = None
    # Rejoue le login pour inspecter l'en-tête brut.
    response = client.post("/api/auth/login", json={"email": admin["email"], "password": admin["password"]})
    set_cookie = response.headers.get("set-cookie", "")
    assert "HttpOnly" in set_cookie
    assert "SameSite=lax" in set_cookie.lower().replace("samesite=none", "none") or "samesite=lax" in set_cookie.lower()
    if not settings.cookie_secure:
        assert "secure" not in set_cookie.lower()


def test_login_wrong_password(client, admin):
    response = client.post("/api/auth/login", json={"email": admin["email"], "password": "mauvais-mot-de-passe"})
    assert response.status_code == 401
    assert "password" not in response.text.lower()


def test_login_rate_limit_durable(client, admin):
    for _ in range(5):
        response = client.post("/api/auth/login", json={"email": admin["email"], "password": "faux"})
        assert response.status_code == 401
    blocked = client.post("/api/auth/login", json={"email": admin["email"], "password": admin["password"]})
    assert blocked.status_code == 429
    assert blocked.headers.get("Retry-After")


def test_csrf_and_origin_required_for_mutations(client, admin):
    csrf = login(client, admin)
    # Sans jeton CSRF
    response = client.post("/api/conversations", json={"title": "test"})
    assert response.status_code == 403
    # Mauvais jeton
    response = client.post("/api/conversations", json={"title": "test"}, headers={"X-CSRF-Token": "mauvais"})
    assert response.status_code == 403
    # Origine étrangère refusée même avec le bon jeton
    response = client.post(
        "/api/conversations",
        json={"title": "test"},
        headers={"X-CSRF-Token": csrf, "Origin": "https://attaquant.example"},
    )
    assert response.status_code == 403
    # Cas nominal
    response = client.post("/api/conversations", json={"title": "Cas légitime"}, headers={"X-CSRF-Token": csrf})
    assert response.status_code == 201


def test_unauthorized_endpoints(client):
    assert client.get("/api/conversations").status_code == 401
    assert client.get("/api/documents").status_code == 401
    assert client.get("/api/status").status_code == 401
    assert client.post("/api/search", json={"query": "x"}).status_code == 401
    assert client.get("/api/jobs").status_code == 401
    assert client.get("/api/settings").status_code == 401


def test_password_change_revokes_other_sessions(client, admin):
    csrf_a = login(client, admin)
    with __import__("httpx").Client(base_url=str(client.base_url), timeout=30.0) as second:
        login(second, admin)
        assert second.get("/api/auth/me").status_code == 200
        response = client.post(
            "/api/auth/password",
            json={"current_password": admin["password"], "new_password": "nouveau-mot-de-passe-456"},
            headers={"X-CSRF-Token": csrf_a},
        )
        assert response.status_code == 200
        assert response.json()["csrf_token"] != csrf_a
        assert second.get("/api/auth/me").status_code == 401
        assert client.get("/api/auth/me").status_code == 200
        # L'ancien mot de passe ne fonctionne plus, le nouveau oui.
        assert (
            client.post("/api/auth/login", json={"email": admin["email"], "password": admin["password"]}).status_code
            == 401
        )
        assert (
            client.post(
                "/api/auth/login", json={"email": admin["email"], "password": "nouveau-mot-de-passe-456"}
            ).status_code
            == 200
        )


def test_admin_only_endpoints(client, admin, other_user):
    csrf = login(client, admin)
    created = client.post("/api/conversations", json={}, headers={"X-CSRF-Token": csrf})
    conversation_id = created.json()["id"]

    with __import__("httpx").Client(base_url=str(client.base_url), timeout=30.0) as second:
        csrf2 = login(second, other_user)
        assert second.get("/api/documents").status_code == 403
        assert second.get("/api/conversations").status_code == 200
        # Isolation stricte : la conversation de l'admin est introuvable pour l'autre compte.
        assert second.get(f"/api/conversations/{conversation_id}").status_code == 404
        assert (
            second.patch(
                f"/api/conversations/{conversation_id}",
                json={"title": "vol"},
                headers={"X-CSRF-Token": csrf2},
            ).status_code
            == 404
        )


def test_worker_token_required_for_internal_endpoint(client, settings):
    assert client.post("/internal/embeddings", json={"kind": "query", "texts": ["x"]}).status_code == 401
    assert (
        client.post(
            "/internal/embeddings",
            json={"kind": "query", "texts": ["x"]},
            headers={"X-Internal-Token": "mauvais-jeton"},
        ).status_code
        == 401
    )
    token = settings.worker_token()
    response = client.post(
        "/internal/embeddings",
        json={"kind": "query", "texts": ["bonjour"]},
        headers={"X-Internal-Token": token},
    )
    assert response.status_code == 200
    payload = response.json()
    assert len(payload["vectors"]) == 1
    assert len(payload["vectors"][0]) == settings.embedding_dim


def test_no_secrets_in_login_and_status(client, admin):
    response = client.post("/api/auth/login", json={"email": admin["email"], "password": admin["password"]})
    body = response.text
    assert admin["password"] not in body
    assert "argon2" not in body.lower()
    csrf = response.json()["csrf_token"]
    status = client.get("/api/status")
    assert status.status_code == 200
    # Aucune clé de secret ne doit apparaître dans la charge utile (le mot
    # « password_changed_at » est légitime : on contrôle les clés exactes).
    forbidden = {"password", "password_hash", "api_key", "session_secret", "worker_token"}
    seen: set[str] = set()

    def walk(node) -> None:  # noqa: ANN001
        if isinstance(node, dict):
            for key, value in node.items():
                seen.add(str(key).lower())
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(status.json())
    assert not (forbidden & seen), forbidden & seen
    assert csrf
