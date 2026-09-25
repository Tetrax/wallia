"""Faux fournisseur OpenAI-compatible — tests locaux uniquement.

Reproduit fidèlement le contrat SSE attendu (data: {...} puis [DONE]) et permet
d'exercer : streaming nominal, arrêt en cours de génération, erreur HTTP,
coupure en milieu de flux et déconnexion cliente réelle.
"""
from __future__ import annotations

import asyncio
import json
import os
import time

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

app = FastAPI(title="wallia-fake-upstream")

STATE: dict = {"mode": "normal", "requests": 0, "disconnects": 0, "last_prompt_chars": 0}

NORMAL_REPLY = (
    "Réponse de démonstration locale (faux fournisseur). "
    "Selon l'extrait [1], le voyant ambre signale un état dégradé sur la version 10.10. "
    "Prochain contrôle utile : relever le journal local et confirmer la version exacte."
)
SLOW_CHUNKS = 14


@app.get("/state")
def state() -> dict:
    return dict(STATE)


@app.post("/mode")
async def set_mode(request: Request) -> dict:
    body = await request.json()
    mode = str(body.get("mode") or "normal")
    STATE["mode"] = mode
    return {"mode": mode}


def _check_auth(request: Request) -> bool:
    expected = os.environ.get("WALLIA_FAKE_TOKEN") or ""
    if not expected:
        return True
    header = request.headers.get("authorization") or ""
    return header == f"Bearer {expected}"


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    if not _check_auth(request):
        return JSONResponse({"error": {"message": "jeton invalide"}}, status_code=401)
    body = await request.json()
    STATE["requests"] += 1
    STATE["last_prompt_chars"] = sum(len(str(m.get("content") or "")) for m in body.get("messages") or [])
    mode = STATE["mode"]

    if mode == "error":
        return JSONResponse({"error": {"message": "boom simulé"}}, status_code=500)

    async def stream():
        completed = False
        try:
            if mode == "slow":
                for index in range(SLOW_CHUNKS):
                    chunk = {"choices": [{"delta": {"content": f"fragment {index}. "}, "index": 0}]}
                    yield f"data: {json.dumps(chunk)}\n\n"
                    await asyncio.sleep(0.35)
            elif mode == "mid_error":
                for index in range(2):
                    chunk = {"choices": [{"delta": {"content": f"amorce {index}. "}, "index": 0}]}
                    yield f"data: {json.dumps(chunk)}\n\n"
                    await asyncio.sleep(0.05)
                # coupure brutale : pas de [DONE]
                raise RuntimeError("coupure simulée en milieu de flux")
            else:
                words = NORMAL_REPLY.split(" ")
                for index in range(0, len(words), 4):
                    piece = " ".join(words[index : index + 4]) + (" " if index + 4 < len(words) else "")
                    chunk = {"choices": [{"delta": {"content": piece}, "index": 0}]}
                    yield f"data: {json.dumps(chunk)}\n\n"
                    await asyncio.sleep(0.01)
                yield 'data: {"choices":[{"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":42,"completion_tokens":21}}\n\n'
            yield "data: [DONE]\n\n"
            completed = True
        except asyncio.CancelledError:
            STATE["disconnects"] += 1
            raise
        except Exception:
            STATE["disconnects"] += 1
            raise
        finally:
            if not completed:
                STATE["disconnects"] += 0

    return StreamingResponse(stream(), media_type="text/event-stream")


@app.get("/v1/models")
def models() -> dict:
    return {"data": [{"id": "fake-model", "object": "model", "created": int(time.time())}]}
