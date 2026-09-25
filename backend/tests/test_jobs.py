"""File d'ingestion durable : leases, reprises, échecs, réindexation, suppressions.

Le parseur Docling est remplacé par une sortie d'extraction contrôlée : la
mécanique testée ici (chunking → embeddings → publication → bascule de
génération) est réelle ; l'extraction Docling réelle est exercée en acceptance.
"""
from __future__ import annotations

import io
import json
import threading

from sqlalchemy import text

from app.worker import Worker
from tests.conftest import login

ITEMS = [
    {"kind": "text", "label": "section_header", "text": "Diagnostic de l'équipement Aster", "page_no": 1, "section": None},
    {
        "kind": "text",
        "label": "paragraph",
        "text": "Le voyant ambre indique une saturation du journal local sur la version 10.9. "
        "Relever le journal puis comparer avec la version 10.10.",
        "page_no": 1,
        "section": "Diagnostic de l'équipement Aster",
    },
    {
        "kind": "table",
        "label": "table",
        "text": "| Version | État |\n| --- | --- |\n| 10.9 | OK |\n| 10.10 | Dégradé |",
        "page_no": 2,
        "section": "Diagnostic de l'équipement Aster",
    },
]


_PDF_CACHE: dict[int, bytes] = {}


def _pdf_bytes(pages: int = 3) -> bytes:
    """PDF fictif reproductible : mêmes octets (donc même empreinte) entre appels."""
    if pages not in _PDF_CACHE:
        from reportlab.pdfgen import canvas as rl_canvas

        buffer = io.BytesIO()
        pdf = rl_canvas.Canvas(buffer)
        for index in range(pages):
            pdf.drawString(72, 720, f"Document fictif de test — page {index + 1}")
            pdf.showPage()
        pdf.save()
        _PDF_CACHE[pages] = buffer.getvalue()
    return _PDF_CACHE[pages]


def import_document(client, csrf, *, title: str = "Guide fictif Aster", versions: str = "10.9,10.10", scope: str = "demo", product: str = "Aster", pages: int = 3):
    return client.post(
        "/api/documents",
        files={"file": ("guide.pdf", _pdf_bytes(pages), "application/pdf")},
        data={
            "title": title,
            "origin": "fixture de test",
            "product": product,
            "versions": versions,
            "language": "fr",
            "demo": "1" if scope == "demo" else "0",
            "scope": scope,
        },
        headers={"X-CSRF-Token": csrf},
    )


def fake_docling(items=ITEMS, *, raises: Exception | None = None):
    def runner(self, input_path, work_dir):  # noqa: ANN001
        work_dir.mkdir(parents=True, exist_ok=True)
        if raises is not None:
            raise raises
        (work_dir / "extracted.json").write_text(
            json.dumps({"items": items, "pages": 3, "markdown": "extrait"}, ensure_ascii=False),
            encoding="utf-8",
        )
        return {"pages": 3, "items": len(items)}

    return runner


def _worker(settings) -> Worker:
    return Worker(settings)


def test_import_enqueues_durable_job_and_worker_publishes(client, admin, settings, monkeypatch):
    csrf = login(client, admin)
    response = import_document(client, csrf)
    assert response.status_code == 201, response.text
    payload = response.json()
    assert payload["status"] == "queued"
    assert payload["job"]["status"] == "queued"
    assert payload["job"]["kind"] == "ingest"

    monkeypatch.setattr(Worker, "run_docling", fake_docling())
    assert _worker(settings).run_once() is True

    document = client.get("/api/documents").json()["documents"][0]
    assert document["status"] == "ready"
    assert document["chunks_current"] == 2  # 1 passage texte + 1 tableau
    assert document["current_generation"] == 1

    chunks = client.get(f"/api/documents/{payload['id']}/chunks").json()["chunks"]
    assert [c["kind"] for c in chunks] == ["text", "table"]
    assert chunks[0]["page_start"] == 1
    assert chunks[1]["page_start"] == 2
    assert all(c["token_count"] > 0 for c in chunks)

    jobs = client.get("/api/jobs").json()["jobs"]
    assert jobs[0]["status"] == "succeeded"
    assert jobs[0]["attempts"] == 1

    # La recherche réelle exploite ces passages.
    search = client.post("/api/search", json={"query": "voyant ambre saturation journal"}).json()
    assert search["status"] == "ok"
    assert search["sources"], search


