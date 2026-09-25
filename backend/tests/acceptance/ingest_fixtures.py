"""Acceptance — import du corpus fictif et ingestion réelle (Docling + E5).

    cat runtime/secrets/initial-access.txt | docker compose -p wallia exec -T api \
        python tests/acceptance/ingest_fixtures.py

Vérifie : import dédupliqué, jobs durables, pipeline réel, passages publiés.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import httpx

from tests.acceptance.common import credentials, login, wait_jobs_idle, write_evidence

FIXTURES = Path(os.environ.get("WALLIA_FIXTURES", "/app/fixtures/corpus"))


def cleanup_failed_documents() -> int:
    """Supprime les documents en statut « failed » (reprise après échec d'import).

    Utile après correction d'un défaut d'ingestion : les documents en échec ne
    peuvent pas être « rejoués » tels quels, on repart proprement.
    """
    with httpx.Client(base_url="http://127.0.0.1:8000", timeout=60.0) as client:
        csrf = login(client)
        documents = client.get("/api/documents").json()["documents"]
        failed = [document for document in documents if document["status"] == "failed"]
        for document in failed:
            response = client.delete(f"/api/documents/{document['id']}", headers={"X-CSRF-Token": csrf})
            print(f"- suppression « {document['title'][:60]} » : HTTP {response.status_code}", flush=True)
        remaining = client.get("/api/documents").json()["documents"]
        print(f"documents restants : {len(remaining)}", flush=True)
    return 0


def reset_documents() -> int:
    """Supprime TOUS les documents (corpus propre avant une recette complète)."""
    with httpx.Client(base_url="http://127.0.0.1:8000", timeout=120.0) as client:
        csrf = login(client)
        documents = client.get("/api/documents").json()["documents"]
        for document in documents:
            response = client.delete(f"/api/documents/{document['id']}", headers={"X-CSRF-Token": csrf})
            print(f"- suppression « {document['title'][:60]} » : HTTP {response.status_code}", flush=True)
        remaining = client.get("/api/documents").json()["documents"]
        print(f"documents restants : {len(remaining)}", flush=True)
    return 0


def main() -> int:
    manifest = json.loads((FIXTURES / "manifest.json").read_text(encoding="utf-8"))
    results: dict = {"documents": [], "checks": []}

    with httpx.Client(base_url=os.environ.get("WALLIA_SMOKE_BASE", "http://api:8000"), timeout=120.0) as client:
        csrf = login(client)
        existing = {
            document["title"]: document for document in client.get("/api/documents").json()["documents"]
        }

        for entry in manifest["documents"]:
            pdf = FIXTURES / entry["filename"]
            print(f"- import {entry['filename']}", flush=True)
            response = client.post(
                "/api/documents",
                files={"file": (entry["filename"], pdf.read_bytes(), "application/pdf")},
                data={
                    "title": entry["title"],
                    "origin": entry["origin"],
                    "product": entry["product"],
                    "versions": ",".join(entry["versions"]),
                    "language": entry["language"],
                    "demo": "1" if entry["demo"] else "0",
                    "scope": entry["scope"],
                },
                headers={"X-CSRF-Token": csrf},
            )
            if response.status_code == 201:
                results["documents"].append(
                    {"filename": entry["filename"], "status": "imported", "id": response.json()["id"], "ok": True}
                )
            elif response.status_code == 409:
                results["documents"].append(
                    {
                        "filename": entry["filename"],
                        "status": "already_present",
                        "id": existing.get(entry["title"], {}).get("id"),
                        "ok": True,
                    }
                )
            else:
                raise SystemExit(f"import refusé pour {entry['filename']}: HTTP {response.status_code} {response.text[:200]}")

        # Déduplication : réimport du même fichier → 409 attendu.
        first = manifest["documents"][0]
        duplicate = client.post(
            "/api/documents",
            files={"file": (first["filename"], (FIXTURES / first["filename"]).read_bytes(), "application/pdf")},
            data={"title": first["title"], "origin": first["origin"], "versions": ",".join(first["versions"]), "scope": first["scope"]},
            headers={"X-CSRF-Token": csrf},
        )
        results["checks"].append({"name": "déduplication import (409)", "ok": duplicate.status_code == 409, "http": duplicate.status_code})

        jobs = client.get("/api/jobs").json()["jobs"]
        print(f"- jobs en file : {len(jobs)}", flush=True)
        summary = wait_jobs_idle(client)
        results["jobs"] = summary

        documents = client.get("/api/documents").json()["documents"]
        results["checks"].append({"name": "corpus importé (4 documents)", "ok": len(documents) >= 4, "count": len(documents)})
        for document in documents:
            chunks = client.get(f"/api/documents/{document['id']}/chunks").json()
            ok = document["status"] == "ready" and chunks["generation"] >= 1 and len(chunks["chunks"]) > 0
            results["documents"].append(
                {
                    "title": document["title"],
                    "status": document["status"],
                    "pages": document["page_count"],
                    "chunks": len(chunks["chunks"]),
                    "generation": chunks["generation"],
                    "versions": document["versions"],
                    "language": document["language"],
                    "ok": ok,
                }
            )
            print(f"  {document['title']}: {document['status']}, {len(chunks['chunks'])} passages", flush=True)

        failed = [document for document in results["documents"] if not document.get("ok")]
        results["checks"].append({"name": "tous les documents indexés", "ok": not failed, "failed": failed})
        results["checks"].append({"name": "aucun job en échec", "ok": summary.get("failed", 0) == 0, "summary": summary})

    target = write_evidence("acceptance-ingestion.json", results)
    print(f"Preuves écrites : {target}")
    ok = all(check["ok"] for check in results["checks"])
    print("Résultat ingestion :", "OK" if ok else "ÉCHEC")
    return 0 if ok else 1


if __name__ == "__main__":
    if "--cleanup-failed" in sys.argv:
        raise SystemExit(cleanup_failed_documents())
    if "--reset-documents" in sys.argv:
        raise SystemExit(reset_documents())
    raise SystemExit(main())
