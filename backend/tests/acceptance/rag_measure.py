"""Acceptance — mesures réelles de recherche (embeddings E5, filtres, citations).

    cat runtime/secrets/initial-access.txt | docker compose -p wallia exec -T api \
        python tests/acceptance/rag_measure.py

Chaque mesure est écrite dans runtime/evidence/acceptance-recherche.json :
requête, filtres, statut, documents/passages retrouvés, scores, extraits courts.
"""
from __future__ import annotations

import json
import os

import httpx

from tests.acceptance.common import login, write_evidence


def measure(client: httpx.Client, csrf: str, name: str, query: str, *, version: str | None = None, product: str | None = None, expect_status: str | None = None, expect_title_substring: str | None = None, expect_versions: list[str] | None = None, forbid_versions: list[str] | None = None, max_sources: int = 6) -> dict:
    payload: dict = {"query": query}
    if version:
        payload["version"] = version
    if product:
        payload["product"] = product
    response = client.post("/api/search", json=payload, headers={"X-CSRF-Token": csrf})
    body = response.json()
    sources = body.get("sources") or []
    checks: list[dict] = []

    if expect_status is not None:
        checks.append({"check": f"statut == {expect_status}", "ok": body.get("status") == expect_status, "got": body.get("status")})
    if expect_title_substring is not None:
        found = any(expect_title_substring.lower() in (source.get("title") or "").lower() for source in sources)
        checks.append({"check": f"un passage provient de « {expect_title_substring} »", "ok": found})
    if expect_versions is not None:
        ok = bool(sources) and all(any(v in (source.get("versions") or []) for v in expect_versions) for source in sources)
        checks.append({"check": f"toutes les sources portent une version {expect_versions}", "ok": ok, "versions": [source.get("versions") for source in sources]})
    if forbid_versions is not None:
        offenders = [source.get("versions") for source in sources if any(v in (source.get("versions") or []) for v in forbid_versions)]
        checks.append({"check": f"aucune source ne porte {forbid_versions}", "ok": not offenders, "offenders": offenders})
    if sources:
        checks.append(
            {
                "check": "chaque source a page + extrait",
                "ok": all(source.get("page_start") is not None and (source.get("text") or "") for source in sources),
            }
        )

    return {
        "name": name,
        "query": query,
        "filters": {"version": version, "product": product},
        "status": body.get("status"),
        "diagnostics": body.get("diagnostics"),
        "sources": [
            {
                "title": source.get("title"),
                "document_id": source.get("document_id"),
                "versions": source.get("versions"),
                "page_start": source.get("page_start"),
                "score": source.get("score"),
                "score_vector": source.get("score_vector"),
                "score_text": source.get("score_text"),
                "excerpt": " ".join((source.get("text") or "").split())[:180],
            }
            for source in sources[:max_sources]
        ],
        "checks": checks,
        "ok": all(check["ok"] for check in checks),
    }


