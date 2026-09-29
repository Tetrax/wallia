"""Continuité du cas côté chat : PJ des tours précédents, déclarations
explicites conservées avec provenance, et configuration de recherche
réellement appliquée par le chat.
"""
from __future__ import annotations

import json

import httpx
import pytest

from tests.conftest import login
from tests.test_chat_stream import configure_provider, new_conversation, read_frames


@pytest.fixture()
def upstream(fake_upstream):
    with httpx.Client(base_url=fake_upstream, timeout=10.0) as client:
        client.post("/mode", json={"mode": "normal"})
        yield client


def upload_text(client, csrf: str, conversation_id: str, name: str, content: str) -> str:
    response = client.post(
        f"/api/conversations/{conversation_id}/attachments",
        files={"file": (name, content.encode("utf-8"), "text/plain")},
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 201
    return response.json()["id"]


def run_chat(client, csrf: str, conversation_id: str, payload: dict) -> list[tuple[str, dict]]:
    with client.stream(
        "POST", f"/api/conversations/{conversation_id}/chat", json=payload, headers={"X-CSRF-Token": csrf}
    ) as response:
        return read_frames(response)


def test_previous_case_attachments_are_included_in_later_turns(client, admin, upstream):
    csrf = login(client, admin)
    configure_provider(client, csrf, str(upstream.base_url))
    conversation_id = new_conversation(client, csrf)
    attachment_id = upload_text(client, csrf, conversation_id, "journal.log", "AMBRE-CODE-42 voyant intermittent")
    other_conversation = new_conversation(client, csrf)
    other_attachment = upload_text(client, csrf, other_conversation, "autre.log", "AUTRE-CAS-99 ne doit pas fuir")

    # Tour 1 : la PJ est jointe explicitement.
    frames = run_chat(
        client, csrf, conversation_id, {"text": "Voici le journal du cas.", "attachment_ids": [attachment_id]}
    )
    assert frames[-1][0] == "done" and frames[-1][1]["status"] == "complete"
    state1 = upstream.get("/state").json()
    assert "AMBRE-CODE-42" in state1["last_prompt"]
    assert "AMBRE-CODE-42" not in state1["last_system"]

    # Tour 2 : aucune PJ renvoyée par l'utilisateur — la PJ du cas reste liée.
    run_chat(client, csrf, conversation_id, {"text": "Et maintenant, que faut-il vérifier ?"})
    state2 = upstream.get("/state").json()
    assert "AMBRE-CODE-42" in state2["last_prompt"], "la PJ du cas doit rester présente au tour suivant"
    assert "AMBRE-CODE-42" not in state2["last_system"]
    # Le système est composé uniquement de règles statiques : identique entre deux tours.
    assert state2["last_system"] == state1["last_system"]
    assert "AUTRE-CAS-99" not in state2["last_prompt"], "une PJ d'un autre cas ne doit jamais apparaître"

    # Le cas voisin ne voit jamais le journal du premier cas.
    run_chat(client, csrf, other_conversation, {"text": "Ici, que dit le journal ?", "attachment_ids": [other_attachment]})
    state3 = upstream.get("/state").json()
    assert "AUTRE-CAS-99" in state3["last_prompt"]
    assert "AMBRE-CODE-42" not in state3["last_prompt"]


def test_explicit_version_is_kept_with_provenance_and_ui_reloads_it(client, admin):
    csrf = login(client, admin)
    conversation_id = new_conversation(client, csrf)
    frames = run_chat(client, csrf, conversation_id, {"text": "Le voyant ambre clignote, version 10.10."})
    assert frames[-1][0] == "done"

    detail = client.get(f"/api/conversations/{conversation_id}").json()
    state = detail["case_state"]
    assert state["version"] == "10.10"
    fact = next(f for f in state["facts"] if "10.10" in f["text"])
    assert fact["origin"] == "user_message"
    assert fact["message_id"], "provenance exigée : la déclaration vient d'un message identifié"
    # Relecture après une nouvelle ouverture de conversation (persisté, pas seulement en mémoire).
    again = client.get(f"/api/conversations/{conversation_id}").json()
    assert again["case_state"]["version"] == "10.10"


def test_speculative_words_are_not_promoted_to_case_state(client, admin):
    csrf = login(client, admin)
    conversation_id = new_conversation(client, csrf)
    run_chat(client, csrf, conversation_id, {"text": "Je crois que c'est la 11 ou la 12, pas sûr du tout."})
    detail = client.get(f"/api/conversations/{conversation_id}").json()
    assert detail["case_state"]["version"] is None
    assert detail["case_state"]["facts"] == []


def test_chat_retrieval_uses_the_reranked_path_with_case_filters(client, admin, settings, monkeypatch):
    """Le chat passe par le chemin RÉEL (SQL + reclassement) avec les filtres du
    cas ; le reclassement fixture remplace l'ancienne configuration cosmétique."""
    from app.routers import chat as chat_module
    from tests.test_jobs import _pdf_bytes, _worker, fake_docling
    from app.worker import Worker

    csrf = login(client, admin)
    # Un document indexé est nécessaire pour que la recherche soit exercée.
    document = client.post(
        "/api/documents",
        files={"file": ("guide.pdf", _pdf_bytes(), "application/pdf")},
        data={"title": "Guide", "scope": "demo"},
        headers={"X-CSRF-Token": csrf},
    )
    assert document.status_code == 201
    monkeypatch.setattr(Worker, "run_docling", fake_docling())
    assert _worker(settings).run_once() is True

    captured: dict = {}
    real = chat_module.hybrid_search

    def spy(db, effective, **kwargs):  # noqa: ANN001
        captured.update(kwargs)
        captured["settings_backend"] = effective.reranker_backend
        return real(db, effective, **kwargs)

    monkeypatch.setattr(chat_module, "hybrid_search", spy)
    conversation_id = new_conversation(client, csrf)
    frames = run_chat(client, csrf, conversation_id, {"text": "voyant ambre"})
    assert frames[-1][0] == "done"
    assert captured.get("query") == "voyant ambre"
    assert captured.get("product") is None and captured.get("version") is None
    assert captured.get("settings_backend") == "fixture"  # test déclaré, jamais la prod


def test_retrieval_unavailable_is_propagated_without_fake_sources(client, admin, monkeypatch):
    """Une panne du reclassement remonte au chat telle quelle : statut
    `retrieval_unavailable`, AUCUNE source factice, message honnête — jamais
    présentée comme une absence de source ni comme un corpus vide."""
    from app.routers import chat as chat_module

    csrf = login(client, admin)
    monkeypatch.setattr(chat_module, "_ready_corpus_exists", lambda db: True)

    def unavailable(db, settings, **kwargs):  # noqa: ANN001
        return {
            "status": "retrieval_unavailable",
            "sources": [],
            "diagnostics": {
                "error": {
                    "type": "reranker_busy",
                    "message": "reclassement documentaire saturé (une inférence est déjà en cours)",
                }
            },
        }

    monkeypatch.setattr(chat_module, "hybrid_search", unavailable)
    conversation_id = new_conversation(client, csrf)
    frames = run_chat(client, csrf, conversation_id, {"text": "voyant ambre"})

    sources_frames = [data for event, data in frames if event == "sources"]
    assert sources_frames and sources_frames[0]["status"] == "retrieval_unavailable"
    assert sources_frames[0]["sources"] == []
    assert sources_frames[0]["diagnostics"]["error"]["type"] == "reranker_busy"
    demo_text = "".join(data.get("text", "") for event, data in frames if event == "delta")
    assert "indisponible" in demo_text.lower()
    assert "aucun document indexé" not in demo_text
    assert frames[-1][0] == "done"


# ---------------------------------------------------------------------------
# Déclarations appliquées AU TOUR COURANT (avant le retrieval) — jamais au tour
# suivant, jamais l'autre version, jamais une substitution.
# ---------------------------------------------------------------------------
def _spy_retrieval_filters(monkeypatch) -> list[dict]:
    """Capture les filtres réellement passés au retrieval réel, appel par appel."""
    from app.routers import chat as chat_module

    calls: list[dict] = []
    monkeypatch.setattr(chat_module, "_ready_corpus_exists", lambda db: True)

    def spy(db, settings, **kwargs):  # noqa: ANN001
        calls.append(
            {
                "product": kwargs.get("product"),
                "version": kwargs.get("version"),
                "query": kwargs.get("query"),
            }
        )
        return {
            "status": "no_relevant_source",
            "sources": [],
            "diagnostics": {"filters": {"product": kwargs.get("product"), "version": kwargs.get("version")}},
        }

    monkeypatch.setattr(chat_module, "hybrid_search", spy)
    return calls


def test_first_turn_declaration_applies_to_the_current_retrieval(client, admin, monkeypatch):
    """« Aster 10.10 » au PREMIER message : le retrieval du même tour filtre
    déjà sur ce produit/version — plus besoin d'attendre le tour suivant."""
    from app.db import session_scope
    from app.models import Document

    csrf = login(client, admin)
    # Produit connu du corpus (nécessaire à l'extraction « Aster 10.10 »).
    with session_scope() as db:
        db.add(
            Document(
                title="Guide produit (démo)",
                product="Aster",
                versions=["10.10"],
                checksum_sha256="0" * 64,
                stored_relpath="demo/guide.pdf",
                original_filename="guide.pdf",
                status="ready",
                scope="demo",
                demo=True,
                current_generation=1,
            )
        )
    calls = _spy_retrieval_filters(monkeypatch)
    conversation_id = new_conversation(client, csrf)
    frames = run_chat(
        client, csrf, conversation_id, {"text": "Sur le produit démo Aster 10.10, le voyant ambre clignote."}
    )
    assert frames[-1][0] == "done"
    assert calls[0]["product"] == "Aster"
    assert calls[0]["version"] == "10.10"

    # Provenance persistée avec l'ID du message, dans l'état relu.
    detail = client.get(f"/api/conversations/{conversation_id}").json()
    state = detail["case_state"]
    assert state["product"] == "Aster" and state["version"] == "10.10"
    for fact in state["facts"]:
        assert fact["origin"] == "user_message" and fact["message_id"], "provenance exigée dès le premier tour"


def test_explicit_correction_applies_to_the_current_turn_and_never_the_other_version(client, admin, monkeypatch):
    """Correction explicite 10.9 → 10.10 : le tour de correction cherche avec
    10.10 (jamais 10.9) ; une nouvelle déclaration neutre ne remplace pas."""
    csrf = login(client, admin)
    calls = _spy_retrieval_filters(monkeypatch)
    conversation_id = new_conversation(client, csrf)
    run_chat(client, csrf, conversation_id, {"text": "La version 10.9 est installée sur le produit démo Aster."})
    assert calls[0]["version"] == "10.9"

    run_chat(client, csrf, conversation_id, {"text": "En fait je me suis trompé, c'était la version 10.10."})
    assert calls[1]["version"] == "10.10", "la correction doit s'appliquer au tour COURANT"

    # Message avec une autre version mais SANS marqueur de correction : aucune
    # promotion, le champ courant reste 10.10 (jamais l'autre version).
    run_chat(client, csrf, conversation_id, {"text": "Peux-tu vérifier le journal de la version 11 ?"})
    assert calls[2]["version"] == "10.10"
    detail = client.get(f"/api/conversations/{conversation_id}").json()
    assert detail["case_state"]["version"] == "10.10"


def test_unknown_version_is_never_substituted(client, admin, monkeypatch):
    """Version inconnue (99.99) déclarée au premier tour : elle est appliquée
    telle quelle au retrieval — aucune substitution par une version connue."""
    csrf = login(client, admin)
    calls = _spy_retrieval_filters(monkeypatch)
    conversation_id = new_conversation(client, csrf)
    run_chat(client, csrf, conversation_id, {"text": "La version 99.99 est installée, le voyant ambre clignote."})
    assert calls[0]["version"] == "99.99"
    detail = client.get(f"/api/conversations/{conversation_id}").json()
    assert detail["case_state"]["version"] == "99.99"


def test_retry_keeps_case_state_and_does_not_duplicate_declarations(client, admin, monkeypatch):
    """Une relance ne rejoue pas l'extraction : l'état du cas est conservé tel
    quel et aucune déclaration parallèle n'est créée."""
    csrf = login(client, admin)
    calls = _spy_retrieval_filters(monkeypatch)
    conversation_id = new_conversation(client, csrf)
    run_chat(client, csrf, conversation_id, {"text": "La version 10.10 est installée."})
    detail = client.get(f"/api/conversations/{conversation_id}").json()
    assistant_id = [m for m in detail["messages"] if m["role"] == "assistant"][-1]["id"]
    # Force la relance : le message doit être dans un état relançable.
    from app.db import session_scope
    from app.models import Message as MessageModel

    with session_scope() as db:
        message = db.get(MessageModel, assistant_id)
        message.status = "cancelled"
    with client.stream(
        "POST", f"/api/messages/{assistant_id}/retry", headers={"X-CSRF-Token": csrf}
    ) as response:
        frames = read_frames(response)
    assert frames[-1][0] == "done"
    assert calls[-1]["version"] == "10.10"
    detail = client.get(f"/api/conversations/{conversation_id}").json()
    facts = [f for f in detail["case_state"]["facts"] if "10.10" in f["text"]]
    assert len(facts) == 1, "aucune déclaration dupliquée par une relance"


def test_uncertain_declarations_never_reach_retrieval_nor_become_confirmed_facts(client, admin, monkeypatch):
    """Chat de bout en bout isolé : un message ambigu/incertain ne produit
    AUCUN filtre version au retrieval réel et AUCUN fait « confirmed »
    incertain persisté — le produit explicitement certain reste appliqué."""
    from app.db import session_scope
    from app.models import Document

    csrf = login(client, admin)
    with session_scope() as db:
        db.add(
            Document(
                title="Guide produit (démo)",
                product="Aster",
                versions=["10.10"],
                checksum_sha256="0" * 64,
                stored_relpath="demo/guide.pdf",
                original_filename="guide.pdf",
                status="ready",
                scope="demo",
                demo=True,
                current_generation=1,
            )
        )
    calls = _spy_retrieval_filters(monkeypatch)
    conversation_id = new_conversation(client, csrf)

    # Tour 1 : ambiguïté entre deux versions (« version 10.9 ou 10.10 »).
    frames = run_chat(
        client, csrf, conversation_id, {"text": "Je ne sais pas si mon Aster est en version 10.9 ou 10.10."}
    )
    assert frames[-1][0] == "done"
    assert calls[0]["version"] is None, f"filtre version ambigu envoyé au retrieval : {calls[0]['version']!r}"
    assert calls[0]["product"] == "Aster"
    detail = client.get(f"/api/conversations/{conversation_id}").json()
    state = detail["case_state"]
    assert state["version"] is None
    assert not any("10.9" in f["text"] for f in state["facts"]), f"fait incertain persisté : {state['facts']}"

    # Tour 2 : incertitude sur le couple produit/version — rien de nouveau.
    run_chat(client, csrf, conversation_id, {"text": "Je ne suis pas sur Aster 10.9."})
    assert calls[1]["version"] is None, f"filtre version incertain envoyé : {calls[1]['version']!r}"
    detail = client.get(f"/api/conversations/{conversation_id}").json()
    state = detail["case_state"]
    assert state["version"] is None
    assert not any("10.9" in f["text"] for f in state["facts"]), f"fait incertain persisté : {state['facts']}"
    assert state["product"] == "Aster", "le produit certain du tour 1 doit rester (aucune suppression à tort)"
