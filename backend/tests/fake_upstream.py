"""Faux fournisseur OpenAI-compatible — tests locaux uniquement.

Reproduit fidèlement le contrat SSE attendu (data: {...} puis [DONE]) et permet
d'exercer : streaming nominal, arrêt en cours de génération, erreur HTTP,
coupure en milieu de flux, EOF sans [DONE], silence total (en-têtes puis aucun
octet, ou silence AVANT les en-têtes) et déconnexion cliente réelle.
"""
from __future__ import annotations

import asyncio
import json
import os
import time

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

app = FastAPI(title="wallia-fake-upstream")

STATE: dict = {
    "mode": "normal",
    "requests": 0,
    "disconnects": 0,
    "last_prompt_chars": 0,
    "last_prompt": "",
    "last_system": "",
    "meta": {},
}

NORMAL_REPLY = (
    "Réponse de démonstration locale (faux fournisseur). "
    "Selon l'extrait [1], le voyant ambre signale un état dégradé sur la version 10.10. "
    "Prochain contrôle utile : relever le journal local et confirmer la version exacte."
)
SLOW_CHUNKS = 14
SILENT_SECONDS = 30.0
PRE_HEADERS_SILENT_SECONDS = 30.0

# Réponse cannée pour la recette UI : Markdown + HTML hostile + citations
# valide [1] et INCONNUE [42], liens dangereux (javascript:/data:) et citation
# forgée (#source-9). Les pièces jointes/sources sont des données non fiables :
# le texte rendu doit être échappé, [42] ne crée jamais de source, les schémas
# dangereux sont neutralisés et #source-9 n'est jamais cliquable.
XSS_MARKDOWN_REPLY = (
    "**Gras attendu** et `code`.\n\n"
    "- puce un\n- puce deux\n\n"
    '<img src=x onerror="window.__walliaXss=1">'
    '<script>window.__walliaXss=2</script>'
    "Fin du texte hostile selon [1] puis citation inconnue [42]. "
    "[lien dangereux](javascript:window.__walliaXss=3) et "
    "[lien data](data:text/html;base64,PHNjcmlwdD53aW5kb3cuX193YWxsaWFYc3M9NDwvc2NyaXB0Pg==) et "
    "[citation forgée](#source-9).\n"
)


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


async def _await_disconnect(request: Request) -> None:
    """Constate la fermeture du transport SANS émettre d'octet.

    Le veilleur lit `receive()` : uvicorn y délivre `http.disconnect` quand le
    client part. C'est le seul mécanisme fiable — `is_disconnected()` seul ne
    voit pas toujours la fermeture d'une réponse en cours.
    """
    receive = getattr(request, "_receive", None)
    if receive is None:  # repli défensif : API publique, moins fiable
        while not await request.is_disconnected():
            await asyncio.sleep(0.05)
        return
    while True:
        message = await receive()
        if message.get("type") == "http.disconnect":
            return


