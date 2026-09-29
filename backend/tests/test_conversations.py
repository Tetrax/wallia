"""Conversations, messages, état de cas, isolation entre comptes."""
from __future__ import annotations

import httpx

from tests.conftest import login


def _create(client, csrf, title: str | None = "Cas test") -> dict:
    response = client.post("/api/conversations", json={"title": title}, headers={"X-CSRF-Token": csrf})
    assert response.status_code == 201, response.text
    return response.json()


def test_conversation_crud(client, admin):
    csrf = login(client, admin)
    conversation = _create(client, csrf)
    assert conversation["case_state"]["product"] is None

    listing = client.get("/api/conversations").json()["conversations"]
    assert [c["id"] for c in listing] == [conversation["id"]]

    detail = client.get(f"/api/conversations/{conversation['id']}").json()
    assert detail["messages"] == []
    assert detail["attachments"] == []

    renamed = client.patch(
        f"/api/conversations/{conversation['id']}", json={"title": "Renommée"}, headers={"X-CSRF-Token": csrf}
    )
    assert renamed.status_code == 200
    assert renamed.json()["title"] == "Renommée"

    deleted = client.delete(f"/api/conversations/{conversation['id']}", headers={"X-CSRF-Token": csrf})
    assert deleted.status_code == 200
    assert client.get(f"/api/conversations/{conversation['id']}").status_code == 404


def test_rename_validation(client, admin):
    csrf = login(client, admin)
    conversation = _create(client, csrf)
    assert (
        client.patch(
            f"/api/conversations/{conversation['id']}", json={"title": ""}, headers={"X-CSRF-Token": csrf}
        ).status_code
        == 422
    )
    long_title = client.patch(
        f"/api/conversations/{conversation['id']}", json={"title": "x" * 500}, headers={"X-CSRF-Token": csrf}
    )
    assert long_title.status_code == 422


def test_case_state_roundtrip_and_provenance(client, admin):
    csrf = login(client, admin)
    conversation = _create(client, csrf)
    payload = {
        "product": "Aster",
        "version": "10.10",
        "symptom": "Voyant ambre",
        "facts": [
            {"text": "Voyant ambre depuis 2 h", "status": "confirmed", "origin": "user_message"},
        ],
        "hypotheses": [{"text": "Saturation disque", "status": "proposed", "origin": "assistant"}],
        "proposed_checks": [{"text": "Lire le journal local", "status": "proposed", "origin": "assistant"}],
        "performed_checks": [],
        "results": [],
        "missing_info": [{"text": "Version du micrologiciel", "status": "missing", "origin": "user_explicit"}],
    }
    response = client.patch(
        f"/api/conversations/{conversation['id']}/case_state", json=payload, headers={"X-CSRF-Token": csrf}
    )
    assert response.status_code == 200, response.text
    state = response.json()["case_state"]
    assert state["product"] == "Aster"
    assert state["version"] == "10.10"
    assert state["proposed_checks"][0]["status"] == "proposed"

    # Provenir d'origine conservée : renvoi avec origin modifié par un client hostile.
    payload["hypotheses"][0]["id"] = state["hypotheses"][0]["id"]
    payload["hypotheses"][0]["origin"] = "user_explicit"
    payload["hypotheses"][0]["status"] = "confirmed"
    second = client.patch(
        f"/api/conversations/{conversation['id']}/case_state", json=payload, headers={"X-CSRF-Token": csrf}
    )
    assert second.status_code == 200
    item = second.json()["case_state"]["hypotheses"][0]
    assert item["origin"] == "assistant"  # provenance d'origine conservée
    assert item["status"] == "confirmed"  # choix explicite de l'utilisateur accepté


def test_case_state_rejects_invalid_version(client, admin):
    csrf = login(client, admin)
    conversation = _create(client, csrf)
    response = client.patch(
        f"/api/conversations/{conversation['id']}/case_state",
        json={"version": "la plus récente !!"},
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 422


def test_messages_endpoint_empty(client, admin):
    csrf = login(client, admin)
    conversation = _create(client, csrf)
    response = client.get(f"/api/conversations/{conversation['id']}/messages")
    assert response.status_code == 200
    assert response.json()["messages"] == []


def test_conversation_isolation_between_users(client, admin, other_user):
    csrf = login(client, admin)
    conversation = _create(client, csrf, "dossier sensible")
    with httpx.Client(base_url=str(client.base_url), timeout=30.0) as second:
        csrf2 = login(second, other_user)
        mine = second.get("/api/conversations").json()["conversations"]
        assert mine == []
        assert second.get(f"/api/conversations/{conversation['id']}/messages").status_code == 404
        assert (
            second.patch(
                f"/api/conversations/{conversation['id']}/case_state",
                json={"product": "X"},
                headers={"X-CSRF-Token": csrf2},
            ).status_code
            == 404
        )
        assert (
            second.delete(f"/api/conversations/{conversation['id']}", headers={"X-CSRF-Token": csrf2}).status_code
            == 404
        )


def test_source_availability_is_resolved_without_renumbering(client, admin):
    """Citations historiques : excerpt conservé, document disparu marqué
    indisponible, numéros de source jamais renumérotés."""
    import uuid

    from sqlalchemy import text

    from app.db import session_scope
    from app.models import Message
    from tests.test_jobs import import_document

    csrf = login(client, admin)
    conversation = _create(client, csrf, "Cas citations")
    document_id = import_document(client, csrf, title="Guide EN rotation").json()["id"]
    with session_scope() as db:
        db.add(
            Message(
                conversation_id=uuid.UUID(conversation["id"]),
                seq=1,
                role="assistant",
                content="Réponse sourcée [1].",
                status="complete",
                sources=[
                    {
                        "index": 1,
                        "chunk_id": "chunk-1",
                        "document_id": document_id,
                        "title": "Guide EN rotation",
                        "page_start": 2,
                        "versions": ["10.10"],
                        "product": "Aster",
                        "text": "Log rotation keeps seven days of entries.",
                    }
                ],
            )
        )
    messages = client.get(f"/api/conversations/{conversation['id']}/messages").json()["messages"]
    source = messages[0]["sources"][0]
    assert source["available"] is True
    assert source["index"] == 1

    with session_scope() as db:
        db.execute(text("DELETE FROM documents WHERE id = :id"), {"id": document_id})

    messages = client.get(f"/api/conversations/{conversation['id']}/messages").json()["messages"]
    source = messages[0]["sources"][0]
    assert source["available"] is False
    assert source["text"] == "Log rotation keeps seven days of entries."
    assert source["index"] == 1
    assert source["page_start"] == 2
