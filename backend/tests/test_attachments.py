"""Pièces jointes : validation type/taille, isolation, téléchargement authentifié."""
from __future__ import annotations

import io

import httpx
import pytest

from tests.conftest import login


def _conversation(client, csrf) -> str:
    response = client.post("/api/conversations", json={}, headers={"X-CSRF-Token": csrf})
    assert response.status_code == 201
    return response.json()["id"]


def _png_bytes(width: int = 10, height: int = 10) -> bytes:
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (width, height), (30, 120, 110)).save(buffer, format="PNG")
    return buffer.getvalue()


def _pdf_bytes(pages: int = 1) -> bytes:
    from reportlab.pdfgen import canvas as rl_canvas

    buffer = io.BytesIO()
    pdf = rl_canvas.Canvas(buffer)
    for index in range(pages):
        pdf.drawString(72, 720, f"Document de test page {index + 1}")
        pdf.showPage()
    pdf.save()
    return buffer.getvalue()


def _upload(client, csrf, conversation_id: str, name: str, data: bytes):
    return client.post(
        f"/api/conversations/{conversation_id}/attachments",
        files={"file": (name, data, "application/octet-stream")},
        headers={"X-CSRF-Token": csrf},
    )


def test_text_attachment_with_hostile_filename(client, admin):
    csrf = login(client, admin)
    conversation_id = _conversation(client, csrf)
    response = _upload(client, csrf, conversation_id, "../../evil script.log", b"ligne de journal\nautre ligne\n")
    assert response.status_code == 201, response.text
    payload = response.json()
    assert payload["kind"] == "text"
    assert "/" not in payload["filename"] and ".." not in payload["filename"]
    assert payload["has_extracted_text"] is True

    listing = client.get(f"/api/conversations/{conversation_id}/attachments").json()["attachments"]
    assert len(listing) == 1

    content = client.get(f"/api/attachments/{payload['id']}/content")
    assert content.status_code == 200
    assert content.headers["x-content-type-options"] == "nosniff"
    assert "attachment" in content.headers["content-disposition"]


def test_image_attachment_and_limits(client, admin):
    csrf = login(client, admin)
    conversation_id = _conversation(client, csrf)
    ok = _upload(client, csrf, conversation_id, "capture.png", _png_bytes(20, 20))
    assert ok.status_code == 201, ok.text
    assert ok.json()["kind"] == "image"

    # Au-delà du plafond de pixels (abaissé pour le test à 1 MP).
    too_big = _upload(client, csrf, conversation_id, "enorme.png", _png_bytes(1200, 1200))
    assert too_big.status_code == 413

    # Image illisible
    broken = _upload(client, csrf, conversation_id, "casse.png", b"\x89PNG\r\n\x1a\npas-une-image")
    assert broken.status_code == 415

    inline = client.get(f"/api/attachments/{ok.json()['id']}/content?inline=1")
    assert inline.status_code == 200
    assert "inline" in inline.headers["content-disposition"]


def test_pdf_attachment_and_unsupported_types(client, admin):
    csrf = login(client, admin)
    conversation_id = _conversation(client, csrf)
    pdf = _upload(client, csrf, conversation_id, "doc.pdf", _pdf_bytes(2))
    assert pdf.status_code == 201, pdf.text
    assert pdf.json()["pages"] == 2

    fake_pdf = _upload(client, csrf, conversation_id, "faux.pdf", b"%PDF-1.7 contenu bidon non valide")
    assert fake_pdf.status_code == 415

    executable = _upload(client, csrf, conversation_id, "binaire.exe", b"MZ\x90\x00\x03\x00\x00\x00")
    assert executable.status_code == 415

    html = _upload(client, csrf, conversation_id, "page.html", b"<html><script>alert(1)</script></html>")
    assert html.status_code == 415


def test_oversize_text_rejected(client, admin):
    csrf = login(client, admin)
    conversation_id = _conversation(client, csrf)
    response = _upload(client, csrf, conversation_id, "enorme.txt", b"a" * (1024 * 1024 + 10))
    assert response.status_code == 413


def test_attachment_isolation_between_conversations_and_users(client, admin, other_user):
    csrf = login(client, admin)
    first = _conversation(client, csrf)
    second = _conversation(client, csrf)
    upload = _upload(client, csrf, first, "note.txt", b"contenu confidentiel")
    assert upload.status_code == 201
    attachment_id = upload.json()["id"]

    with httpx.Client(base_url=str(client.base_url), timeout=30.0) as other:
        login(other, other_user)
        assert other.get(f"/api/attachments/{attachment_id}/content").status_code == 404
        assert other.delete(f"/api/attachments/{attachment_id}", headers={"X-CSRF-Token": "x"}).status_code in (403, 404)

    # La suppression fonctionne pour le propriétaire.
    assert client.delete(f"/api/attachments/{attachment_id}", headers={"X-CSRF-Token": csrf}).status_code == 200
    assert client.get(f"/api/attachments/{attachment_id}/content").status_code == 404


def test_attachment_delete_removes_file(client, admin, settings):
    csrf = login(client, admin)
    conversation_id = _conversation(client, csrf)
    upload = _upload(client, csrf, conversation_id, "note.txt", b"quelque chose")
    attachment_id = upload.json()["id"]
    from app.db import session_scope
    from app.models import Attachment

    with session_scope() as db:
        stored = db.get(Attachment, __import__("uuid").UUID(attachment_id))
        path = settings.uploads_dir / stored.stored_relpath
    assert path.is_file()
    client.delete(f"/api/attachments/{attachment_id}", headers={"X-CSRF-Token": csrf})
    assert not path.exists()
