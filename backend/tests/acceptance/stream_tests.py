"""Acceptance — streaming/stop/erreurs/relance contre le faux fournisseur local.

Prérequis : service `fake-upstream` démarré (profil testtools) et API autorisant
son domaine (`WALLIA_PROVIDER_ALLOWED_DOMAINS` contenant `fake-upstream`).

    cat runtime/secrets/initial-access.txt | docker compose -p wallia exec -T api \
        python tests/acceptance/stream_tests.py
"""
from __future__ import annotations

import json
import os
import threading
import time

import httpx

from tests.acceptance.common import login, write_evidence

FAKE = os.environ.get("WALLIA_FAKE_URL", "http://fake-upstream:8990")
CHAT_TIMEOUT = 120.0


def read_frames(response, limit: int = 500) -> list[tuple[str, dict]]:
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


def wait_status(client: httpx.Client, conversation_id: str, status: str, timeout: float = 20.0) -> str | None:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        messages = client.get(f"/api/conversations/{conversation_id}/messages").json()["messages"]
        if messages:
            last = messages[-1]["status"]
            if last == status:
                return last
        time.sleep(0.25)
    return last


def main() -> int:
    results: dict = {"scenarios": []}
    with httpx.Client(base_url=os.environ.get("WALLIA_SMOKE_BASE", "http://api:8000"), timeout=CHAT_TIMEOUT) as client:
        csrf = login(client)

        with httpx.Client(base_url=FAKE, timeout=20.0) as fake:
            fake.post("/mode", json={"mode": "normal"})
            fake_state_before = fake.get("/state").json()

            # 1. Configuration du fournisseur applicatif (clé locale de test).
            configured = client.put(
                "/api/settings",
                json={"provider_endpoint": f"{FAKE}/v1", "provider_model": "fake-model", "api_key": "cle-locale-test-acceptance"},
                headers={"X-CSRF-Token": csrf},
            )
            results["scenarios"].append(
                {"name": "configuration fournisseur", "ok": configured.status_code == 200 and configured.json()["provider"]["key_configured"] is True, "http": configured.status_code}
            )

            conversation = client.post("/api/conversations", json={"title": "acceptance streaming"}, headers={"X-CSRF-Token": csrf}).json()

            # 2. Streaming nominal.
            with client.stream("POST", f"/api/conversations/{conversation['id']}/chat", json={"text": "Explique le voyant ambre."}, headers={"X-CSRF-Token": csrf}) as stream:
                frames = read_frames(stream)
            deltas = [data["text"] for kind, data in frames if kind == "delta"]
            results["scenarios"].append(
                {
                    "name": "streaming nominal",
                    "ok": bool(deltas) and frames[-1][0] == "done" and frames[-1][1]["status"] == "complete",
                    "deltas": len(deltas),
                    "content_preview": " ".join("".join(deltas).split())[:160],
                }
            )

            # 3. Arrêt en cours de génération (lecture en UNE seule passe : on
            # n'itère jamais deux fois sur le même flux HTTP).
            fake.post("/mode", json={"mode": "slow"})
            conversation_stop = client.post("/api/conversations", json={"title": "arrêt"}, headers={"X-CSRF-Token": csrf}).json()
            frames: list[tuple[str, dict]] = []
            first_delta = threading.Event()

            def stopper() -> None:
                # On n'interrompt qu'après le début réel du streaming.
                first_delta.wait(timeout=30)
                message_id = next((data["message_id"] for kind, data in frames if kind == "meta"), None)
                if not message_id:
                    return
                with httpx.Client(base_url=str(client.base_url), timeout=30.0) as stopper_client:
                    stopper_client.cookies.update(client.cookies)
                    stopper_client.post(f"/api/messages/{message_id}/stop", headers={"X-CSRF-Token": csrf})

            stop_thread = threading.Thread(target=stopper, daemon=True)
            stop_thread.start()
            with client.stream(
                "POST",
                f"/api/conversations/{conversation_stop['id']}/chat",
                json={"text": "Question longue à interrompre."},
                headers={"X-CSRF-Token": csrf},
            ) as stream:
                event = None
                for line in stream.iter_lines():
                    if line.startswith("event: "):
                        event = line[len("event: ") :]
                    elif line.startswith("data: "):
                        data = json.loads(line[len("data: ") :])
                        frames.append((event or "message", data))
                        if event == "delta":
                            first_delta.set()
                        if event == "done":
                            break
            stop_thread.join(timeout=10)
            final = wait_status(client, conversation_stop["id"], "cancelled")
            results["scenarios"].append(
                {"name": "arrêt utilisateur", "ok": final == "cancelled", "final_status": final}
            )

            # 4. Erreur fournisseur puis relance.
            fake.post("/mode", json={"mode": "error"})
            conversation_error = client.post("/api/conversations", json={"title": "erreur"}, headers={"X-CSRF-Token": csrf}).json()
            with client.stream("POST", f"/api/conversations/{conversation_error['id']}/chat", json={"text": "Provoque une erreur."}, headers={"X-CSRF-Token": csrf}) as stream:
                frames = read_frames(stream)
            error_frames = [data for kind, data in frames if kind == "error"]
            with httpx.Client(base_url=str(client.base_url), timeout=CHAT_TIMEOUT) as probe:
                probe.cookies.update(client.cookies)
                messages = probe.get(f"/api/conversations/{conversation_error['id']}/messages").json()["messages"]
            error_message_id = messages[-1]["id"]
            fake.post("/mode", json={"mode": "normal"})
            with client.stream("POST", f"/api/messages/{error_message_id}/retry", headers={"X-CSRF-Token": csrf}) as stream:
                retry_frames = read_frames(stream)
            with httpx.Client(base_url=str(client.base_url), timeout=CHAT_TIMEOUT) as probe:
                probe.cookies.update(client.cookies)
                messages_after = probe.get(f"/api/conversations/{conversation_error['id']}/messages").json()["messages"]
            results["scenarios"].append(
                {
                    "name": "erreur fournisseur + relance",
                    "ok": bool(error_frames)
                    and retry_frames[-1][0] == "done"
                    and retry_frames[-1][1]["status"] == "complete"
                    and messages_after[-1]["status"] == "complete",
                    "error_retryable": error_frames[0]["retryable"] if error_frames else None,
                }
            )

            # 5. Retour au mode démonstration après effacement de la clé.
            cleared = client.put("/api/settings", json={"clear_api_key": True}, headers={"X-CSRF-Token": csrf})
            conversation_demo = client.post("/api/conversations", json={"title": "démo"}, headers={"X-CSRF-Token": csrf}).json()
            with client.stream("POST", f"/api/conversations/{conversation_demo['id']}/chat", json={"text": "Sans fournisseur."}, headers={"X-CSRF-Token": csrf}) as stream:
                frames = read_frames(stream)
            demo_ok = frames[0][0] == "meta" and frames[0][1]["demo"] is True and any(kind == "status" and data.get("state") == "demo" for kind, data in frames)
            results["scenarios"].append(
                {
                    "name": "retour mode démonstration",
                    "ok": cleared.status_code == 200 and cleared.json()["provider"]["key_configured"] is False and demo_ok,
                }
            )
            fake_state_after = fake.get("/state").json()
            results["fake_upstream"] = {"before": fake_state_before, "after": fake_state_after}

    target = write_evidence("acceptance-streaming.json", results)
    print(f"Preuves écrites : {target}")
    ok = all(scenario["ok"] for scenario in results["scenarios"])
    for scenario in results["scenarios"]:
        print(f"[{'OK' if scenario['ok'] else 'ÉCHEC'}] {scenario['name']}")
    print("Résultat streaming :", "OK" if ok else "ÉCHEC")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
