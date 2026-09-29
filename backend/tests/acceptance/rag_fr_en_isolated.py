"""Acceptance RAG isolée (lot3d) — chemin INTÉGRÉ complet, reranker RÉEL.

Exécute la recette métier sur le chemin applicatif RÉEL — fonction `hybrid_search`
(SQL pgvector384 + lexical + filtres, pool borné, reclassement cross-encoder
réel, seuil gelé 1.1491) — dans le harnais isolé, sans jamais toucher la pile
vivante ni la base vivante :

  1. CALIBRATION rejouée sur le chemin intégré avec le reranker RÉEL : les 14
     positifs et 14 négatifs du jeu `calibration-nova-boreal` (vecteurs E5 réels
     exportés) doivent retrouver la séparation validée par le probe
     (docs/reranker-probe.md) — 14/14 servis, 14/14 rejetés ;
  2. RECETTE corpus : export Docling réel (4 documents / 16 passages) importé
     dans la base de test dédiée ; question FR exacte → passage EN « seven
     days » p.2 ; avec/sans filtre Aster 10.10 ; négatif commercial → 0 source ;
     restriction 10.9 sans fuite 10.10 ET question positive 10.9 répondable
     (contrôle indépendant, vecteur E5 réel calculé dans le runner) ; version
     99.99 sans substitution ; aucun passage hors génération courante ou
     provenant d'un document non prêt ;
  3. COEXISTENCE E5 + CE chargés ensemble dans CE runner borné (≤ 2000 Mio) :
     mémoire cgroup, latences et absence d'OOM, sur entrée normale ET entrée
     longue tronquée à 512 tokens.

Environnement : WALLIA_TEST_RUNNER=isolated, base admin du service de test
isolé, modèle reranker monté en lecture (défaut :
/run/isolation/reranker-probe-model), E5 réel depuis l'image. Aucune extraction
Docling, aucun téléchargement, aucun appel réseau.
Sortie : acceptance-rag-fr-en-isolated.json dans WALLIA_EVIDENCE_DIR.
"""
from __future__ import annotations

import json
import math
import os
import sys
import time
from pathlib import Path

from sqlalchemy import create_engine, text

ACCEPTANCE_DB = "wallia_acceptance"
CALIBRATION_DB = "wallia_acceptance_calibration"

QUESTION_FR = "Combien de jours de journaux la rotation conserve-t-elle dans Aster 10.10 ?"
NEGATIVE_FR = "Quel est le tarif de la licence annuelle en euros et les conditions commerciales de revente ?"
QUESTION_109_FR = "Dans Aster 10.9, à combien est plafonné le journal local ?"

RERANKER_MODEL = "cross-encoder/mmarco-mMiniLMv2-L12-H384-v1"
RERANKER_REVISION = "1427fd652930e4ba29e8149678df786c240d8825"
RERANKER_SHA256 = "5daeca2481a76b5976a2bdc32f0a78532b6716da4f8cd3ff59460ef8d2f359b4"
FROZEN_THRESHOLD = 1.1491
EXPECTED_SEVEN_LOGIT = 5.75806  # trace du probe réel (rang 1, question FR sans filtre)
MEMORY_BOUND_BYTES = 2000 * 1024 * 1024  # borne exigée : ≤ 2000 Mio pour E5+CE


def _require_isolated() -> None:
    if os.environ.get("WALLIA_TEST_RUNNER") != "isolated":
        raise SystemExit("refus : cette recette doit tourner dans le harnais isolé (WALLIA_TEST_RUNNER=isolated)")
    if "/secrets/db_password" in os.environ.get("WALLIA_TEST_DB_URL", "") or Path("/secrets/db_password").exists():
        raise SystemExit("refus : conteneur de livraison détecté")


def _configure_models() -> dict:
    """Reranker RÉEL + E5 RÉEL : variables posées AVANT tout import applicatif."""
    model_dir = Path(os.environ.get("WALLIA_ACCEPTANCE_RERANKER_MODEL", "/run/isolation/reranker-probe-model"))
    if not model_dir.is_dir():
        raise SystemExit(f"refus : modèle reranker absent du runner ({model_dir})")
    os.environ["WALLIA_RERANKER_BACKEND"] = "transformers"
    os.environ["WALLIA_RERANKER_MODEL_DIR"] = str(model_dir)
    os.environ["WALLIA_EMBEDDING_BACKEND"] = "e5"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    os.environ["HF_HUB_OFFLINE"] = "1"
    return {"reranker_model_dir": str(model_dir)}


