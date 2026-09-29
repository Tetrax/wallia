"""Diagnostic borné — fermeture de l'amont AVANT en-têtes sur annulation.

Scénarios réels, amont silencieux AVANT en-têtes :
  - HTTP suivi (mécanisme actuel : socket enregistré dès l'ouverture) ;
  - HTTP hérité (client.close seul) — reproduction du défaut de la revue run163 ;
  - HTTPS avec CA ÉPHÉMÈRE VÉRIFIÉE (jamais verify=False) : la requête doit
    avoir été REÇUE par l'amont DANS TLS puis silence avant en-têtes ; stop,
    déconnexion et délai doivent fermer le transport TLS, terminer le thread
    fournisseur et laisser le fournisseur sain (appel réel suivant OK).

Angle mort reproduit : httpcore `SyncStream.start_tls` renvoie un NOUVEAU flux
(TLS) et détache l'ancien socket ; sans suivi du flux renvoyé par `start_tls`,
la fermeture d'annulation viserait un descripteur détaché et HTTPS ne serait
pas coupé (le thread resterait bloqué jusqu'au délai).

Sortie JSON : runtime/tests-isolated/evidence/lot4-repro-pre-headers.json
Aucun réseau externe, aucun secret, aucun lifecycle de la pile vivante.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

sys.path.insert(0, "/app")

import httpx  # noqa: E402
import uvicorn  # noqa: E402

from app.llm import Provider, ProviderCancellation  # noqa: E402
from tests.fake_upstream import app as fake_app  # noqa: E402

CLOSE_WINDOW_S = 6.0
PROVIDER_TIMEOUT_S = 45.0
TIMEOUT_SCENARIO_S = 6.0


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def start_upstream(*, cert: str | None = None, key: str | None = None) -> tuple[uvicorn.Server, int]:
    port = free_port()
    config = uvicorn.Config(
        fake_app,
        host="127.0.0.1",
        port=port,
        log_level="error",
        ssl_certfile=cert,
        ssl_keyfile=key,
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True, name="repro-upstream")
    thread.start()
    deadline = time.time() + 20
    while time.time() < deadline and not getattr(server, "started", False):
        time.sleep(0.05)
    if not getattr(server, "started", False):
        raise RuntimeError("faux amont non démarré")
    return server, port


def make_ephemeral_ca(dirpath: Path) -> tuple[str, str, str]:
    """CA + certificat serveur éphémères (SAN IP:127.0.0.1), CA vérifiée par le client."""
    ca_key, ca_crt = dirpath / "ca.key", dirpath / "ca.crt"
    srv_key, srv_csr, srv_crt = dirpath / "srv.key", dirpath / "srv.csr", dirpath / "srv.crt"
    ext = dirpath / "san.cnf"
    ext.write_text("subjectAltName=IP:127.0.0.1\n", encoding="utf-8")
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
         "-keyout", str(ca_key), "-out", str(ca_crt), "-subj", "/CN=wallia-repro-ca"],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["openssl", "req", "-newkey", "rsa:2048", "-nodes",
         "-keyout", str(srv_key), "-out", str(srv_csr), "-subj", "/CN=127.0.0.1"],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["openssl", "x509", "-req", "-in", str(srv_csr), "-CA", str(ca_crt), "-CAkey", str(ca_key),
         "-CAcreateserial", "-out", str(srv_crt), "-days", "1", "-extfile", str(ext)],
        check=True, capture_output=True,
    )
    return str(ca_crt), str(srv_crt), str(srv_key)


def upstream_state(base: str, *, verify) -> dict:
    return httpx.get(f"{base}/state", timeout=5, verify=verify).json()


def scenario(port: int, *, tracking: bool = True, tls: bool = False, cancel_reason: str = "stop",
             timeout_s: float = PROVIDER_TIMEOUT_S, ca: str | None = None) -> dict:
    import app.llm as llm

    scheme = "https" if tls else "http"
    base = f"{scheme}://127.0.0.1:{port}"
    # Vérification TLS TOUJOURS ACTIVE : CA éphémère fournie via SSL_CERT_FILE
    # (httpx trust_env) — aucune désactivation de vérification n'est utilisée.
    verify: str | bool = ca if (tls and ca) else True
    httpx.post(f"{base}/mode", json={"mode": "pre_headers_silent"}, timeout=5, verify=verify)
    before = upstream_state(base, verify=verify)
    result: dict = {
        "scenario": f"{scheme}-{cancel_reason}" + ("" if tracking else "-legacy"),
        "tls": tls,
        "cancel_reason": cancel_reason,
    }

    original_install = llm._install_socket_tracking
    if not tracking:
        llm._install_socket_tracking = lambda client, registry: False  # ANCIEN comportement

    from app.config import load_settings

    settings = load_settings()
    provider = Provider(
        settings,
        endpoint=f"{base}/v1",
        model="fake-model",
        api_key="cle-de-test-locale",
        timeout_s=timeout_s,
    )
    cancel = ProviderCancellation()

    def run() -> None:
        try:
            for _event in provider.stream_chat([{"role": "user", "content": "repro"}], cancellation=cancel):
                pass
            result["provider_returned"] = True
        except Exception as exc:  # noqa: BLE001
            result["provider_exception"] = type(exc).__name__

    try:
        worker = threading.Thread(target=run, daemon=True, name=f"repro-provider-{uuid.uuid4().hex[:6]}")
        worker.start()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and upstream_state(base, verify=verify)["requests"] <= before["requests"]:
            time.sleep(0.1)
        result["request_seen_upstream"] = upstream_state(base, verify=verify)["requests"] > before["requests"]

        window = CLOSE_WINDOW_S if cancel_reason != "timeout" else timeout_s + CLOSE_WINDOW_S
        result["close_window_s"] = window
        started = time.monotonic()
        if cancel_reason != "timeout":
            cancel.cancel(cancel_reason)
        closed: float | None = None
        while time.monotonic() - started < window:
            if upstream_state(base, verify=verify)["disconnects"] > before["disconnects"]:
                closed = time.monotonic() - started
                break
            time.sleep(0.1)
        result["close_observed_s"] = round(closed, 3) if closed is not None else None
        result["closed_within_5s"] = closed is not None and closed <= 5.0
        result["closed_within_window"] = closed is not None
        worker.join(timeout=2.0)
        result["provider_thread_finished"] = not worker.is_alive()
        time.sleep(0.2)

        # Fournisseur SAIN après l'annulation : un appel réel suivant aboutit.
        httpx.post(f"{base}/mode", json={"mode": "normal"}, timeout=5, verify=verify)
        health = Provider(
            settings, endpoint=f"{base}/v1", model="fake-model", api_key="cle-de-test-locale", timeout_s=20.0
        )
        health_events: list[str] = []
        try:
            for event in health.stream_chat([{"role": "user", "content": "sante"}], cancellation=ProviderCancellation()):
                health_events.append(str(event.get("type")))
        except Exception as exc:  # noqa: BLE001
            result["health_exception"] = type(exc).__name__
        result["health_after"] = "done" in health_events
    finally:
        llm._install_socket_tracking = original_install
    return result


def main() -> int:
    env = os.environ.copy()
    env.setdefault("WALLIA_ENV", "test")
    env.setdefault("WALLIA_PROVIDER_ALLOWED_DOMAINS", "127.0.0.1,localhost")
    os.environ.update(env)

    tls_dir = Path(tempfile.mkdtemp(prefix="wallia-repro-tls-"))
    ca, srv_crt, srv_key = make_ephemeral_ca(tls_dir)

    server_http, port_http = start_upstream()
    server_tls, port_tls = start_upstream(cert=srv_crt, key=srv_key)
    # Vérification TLS RÉELLE : le client fait confiance à la CA éphémère via
    # SSL_CERT_FILE (httpx trust_env) — jamais verify=False ni -k.
    os.environ["SSL_CERT_FILE"] = ca
    try:
        report = {
            "close_window_s": CLOSE_WINDOW_S,
            "provider_timeout_s": PROVIDER_TIMEOUT_S,
            "tls": {
                "ca": "éphémère (SAN IP:127.0.0.1)",
                "verification": "activée (CA éphémère via SSL_CERT_FILE, httpx trust_env)",
            },
            "scenarios": [
                scenario(port_http, tracking=True),
                scenario(port_http, tracking=False),
                scenario(port_tls, tracking=True, tls=True, cancel_reason="stop", ca=ca),
                scenario(port_tls, tracking=True, tls=True, cancel_reason="disconnect", ca=ca),
                scenario(port_tls, tracking=True, tls=True, cancel_reason="timeout", timeout_s=TIMEOUT_SCENARIO_S, ca=ca),
            ],
        }
    finally:
        server_http.should_exit = True
        server_tls.should_exit = True

    evidence_dir = Path(os.environ.get("WALLIA_EVIDENCE_DIR", "/run/isolation/evidence"))
    evidence_dir.mkdir(parents=True, exist_ok=True)
    out = evidence_dir / "lot4-repro-pre-headers.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=1))

    http_ok = report["scenarios"][0].get("closed_within_5s") is True
    legacy_ok = report["scenarios"][1].get("closed_within_5s") is True
    tls_results = [item for item in report["scenarios"] if item.get("tls")]
    tls_ok = all(
        item.get("request_seen_upstream")
        and item.get("closed_within_window")
        and item.get("provider_thread_finished")
        and item.get("health_after")
        for item in tls_results
    )
    print(f"http_tracking_ok={http_ok} legacy_ok={legacy_ok} tls_ok={tls_ok}")
    return 0 if (http_ok and tls_ok) else 1


if __name__ == "__main__":
    raise SystemExit(main())
