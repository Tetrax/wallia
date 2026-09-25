"""Administration documentaire : import, métadonnées, téléchargements, jobs."""
from __future__ import annotations

import io

import httpx

from tests.conftest import login


_PDF_CACHE: dict[int, bytes] = {}


def _pdf_bytes(pages: int = 2) -> bytes:
    """PDF fictif reproductible : mêmes octets (donc même empreinte) entre appels."""
    if pages not in _PDF_CACHE:
        from reportlab.pdfgen import canvas as rl_canvas

        buffer = io.BytesIO()
        pdf = rl_canvas.Canvas(buffer)
        for index in range(pages):
            pdf.drawString(72, 720, f"page {index + 1}")
            pdf.showPage()
        pdf.save()
        _PDF_CACHE[pages] = buffer.getvalue()
    return _PDF_CACHE[pages]


def _import(client, csrf, *, title="Guide fictif", versions="10.10", scope="demo", pages=2):
    return client.post(
        "/api/documents",
        files={"file": (f"{title}.pdf", _pdf_bytes(pages), "application/pdf")},
        data={
            "title": title,
            "origin": "fixture",
            "product": "Aster",
            "versions": versions,
            "language": "fr",
            "demo": "1" if scope == "demo" else "0",
            "scope": scope,
        },
        headers={"X-CSRF-Token": csrf},
    )


def test_import_validation_and_filters(client, admin):
    csrf = login(client, admin)
    created = _import(client, csrf, title="Guide démo 10.10")
    assert created.status_code == 201
    document_id = created.json()["id"]

    assert client.post("/api/documents", files={"file": ("x.txt", b"pas un pdf")}, data={"title": "x"}, headers={"X-CSRF-Token": csrf}).status_code == 415
    assert _import(client, csrf, title="trop long", pages=101).status_code == 413
    bad_version = client.post(
        "/api/documents",
        files={"file": ("ok.pdf", _pdf_bytes(1), "application/pdf")},
        data={"title": "version invalide", "versions": "10.10 beta!!"},
        headers={"X-CSRF-Token": csrf},
    )
    assert bad_version.status_code == 422

    listing = client.get("/api/documents").json()["documents"]
    assert len(listing) == 1
    assert listing[0]["id"] == document_id
    assert listing[0]["versions"] == ["10.10"]
    assert listing[0]["status"] == "queued"

    assert client.get("/api/documents", params={"scope": "official"}).json()["documents"] == []
    assert client.get("/api/documents", params={"q": "introuvable"}).json()["documents"] == []
    assert client.get("/api/documents", params={"q": "démo"}).json()["documents"][0]["id"] == document_id


def test_patch_metadata_and_scope_conflict(client, admin):
    csrf = login(client, admin)
    document_id = _import(client, csrf, title="Guide", scope="demo").json()["id"]

    patched = client.patch(
        f"/api/documents/{document_id}",
        json={"title": "Guide corrigé", "versions": ["10.9", "10.10"], "origin": "fixture v2"},
        headers={"X-CSRF-Token": csrf},
    )
    assert patched.status_code == 200
    assert patched.json()["versions"] == ["10.9", "10.10"]

    invalid = client.patch(
        f"/api/documents/{document_id}", json={"versions": ["10.10 !!"]}, headers={"X-CSRF-Token": csrf}
    )
    assert invalid.status_code == 422

    # Le même contenu importé en périmètre officiel est autorisé…
    duplicate_official = _import(client, csrf, title="Guide officiel", scope="official")
    assert duplicate_official.status_code == 201
    # … mais basculer le périmètre créerait un doublon : refusé.
    conflict = client.patch(
        f"/api/documents/{duplicate_official.json()['id']}",
        json={"scope": "demo"},
        headers={"X-CSRF-Token": csrf},
    )
    assert conflict.status_code == 409


def test_original_download_is_authenticated(client, admin, other_user):
    csrf = login(client, admin)
    document_id = _import(client, csrf).json()["id"]

    assert client.get(f"/api/documents/{document_id}/original").status_code == 200  # admin connecté
    response = client.get(f"/api/documents/{document_id}/original")
    assert response.headers["content-type"] == "application/pdf"
    assert "inline" in response.headers["content-disposition"]

    anonymous = httpx.Client(base_url=str(client.base_url), timeout=30.0)
    assert anonymous.get(f"/api/documents/{document_id}/original").status_code == 401
    anonymous.close()

    with httpx.Client(base_url=str(client.base_url), timeout=30.0) as second:
        login(second, other_user)
        # Décision : un utilisateur authentifié peut ouvrir l'original d'un
        # document déjà cité (les extraits lui sont visibles) ; la gestion du
        # corpus (import/métadonnées/suppression) reste réservée aux admins.
        assert second.get(f"/api/documents/{document_id}/original").status_code == 200


def test_chunks_and_jobs_endpoints(client, admin):
    csrf = login(client, admin)
    document_id = _import(client, csrf).json()["id"]
    chunks = client.get(f"/api/documents/{document_id}/chunks").json()
    assert chunks["chunks"] == []
    assert chunks["generation"] == 0

    jobs = client.get(f"/api/documents/{document_id}/jobs").json()["jobs"]
    assert len(jobs) == 1 and jobs[0]["kind"] == "ingest"
    assert client.get("/api/jobs", params={"status": "queued"}).json()["jobs"]

    retry_ok_job = client.post(f"/api/jobs/{jobs[0]['id']}/retry", headers={"X-CSRF-Token": csrf})
    assert retry_ok_job.status_code == 409  # seuls les jobs échoués sont relançables


def test_non_admin_cannot_touch_corpus_or_settings(client, admin, other_user):
    with httpx.Client(base_url=str(client.base_url), timeout=30.0) as second:
        csrf = login(second, other_user)
        assert second.get("/api/documents").status_code == 403
        assert second.get("/api/jobs").status_code == 403
        assert second.get("/api/settings").status_code == 403
        assert (
            second.put("/api/settings", json={"provider_model": "x"}, headers={"X-CSRF-Token": csrf}).status_code
            == 403
        )
        assert second.post("/api/settings/test", headers={"X-CSRF-Token": csrf}).status_code == 403
