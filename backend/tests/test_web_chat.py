"""Chat — repli web borné : statuts SSE RÉELS et notes réellement transmises
au fournisseur (messages capturés par le double), sans réseau réel.

Le corpus est vide dans ces cas (aucun document indexé) : le repli est autorisé
sur `empty_corpus`, et l'indépendance du statut corpus est vérifiée. Les doubles
HTTP (fournisseur NATIF simulé et Firecrawl) et DNS sont partagés avec
`tests.test_web` — aucun appel réseau réel.

NON EXÉCUTÉ à l'écriture de ce lot : le principal exécute la suite après revue.
"""
from __future__ import annotations

import json

import httpx
import pytest

from tests.conftest import login

# Doubles et helpers partagés (fixtures importées : pytest les enregistre).
from tests.test_web import (  # noqa: F401
    ALLOWED_SOURCE_URL,
    _activate_web_operator,
    _item,
    dns_public,
    firecrawl_double,
    firecrawl_key,
    web_env_enabled,
    web_mp_fork,
)


@pytest.fixture()
def upstream(fake_upstream):
    with httpx.Client(base_url=fake_upstream, timeout=10.0) as client:
        client.post("/mode", json={"mode": "normal"})
        yield client
        client.post("/mode", json={"mode": "normal"})


def configure_provider(client, csrf: str, base: str) -> None:
    response = client.put(
        "/api/settings",
        json={
            "provider_endpoint": f"{base}/v1",
            "provider_model": "fake-model",
            "api_key": "cle-de-test-locale",
        },
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 200, response.text


def new_conversation(client, csrf: str) -> str:
    return client.post(
        "/api/conversations", json={"title": "Cas web"}, headers={"X-CSRF-Token": csrf}
    ).json()["id"]


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


def _sources_frame(frames: list[tuple[str, dict]]) -> dict:
    sources_frames = [data for kind, data in frames if kind == "sources"]
    assert sources_frames, "frame SSR « sources » manquant"
    return sources_frames[0]


def test_chat_web_ok_transmits_notes_and_sources_and_keeps_meta(
    client, admin, upstream, firecrawl_double, firecrawl_key, web_env_enabled, dns_public, web_mp_fork
):
    _activate_web_operator()
    firecrawl_double.items = [_item()]
    csrf = login(client, admin)
    configure_provider(client, csrf, str(upstream.base_url))
    conversation_id = new_conversation(client, csrf)
    with client.stream(
        "POST",
        f"/api/conversations/{conversation_id}/chat",
        json={"text": "Le voyant du Bastion et le journal de rotation, version 10.10"},
        headers={"X-CSRF-Token": csrf},
    ) as response:
        assert response.status_code == 200, response.text
        frames = read_frames(response)
    assert frames[-1][0] == "done" and frames[-1][1]["status"] == "complete"

    # Métadonnée SSE `web` : statut opérationnel distinct du statut corpus.
    sources_frame = _sources_frame(frames)
    meta = sources_frame["web"]
    assert meta["status"] == "ok"
    assert meta["query"].startswith("site:wallix.com 10.10")
    assert "voyant" in meta["query"] and "journal" in meta["query"]
    # La version validée du tour est utilisée ; le texte brut ne sort jamais.
    assert "bastion" not in meta["query"].lower()

    web_sources = [s for s in sources_frame["sources"] if s.get("source_type") == "web"]
    assert web_sources, "source web absente du frame sources"
    assert web_sources[0]["url"] == ALLOWED_SOURCE_URL
    assert web_sources[0]["version_state"] == "non_verifiee"
    assert web_sources[0]["document_id"] is None and web_sources[0]["score"] is None
    assert sources_frame["status"] == "empty_corpus"  # statut corpus indépendant

    # Les sources persistées conservent la provenance web (relecture).
    messages = client.get(f"/api/conversations/{conversation_id}/messages").json()["messages"]
    persisted = messages[-1]["sources"] or []
    assert any(s.get("source_type") == "web" for s in persisted)

    # Messages RÉELLEMENT envoyés au fournisseur (capturés par le double) :
    # les notes corpus ET web sont transmises, pas seulement calculées.
    state = upstream.get("/state").json()
    prompt = state["last_prompt"]
    assert "extrait_web_public" in prompt
    assert "www.wallix.com" in prompt
    assert "version non vérifiée" in prompt
    assert "Le corpus documentaire est vide" in prompt
    assert "extraits web publics" in prompt
    # Les extraits web restent des DONNÉES : jamais dans le message système.
    assert "extrait_web_public" not in state["last_system"]
    assert "wallix.com" not in state["last_system"]
    assert firecrawl_double.requests == 1

    # Le message final reste complet et attribué au fournisseur configuré.
    assert messages[-1]["status"] == "complete"
    assert messages[-1]["model"] == "fake-model"


def test_chat_web_disabled_reports_status_without_any_call(
    client, admin, upstream, firecrawl_double, firecrawl_key, dns_public, web_mp_fork
):
    """Clé présente + service joignable, mais fonction non activée : aucun appel
    web, statut `disabled` explicite — et la note corpus part quand même."""
    csrf = login(client, admin)
    configure_provider(client, csrf, str(upstream.base_url))
    conversation_id = new_conversation(client, csrf)
    with client.stream(
        "POST",
        f"/api/conversations/{conversation_id}/chat",
        json={"text": "Le journal de rotation du Bastion"},
        headers={"X-CSRF-Token": csrf},
    ) as response:
        frames = read_frames(response)
    assert frames[-1][0] == "done"

    sources_frame = _sources_frame(frames)
    meta = sources_frame["web"]
    assert meta["status"] == "disabled"
    assert "désactivé" in meta["reason"]
    assert meta["query"] is None
    assert [s for s in sources_frame["sources"] if s.get("source_type") == "web"] == []
    assert firecrawl_double.requests == 0

    prompt = upstream.get("/state").json()["last_prompt"]
    assert "Le corpus documentaire est vide" in prompt
    assert "extraits web publics" not in prompt


def test_chat_web_unavailable_is_explicit_and_corpus_status_untouched(
    client, admin, upstream, firecrawl_double, firecrawl_key, web_env_enabled, dns_public, web_mp_fork
):
    _activate_web_operator()
    firecrawl_double.mode = "http_error"
    csrf = login(client, admin)
    configure_provider(client, csrf, str(upstream.base_url))
    conversation_id = new_conversation(client, csrf)
    with client.stream(
        "POST",
        f"/api/conversations/{conversation_id}/chat",
        json={"text": "Le voyant est ambre, que vérifier ?"},
        headers={"X-CSRF-Token": csrf},
    ) as response:
        frames = read_frames(response)
    assert frames[-1][0] == "done" and frames[-1][1]["status"] == "complete"

    sources_frame = _sources_frame(frames)
    meta = sources_frame["web"]
    assert meta["status"] == "unavailable"
    assert meta["reason"] == "service HTTP 500"
    assert "sk-double" not in meta["reason"]
    assert meta["query"].startswith("site:wallix.com")
    assert [s for s in sources_frame["sources"] if s.get("source_type") == "web"] == []
    # Le statut corpus n'est ni transformé ni masqué par la panne du web.
    assert sources_frame["status"] == "empty_corpus"

    # L'indisponibilité est transmise honnêtement au modèle (jamais présentée active).
    prompt = upstream.get("/state").json()["last_prompt"]
    assert "Recherche web complémentaire indisponible" in prompt
    assert "Le corpus documentaire est vide" in prompt