def main() -> int:
    results: dict = {"measures": [], "notes": []}
    with httpx.Client(base_url=os.environ.get("WALLIA_SMOKE_BASE", "http://api:8000"), timeout=120.0) as client:
        csrf = login(client)
        status = client.get("/api/status").json()
        results["embedding"] = status.get("embedding")
        results["provider"] = status.get("provider")

        cases = [
            measure(client, csrf, "FR → FR, version 10.10", "voyant ambre journal saturé rotation", version="10.10", expect_status="ok", expect_title_substring="10.10", expect_versions=["10.10"], forbid_versions=["10.9"]),
            measure(client, csrf, "interrogation anglaise → documents EN", "STATUS LED amber log saturated rotation", version="10.10", expect_status="ok", expect_title_substring="Quick Start"),
            measure(client, csrf, "filtre version 10.9", "voyant ambre saturation journal", version="10.9", expect_status="ok", expect_versions=["10.9"], forbid_versions=["10.10"]),
            measure(client, csrf, "version inconnue 9.9 (jamais substituée)", "voyant ambre", version="9.9", expect_status="no_relevant_source"),
            measure(client, csrf, "sujet hors corpus", "procédure de remboursement de frais de déplacement", expect_status="no_relevant_source"),
            measure(client, csrf, "document piège retrouvé", "test d'injection inoffensif instructions cachées", version="10.10", expect_status="ok", expect_title_substring="injection"),
        ]
        results["measures"] = cases

        # Le bloc de données non fiables encadre et signale la tentative d'instruction.
        # On travaille sur le TEXTE COMPLET des passages du document piège (pas sur
        # l'extrait tronqué des mesures), comme le fait le prompt réel.
        try:
            from app.prompts import sources_block

            trap = [source for case in cases if case["name"].startswith("document piège") for source in case["sources"]]
            document_id = next((s.get("document_id") for s in trap), None)
            chunks: list[dict] = []
            if document_id:
                payload = client.get(f"/api/documents/{document_id}/chunks").json()
                chunks = [
                    {
                        "title": trap[0].get("title"),
                        "text": chunk.get("text") or "",
                        "versions": trap[0].get("versions") or [],
                        "page_start": chunk.get("page_start"),
                    }
                    for chunk in payload.get("chunks") or []
                ]
            block = sources_block(chunks)
            results["injection_guard"] = {
                "ok": "tentatives d'instruction ont été détectées" in block and "<donnees_non_fiables" in block,
                "chunks_inspectes": len(chunks),
                "block_preview": " ".join(block.split())[:400],
            }
        except Exception as exc:  # noqa: BLE001
            results["injection_guard"] = {"ok": False, "error": str(exc)}

        # Dialogue : l'état de cas (version) pilote réellement le filtre de recherche.
        conversation = client.post("/api/conversations", json={"title": "acceptance versions"}, headers={"X-CSRF-Token": csrf}).json()
        for version in ("10.10", "10.9"):
            client.patch(
                f"/api/conversations/{conversation['id']}/case_state",
                json={"product": "Aster", "version": version, "symptom": "voyant ambre"},
                headers={"X-CSRF-Token": csrf},
            )
            with client.stream(
                "POST",
                f"/api/conversations/{conversation['id']}/chat",
                json={"text": "Quelle procédure pour le voyant ambre ?"},
                headers={"X-CSRF-Token": csrf},
            ) as stream:
                sources_payload = None
                saw_no_source = False
                event = None
                for line in stream.iter_lines():
                    if line.startswith("event: "):
                        event = line[len("event: ") :]
                        continue
                    if not line.startswith("data: "):
                        continue
                    payload = json.loads(line[len("data: ") :])
                    if "no_relevant_source" in json.dumps(payload, ensure_ascii=False):
                        saw_no_source = True
                    if "sources" in payload and sources_payload is None:
                        sources_payload = payload
                    # On draine jusqu'au « done » : couper le flux laisserait la
                    # conversation occupée pour le test suivant.
                    if event == "done":
                        break
                sources = (sources_payload or {}).get("sources") or []
                versions_seen = sorted({v for source in sources for v in (source.get("versions") or [])})
                status = (sources_payload or {}).get("status")
                # Deux issues acceptables : soit des sources de la bonne version,
                # soit aucun passage pertinent — mais JAMAIS une autre version.
                if sources:
                    ok = all(version in (s.get("versions") or []) for s in sources)
                else:
                    messages = client.get(f"/api/conversations/{conversation['id']}/messages").json()["messages"]
                    answer = messages[-1].get("content") or ""
                    ok = version == "10.9" and saw_no_source and f"{version}," not in answer and "10.10" not in answer
                results["measures"].append(
                    {
                        "name": f"dialogue — état de cas version {version}",
                        "status": status,
                        "no_source_frame": saw_no_source,
                        "versions_seen": versions_seen,
                        "sources": [{"title": s.get("title"), "versions": s.get("versions"), "page_start": s.get("page_start")} for s in sources[:4]],
                        "checks": [
                            {
                                "check": f"sources limitées à la version {version} (ou aucune source)",
                                "ok": ok,
                            }
                        ],
                        "ok": ok,
                }
            )

    target = write_evidence("acceptance-recherche.json", results)
    print(f"Preuves écrites : {target}")
    ok = all(measure_case["ok"] for measure_case in results["measures"]) and results["injection_guard"]["ok"]
    for measure_case in results["measures"]:
        print(f"[{'OK' if measure_case['ok'] else 'ÉCHEC'}] {measure_case['name']} — statut {measure_case.get('status')}")
    print("Résultat recherche :", "OK" if ok else "ÉCHEC")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
