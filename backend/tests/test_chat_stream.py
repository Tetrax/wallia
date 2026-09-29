"""Streaming SSE réel contre un faux fournisseur OpenAI-compatible.

Couvre : mode démonstration honnête, streaming nominal, arrêt utilisateur (y
compris avant le premier delta et en silence), déconnexion cliente réelle (sans
dépendre du ramasse-miettes), erreur fournisseur, EOF sans [DONE], silence
borné par le délai, relance, concurrence (par conversation ET plafond global),
retour au mode démonstration après effacement de clé.
"""
from __future__ import annotations

import json
import threading
import time

import httpx
import pytest

from tests.conftest import login


@pytest.fixture()
def upstream(fake_upstream):
    with httpx.Client(base_url=fake_upstream, timeout=10.0) as client:
        client.post("/mode", json={"mode": "normal"})
        yield client


def configure_provider(client, csrf, base: str, *, timeout_s: float | None = None) -> None:
    payload: dict = {
        "provider_endpoint": f"{base}/v1",
        "provider_model": "fake-model",
        "api_key": "cle-de-test-locale",
    }
    if timeout_s is not None:
        payload["provider_timeout_s"] = timeout_s
    response = client.put(
        "/api/settings",
        json=payload,
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
    # L'effort de modèle ne fuit jamais dans le contenu ni dans les événements.
    assert "effort" not in json.dumps(frames, ensure_ascii=False).lower()
    messages = client.get(f"/api/conversations/{conversation_id}/messages").json()["messages"]
    assistant = messages[-1]
    assert assistant["model"] == "fake-model"
    assert assistant["demo"] is False
    assert "effort" not in (assistant["content"] or "").lower()
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
    # Le client est resté connecté : la fin de flux (done/cancelled) est requise.
    assert done_frames and done_frames[-1]["status"] == "cancelled"
    upstream.post("/mode", json={"mode": "normal"})


def test_client_disconnect_marks_message_cancelled_and_closes_upstream(client, admin, upstream):
    csrf = login(client, admin)
    configure_provider(client, csrf, str(upstream.base_url))
    upstream.post("/mode", json={"mode": "slow"})
    before = upstream.get("/state").json()["disconnects"]
    conversation_id = new_conversation(client, csrf)
    with client.stream(
        "POST", f"/api/conversations/{conversation_id}/chat", json={"text": "Question interrompue"}, headers={"X-CSRF-Token": csrf}
    ) as response:
        # On attend le PREMIER delta : le transport amont est alors réellement
        # ouvert et en cours de lecture.
        event = None
        for line in response.iter_lines():
            if line.startswith("event: "):
                event = line[len("event: ") :]
            elif line.startswith("data: ") and event == "delta":
                break
        # Fermeture brutale du client : AUCUN gc.collect — la fermeture du
        # transport amont doit être explicite (annulation propagée au fournisseur).
    assert upstream.get("/state").json()["requests"] >= 1, "le flux amont doit avoir démarré avant la déconnexion"
    wait_status(client, conversation_id, "cancelled", timeout=20)
    deadline = time.time() + 10
    disconnected = False
    while time.time() < deadline:
        if upstream.get("/state").json()["disconnects"] > before:
            disconnected = True
            break
        time.sleep(0.2)
    state = upstream.get("/state").json()
    assert disconnected, f"le transport amont n'a pas été fermé après la déconnexion cliente: {state}"
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
        meta = read_one_frame(response)
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


def read_one_frame(response, *, wanted: str = "data") -> dict | None:
    """Lit les lignes SSE jusqu'au premier frame portant des données.

    L'itérateur est conservé sur la réponse : abandonner un itérateur httpx
    fermerait la connexion (et le test croirait à une déconnexion cliente).
    """
    iterator = getattr(response, "_wallia_frame_iterator", None)
    if iterator is None:
        iterator = response.iter_lines()
        setattr(response, "_wallia_frame_iterator", iterator)
    for line in iterator:
        if line.startswith("data: "):
            return json.loads(line[len("data: ") :])
    return None


def _request_stop(client, csrf: str, message_id: str) -> None:
    with httpx.Client(base_url=str(client.base_url), timeout=30.0) as stopper:
        stopper.cookies.update(client.cookies)
        stopped = stopper.post(f"/api/messages/{message_id}/stop", headers={"X-CSRF-Token": csrf})
        assert stopped.status_code == 200


def test_stop_before_first_delta_closes_silent_upstream(client, admin, upstream):
    """Arrêt demandé pendant que le fournisseur est SILENCIEUX (aucun delta).

    Le transport amont est réellement fermé (déconnexion constatée côté
    fournisseur) sans aucun gc.collect, et le message n'est pas « complete ».
    """
    csrf = login(client, admin)
    configure_provider(client, csrf, str(upstream.base_url))
    upstream.post("/mode", json={"mode": "silent"})
    before = upstream.get("/state").json()["disconnects"]
    conversation_id = new_conversation(client, csrf)
    frames: list[tuple[str, dict]] = []
    meta = None
    with client.stream(
        "POST", f"/api/conversations/{conversation_id}/chat", json={"text": "Question silencieuse"}, headers={"X-CSRF-Token": csrf}
    ) as response:
        event = None
        for line in response.iter_lines():
            if line.startswith("event: "):
                event = line[len("event: ") :]
            elif line.startswith("data: "):
                data = json.loads(line[len("data: ") :])
                frames.append((event or "message", data))
                if event == "meta":
                    meta = data
                    # Stop AVANT tout delta : le flux est encore silencieux.
                    _request_stop(client, csrf, data["message_id"])
                if event == "done":
                    break
    assert meta is not None
    deltas = [data for kind, data in frames if kind == "delta"]
    assert deltas == []  # rien n'a été reçu du fournisseur silencieux
    done = [data for kind, data in frames if kind == "done"]
    assert done and done[-1]["status"] == "cancelled"
    wait_status(client, conversation_id, "cancelled", timeout=20)
    deadline = time.time() + 10
    disconnected = False
    while time.time() < deadline:
        if upstream.get("/state").json()["disconnects"] > before:
            disconnected = True
            break
        time.sleep(0.2)
    assert disconnected, "le transport amont silencieux n'a pas été fermé sur stop"
    upstream.post("/mode", json={"mode": "normal"})


def test_silent_upstream_is_bounded_by_provider_deadline(client, admin, upstream):
    """Fournisseur silencieux : erreur bornée par le délai, jamais un flux figé."""
    csrf = login(client, admin)
    configure_provider(client, csrf, str(upstream.base_url), timeout_s=5)
    upstream.post("/mode", json={"mode": "silent"})
    conversation_id = new_conversation(client, csrf)
    started = time.time()
    with client.stream(
        "POST", f"/api/conversations/{conversation_id}/chat", json={"text": "Silence prolongé"}, headers={"X-CSRF-Token": csrf}
    ) as response:
        frames = []
        event = None
        for line in response.iter_lines():
            if line.startswith("event: "):
                event = line[len("event: ") :]
            elif line.startswith("data: "):
                frames.append((event or "message", json.loads(line[len("data: ") :])))
            if any(kind == "error" for kind, _ in frames):
                break
    elapsed = time.time() - started
    assert elapsed < 20, f"le silence n'a pas été borné ({elapsed:.1f}s)"
    errors = [data for kind, data in frames if kind == "error"]
    assert errors and "délai" in errors[0]["message"].lower()
    wait_status(client, conversation_id, "error", timeout=20)
    messages = client.get(f"/api/conversations/{conversation_id}/messages").json()["messages"]
    assert messages[-1]["status"] == "error"
    assert messages[-1]["content"] in ("", None)
    upstream.post("/mode", json={"mode": "normal"})


def test_pre_headers_silence_is_bounded(client, admin, upstream):
    """Silence AVANT les en-têtes de réponse : borné par le délai fournisseur."""
    csrf = login(client, admin)
    configure_provider(client, csrf, str(upstream.base_url), timeout_s=5)
    upstream.post("/mode", json={"mode": "pre_headers_silent"})
    conversation_id = new_conversation(client, csrf)
    started = time.time()
    with client.stream(
        "POST", f"/api/conversations/{conversation_id}/chat", json={"text": "En-têtes absents"}, headers={"X-CSRF-Token": csrf}
    ) as response:
        frames = []
        event = None
        for line in response.iter_lines():
            if line.startswith("event: "):
                event = line[len("event: ") :]
            elif line.startswith("data: "):
                frames.append((event or "message", json.loads(line[len("data: ") :])))
            if any(kind == "error" for kind, _ in frames):
                break
    elapsed = time.time() - started
    assert elapsed < 20, f"le silence pré-en-têtes n'a pas été borné ({elapsed:.1f}s)"
    errors = [data for kind, data in frames if kind == "error"]
    assert errors
    wait_status(client, conversation_id, "error", timeout=20)
    upstream.post("/mode", json={"mode": "normal"})


def test_eof_without_done_is_error_never_complete(client, admin, upstream):
    csrf = login(client, admin)
    configure_provider(client, csrf, str(upstream.base_url))
    upstream.post("/mode", json={"mode": "eof_notdone"})
    conversation_id = new_conversation(client, csrf)
    with client.stream(
        "POST", f"/api/conversations/{conversation_id}/chat", json={"text": "Flux tronqué"}, headers={"X-CSRF-Token": csrf}
    ) as response:
        frames = read_frames(response)
    kinds = [kind for kind, _ in frames]
    assert "error" in kinds
    done = [data for kind, data in frames if kind == "done"]
    assert not any(data["status"] == "complete" for data in done)
    wait_status(client, conversation_id, "error", timeout=20)
    messages = client.get(f"/api/conversations/{conversation_id}/messages").json()["messages"]
    assert messages[-1]["status"] == "error"
    assert "interrompu" in (messages[-1]["error"] or "")
    assert messages[-1]["content"] == "partiel 0. partiel 1. "  # partiel conservé, jamais présenté comme complet
    upstream.post("/mode", json={"mode": "normal"})


def test_demo_mode_respects_stop(client, admin):
    """Le mode démonstration respecte l'arrêt (partiel persisté, jamais complet)."""
    csrf = login(client, admin)
    conversation_id = new_conversation(client, csrf)
    frames: list[tuple[str, dict]] = []
    with client.stream(
        "POST", f"/api/conversations/{conversation_id}/chat", json={"text": "Question démo"}, headers={"X-CSRF-Token": csrf}
    ) as response:
        event = None
        for line in response.iter_lines():
            if line.startswith("event: "):
                event = line[len("event: ") :]
            elif line.startswith("data: "):
                data = json.loads(line[len("data: ") :])
                frames.append((event or "message", data))
                if event == "meta":
                    _request_stop(client, csrf, data["message_id"])
                if event == "delta" and len([1 for kind, _ in frames if kind == "delta"]) >= 2:
                    # On laisse le veilleur constater l'arrêt demandé.
                    time.sleep(0.8)
                if event == "done":
                    break
    done = [data for kind, data in frames if kind == "done"]
    assert done, "fin de flux manquante"
    assert done[-1]["status"] == "cancelled"
    deltas = "".join(data["text"] for kind, data in frames if kind == "delta")
    assert deltas  # du contenu partiel a été émis
    assert "Pour obtenir une réponse rédigée" not in deltas  # la fin du texte démo n'a pas été atteinte
    wait_status(client, conversation_id, "cancelled", timeout=20)


def test_global_concurrency_cap_and_release(client, admin, upstream):
    """Plafond global de générations simultanées (2 en test) — retry compris."""
    csrf = login(client, admin)
    configure_provider(client, csrf, str(upstream.base_url))
    upstream.post("/mode", json={"mode": "slow"})
    first_id = new_conversation(client, csrf)
    second_id = new_conversation(client, csrf)
    third_id = new_conversation(client, csrf)
    with client.stream("POST", f"/api/conversations/{first_id}/chat", json={"text": "A"}, headers={"X-CSRF-Token": csrf}) as ra:
        meta_a = read_one_frame(ra)
        assert meta_a
        with client.stream("POST", f"/api/conversations/{second_id}/chat", json={"text": "B"}, headers={"X-CSRF-Token": csrf}) as rb:
            meta_b = read_one_frame(rb)
            assert meta_b
            refused = client.post(
                f"/api/conversations/{third_id}/chat", json={"text": "C"}, headers={"X-CSRF-Token": csrf}
            )
            assert refused.status_code == 429
            _request_stop(client, csrf, meta_a["message_id"])
            _request_stop(client, csrf, meta_b["message_id"])
    wait_status(client, first_id, "cancelled", timeout=20)
    wait_status(client, second_id, "cancelled", timeout=20)
    # Le plafond est libéré : une nouvelle génération passe.
    upstream.post("/mode", json={"mode": "normal"})
    with client.stream("POST", f"/api/conversations/{third_id}/chat", json={"text": "D"}, headers={"X-CSRF-Token": csrf}) as rc:
        frames = read_frames(rc)
    assert frames[-1][0] == "done" and frames[-1][1]["status"] == "complete"


def test_client_close_right_after_meta_closes_upstream(client, admin, upstream):
    """Fermeture cliente AVANT tout delta (juste après `meta`).

    Le flux amont est réellement SILENCIEUX (zéro octet) : la déconnexion doit
    être constatée côté serveur, le transport amont fermé, et le message
    persiste « cancelled » — sans dépendre du ramasse-miettes ni d'un delta.
    """
    csrf = login(client, admin)
    configure_provider(client, csrf, str(upstream.base_url))
    upstream.post("/mode", json={"mode": "silent"})
    before = upstream.get("/state").json()["disconnects"]
    requests_before = upstream.get("/state").json()["requests"]
    conversation_id = new_conversation(client, csrf)
    with client.stream(
        "POST", f"/api/conversations/{conversation_id}/chat", json={"text": "Fermeture après meta"}, headers={"X-CSRF-Token": csrf}
    ) as response:
        assert response.status_code == 200
        meta = read_one_frame(response)
        assert meta and meta["message_id"]
        # La requête amont doit être RÉELLEMENT en vol (sinon il n'y a aucun
        # transport à fermer) : on attend qu'elle soit constatée côté amont.
        deadline = time.time() + 10
        while time.time() < deadline and upstream.get("/state").json()["requests"] <= requests_before:
            time.sleep(0.1)
        assert upstream.get("/state").json()["requests"] > requests_before, "requête amont jamais reçue"
        # Sortie du contexte AVANT tout delta : fermeture brutale côté client.
    wait_status(client, conversation_id, "cancelled", timeout=20)
    deadline = time.time() + 10
    while time.time() < deadline:
        if upstream.get("/state").json()["disconnects"] > before:
            break
        time.sleep(0.2)
    state = upstream.get("/state").json()
    assert state["disconnects"] > before, f"transport amont silencieux non fermé après fermeture cliente: {state}"
    messages = client.get(f"/api/conversations/{conversation_id}/messages").json()["messages"]
    assert messages[-1]["status"] == "cancelled"
    assert messages[-1]["content"] in ("", None)  # aucun octet reçu : aucun contenu inventé
    upstream.post("/mode", json={"mode": "normal"})


def test_post_delta_silence_is_bounded_and_partial_preserved(client, admin, upstream):
    """Deltas reçus PUIS silence total (zéro octet) : borné par le délai,
    contenu partiel conservé et jamais présenté comme complet."""
    csrf = login(client, admin)
    configure_provider(client, csrf, str(upstream.base_url), timeout_s=5)
    upstream.post("/mode", json={"mode": "delta_then_silent"})
    conversation_id = new_conversation(client, csrf)
    started = time.time()
    with client.stream(
        "POST", f"/api/conversations/{conversation_id}/chat", json={"text": "Deltas puis silence"}, headers={"X-CSRF-Token": csrf}
    ) as response:
        frames = read_frames(response)
    elapsed = time.time() - started
    assert elapsed < 20, f"le silence après deltas n'a pas été borné ({elapsed:.1f}s)"
    errors = [data for kind, data in frames if kind == "error"]
    assert errors and "délai" in errors[0]["message"].lower()
    deltas = "".join(data["text"] for kind, data in frames if kind == "delta")
    assert deltas == "amorce 0. amorce 1. "
    wait_status(client, conversation_id, "error", timeout=20)
    messages = client.get(f"/api/conversations/{conversation_id}/messages").json()["messages"]
    assert messages[-1]["status"] == "error"
    assert messages[-1]["content"] == deltas  # partiel conservé, jamais complet
    upstream.post("/mode", json={"mode": "normal"})


def test_stop_and_disconnect_do_not_break_persistence(client, admin, upstream):
    """Stop demandé ET déconnexion cliente : le message reste « cancelled » et
    le transport amont est fermé — aucune double finalisation incohérente."""
    csrf = login(client, admin)
    configure_provider(client, csrf, str(upstream.base_url))
    upstream.post("/mode", json={"mode": "slow"})
    before = upstream.get("/state").json()["disconnects"]
    conversation_id = new_conversation(client, csrf)
    with client.stream(
        "POST", f"/api/conversations/{conversation_id}/chat", json={"text": "Stop puis fermeture"}, headers={"X-CSRF-Token": csrf}
    ) as response:
        meta = read_one_frame(response)
        assert meta and meta["message_id"]
        _request_stop(client, csrf, meta["message_id"])
        # Fermeture immédiate du client après le stop demandé.
    wait_status(client, conversation_id, "cancelled", timeout=20)
    deadline = time.time() + 10
    while time.time() < deadline:
        if upstream.get("/state").json()["disconnects"] > before:
            break
        time.sleep(0.2)
    assert upstream.get("/state").json()["disconnects"] > before, "transport amont non fermé (stop + déconnexion)"
    messages = client.get(f"/api/conversations/{conversation_id}/messages").json()["messages"]
    assert messages[-1]["status"] == "cancelled"
    upstream.post("/mode", json={"mode": "normal"})


def test_oversized_api_key_is_never_echoed_in_validation_error(client, admin):
    csrf = login(client, admin)
    secret = "sk-" + "A" * 600
    response = client.put("/api/settings", json={"api_key": secret}, headers={"X-CSRF-Token": csrf})
    assert response.status_code == 422
    assert secret not in response.text
    assert "input" not in response.text


# ---------------------------------------------------------------------------
# Annulation AVANT les en-têtes de réponse du fournisseur (délai LONG) :
#   - le stop doit fermer le transport amont réellement et rapidement ;
#   - la déconnexion cliente doit produire le même effet ;
#   - le délai fournisseur n'est JAMAIS un substitut : il est long (45 s) et
#     la fermeture doit être constatée en ≤ 5 s (borne cible 3 s).
# ---------------------------------------------------------------------------

UPSTREAM_CLOSE_BOUND_S = 5.0


def _upstream_state(upstream) -> dict:
    return upstream.get("/state").json()


def _wait_upstream_request(upstream, before: int, timeout: float = 10.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _upstream_state(upstream)["requests"] > before:
            return
        time.sleep(0.1)
    raise AssertionError("la requête amont n'a jamais été reçue")


def _wait_upstream_close(upstream, before: int, timeout: float) -> float:
    """Retourne le délai de fermeture constaté côté amont, ou -1 si absent."""
    started = time.monotonic()
    deadline = started + timeout
    while time.monotonic() < deadline:
        if _upstream_state(upstream)["disconnects"] > before:
            return time.monotonic() - started
        time.sleep(0.1)
    return -1.0


def test_stop_before_headers_closes_silent_upstream_quickly(client, admin, upstream):
    """Stop pendant un silence AVANT en-têtes, délai fournisseur LONG.

    Le transport remonte la requête amont sans jamais envoyer d'en-tête ;
    l'arrêt doit fermer le transport côté amont en ≤ 5 s (cible 3 s), l'API
    rester réactive, et le message finir `cancelled` — jamais « complete ».
    """
    csrf = login(client, admin)
    configure_provider(client, csrf, str(upstream.base_url), timeout_s=45)
    upstream.post("/mode", json={"mode": "pre_headers_silent"})
    state = _upstream_state(upstream)
    before_requests, before_disconnects = state["requests"], state["disconnects"]

    conversation_id = new_conversation(client, csrf)
    outcome: dict = {}

    def stopper(message_id: str) -> None:
        _wait_upstream_request(upstream, before_requests)
        # L'API reste réactive pendant le silence fournisseur.
        started = time.monotonic()
        health = client.get("/healthz")
        outcome["health_status"] = health.status_code
        outcome["health_ms"] = (time.monotonic() - started) * 1000
        _request_stop(client, csrf, message_id)
        outcome["close_elapsed"] = _wait_upstream_close(upstream, before_disconnects, timeout=UPSTREAM_CLOSE_BOUND_S)

    frames: list[tuple[str, dict]] = []
    with client.stream(
        "POST",
        f"/api/conversations/{conversation_id}/chat",
        json={"text": "Fournisseur muet avant en-têtes"},
        headers={"X-CSRF-Token": csrf},
    ) as response:
        event = None
        for line in response.iter_lines():
            if line.startswith("event: "):
                event = line[len("event: ") :]
            elif line.startswith("data: "):
                data = json.loads(line[len("data: ") :])
                frames.append((event or "message", data))
                if event == "meta" and "stopper" not in outcome:
                    outcome["stopper"] = True
                    threading.Thread(target=stopper, args=(data["message_id"],), daemon=True).start()
                if event == "done":
                    break
    deadline = time.time() + 10
    while "close_elapsed" not in outcome and time.time() < deadline:
        time.sleep(0.1)

    assert outcome.get("health_status") == 200, outcome
    assert outcome.get("health_ms", 9999) < 3000, f"API non réactive: {outcome}"
    elapsed = outcome.get("close_elapsed", -1.0)
    assert elapsed >= 0, f"fermeture amont non constatée après stop pré-en-têtes: {elapsed}"
    assert elapsed <= UPSTREAM_CLOSE_BOUND_S, f"fermeture amont trop lente: {elapsed:.2f}s"
    done = [data for kind, data in frames if kind == "done"]
    assert done and done[-1]["status"] == "cancelled"
    deltas = [data for kind, data in frames if kind == "delta"]
    assert deltas == []  # aucun octet fournisseur n'a été reçu
    wait_status(client, conversation_id, "cancelled", timeout=20)
    upstream.post("/mode", json={"mode": "normal"})


def test_disconnect_before_headers_closes_silent_upstream_quickly(client, admin, upstream):
    """Déconnexion cliente pendant un silence AVANT en-têtes, délai long."""
    csrf = login(client, admin)
    configure_provider(client, csrf, str(upstream.base_url), timeout_s=45)
    upstream.post("/mode", json={"mode": "pre_headers_silent"})
    state = _upstream_state(upstream)
    before_requests, before_disconnects = state["requests"], state["disconnects"]

    conversation_id = new_conversation(client, csrf)
    with client.stream(
        "POST",
        f"/api/conversations/{conversation_id}/chat",
        json={"text": "Déconnexion avant en-têtes"},
        headers={"X-CSRF-Token": csrf},
    ) as response:
        meta = read_one_frame(response)
        assert meta and meta["message_id"]
        # La requête doit être RÉELLEMENT en vol côté amont avant la coupure.
        _wait_upstream_request(upstream, before_requests)
        # Sortie de contexte : fermeture brutale côté client, avant tout octet.
    elapsed = _wait_upstream_close(upstream, before_disconnects, timeout=UPSTREAM_CLOSE_BOUND_S)
    assert elapsed >= 0, "fermeture amont non constatée après déconnexion pré-en-têtes"
    assert elapsed <= UPSTREAM_CLOSE_BOUND_S, f"fermeture amont trop lente: {elapsed:.2f}s"
    wait_status(client, conversation_id, "cancelled", timeout=20)
    messages = client.get(f"/api/conversations/{conversation_id}/messages").json()["messages"]
    assert messages[-1]["status"] == "cancelled"
    assert messages[-1]["content"] in ("", None)
    upstream.post("/mode", json={"mode": "normal"})


def test_socket_tracking_is_active_in_the_installed_stack():
    """Le mécanisme réel du stop pré-en-têtes est ACTIF (pas une intention) :
    le transport httpcore installé est bien enregistré/enveloppé. Si cette
    compatibilité casse, le test échoue AVANT qu'un défaut discret ne revienne."""
    import httpx

    from app.llm import _SocketRegistry, _install_socket_tracking

    registry = _SocketRegistry()
    with httpx.Client() as probe_client:
        assert _install_socket_tracking(probe_client, registry) is True
        pool = getattr(probe_client._transport, "_pool", None)
        assert pool is not None
        assert "Tracking" in type(pool._network_backend).__name__
