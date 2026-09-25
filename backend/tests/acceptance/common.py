"""Socle commun des scripts d'acceptance (exécutés contre la pile réelle).

Connexion via identifiants lus sur stdin ou fichier ; aucune impression de secret.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import httpx

EVIDENCE_DIR = Path(os.environ.get("WALLIA_EVIDENCE_DIR", "/data/evidence"))

_CREDENTIALS: tuple[str, str] | None = None


def credentials() -> tuple[str, str]:
    """Identifiants lus une seule fois (stdin ou fichier) — jamais affichés.

    Idempotent : plusieurs appels (login, debug) ne consomment pas deux fois stdin.
    """
    global _CREDENTIALS
    if _CREDENTIALS is not None:
        return _CREDENTIALS
    if not sys.stdin.isatty():
        raw = sys.stdin.read()
    else:
        path = os.environ.get("WALLIA_INITIAL_ACCESS", "/secrets/initial-access.txt")
        raw = Path(path).read_text(encoding="utf-8")
    email = password = None
    for line in raw.splitlines():
        key, _, value = line.partition(":")
        key = key.strip().lower()
        if key == "email":
            email = value.strip()
        elif key == "password":
            password = value.strip()
    if not email or not password:
        raise SystemExit("identifiants introuvables (stdin ou fichier d'accès initial)")
    _CREDENTIALS = (email, password)
    return _CREDENTIALS


def login(client: httpx.Client) -> str:
    email, password = credentials()
    response = client.post("/api/auth/login", json={"email": email, "password": password})
    if response.status_code != 200:
        raise SystemExit(f"connexion refusée (HTTP {response.status_code})")
    return response.json()["csrf_token"]


def write_evidence(name: str, payload: dict) -> Path:
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    target = EVIDENCE_DIR / name
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return target


def wait_jobs_idle(client: httpx.Client, timeout: float = 1800.0, poll: float = 5.0) -> dict:
    deadline = time.time() + timeout
    last: dict = {}
    while time.time() < deadline:
        jobs = client.get("/api/jobs").json()["jobs"]
        active = [job for job in jobs if job["status"] in {"queued", "running"}]
        last = {
            "total": len(jobs),
            "queued": len([j for j in jobs if j["status"] == "queued"]),
            "running": len([j for j in jobs if j["status"] == "running"]),
            "done": len([j for j in jobs if j["status"] in {"succeeded", "done"}]),
            "failed": len([j for j in jobs if j["status"] == "failed"]),
        }
        print(
            f"  jobs: queued={last['queued']} running={last['running']} done={last['done']} failed={last['failed']}",
            flush=True,
        )
        if not active:
            return last
        time.sleep(poll)
    raise SystemExit(f"jobs non terminés après {timeout:.0f}s: {last}")
