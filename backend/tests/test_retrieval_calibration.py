"""Calibration — vérification d'exécution RÉELLE sur le runner isolé.

Le contrat d'éligibilité cosinus/lexical (lot3/lot3b) est REMPLACÉ par le
reclassement cross-encoder (docs/reranker-probe.md) : les anciennes limites
mesurées du scoring cosinus (pos7/pos8/pos14) ne s'appliquent plus. Le contrat
RÉEL du reclassement (14/14 positifs servis, 14/14 négatifs rejetés au seuil
gelé 1.1491, sur ce jeu de vecteurs réels) est re-vérifié par la recette
isolée `tests/acceptance/rag_fr_en_isolated.py`, qui charge le VRAI modèle sur
le chemin SQL + service intégré.

Ce test-ci exerce ce même chemin intégré (SQL + fusion + pool + reclassement)
sur les vecteurs E5 RÉELS du jeu de calibration avec le backend `fixture`
(mécanique déclarée) et vérifie les invariants OBSERVABLES :

  - aucun état `retrieval_unavailable` (le backend fixture est toujours prêt) ;
  - chaque source a franchi le seuil gelé sur SON propre logit ; tri
    décroissant, départage déterministe ;
  - les diagnostics exposent les signaux bruts (rang vecteur, rang texte,
    score RRF, score de reclassement) et l'identité du modèle ;
  - deux exécutions identiques donnent des résultats identiques.

Sans vecteurs fournis (CI), le test est ignoré explicitement.
"""
from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text

from app.retrieval import hybrid_search
from app.reranking import RERANK_LOGIT_THRESHOLD

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "calibration" / "calibration.json"


def _vectors_path() -> Path | None:
    explicit = os.environ.get("WALLIA_CALIBRATION_VECTORS")
    if explicit and Path(explicit).is_file():
        return Path(explicit)
    bundled = Path(__file__).resolve().parents[1] / "fixtures" / "calibration" / "calibration-vectors.json"
    return bundled if bundled.is_file() else None


def _literal(vector: list[float]) -> str:
    return "[" + ",".join(repr(float(x)) for x in vector) + "]"


def _insert_calibration(db, calibration: dict, vectors: dict) -> dict[str, str]:
    passage_ids: dict[str, str] = {}
    for passage in calibration["corpus"]:
        document_id = uuid.uuid4()
        db.execute(
            text(
                """
                INSERT INTO documents (id, title, origin, product, versions, language, demo, scope,
                                       status, stored_relpath, original_filename, content_type, size_bytes,
                                       page_count, checksum_sha256, current_generation)
                VALUES (:id, :title, 'calibration', 'Nova Boreal', '{}', :lang, true, 'demo',
                        'ready', '', :file, 'text/plain', 1, 1, :sum, 1)
                """
            ),
            {
                "id": str(document_id),
                "title": f"Calibration {passage['id']}",
                "lang": passage["lang"],
                "file": f"{passage['id']}.txt",
                "sum": f"calibration-{passage['id']}",
            },
        )
        db.execute(
            text(
                """
                INSERT INTO chunks (document_id, generation, seq, text, page_start, page_end, section, kind,
                                    token_count, embedding)
                VALUES (:doc, 1, 1, :txt, 1, 1, NULL, 'text', 10, CAST(:emb AS vector))
                """
            ),
            {"doc": str(document_id), "txt": passage["text"], "emb": _literal(vectors[passage["id"]])},
        )
        passage_ids[passage["id"]] = str(document_id)
    db.commit()
    return passage_ids


def _run_all(db, settings, calibration: dict, vectors: dict) -> list[dict]:
    outcomes: list[dict] = []
    for question in calibration["positives"]:
        result = hybrid_search(db, settings, query=question["question"], query_vector=vectors[question["id"]])
        outcomes.append({"id": question["id"], "kind": "positive", "result": result})
    for question in calibration["negatives"]:
        result = hybrid_search(db, settings, query=question["question"], query_vector=vectors[question["id"]])
        outcomes.append({"id": question["id"], "kind": "negative", "result": result})
    return outcomes


def _summarize(outcomes: list[dict], passage_ids: dict[str, str]) -> list[dict]:
    summary: list[dict] = []
    for outcome in outcomes:
        result = outcome["result"]
        served_ids = [source["document_id"] for source in result.get("sources", [])]
        served_labels = sorted(pid for pid, did in passage_ids.items() if did in served_ids)
        summary.append(
            {
                "id": outcome["id"],
                "kind": outcome["kind"],
                "status": result["status"],
                "served": served_labels,
                "served_scores": [source["score"] for source in result.get("sources", [])],
                "pool": result["diagnostics"].get("pool"),
            }
        )
    return summary


def test_integrated_search_uses_real_vectors_and_frozen_threshold(migrated):
    vectors_path = _vectors_path()
    if vectors_path is None:
        pytest.skip("vecteurs de calibration absents (exécuter scripts/tests-isolated/fetch-embeddings.py)")

    calibration = json.loads(FIXTURE.read_text(encoding="utf-8"))
    vectors_doc = json.loads(vectors_path.read_text(encoding="utf-8"))
    vectors = {item["id"]: item["vector"] for item in vectors_doc["items"]}

    from app.db import session_scope

    with session_scope() as db:
        passage_ids = _insert_calibration(db, calibration, vectors)
        outcomes = _run_all(db, migrated, calibration, vectors)
        replay = _run_all(db, migrated, calibration, vectors)

    # Le chemin intégré ne se dérobe jamais : le backend fixture est prêt.
    assert all(o["result"]["status"] != "retrieval_unavailable" for o in outcomes)
    # Chaque source a franchi le seuil gelé sur SON propre logit ; tri strict.
    for outcome in outcomes:
        result = outcome["result"]
        scores = [source["score"] for source in result.get("sources", [])]
        assert scores == sorted(scores, reverse=True)
        assert all(score >= RERANK_LOGIT_THRESHOLD for score in scores)
        assert all(source["score_kind"] == "rerank_logit" for source in result.get("sources", []))
        for candidate in result["diagnostics"].get("candidates", []):
            assert candidate["served"] == (candidate["score_rerank"] >= RERANK_LOGIT_THRESHOLD)
        assert result["diagnostics"]["reranker"]["threshold"] == RERANK_LOGIT_THRESHOLD

    # Déterminisme intégral (mêmes entrées → mêmes sorties et mêmes preuves).
    assert _summarize(outcomes, passage_ids) == _summarize(replay, passage_ids)

    evidence_dir = Path(os.environ.get("WALLIA_EVIDENCE_DIR", "/run/isolation/evidence"))
    evidence_dir.mkdir(parents=True, exist_ok=True)
    (evidence_dir / "calibration-runner.json").write_text(
        json.dumps(
            {
                "kind": "calibration-runner",
                "mode": "chemin intégré SQL+service, backend fixture (mécanique déclarée)",
                "real_model_contract": (
                    "14/14 positifs servis et 14/14 négatifs rejetés au seuil gelé sont vérifiés par "
                    "tests/acceptance/rag_fr_en_isolated.py avec le reranker RÉEL (lot3d)."
                ),
                "vectors_texts_sha256": vectors_doc["texts_sha256"],
                "embedding": vectors_doc["info"],
                "threshold": RERANK_LOGIT_THRESHOLD,
                "summary": _summarize(outcomes, passage_ids),
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
