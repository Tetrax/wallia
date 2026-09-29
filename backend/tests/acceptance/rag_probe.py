"""Sonde : que renvoie réellement la recherche pour la question FR→EN ?"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, "/app")

import httpx

from tests.acceptance.common import login

QUESTION = "Combien de jours de journaux la rotation conserve-t-elle dans Aster 10.10 ?"
BASE = os.environ.get("WALLIA_API_BASE_URL", "http://127.0.0.1:8000")

with httpx.Client(base_url=BASE, timeout=120.0) as client:
    csrf = login(client)
    status = client.get("/api/status").json()
    print("embedding:", status["embedding"])
    print("retrieval:", status["retrieval"])
    print("corpus:", status["corpus"])
    response = client.post("/api/search", json={"query": QUESTION}, headers={"X-CSRF-Token": csrf})
    body = response.json()
    print("status:", body.get("status"))
    print("diagnostics:", json.dumps(body.get("diagnostics"), ensure_ascii=False)[:400])
    for index, source in enumerate(body.get("sources") or [], start=1):
        print(
            f"[{index}] {source.get('title')!r} p.{source.get('page_start')} "
            f"cos={source.get('score_vector')} txt={source.get('score_text')} "
            f"| {str(source.get('text') or '')[:80]!r}"
        )