def test_duplicate_import_is_rejected(client, admin):
    csrf = login(client, admin)
    assert import_document(client, csrf).status_code == 201
    duplicate = import_document(client, csrf)
    assert duplicate.status_code == 409


def test_reindex_publishes_new_generation_and_keeps_old_on_failure(client, admin, settings, monkeypatch):
    csrf = login(client, admin)
    document_id = import_document(client, csrf).json()["id"]
    monkeypatch.setattr(Worker, "run_docling", fake_docling())
    assert _worker(settings).run_once() is True

    # Échec définitif de réindexation : l'ancienne génération reste servie.
    reindex = client.post(f"/api/documents/{document_id}/reindex", headers={"X-CSRF-Token": csrf})
    assert reindex.status_code == 202
    monkeypatch.setattr(Worker, "run_docling", fake_docling(raises=__import__("app.worker", fromlist=["JobAbort"]).JobAbort("PDF non pris en charge")))
    assert _worker(settings).run_once() is True

    document = client.get("/api/documents").json()["documents"][0]
    assert document["status"] == "ready"
    assert document["current_generation"] == 1
    assert document["error"] and "réindexation" in document["error"]
    assert client.post("/api/search", json={"query": "voyant ambre"}).json()["status"] == "ok"

    failing_job = client.get("/api/jobs").json()["jobs"][0]
    assert failing_job["status"] == "failed"

    # Réindexation réussie : nouvelle génération publiée, ancienne supprimée.
    client.post(f"/api/documents/{document_id}/reindex", headers={"X-CSRF-Token": csrf})
    monkeypatch.setattr(Worker, "run_docling", fake_docling())
    assert _worker(settings).run_once() is True
    document = client.get("/api/documents").json()["documents"][0]
    assert document["current_generation"] == 2
    assert document["status"] == "ready"
    chunks = client.get(f"/api/documents/{document_id}/chunks").json()
    assert chunks["generation"] == 2 and len(chunks["chunks"]) == 2

    from app.db import session_scope

    with session_scope() as db:
        old = db.execute(
            text("SELECT count(*) FROM chunks WHERE document_id = :d AND generation = 1"), {"d": document_id}
        ).scalar_one()
    assert old == 0


def test_retryable_failure_backoff_then_exhaustion(client, admin, settings, monkeypatch):
    csrf = login(client, admin)
    document_id = import_document(client, csrf).json()["id"]
    monkeypatch.setattr(Worker, "run_docling", fake_docling(raises=RuntimeError("capteur instable")))

    def force_available():
        from app.db import session_scope

        with session_scope() as db:
            db.execute(text("UPDATE ingestion_jobs SET available_at = now() WHERE document_id = :d"), {"d": document_id})

    for attempt in (1, 2):
        assert _worker(settings).run_once() is True
        job = client.get("/api/jobs").json()["jobs"][0]
        assert job["status"] == "queued"
        assert job["attempts"] == attempt
        assert job["available_at"]
        force_available()

    assert _worker(settings).run_once() is True
    job = client.get("/api/jobs").json()["jobs"][0]
    assert job["status"] == "failed"
    assert job["attempts"] == 3
    document = client.get("/api/documents").json()["documents"][0]
    assert document["status"] == "failed"
    assert "capteur instable" in (document["error"] or "")

    # Reprise manuelle bornée : une fois remise en file, elle est réexécutée.
    retry = client.post(f"/api/jobs/{job['id']}/retry", headers={"X-CSRF-Token": csrf})
    assert retry.status_code == 202
    job = client.get("/api/jobs").json()["jobs"][0]
    assert job["status"] == "queued" and job["attempts"] == 0
    second = client.post(f"/api/jobs/{job['id']}/retry", headers={"X-CSRF-Token": csrf})
    assert second.status_code == 409


