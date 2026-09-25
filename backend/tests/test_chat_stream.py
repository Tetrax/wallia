"""Streaming SSE réel contre un faux fournisseur OpenAI-compatible.

Couvre : mode démonstration honnête, streaming nominal, arrêt utilisateur,
déconnexion cliente, erreur fournisseur, relance, concurrence, retour au mode
démonstration après effacement de clé.
"""
from __future__ import annotations

import gc
import json
import socket
import threading
import time

import httpx
import pytest

from tests.conftest import login


@pytest.fixture(scope="session")
def fake_upstream():
    import uvicorn

    from tests.fake_upstream import app as fake_app

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    config = uvicorn.Config(fake_app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True, name="wallia-fake-upstream")
    thread.start()
    deadline = time.time() + 20
    while time.time() < deadline and not getattr(server, "started", False):
        time.sleep(0.05)
    if not getattr(server, "started", False):
        raise RuntimeError("faux fournisseur non démarré")
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=10)


@pytest.fixture()
def upstream(fake_upstream):
    with httpx.Client(base_url=fake_upstream, timeout=10.0) as client:
        client.post("/mode", json={"mode": "normal"})
        yield client


def configure_provider(client, csrf, base: str) -> None:
    response = client.put(
        "/api/settings",
        json={"provider_endpoint": f"{base}/v1", "provider_model": "fake-model", "api_key": "cle-de-test-locale"},
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 200, response.text
    assert response.json()["provider"]["key_configured"] is True
    assert "cle-de-test-locale" not in response.text


def new_conversation(client, csrf) -> str:
    return client.post("/api/conversations", json={"title": "Cas streaming"}, headers={"X-CSRF-Token": csrf}).json()["id"]


def read_frames(response, *, limit: int = 400) -> list[tuple[str, dict]]:
    frames: list[tuple[str, dict]] = []
    event: str | None = None
    for line in response.iter_lines():
        if not line:
            continue
        if line.startswith("event: "):
            event = line[len("event: ") :]
        elif line.startswith("data: "):
            frames.append((event or "message", json.loads(line[len("data: ") :])))
            event = None
            if len(frames) >= limit:
                break
    return frames


def wait_status(client, conversation_id: str, status: str, timeout: float = 15.0) -> str:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        messages = client.get(f"/api/conversations/{conversation_id}/messages").json()["messages"]
        if messages:
            last = messages[-1]
            if last["status"] == status:
                return status
        time.sleep(0.2)
    raise AssertionError(f"statut {status} non atteint (dernier: {last})")


def test_demo_mode_message_when_provider_absent(client, admin):
    csrf = login(client, admin)
    conversation_id = new_conversation(client, csrf)
    with client.stream(
        "POST", f"/api/conversations/{conversation_id}/chat", json={"text": "Bonjour"}, headers={"X-CSRF-Token": csrf}
    ) as response:
        assert response.status_code == 200
        frames = read_frames(response)
    kinds = [kind for kind, _ in frames]
    assert kinds[0] == "meta"
    assert frames[0][1]["demo"] is True
    assert "sources" in kinds
    assert any(kind == "status" and data["state"] == "demo" for kind, data in frames)
    assert kinds[-1] == "done"
    assert frames[-1][1]["status"] == "complete"
    content = "".join(data["text"] for kind, data in frames if kind == "delta")
    assert "Mode démonstration" in content

    messages = client.get(f"/api/conversations/{conversation_id}/messages").json()["messages"]
    assert [m["role"] for m in messages] == ["user", "assistant"]
    assert messages[1]["demo"] is True
    assert messages[1]["status"] == "complete"
    assert messages[1]["error"] is None


def test_streaming_nominal_with_fake_provider(client, admin, upstream):
    csrf = login(client, admin)
    configure_provider(client, csrf, str(upstream.base_url))
    conversation_id = new_conversation(client, csrf)
    with client.stream(
        "POST",
        f"/api/conversations/{conversation_id}/chat",
        json={"text": "Quel est le rôle du voyant ambre ?"},
        headers={"X-CSRF-Token": csrf},
    ) as response:
        assert response.status_code == 200
        frames = read_frames(response)
    deltas = [data["text"] for kind, data in frames if kind == "delta"]
    assert len(deltas) > 2
    assert frames[-1][0] == "done" and frames[-1][1]["status"] == "complete"
    messages = client.get(f"/api/conversations/{conversation_id}/messages").json()["messages"]
    assistant = messages[-1]
    assert assistant["model"] == "fake-model"
    assert assistant["demo"] is False
    assert "[1]" in assistant["content"]
    assert upstream.get("/state").json()["requests"] >= 1


def test_user_stop_cancels_generation(client, admin, upstream):
    csrf = login(client, admin)
    configure_provider(client, csrf, str(upstream.base_url))
    upstream.post("/mode", json={"mode": "slow"})
    conversation_id = new_conversation(client, csrf)
    frames: list[tuple[str, dict]] = []
    lock = threading.Lock()
    first_delta = threading.Event()

    def stopper() -> None:
        # On n'interrompt qu'après le début réel du streaming.
        first_delta.wait(timeout=30)
        with lock:
            message_id = next((data["message_id"] for kind, data in frames if kind == "meta"), None)
        if not message_id:
            return
        with httpx.Client(base_url=str(client.base_url), timeout=30.0) as stopper_client:
            stopper_client.cookies.update(client.cookies)
            stopper_client.post(f"/api/messages/{message_id}/stop", headers={"X-CSRF-Token": csrf})

    stop_thread = threading.Thread(target=stopper, daemon=True)
    stop_thread.start()
    with client.stream(
        "POST", f"/api/conversations/{conversation_id}/chat", json={"text": "Question longue"}, headers={"X-CSRF-Token": csrf}
    ) as response:
        event = None
        for line in response.iter_lines():
            if line.startswith("event: "):
                event = line[len("event: ") :]
            elif line.startswith("data: "):
                data = json.loads(line[len("data: ") :])
                with lock:
                    frames.append((event or "message", data))
                if event == "delta":
                    first_delta.set()
                if event == "done":
                    break
    stop_thread.join(timeout=10)
    wait_status(client, conversation_id, "cancelled", timeout=20)
    message_id = next((data["message_id"] for kind, data in frames if kind == "meta"), None)
    assert message_id
    messages = client.get(f"/api/conversations/{conversation_id}/messages").json()["messages"]
    assert messages[-1]["status"] == "cancelled"
    done_frames = [data for kind, data in frames if kind == "done"]
    if done_frames:
        assert done_frames[-1]["status"] == "cancelled"
    upstream.post("/mode", json={"mode": "normal"})


def test_client_disconnect_marks_message_cancelled(client, admin, upstream):
    csrf = login(client, admin)
    configure_provider(client, csrf, str(upstream.base_url))
    upstream.post("/mode", json={"mode": "slow"})
    before = upstream.get("/state").json()["disconnects"]
    conversation_id = new_conversation(client, csrf)
    with client.stream(
        "POST", f"/api/conversations/{conversation_id}/chat", json={"text": "Question interrompue"}, headers={"X-CSRF-Token": csrf}
    ) as response:
        for line in response.iter_lines():
            if line.startswith("data: "):
                break
        # Fermeture brutale du client : le contrat vérifié est serveur — le
        # message ne doit jamais rester « streaming » et doit être marqué
        # « cancelled ». La libération du flux amont dépend du ramasse-miettes du
        # générateur serveur (best effort, non garanti au niveau transport).
    gc.collect()
    wait_status(client, conversation_id, "cancelled", timeout=20)
    deadline = time.time() + 10
    while time.time() < deadline:
        if upstream.get("/state").json()["disconnects"] > before:
            break
        time.sleep(0.3)
    messages = client.get(f"/api/conversations/{conversation_id}/messages").json()["messages"]
    assert messages[-1]["status"] == "cancelled"
    upstream.post("/mode", json={"mode": "normal"})


def test_upstream_error_is_reported_without_leaking_secrets(client, admin, upstream):
    csrf = login(client, admin)
    configure_provider(client, csrf, str(upstream.base_url))
    upstream.post("/mode", json={"mode": "error"})
    conversation_id = new_conversation(client, csrf)
    with client.stream(
        "POST", f"/api/conversations/{conversation_id}/chat", json={"text": "Provoque une erreur"}, headers={"X-CSRF-Token": csrf}
    ) as response:
        frames = read_frames(response)
    errors = [data for kind, data in frames if kind == "error"]
    assert errors and errors[0]["retryable"] is True
    assert "cle-de-test-locale" not in json.dumps(frames)
    messages = client.get(f"/api/conversations/{conversation_id}/messages").json()["messages"]
    assert messages[-1]["status"] == "error"
    assert messages[-1]["error"]

    # Relance après configuration nominale.
    upstream.post("/mode", json={"mode": "normal"})
    message_id = messages[-1]["id"]
    with client.stream(
        "POST", f"/api/messages/{message_id}/retry", headers={"X-CSRF-Token": csrf}
    ) as response:
        frames = read_frames(response)
    assert frames[-1][0] == "done" and frames[-1][1]["status"] == "complete"
    messages = client.get(f"/api/conversations/{conversation_id}/messages").json()["messages"]
    assert len(messages) == 3
    assert messages[-1]["status"] == "complete"
    assert messages[-1]["content"]


def test_concurrent_generation_is_refused(client, admin, upstream):
    csrf = login(client, admin)
    configure_provider(client, csrf, str(upstream.base_url))
    upstream.post("/mode", json={"mode": "slow"})
    conversation_id = new_conversation(client, csrf)
    meta: dict | None = None
    with client.stream(
        "POST", f"/api/conversations/{conversation_id}/chat", json={"text": "Première"}, headers={"X-CSRF-Token": csrf}
    ) as response:
        for line in response.iter_lines():
            if line.startswith("data: "):
                meta = json.loads(line[len("data: ") :])
                break
        assert meta is not None
        second = client.post(
            f"/api/conversations/{conversation_id}/chat", json={"text": "Deuxième"}, headers={"X-CSRF-Token": csrf}
        )
        assert second.status_code == 409
        with httpx.Client(base_url=str(client.base_url), timeout=30.0) as stopper:
            stopper.cookies.update(client.cookies)
            stopper.post(f"/api/messages/{meta['message_id']}/stop", headers={"X-CSRF-Token": csrf})
    upstream.post("/mode", json={"mode": "normal"})


def test_clearing_key_returns_to_honest_demo(client, admin, upstream):
    csrf = login(client, admin)
    configure_provider(client, csrf, str(upstream.base_url))
    cleared = client.put("/api/settings", json={"clear_api_key": True}, headers={"X-CSRF-Token": csrf})
    assert cleared.status_code == 200
    assert cleared.json()["provider"]["key_configured"] is False
    conversation_id = new_conversation(client, csrf)
    with client.stream(
        "POST", f"/api/conversations/{conversation_id}/chat", json={"text": "Sans fournisseur"}, headers={"X-CSRF-Token": csrf}
    ) as response:
        frames = read_frames(response)
    assert frames[0][1]["demo"] is True
    assert any(kind == "status" and data["state"] == "demo" for kind, data in frames)


def test_endpoint_validation_rejects_unknown_domain(client, admin):
    csrf = login(client, admin)
    refused = client.put(
        "/api/settings",
        json={"provider_endpoint": "https://fournisseur-inconnu.example/v1"},
        headers={"X-CSRF-Token": csrf},
    )
    assert refused.status_code == 422