def _literal(vector: list[float]) -> str:
    return "[" + ",".join(repr(float(x)) for x in vector) + "]"


def _admin_engine():
    from sqlalchemy.engine import make_url

    url = make_url(os.environ["WALLIA_TEST_DB_URL"])
    return create_engine(url.set(database=url.database or "postgres").render_as_string(hide_password=False))


def _recreate_database(name: str) -> None:
    engine = _admin_engine()
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        conn.exec_driver_sql(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %(name)s",
            {"name": name},
        )
        conn.exec_driver_sql(f'DROP DATABASE IF EXISTS "{name}"')
        conn.exec_driver_sql(f'CREATE DATABASE "{name}"')
    engine.dispose()


def _use_database(name: str):
    from sqlalchemy.engine import make_url

    url = make_url(os.environ["WALLIA_TEST_DB_URL"])
    url = url.set(database=name)
    os.environ["WALLIA_DB_URL"] = url.render_as_string(hide_password=False)
    import app.config as app_config
    import app.db as app_db

    app_config.get_settings.cache_clear()
    app_db.reset_engine()
    from app.migrate import run_migrations

    run_migrations(verbose=False)
    return app_config.get_settings()


def _load_corpus(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _load_vectors(path: Path) -> dict:
    document = json.loads(path.read_text(encoding="utf-8"))
    return {item["id"]: item for item in document["items"]} | {
        "_info": document["info"],
        "_sha": document["texts_sha256"],
    }


def _insert_corpus(engine, corpus: dict) -> None:
    with engine.begin() as conn:
        for document in corpus["documents"]:
            conn.execute(
                text(
                    """
                    INSERT INTO documents (id, title, origin, product, versions, language, demo, scope, status,
                                           stored_relpath, original_filename, content_type, size_bytes, page_count,
                                           checksum_sha256, current_generation, embedding_model, embedding_revision,
                                           embedding_dim)
                    VALUES (:id, :title, 'export-lot3', :product, CAST(:versions AS text[]), :language, :demo, :scope,
                            :status, '', :file, 'application/pdf', 1024, :pages, :sum, :generation,
                            :emb_model, :emb_revision, :emb_dim)
                    """
                ),
                {
                    "id": document["id"],
                    "title": document["title"],
                    "product": document["product"],
                    "versions": document["versions"],
                    "language": document["language"],
                    "demo": document["demo"],
                    "scope": document["scope"],
                    "status": document["status"],
                    "file": f"export-{document['id'][:8]}.pdf",
                    "pages": document["page_count"],
                    "sum": f"export-lot3-{document['id']}",
                    "generation": document["current_generation"],
                    "emb_model": document["embedding_model"],
                    "emb_revision": document["embedding_revision"],
                    "emb_dim": document["embedding_dim"],
                },
            )
        for chunk in corpus["chunks"]:
            conn.execute(
                text(
                    """
                    INSERT INTO chunks (document_id, generation, seq, text, page_start, page_end, section, kind,
                                        token_count, embedding)
                    VALUES (:doc, :gen, :seq, :txt, :ps, :pe, :section, :kind, 10, CAST(:emb AS vector))
                    """
                ),
                {
                    "doc": chunk["document_id"],
                    "gen": chunk["generation"],
                    "seq": chunk["seq"],
                    "txt": chunk["text"],
                    "ps": chunk["page_start"],
                    "pe": chunk["page_end"],
                    "section": chunk["section"],
                    "kind": chunk["kind"],
                    "emb": _literal(chunk["embedding"]),
                },
            )


def _insert_eligibility_controls(engine, corpus: dict) -> dict:
    """Contrôles indépendants « hors corpus » (fixtures fictives, base de test) :

    - un chunk de GÉNÉRATION PÉRIMÉE (copie du passage « seven days », vecteur
      réel réutilisé) ne doit jamais être sélectionné ni servi ;
    - un document au statut non prêt (copie, statut `processing`) non plus.
    """
    seven = next(c for c in corpus["chunks"] if "seven days" in (c["text"] or "").lower())
    en_doc = next(d for d in corpus["documents"] if d["id"] == seven["document_id"])

    with engine.begin() as conn:
        stale_id = conn.execute(
            text(
                """
                INSERT INTO chunks (document_id, generation, seq, text, page_start, page_end, section, kind,
                                    token_count, embedding)
                VALUES (:doc, 0, 99, :txt, 2, 2, NULL, 'text', 10, CAST(:emb AS vector))
                RETURNING id
                """
            ),
            {"doc": en_doc["id"], "txt": seven["text"], "emb": _literal(seven["embedding"])},
        ).scalar_one()

        not_ready_doc = conn.execute(
            text(
                """
                INSERT INTO documents (id, title, origin, product, versions, language, demo, scope, status,
                                       stored_relpath, original_filename, content_type, size_bytes, page_count,
                                       checksum_sha256, current_generation)
                VALUES (gen_random_uuid(), :title, 'export-lot3-controle', :product, CAST(:versions AS text[]),
                        :language, true, 'demo', 'processing', '', 'controle.pdf', 'application/pdf',
                        1024, 1, 'controle-statut', 1)
                RETURNING id
                """
            ),
            {
                "title": f"Contrôle statut — {en_doc['title']}",
                "product": en_doc["product"],
                "versions": en_doc["versions"],
                "language": en_doc["language"],
            },
        ).scalar_one()
        not_ready_chunk = conn.execute(
            text(
                """
                INSERT INTO chunks (document_id, generation, seq, text, page_start, page_end, section, kind,
                                    token_count, embedding)
                VALUES (:doc, 1, 1, :txt, 2, 2, NULL, 'text', 10, CAST(:emb AS vector))
                RETURNING id
                """
            ),
            {"doc": not_ready_doc, "txt": seven["text"], "emb": _literal(seven["embedding"])},
        ).scalar_one()
    return {"stale_generation_chunk": str(stale_id), "not_ready_chunk": str(not_ready_chunk)}


def _chunk_ids_by_key(engine) -> dict[tuple[str, int], str]:
    with engine.connect() as conn:
        rows = conn.execute(text("SELECT id, document_id, seq FROM chunks")).fetchall()
    return {(str(doc), int(seq)): str(chunk_id) for chunk_id, doc, seq in rows}


def _insert_calibration(engine, calibration: dict, vectors: dict) -> dict[str, str]:
    passage_ids: dict[str, str] = {}
    with engine.begin() as conn:
        for passage in calibration["corpus"]:
            document_id = conn.execute(
                text(
                    """
                    INSERT INTO documents (id, title, origin, product, versions, language, demo, scope,
                                           status, stored_relpath, original_filename, content_type, size_bytes,
                                           page_count, checksum_sha256, current_generation)
                    VALUES (gen_random_uuid(), :title, 'calibration', 'Nova Boreal', '{}', :lang, true, 'demo',
                            'ready', '', :file, 'text/plain', 1, 1, :sum, 1)
                    RETURNING id
                    """
                ),
                {
                    "title": f"Calibration {passage['id']}",
                    "lang": passage["lang"],
                    "file": f"{passage['id']}.txt",
                    "sum": f"calibration-{passage['id']}",
                },
            ).scalar_one()
            conn.execute(
                text(
                    """
                    INSERT INTO chunks (document_id, generation, seq, text, page_start, page_end, section, kind,
                                        token_count, embedding)
                    VALUES (:doc, 1, 1, :txt, 1, 1, NULL, 'text', 10, CAST(:emb AS vector))
                    """
                ),
                {"doc": document_id, "txt": passage["text"], "emb": _literal(vectors[passage["id"]])},
            )
            passage_ids[passage["id"]] = str(document_id)
    return passage_ids


def _search(settings, query: str, vector: list[float], *, product: str | None = None, version: str | None = None) -> dict:
    from app.db import session_scope
    from app.retrieval import hybrid_search

    with session_scope() as db:
        return hybrid_search(db, settings, query=query, query_vector=vector, product=product, version=version)


def _sources_summary(result: dict) -> list[dict]:
    return [
        {
            "title": source["title"],
            "document_id": source["document_id"],
            "product": source["product"],
            "versions": source["versions"],
            "language": source["language"],
            "page_start": source["page_start"],
            "score": source["score"],
            "score_kind": source["score_kind"],
            "score_vector": source["score_vector"],
            "score_text": source["score_text"],
            "has_seven_days": "seven days" in (source["text"] or "").lower(),
            "has_200_mo": "200 Mo" in (source["text"] or ""),
        }
        for source in result.get("sources", [])
    ]


def _quick_passages(sources: list[dict]) -> list[dict]:
    return [
        s
        for s in sources
        if "Quick Start" in (s["title"] or "") and s["page_start"] == 2 and "seven days" in (s["text"] or "").lower()
    ]


def _cosine(left: list[float], right: list[float]) -> float:
    dot = sum(a * b for a, b in zip(left, right))
    lnorm = math.sqrt(sum(a * a for a in left))
    rnorm = math.sqrt(sum(b * b for b in right))
    return dot / (lnorm * rnorm) if lnorm and rnorm else 0.0


def _read_int(path: str) -> int | None:
    try:
        value = Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return int(value) if value.isdigit() else None


def _read_events(path: str) -> dict[str, int]:
    events: dict[str, int] = {}
    try:
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[1].isdigit():
                events[parts[0]] = int(parts[1])
    except OSError:
        pass
    return events


def _vm_hwm_kb() -> int | None:
    try:
        for line in Path("/proc/self/status").read_text(encoding="utf-8").splitlines():
            if line.startswith("VmHWM:"):
                return int(line.split()[1])
    except OSError:
        return None
    return None


def _long_text(corpus: dict) -> str:
    """Texte LONG construit à partir de passages RÉELS du corpus (aucune
    invention) : au-delà de 512 tokens pour exercer la troncature du modèle."""
    texts = [chunk["text"] or "" for chunk in corpus["chunks"]]
    joined = "\n\n".join(texts)
    while len(joined) < 8000:
        joined = joined + "\n\n" + "\n\n".join(texts)
    return joined


def main() -> int:
    _require_isolated()
    models = _configure_models()
    corpus_path = Path(os.environ["WALLIA_ACCEPTANCE_CORPUS"])
    vectors_path = Path(os.environ["WALLIA_ACCEPTANCE_QUERIES"])
    calibration_vectors_path = Path(os.environ.get("WALLIA_CALIBRATION_VECTORS", "/run/isolation/calibration-vectors.json"))
    evidence_dir = Path(os.environ.get("WALLIA_EVIDENCE_DIR", "/run/isolation/evidence"))
    corpus = _load_corpus(corpus_path)
    vectors = _load_vectors(vectors_path)

    if vectors["q-positif-fr-en"]["text"] != QUESTION_FR:
        raise SystemExit("refus : la question positive du jeu d'acceptance a changé — textes non modifiés exigés.")
    if vectors["q-negatif-commercial"]["text"] != NEGATIVE_FR:
        raise SystemExit("refus : le négatif commercial du jeu d'acceptance a changé — textes non modifiés exigés.")
    seven_chunks = [c for c in corpus["chunks"] if "seven days" in (c["text"] or "").lower()]
    if len(seven_chunks) != 1:
        raise SystemExit(f"refus : « seven days » doit matcher exactement 1 passage du corpus (trouvé {len(seven_chunks)}).")
    seven_chunk = seven_chunks[0]
    ten_nine_chunks = [
        c for c in corpus["chunks"] if "10.9" in (next(d for d in corpus["documents"] if d["id"] == c["document_id"])["versions"])
    ]
    if not any("200 Mo" in (c["text"] or "") for c in ten_nine_chunks):
        raise SystemExit("refus : le contrôle positif 10.9 (« 200 Mo ») a perdu sa source — fixtures modifiées.")

    # --- Coexistence : E5 + CE chargés ENSEMBLE dans ce runner borné ----------
    from app.embeddings import get_embedding_service
    from app.reranking import RERANK_LOGIT_THRESHOLD, get_reranker_service

    coexistence: dict = {"bound_bytes": MEMORY_BOUND_BYTES, "models": models}
    t0 = time.perf_counter()
    e5 = get_embedding_service()
    e5.warmup()
    coexistence["e5_cold_load_s"] = round(time.perf_counter() - t0, 3)
    t0 = time.perf_counter()
    reranker = get_reranker_service()
    reranker.warmup()
    coexistence["ce_cold_load_s"] = round(time.perf_counter() - t0, 3)
    coexistence["ce_verification"] = reranker.verify_model_files()

    # Vecteurs de requête : exportés (réels) + calculs E5 réels dans le runner.
    t0 = time.perf_counter()
    fresh_positive = e5.encode([QUESTION_FR], kind="query")[0]
    fresh_negative = e5.encode([NEGATIVE_FR], kind="query")[0]
    fresh_109 = e5.encode([QUESTION_109_FR], kind="query")[0]
    coexistence["e5_queries_seconds"] = round(time.perf_counter() - t0, 4)
    cosine_exported_vs_fresh = _cosine(vectors["q-positif-fr-en"]["vector"], fresh_positive)
    coexistence["exported_vector_cross_check_cosine"] = round(cosine_exported_vs_fresh, 6)
    if cosine_exported_vs_fresh < 0.99:
        raise SystemExit("refus : le vecteur E5 exporté ne correspond pas au modèle réel du runner — arrêt.")

    # Mesure « entrée normale » : reclassement du pool réel du corpus.
    pool_normal = [chunk["text"] or "" for chunk in corpus["chunks"]]
    t0 = time.perf_counter()
    reranker.score(QUESTION_FR, pool_normal)
    coexistence["normal_rerank_seconds"] = round(time.perf_counter() - t0, 4)
    coexistence["normal_pairs"] = len(pool_normal)

    # --- RECETTE corpus : chemin SQL + service + reranker réel ----------------
    _recreate_database(ACCEPTANCE_DB)
    settings = _use_database(ACCEPTANCE_DB)
    if settings.reranker_backend != "transformers" or settings.reranker_model != RERANKER_MODEL:
        raise SystemExit("refus : le reclassement réel n'est pas actif dans la recette.")
    engine = create_engine(os.environ["WALLIA_DB_URL"])
    _insert_corpus(engine, corpus)
    controls = _insert_eligibility_controls(engine, corpus)
    chunk_ids = _chunk_ids_by_key(engine)
    seven_chunk_id = chunk_ids[(seven_chunk["document_id"], int(seven_chunk["seq"]))]
    en_doc = next(d for d in corpus["documents"] if d["id"] == seven_chunk["document_id"])
    ten_nine_doc = next(d for d in corpus["documents"] if "10.9" in d["versions"] and d["versions"] == ["10.9"])

    positive = _search(settings, QUESTION_FR, vectors["q-positif-fr-en"]["vector"])
    filtered = _search(settings, QUESTION_FR, vectors["q-positif-fr-en"]["vector"], product="Aster", version="10.10")
    negative = _search(settings, NEGATIVE_FR, vectors["q-negatif-commercial"]["vector"])
    version_109 = _search(settings, QUESTION_FR, vectors["q-positif-fr-en"]["vector"], version="10.9")
    version_unknown = _search(settings, QUESTION_FR, vectors["q-positif-fr-en"]["vector"], version="99.99")
    positive_109 = _search(settings, QUESTION_109_FR, fresh_109, version="10.9")

    quick = _quick_passages(positive.get("sources", []))
    quick_filtered = _quick_passages(filtered.get("sources", []))
    seven_source = next(
        (source for source in positive.get("sources", []) if source["chunk_id"] == seven_chunk_id), None
    )
    seven_scores = [seven_source["score"]] if seven_source else []
    expected_logit_delta = (
        round(abs(seven_scores[0] - EXPECTED_SEVEN_LOGIT), 6) if seven_scores else None
    )
    seven_rank = next(
        (index + 1 for index, source in enumerate(positive.get("sources", [])) if source["chunk_id"] == seven_chunk_id),
        None,
    )
    quick_filtered_rank = next(
        (index + 1 for index, source in enumerate(filtered.get("sources", [])) if source["chunk_id"] == seven_chunk_id),
        None,
    )

    def _control_absent(result: dict) -> bool:
        seen = {c["chunk_id"] for c in result.get("diagnostics", {}).get("candidates", [])} | {
            s["chunk_id"] for s in result.get("sources", [])
        }
        return controls["stale_generation_chunk"] not in seen and controls["not_ready_chunk"] not in seen

    positive_sources_109 = [
        s for s in positive_109.get("sources", []) if s["document_id"] == ten_nine_doc["id"]
    ]
    checks = {
        "positif_seven_days_servi_avec_provenance": {
            "ok": positive.get("status") == "ok"
            and bool(quick)
            and seven_rank is not None
            and seven_rank <= 6
            and seven_source is not None
            and seven_source["document_id"] == en_doc["id"]
            and seven_source["product"] == "Aster"
            and "10.10" in seven_source["versions"]
            and seven_source["language"] == "en"
            and seven_source["page_start"] == 2,
            "detail": "question FR → passage EN (Quick Start) p.2 « seven days », provenance réelle (document/produit/version/langue/page)",
        },
        "positif_seven_days_rang1_et_logit_probe": {
            "ok": seven_rank == 1 and bool(seven_scores) and expected_logit_delta is not None and expected_logit_delta <= 0.001,
            "detail": f"rang réel {seven_rank} ; logit {seven_scores[0] if seven_scores else None} vs trace probe {EXPECTED_SEVEN_LOGIT} (écart {expected_logit_delta})",
        },
        "positif_filtre_aster_10_10": {
            "ok": filtered.get("status") == "ok"
            and bool(quick_filtered)
            and quick_filtered_rank is not None
            and quick_filtered_rank <= 6
            and all(s["product"] == "Aster" and "10.10" in s["versions"] for s in filtered.get("sources", [])),
            "detail": "périmètre product=Aster/version=10.10 : passage « seven days » servi, tous les périmètres respectés",
        },
        "negatif_commercial_zero_source": {
            "ok": negative.get("status") == "no_relevant_source" and not negative.get("sources"),
            "detail": "absence commerciale : aucune source, aucun remplacement",
        },
        "restriction_10_9_sans_fuite_10_10": {
            "ok": version_109.get("status") == "no_relevant_source"
            and not version_109.get("sources")
            and all(
                candidate["document_id"] == ten_nine_doc["id"]
                for candidate in version_109.get("diagnostics", {}).get("candidates", [])
            ),
            "detail": "question FR→EN restreinte à 10.9 : la réponse est ABSENTE du périmètre (aucune source, aucune fuite 10.10) ; les candidats examinés sont tous 10.9",
        },
        "question_10_9_repondable": {
            "ok": positive_109.get("status") == "ok"
            and bool(positive_sources_109)
            and all("10.9" in s["versions"] and "10.10" not in s["versions"] for s in positive_109.get("sources", []))
            and any("200 Mo" in (s["text"] or "") for s in positive_109.get("sources", [])),
            "detail": "contrôle indépendant : la question 10.9 (plafond du journal) est répondable dans SON périmètre, sans fuite 10.10",
        },
        "version_99_99_rien": {
            "ok": version_unknown.get("status") == "no_relevant_source"
            and not version_unknown.get("sources")
            and version_unknown.get("diagnostics", {}).get("fused") == 0,
            "detail": "version inconnue : zéro candidat, aucune substitution",
        },
        "hors_corpus_generation_et_statut": {
            "ok": _control_absent(positive) and _control_absent(filtered) and _control_absent(positive_109),
            "detail": "aucun passage de génération périmée ni de document non prêt dans les candidats ou les sources",
        },
        "seuil_gele_et_identite_reranker": {
            "ok": RERANK_LOGIT_THRESHOLD == FROZEN_THRESHOLD
            and FROZEN_THRESHOLD == 1.1491
            and positive["diagnostics"]["reranker"]["threshold"] == FROZEN_THRESHOLD
            and positive["diagnostics"]["reranker"]["model"] == RERANKER_MODEL
            and positive["diagnostics"]["reranker"]["revision"] == RERANKER_REVISION
            and positive["diagnostics"]["reranker"]["state"] == "ready"
            and coexistence["ce_verification"]["weights_sha256_verified"] == RERANKER_SHA256
            and all(s["score_kind"] == "rerank_logit" for s in positive.get("sources", [])),
            "detail": "seuil 1.1491 gelé ; modèle/révision/SHA conformes à docs/reranker-probe.md ; score = logit brut",
        },
    }

    # --- CALIBRATION rejouée sur le chemin intégré (reranker réel) ------------
    calibration_fixture = Path(
        os.environ.get("WALLIA_ACCEPTANCE_CALIBRATION", "/app/fixtures/calibration/calibration.json")
    )
    calibration = json.loads(calibration_fixture.read_text(encoding="utf-8"))
    calibration_vectors_doc = json.loads(calibration_vectors_path.read_text(encoding="utf-8"))
    calibration_vectors = {item["id"]: item["vector"] for item in calibration_vectors_doc["items"]}
    _recreate_database(CALIBRATION_DB)
    settings_cal = _use_database(CALIBRATION_DB)
    engine_cal = create_engine(os.environ["WALLIA_DB_URL"])
    passage_ids = _insert_calibration(engine_cal, calibration, calibration_vectors)

    calibration_outcomes = []
    for question in calibration["positives"]:
        result = _search(settings_cal, question["question"], calibration_vectors[question["id"]])
        served = sorted(pid for pid, doc in passage_ids.items() if doc in {s["document_id"] for s in result["sources"]})
        calibration_outcomes.append(
            {
                "id": question["id"],
                "kind": "positive",
                "status": result["status"],
                "served": served,
                "expected": question["expected"],
                "top_scores": [s["score"] for s in result["sources"][:3]],
                "ok": result["status"] == "ok" and bool(set(served) & set(question["expected"])),
            }
        )
    for question in calibration["negatives"]:
        result = _search(settings_cal, question["question"], calibration_vectors[question["id"]])
        calibration_outcomes.append(
            {
                "id": question["id"],
                "kind": "negative",
                "category": question.get("category"),
                "status": result["status"],
                "served": sorted(pid for pid, doc in passage_ids.items() if doc in {s["document_id"] for s in result["sources"]}),
                "ok": result["status"] == "no_relevant_source" and not result["sources"],
            }
        )
    positives_ok = [o for o in calibration_outcomes if o["kind"] == "positive" and o["ok"]]
    negatives_ok = [o for o in calibration_outcomes if o["kind"] == "negative" and o["ok"]]
    positives_total = [o for o in calibration_outcomes if o["kind"] == "positive"]
    negatives_total = [o for o in calibration_outcomes if o["kind"] == "negative"]
    checks["calibration_integree_14_14"] = {
        "ok": len(positives_ok) == len(positives_total) == 14 and len(negatives_ok) == len(negatives_total) == 14,
        "detail": f"chemin intégré + reranker réel : positifs {len(positives_ok)}/{len(positives_total)}, négatifs {len(negatives_ok)}/{len(negatives_total)}",
    }

    # --- Entrée LONGUE (512 tokens) : latence + mémoire ------------------------
    long_text = _long_text(corpus)
    long_tokens = None
    try:
        long_tokens = len(reranker._tokenizer.encode(long_text))  # lecture d'évidence (modèle chargé localement)
    except Exception:  # noqa: BLE001 - mesure non bloquante
        long_tokens = None
    t0 = time.perf_counter()
    e5.encode([long_text], kind="passage")
    coexistence["long_e5_seconds"] = round(time.perf_counter() - t0, 4)
    t0 = time.perf_counter()
    long_logit = reranker.score(QUESTION_FR, [long_text])[0]
    coexistence["long_rerank_seconds"] = round(time.perf_counter() - t0, 4)
    coexistence["long_chars"] = len(long_text)
    coexistence["long_tokens_before_truncation"] = long_tokens
    coexistence["long_logit_finite"] = math.isfinite(long_logit)
    coexistence["long_truncation_exercised"] = bool(long_tokens is None or long_tokens > 512)

    # --- Mémoire du runner borné (E5 + CE ensemble) ---------------------------
    max_bytes = _read_int("/sys/fs/cgroup/memory.max")
    peak_bytes = _read_int("/sys/fs/cgroup/memory.peak")
    events = _read_events("/sys/fs/cgroup/memory.events")
    coexistence["cgroup_memory_max_bytes"] = max_bytes
    coexistence["cgroup_memory_peak_bytes"] = peak_bytes
    coexistence["cgroup_memory_events"] = events
    coexistence["vm_hwm_kb"] = _vm_hwm_kb()
    coexistence["python"] = sys.version.split()[0]
    try:
        import torch  # noqa: PLC0415

        coexistence["torch"] = torch.__version__
    except Exception:  # noqa: BLE001
        coexistence["torch"] = None
    checks["coexistence_e5_ce_sous_borne_2000_mio"] = {
        "ok": (max_bytes is not None)
        and max_bytes <= MEMORY_BOUND_BYTES
        and (peak_bytes is None or peak_bytes <= max_bytes)
        and events.get("oom_kill", 0) == 0,
        "detail": (
            f"E5+CE dans le même runner : cgroup max={max_bytes} peak={peak_bytes} oom_kill={events.get('oom_kill', 0)} ; "
            f"max ≤ {MEMORY_BOUND_BYTES} octets (2000 Mio)"
        ),
    }

    ok = all(check["ok"] for check in checks.values())
    evidence = {
        "kind": "acceptance-rag-fr-en-isolated",
        "lot": "3d — chemin intégré SQL+service+reranker réel",
        "question": QUESTION_FR,
        "question_109": QUESTION_109_FR,
        "negative": NEGATIVE_FR,
        "code": "snapshot de travail monté en lecture (backend/) exécuté dans le harnais isolé",
        "corpus": {
            "source": corpus["source"],
            "documents": corpus["counts"]["documents"],
            "chunks": corpus["counts"]["chunks"],
            "chunks_sha256": corpus["chunks_sha256"],
            "docling": "documents importés dans la pile vivante via le worker Docling réel (textes/pages/tableaux), exportés en lecture seule",
        },
        "layers": {
            "e5": vectors["_info"],
            "query_vectors_texts_sha256": vectors["_sha"],
            "reranker": {
                "model": RERANKER_MODEL,
                "revision": RERANKER_REVISION,
                "weights_sha256_verified": RERANKER_SHA256,
                "threshold": FROZEN_THRESHOLD,
            },
            "pgvector_ext_version": corpus["vector_ext_version"],
        },
        "controls_hors_corpus": controls,
        "measurements": {
            "positif": {"status": positive.get("status"), "sources": _sources_summary(positive), "pool": positive["diagnostics"].get("pool")},
            "positif_filtre_aster_10_10": {"status": filtered.get("status"), "sources": _sources_summary(filtered)},
            "positif_10_9": {"status": positive_109.get("status"), "sources": _sources_summary(positive_109)},
            "negatif": {"status": negative.get("status"), "sources": _sources_summary(negative)},
            "version_10_9": {"status": version_109.get("status"), "sources": _sources_summary(version_109)},
            "version_99_99": {"status": version_unknown.get("status"), "sources": _sources_summary(version_unknown)},
        },
        "calibration_integree": {
            "dataset": "calibration-nova-boreal (vecteurs E5 réels exportés)",
            "threshold": FROZEN_THRESHOLD,
            "positives_servis": len(positives_ok),
            "negatifs_rejetes": len(negatives_ok),
            "outcomes": calibration_outcomes,
        },
        "coexistence_e5_ce": coexistence,
        "checks": checks,
        "ok": ok,
    }
    evidence_dir.mkdir(parents=True, exist_ok=True)
    target = evidence_dir / "acceptance-rag-fr-en-isolated.json"
    target.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"preuves écrites : {target}")
    for name, check in checks.items():
        print(f"  [{'OK' if check['ok'] else 'ÉCHEC'}] {name} — {check['detail']}")
    print("Résultat acceptance isolée :", "OK" if ok else "ÉCHEC")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
