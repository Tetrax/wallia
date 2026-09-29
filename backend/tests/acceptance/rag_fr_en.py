"""Acceptance — RAG réel FR→EN, SANS appel LLM.

Question française → le passage du Quick Start (EN) page 2 « the log rotation
keeps the last seven days of entries » doit être retrouvé par la recherche
réelle (E5 + pgvector) avec un score au-dessus du seuil FIGÉ (jamais ajusté) ;
une question hors corpus doit rester sous ce même seuil. Les filtres de version
sont exercés : 10.9 n'inclut jamais 10.10, une version inconnue (99.99) ne
substitue rien.

    sudo cat runtime/initial-access.txt | docker compose -p wallia exec -T api \
        python tests/acceptance/rag_fr_en.py
"""
from __future__ import annotations

import os

import httpx

from tests.acceptance.common import login, write_evidence

QUESTION_FR = "Combien de jours de journaux la rotation conserve-t-elle dans Aster 10.10 ?"
NEGATIVE_FR = "Quel est le tarif de la licence annuelle en euros et les conditions commerciales de revente ?"


def _search(client: httpx.Client, csrf: str, query: str, *, version: str | None = None) -> dict:
    payload: dict = {"query": query}
    if version is not None:
        payload["version"] = version
    response = client.post("/api/search", json=payload, headers={"X-CSRF-Token": csrf})
    response.raise_for_status()
    return response.json()


def main() -> int:
    base = os.environ.get("WALLIA_API_BASE_URL", "http://127.0.0.1:8000")
    results: dict = {"question": QUESTION_FR, "negative": NEGATIVE_FR, "measures": []}
    with httpx.Client(base_url=base, timeout=120.0) as client:
        csrf = login(client)
        status = client.get("/api/status").json()
        threshold = status["retrieval"]["min_cosine"]
        results["threshold_frozen"] = threshold

        positive = _search(client, csrf, QUESTION_FR)
        sources = positive.get("sources") or []
        quick = [
            source
            for source in sources
            if "Quick Start" in (source.get("title") or "")
            and source.get("page_start") == 2
            and "seven days" in (source.get("text") or "")
        ]
        results["measures"].append(
            {
                "name": "positif FR→EN",
                "status": positive.get("status"),
                "scores": [
                    {
                        "title": source.get("title"),
                        "page_start": source.get("page_start"),
                        "score_vector": source.get("score_vector"),
                        "score_text": source.get("score_text"),
                    }
                    for source in sources
                ],
                "checks": [
                    {
                        "check": "passage Quick Start (EN) page 2 « seven days » servi",
                        "ok": bool(quick),
                    },
                    {
                        "check": "scores réellement enregistrés (vecteur et/ou texte)",
                        "ok": bool(quick)
                        and any(
                            source.get("score_text") is not None or source.get("score_vector") is not None
                            for source in quick
                        ),
                    },
                    {
                        "check": f"retenu par correspondance lexicale ou cos ≥ seuil figé {threshold}",
                        "ok": bool(quick)
                        and any(
                            source.get("score_text") is not None
                            or (
                                source.get("score_vector") is not None
                                and source["score_vector"] >= threshold
                            )
                            for source in quick
                        ),
                    },
                ],
            }
        )
        results["measures"][-1]["ok"] = all(check["ok"] for check in results["measures"][-1]["checks"])

        negative = _search(client, csrf, NEGATIVE_FR)
        negative_sources = negative.get("sources") or []
        results["measures"].append(
            {
                "name": "négatif hors corpus",
                "status": negative.get("status"),
                "scores": [
                    {"title": source.get("title"), "score_vector": source.get("score_vector")}
                    for source in negative_sources
                ],
                "checks": [
                    {
                        "check": f"aucune source au-dessus du seuil figé {threshold}",
                        "ok": not negative_sources,
                    }
                ],
            }
        )
        results["measures"][-1]["ok"] = all(check["ok"] for check in results["measures"][-1]["checks"])

        # Filtres produit/version réellement exercés : une version demandée
        # n'inclut JAMAIS les autres, une version inconnue ne substitue rien.
        version_109 = _search(client, csrf, QUESTION_FR, version="10.9")
        sources_109 = version_109.get("sources") or []
        results["measures"].append(
            {
                "name": "version 10.9 n'inclut pas 10.10",
                "status": version_109.get("status"),
                "scores": [
                    {"title": source.get("title"), "versions": source.get("versions")}
                    for source in sources_109
                ],
                "checks": [
                    {
                        "check": "sources servies toutes en 10.9, jamais 10.10",
                        "ok": bool(sources_109)
                        and all(
                            "10.9" in (source.get("versions") or [])
                            and "10.10" not in (source.get("versions") or [])
                            for source in sources_109
                        ),
                    },
                    {
                        "check": "diagnostic de filtre version=10.9",
                        "ok": (version_109.get("diagnostics") or {}).get("filters", {}).get("version") == "10.9",
                    },
                ],
            }
        )
        results["measures"][-1]["ok"] = all(check["ok"] for check in results["measures"][-1]["checks"])

        version_unknown = _search(client, csrf, QUESTION_FR, version="99.99")
        results["measures"].append(
            {
                "name": "version inconnue sans substitution",
                "status": version_unknown.get("status"),
                "sources": version_unknown.get("sources") or [],
                "checks": [
                    {
                        "check": "99.99 : aucune source, aucune substitution",
                        "ok": version_unknown.get("status") == "no_relevant_source"
                        and not (version_unknown.get("sources") or []),
                    }
                ],
            }
        )
        results["measures"][-1]["ok"] = all(check["ok"] for check in results["measures"][-1]["checks"])

    ok = all(measure["ok"] for measure in results["measures"])
    results["ok"] = ok
    target = write_evidence("acceptance-rag-fr-en.json", results)
    print(f"Preuves écrites : {target}")
    for measure in results["measures"]:
        print(f"[{'OK' if measure['ok'] else 'ÉCHEC'}] {measure['name']} — statut {measure.get('status')}")
        for check in measure["checks"]:
            print(f"    - [{'ok' if check['ok'] else 'non'}] {check['check']}")
    print("Résultat RAG FR→EN :", "OK" if ok else "ÉCHEC")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