def test_recover_stale_jobs_and_streaming_messages(client, admin, settings):
    csrf = login(client, admin)
    document_id = import_document(client, csrf).json()["id"]
    from app.db import session_scope
    from app.jobs import recover_stale_jobs, recover_stale_streams

    with session_scope() as db:
        db.execute(
            text(
                "UPDATE ingestion_jobs SET status = 'running', attempts = 1,"
                " lease_until = now() - interval '10 minutes' WHERE document_id = :d"
            ),
            {"d": document_id},
        )
    with session_scope() as db:
        assert recover_stale_jobs(db, settings) == 1
    job = client.get("/api/jobs").json()["jobs"][0]
    assert job["status"] == "queued"

    with session_scope() as db:
        db.execute(
            text(
                "UPDATE ingestion_jobs SET status = 'running', attempts = 3,"
                " lease_until = now() - interval '10 minutes' WHERE document_id = :d"
            ),
            {"d": document_id},
        )
    with session_scope() as db:
        assert recover_stale_jobs(db, settings) == 1
    assert client.get("/api/jobs").json()["jobs"][0]["status"] == "failed"

    # Messages restés « streaming » après un redémarrage : marqués interrompus.
    conversation = client.post("/api/conversations", json={}, headers={"X-CSRF-Token": csrf}).json()
    from app.models import Message

    with session_scope() as db:
        db.add(
            Message(
                conversation_id=__import__("uuid").UUID(conversation["id"]),
                seq=1,
                role="assistant",
                content="fragment",
                status="streaming",
            )
        )
    with session_scope() as db:
        db.execute(text("UPDATE messages SET updated_at = now() - interval '20 minutes'"))
        assert recover_stale_streams(db, 300) == 1
    messages = client.get(f"/api/conversations/{conversation['id']}/messages").json()["messages"]
    assert messages[0]["status"] == "interrupted"


def test_claim_is_exclusive_between_workers(client, admin, settings):
    csrf = login(client, admin)
    for index in range(2):
        assert import_document(client, csrf, title=f"Guide {index}", pages=1 + index).status_code == 201
    from app.db import session_scope
    from app.jobs import claim_job

    claimed: list[str] = []
    lock = threading.Lock()

    def claim(worker_id: str) -> None:
        with session_scope() as db:
            job = claim_job(db, worker_id, 60)
            if job is not None:
                with lock:
                    claimed.append(str(job.id))

    threads = [threading.Thread(target=claim, args=(f"w{i}",)) for i in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(claimed) == len(set(claimed))  # aucun job réclamé deux fois
    assert len(claimed) >= 1


def test_delete_document_cancels_jobs_and_removes_file(client, admin):
    csrf = login(client, admin)
    document_id = import_document(client, csrf).json()["id"]
    from app.db import session_scope
    from app.models import Document

    with session_scope() as db:
        stored = db.get(Document, __import__("uuid").UUID(document_id)).stored_relpath
    from app.config import get_settings

    path = get_settings().documents_dir / stored
    assert path.is_file()

    response = client.delete(f"/api/documents/{document_id}", headers={"X-CSRF-Token": csrf})
    assert response.status_code == 200
    assert not path.exists()
    jobs = client.get("/api/jobs").json()["jobs"]
    # Le job d'ingestion disparaît avec son document (cascade) : plus aucune
    # trace de ce document dans la file.
    assert all(document_id not in json.dumps(job) for job in jobs)
    assert client.get(f"/api/documents/{document_id}/chunks").status_code == 404