def _note_disconnect(reason: str) -> None:
    STATE["disconnects"] += 1
    STATE["last_close"] = reason


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    if not _check_auth(request):
        return JSONResponse({"error": {"message": "jeton invalide"}}, status_code=401)
    body = await request.json()
    STATE["requests"] += 1
    messages = body.get("messages") or []
    STATE["last_prompt_chars"] = sum(len(str(m.get("content") or "")) for m in messages)
    STATE["last_prompt"] = "\n".join(
        f"<{m.get('role')}>{m.get('content') or ''}" for m in messages
    )[:40000]
    STATE["last_system"] = next(
        (str(m.get("content") or "") for m in messages if m.get("role") == "system"), ""
    )
    STATE["meta"] = {"message_count": len(messages)}
    mode = STATE["mode"]

    if mode == "error":
        return JSONResponse({"error": {"message": "boom simulé"}}, status_code=500)

    if mode == "pre_headers_silent":
        # Silence AVANT les en-têtes de réponse : rien n'est renvoyé au client,
        # même pas le début du flux. La fermeture RÉELLE du transport est
        # constatée par `receive()` (http.disconnect) — la même preuve que les
        # autres modes ; un simple sommeil ne détecterait jamais la coupure.
        watcher = asyncio.create_task(_await_disconnect(request))
        deadline = time.monotonic() + PRE_HEADERS_SILENT_SECONDS
        try:
            while not watcher.done() and time.monotonic() < deadline:
                await asyncio.sleep(0.05)
            if watcher.done() and not watcher.cancelled():
                _note_disconnect("receive-pre-headers")
                # Le client est parti : la réponse ne sera pas délivrée.
                return JSONResponse({"error": {"message": "client parti"}}, status_code=499)
        finally:
            watcher.cancel()
        return JSONResponse({"error": {"message": "jamais atteint"}}, status_code=504)

    # Le corps de requête est déjà lu : le veilleur ne peut plus voler de
    # message, il ne verra que la déconnexion du client.
    watcher = asyncio.create_task(_await_disconnect(request))

    async def stream():
        try:
            if mode == "silent":
                # ZÉRO OCTET exigé : aucune écriture, même pas un commentaire
                # keepalive. La fermeture du transport est constatée par
                # `receive()`, jamais cachée par des octets émis côté serveur.
                deadline = time.monotonic() + SILENT_SECONDS
                while not watcher.done() and time.monotonic() < deadline:
                    await asyncio.sleep(0.05)
                if watcher.done():
                    _note_disconnect("receive")
                return
            elif mode == "delta_then_silent":
                # Deux deltas RÉELS puis silence TOTAL (zéro octet) : la
                # fermeture est constatée par receive(), jamais par un envoi.
                for index in range(2):
                    chunk = {"choices": [{"delta": {"content": f"amorce {index}. "}, "index": 0}]}
                    yield f"data: {json.dumps(chunk)}\n\n"
                    await asyncio.sleep(0.05)
                deadline = time.monotonic() + SILENT_SECONDS
                while not watcher.done() and time.monotonic() < deadline:
                    await asyncio.sleep(0.05)
                if watcher.done():
                    _note_disconnect("receive")
                return
            elif mode == "slow":
                for index in range(SLOW_CHUNKS):
                    chunk = {"choices": [{"delta": {"content": f"fragment {index}. "}, "index": 0}]}
                    yield f"data: {json.dumps(chunk)}\n\n"
                    done, _pending = await asyncio.wait({watcher}, timeout=0.35)
                    if done:
                        _note_disconnect("receive")
                        return
            elif mode == "mid_error":
                for index in range(2):
                    chunk = {"choices": [{"delta": {"content": f"amorce {index}. "}, "index": 0}]}
                    yield f"data: {json.dumps(chunk)}\n\n"
                    await asyncio.sleep(0.05)
                # coupure brutale : pas de [DONE]
                raise RuntimeError("coupure simulée en milieu de flux")
            elif mode == "eof_notdone":
                # Fin de flux propre (fermeture du corps) SANS `[DONE]` :
                # jamais une réponse complète.
                for index in range(2):
                    chunk = {"choices": [{"delta": {"content": f"partiel {index}. "}, "index": 0}]}
                    yield f"data: {json.dumps(chunk)}\n\n"
                    await asyncio.sleep(0.05)
                return
            elif mode == "xss_markdown":
                words = XSS_MARKDOWN_REPLY.split(" ")
                for index in range(0, len(words), 3):
                    piece = " ".join(words[index : index + 3]) + (" " if index + 3 < len(words) else "")
                    chunk = {"choices": [{"delta": {"content": piece}, "index": 0}]}
                    yield f"data: {json.dumps(chunk)}\n\n"
                    await asyncio.sleep(0.01)
                yield 'data: {"choices":[{"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":7,"completion_tokens":9}}\n\n'
            else:
                words = NORMAL_REPLY.split(" ")
                for index in range(0, len(words), 4):
                    piece = " ".join(words[index : index + 4]) + (" " if index + 4 < len(words) else "")
                    chunk = {"choices": [{"delta": {"content": piece}, "index": 0}]}
                    yield f"data: {json.dumps(chunk)}\n\n"
                    await asyncio.sleep(0.01)
                yield 'data: {"choices":[{"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":42,"completion_tokens":21}}\n\n'
            yield "data: [DONE]\n\n"
        except GeneratorExit:
            # Fermeture du corps (client amont parti) : constatée par le
            # prochain envoi, jamais silencieusement ignorée.
            _note_disconnect("GeneratorExit")
            raise
        except asyncio.CancelledError:
            _note_disconnect("CancelledError")
            raise
        except Exception:
            _note_disconnect("Exception")
            raise
        finally:
            watcher.cancel()

    return StreamingResponse(stream(), media_type="text/event-stream")


@app.get("/v1/models")
def models() -> dict:
    return {"data": [{"id": "fake-model", "object": "model", "created": int(time.time())}]}
