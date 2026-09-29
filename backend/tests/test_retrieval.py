"""Recherche hybride SQL + reclassement : filtres avant sélection, pool borné,
score INDIVIDUEL par passage (seuil gelé 1.1491), aucune accumulation.

Les anciennes barrières d'éligibilité (cosinus 0.87 ; correspondances
lexicales ≥2 ou corroborées) ont été REMPLACÉES par le reclassement
cross-encoder réel (docs/reranker-probe.md). Dans cette suite, le backend
`fixture` (mécanique de test déclarée : `-1.0 + 2.0 × jetons communs`, plafond
3) rend la mécanique observable et déterministe :

  * vecteur/lexical ne font QUE sélectionner des candidats — ils ne
    qualifient plus personne ;
  * un passage n'est servi QUE si son propre logit atteint 1.1491 ; aucun
    passage n'est entraîné par les autres passages du corpus ;
  * les protections historiques (pas d'accumulation à travers le corpus, pas
    de bruit lexical servi par une source forte, filtres avant classement)
    sont conservées et re-exprimées sur ces invariants.

Changement de sensibilité assumé (lot3d) : la question réduite à un unique
lexème significatif n'a plus de « porte 1/1 » propre — le classement dépend du
reclassement seul ; et les textes des fixtures de test sont choisis pour être
réellement pertinents (≥ 2 jetons partagés) là où le test porte sur les
filtres ou la fusion, pas sur l'éligibilité.
"""
from __future__ import annotations

import uuid

from sqlalchemy import text

from app.retrieval import MAX_CANDIDATES, hybrid_search
from app.reranking import RERANK_LOGIT_THRESHOLD, fixture_logit


def _one_hot(index: int, dim: int = 384) -> list[float]:
    vector = [0.0] * dim
    vector[index % dim] = 1.0
    return vector


def _literal(vector: list[float]) -> str:
    return "[" + ",".join(f"{x:.6f}" for x in vector) + "]"


def _document(db, *, title, product=None, versions=(), scope="demo", demo=True, generation=1, language="fr"):
    from app.models import Document

    document = Document(
        title=title,
        origin="fixture",
        product=product,
        versions=list(versions),
        language=language,
        demo=demo,
        scope=scope,
        status="ready",
        stored_relpath="",
        original_filename=f"{title}.pdf",
        content_type="application/pdf",
        size_bytes=10,
        page_count=1,
        checksum_sha256=f"test-{uuid.uuid4().hex}",
        current_generation=generation if generation else 0,
    )
    db.add(document)
    db.flush()
    return document


def _chunk(db, document, *, seq, text_value, vector_index, page=1, generation=1, kind="text"):
    db.execute(
        text(
            """
            INSERT INTO chunks (document_id, generation, seq, text, page_start, page_end, section, kind, token_count, embedding)
            VALUES (:doc, :gen, :seq, :txt, :page, :page, NULL, :kind, 10, CAST(:emb AS vector))
            """
        ),
        {
            "doc": str(document.id),
            "gen": generation,
            "seq": seq,
            "txt": text_value,
            "page": page,
            "kind": kind,
            "emb": _literal(_one_hot(vector_index)),
        },
    )


def _trace(result: dict) -> dict[str, dict]:
    """Diagnostic PAR PASSAGE : signaux bruts, score RRF, logit et verdict."""
    return {entry["chunk_id"]: entry for entry in result["diagnostics"]["candidates"]}


def test_filters_are_applied_before_ranking(settings):
    from app.db import session_scope

    with session_scope() as db:
        doc_109 = _document(db, title="Guide 10.9", product="Aster", versions=["10.9"])
        _chunk(db, doc_109, seq=1, text_value="procédure voyant ambre version 10.9", vector_index=0)
        doc_1010 = _document(db, title="Guide 10.10", product="Aster", versions=["10.10"])
        _chunk(db, doc_1010, seq=1, text_value="procédure voyant ambre version 10.10 : relever le journal local", vector_index=0)
        doc_official = _document(db, title="Note officielle", product="Aster", versions=["10.10"], scope="official", demo=False)
        _chunk(db, doc_official, seq=1, text_value="note officielle 10.10 : voyant ambre", vector_index=0)
        doc_other = _document(db, title="Autre produit", product="Boreal", versions=["10.10"])
        _chunk(db, doc_other, seq=1, text_value="autre produit borealis", vector_index=5)
        db.commit()

        query = _one_hot(0)

        result = hybrid_search(db, settings, query="voyant ambre", query_vector=query, version="10.10")
        titles = {source["title"] for source in result["sources"]}
        assert titles == {"Guide 10.10", "Note officielle"}  # jamais la 10.9
        assert all("10.10" in source["versions"] for source in result["sources"])
        # La 10.9 aurait été pertinente par son texte : seuls les filtres
        # l'ont exclue, avant toute sélection ou tout reclassement.
        assert all(source["title"] != "Guide 10.9" for source in result["sources"])

        official = hybrid_search(db, settings, query="voyant ambre", query_vector=query, scope="official")
        assert {source["title"] for source in official["sources"]} == {"Note officielle"}

        by_product = hybrid_search(db, settings, query="autre produit", query_vector=query, product="Boreal")
        assert {source["title"] for source in by_product["sources"]} == {"Autre produit"}

        unknown = hybrid_search(db, settings, query="voyant ambre", query_vector=query, product="ProduitInconnu")
        assert unknown["status"] == "no_relevant_source"
        assert unknown["sources"] == []
        # Aucun candidat : le reclassement n'est même pas sollicité.
        assert unknown["diagnostics"]["pool"] == 0


def test_unknown_version_is_never_substituted(settings):
    from app.db import session_scope

    with session_scope() as db:
        document = _document(db, title="Guide 10.9", product="Aster", versions=["10.9"])
        _chunk(db, document, seq=1, text_value="voyant ambre 10.9", vector_index=0)
        db.commit()
        result = hybrid_search(db, settings, query="voyant ambre", query_vector=_one_hot(0), version="9.9")
        assert result["status"] == "no_relevant_source"
        assert result["diagnostics"]["filters"] == {"product": None, "version": "9.9", "scope": "all"}


def test_reranker_scores_each_passage_individually(settings):
    from app.db import session_scope

    with session_scope() as db:
        document = _document(db, title="Guide", product="Aster", versions=["10.10"])
        _chunk(db, document, seq=1, text_value="le journal local sature au-delà de 80 %", vector_index=0)
        db.commit()

        # Aucun jeton commun question/passage : le reclassement ne sert rien —
        # l'ancienne barrière cosinus n'existe plus, le logit décide seul.
        orthogonal = hybrid_search(db, settings, query="sujet totalement différent", query_vector=_one_hot(200))
        assert orthogonal["status"] == "no_relevant_source"
        assert orthogonal["diagnostics"]["best_cosine"] is not None
        trace = _trace(orthogonal)
        assert trace and all(not entry["served"] for entry in trace.values())
        assert all(entry["score_rerank"] == fixture_logit("sujet totalement différent", "le journal local sature au-delà de 80 %") for entry in trace.values())

        # Correspondance réelle (3 jetons partagés) : le passage est servi PAR
        # SON PROPRE logit, au-dessus du seuil gelé.
        lexical = hybrid_search(db, settings, query="journal local sature", query_vector=_one_hot(200))
        assert lexical["status"] == "ok"
        assert lexical["sources"][0]["score_text"] and lexical["sources"][0]["score_text"] > 0
        served = _trace(lexical)[lexical["sources"][0]["chunk_id"]]
        assert served["served"] is True
        assert served["score_rerank"] == fixture_logit("journal local sature", "le journal local sature au-delà de 80 %")
        assert served["score_rerank"] >= RERANK_LOGIT_THRESHOLD
        assert lexical["sources"][0]["score_kind"] == "rerank_logit"


def test_disjoint_matches_never_accumulate(settings):
    """Protection lot3 conservée : deux termes répartis dans deux passages
    différents ne qualifient NI l'un NI l'autre — il n'existe aucune
    accumulation à travers le corpus ; chaque logit est celui de sa paire."""
    from app.db import session_scope

    with session_scope() as db:
        doc_a = _document(db, title="Alpha", product="Aster", versions=["10.10"])
        _chunk(db, doc_a, seq=1, text_value="zeppelin", vector_index=0)
        doc_b = _document(db, title="Beta", product="Aster", versions=["10.10"])
        _chunk(db, doc_b, seq=1, text_value="quasar", vector_index=1)
        db.commit()

        result = hybrid_search(db, settings, query="zeppelin quasar", query_vector=_one_hot(40))
        assert result["status"] == "no_relevant_source"
        assert result["sources"] == []
        trace = _trace(result)
        assert len(trace) == 2
        # Chaque passage porte SON logit (1 jeton commun → +1.0, sous le seuil).
        assert all(entry["score_rerank"] == 1.0 and entry["served"] is False for entry in trace.values())


def test_no_passage_is_dragged_in_by_another(settings):
    """Protection lot3 conservée : un passage faible n'est JAMAIS qualifié par
    la présence d'un passage fort ; retirer le fort ne change rien pour lui."""
    from app.db import session_scope

    with session_scope() as db:
        strong = _document(db, title="Fort", product="Aster", versions=["10.10"])
        _chunk(db, strong, seq=1, text_value="saturation du journal local", vector_index=5)
        noise = _document(db, title="Bruit", product="Aster", versions=["10.10"])
        _chunk(db, noise, seq=1, text_value="saturation alpha", vector_index=6)
        db.commit()

        result = hybrid_search(db, settings, query="saturation journal zeppelin", query_vector=_one_hot(5))
        assert result["status"] == "ok"
        assert [source["title"] for source in result["sources"]] == ["Fort"]
        trace = _trace(result)
        noise_entry = next(e for e in trace.values() if e["document_id"] == str(noise.id))
        assert noise_entry["score_rerank"] == 1.0  # 1 seul jeton (« saturation »)
        assert noise_entry["served"] is False

        # Indépendance : sans le passage fort, le bruit garde EXACTEMENT le
        # même verdict et le même logit — il n'a jamais été entraîné.
        db.execute(text("DELETE FROM chunks WHERE document_id = :doc"), {"doc": str(strong.id)})
        db.execute(text("DELETE FROM documents WHERE id = :doc"), {"doc": str(strong.id)})
        db.commit()
        without_strong = hybrid_search(db, settings, query="saturation journal zeppelin", query_vector=_one_hot(5))
        assert without_strong["status"] == "no_relevant_source"
        again = next(e for e in _trace(without_strong).values() if e["document_id"] == str(noise.id))
        assert again["score_rerank"] == noise_entry["score_rerank"]
        assert again["served"] is False


def test_frozen_threshold_decision_boundary(settings):
    """Seuil GELÉ 1.1491 : 1 jeton partagé (+1.0) n'est pas servi ; 2 jetons
    (+3.0) le sont. Le seuil n'est ni un réglage ni une probabilité."""
    from app.db import session_scope

    with session_scope() as db:
        weak = _document(db, title="Faible", product="Aster", versions=["10.10"])
        _chunk(db, weak, seq=1, text_value="journal epsilon", vector_index=0)
        strong = _document(db, title="Fort", product="Aster", versions=["10.10"])
        _chunk(db, strong, seq=1, text_value="journal delta zeta", vector_index=1)
        db.commit()

        result = hybrid_search(db, settings, query="journal delta zeta", query_vector=_one_hot(200))
        assert result["status"] == "ok"
        assert [source["title"] for source in result["sources"]] == ["Fort"]
        assert result["diagnostics"]["reranker"]["threshold"] == RERANK_LOGIT_THRESHOLD
        # 1 jeton partagé → +1.0 : sous le seuil gelé.
        weak_entry = next(e for e in _trace(result).values() if e["document_id"] == str(weak.id))
        assert weak_entry["score_rerank"] == 1.0
        assert weak_entry["score_rerank"] < RERANK_LOGIT_THRESHOLD
        assert weak_entry["served"] is False


def test_rrf_favours_chunks_present_in_both_lists(settings):
    from app.db import session_scope

    with session_scope() as db:
        strong = _document(db, title="Fort", product="Aster", versions=["10.10"])
        _chunk(db, strong, seq=1, text_value="saturation du journal", vector_index=5)
        strongest = _document(db, title="Très fort", product="Aster", versions=["10.10"])
        _chunk(db, strongest, seq=1, text_value="saturation journal local immédiat", vector_index=5)
        weak = _document(db, title="Faible", product="Aster", versions=["10.10"])
        _chunk(db, weak, seq=1, text_value="texte sans rapport", vector_index=6)
        db.commit()

        result = hybrid_search(db, settings, query="saturation journal local", query_vector=_one_hot(5))
        assert result["status"] == "ok"
        # Tri par logit décroissant (départage déterministe), pas par RRF seul.
        titles = [source["title"] for source in result["sources"]]
        assert titles == ["Très fort", "Fort"]
        scores = [source["score"] for source in result["sources"]]
        assert scores == sorted(scores, reverse=True)
        trace = _trace(result)
        fort = next(e for e in trace.values() if e["document_id"] == str(strong.id))
        faible = next(e for e in trace.values() if e["document_id"] == str(weak.id))
        assert fort["score_rrf"] > faible["score_rrf"]
        # Le passage présent dans les deux listes (vecteur + lexical) porte
        # bien ses deux signaux de sélection.
        assert fort["rank_vector"] is not None and fort["rank_text"] is not None
        assert fort["served"] is True and faible["served"] is False


def test_pool_is_bounded(settings):
    """Le pool de reclassement ne dépasse jamais la borne effective."""
    from app.db import session_scope

    with session_scope() as db:
        for index in range(9):
            document = _document(db, title=f"Doc {index}", product="Aster", versions=["10.10"])
            _chunk(db, document, seq=1, text_value=f"saturation journal item {index}", vector_index=index)
        db.commit()

        small = hybrid_search(db, settings, query="saturation journal", query_vector=_one_hot(0), top_k=1)
        assert small["diagnostics"]["pool"] == 9
        assert small["diagnostics"]["pool_limit"] == min(
            max(settings.retrieval_candidates, 3), MAX_CANDIDATES
        )
        assert settings.retrieval_candidates <= MAX_CANDIDATES


def test_only_current_generation_is_searchable(settings):
    from app.db import session_scope

    with session_scope() as db:
        document = _document(db, title="Réindexé", product="Aster", versions=["10.10"], generation=2)
        _chunk(db, document, seq=1, text_value="ancienne génération", vector_index=10, generation=1)
        _chunk(db, document, seq=1, text_value="nouvelle génération", vector_index=10, generation=2)
        db.commit()

        result = hybrid_search(db, settings, query="nouvelle génération", query_vector=_one_hot(10))
        assert result["status"] == "ok"
        assert all("nouvelle génération" in source["text"] for source in result["sources"])


def test_source_payload_exposes_citation_metadata(settings):
    from app.db import session_scope

    with session_scope() as db:
        document = _document(db, title="Guide cité", product="Aster", versions=["10.9", "10.10"], language="fr")
        _chunk(db, document, seq=1, text_value="extrait citable", vector_index=3, page=7, kind="table")
        db.commit()
        result = hybrid_search(db, settings, query="extrait citable", query_vector=_one_hot(3))
        source = result["sources"][0]
        assert source["document_id"] == str(document.id)
        assert source["page_start"] == 7 and source["page_end"] == 7
        assert source["versions"] == ["10.9", "10.10"]
        assert source["kind"] == "table"
        assert source["demo"] is True
        assert source["scope"] == "demo"
        assert source["score_kind"] == "rerank_logit"
        assert source["score"] >= RERANK_LOGIT_THRESHOLD
        assert source["chunk_id"]
        assert uuid.UUID(source["chunk_id"])


def test_small_corpus_never_drops_strong_lexemes(settings):
    """Régression : un corpus réduit à UN passage ne doit jamais éliminer les
    lexèmes exacts de la question ; la branche lexicale sélectionne le
    candidat, le reclassement le sert sur son propre logit."""
    from app.db import session_scope

    with session_scope() as db:
        document = _document(db, title="Petit corpus", product="Aster", versions=["10.10"])
        _chunk(db, document, seq=1, text_value="rotation du journal local", vector_index=0)
        db.commit()

        result = hybrid_search(db, settings, query="rotation journal", query_vector=_one_hot(200))
        assert result["status"] == "ok"
        entry = _trace(result)[result["sources"][0]["chunk_id"]]
        assert entry["score_rerank"] == fixture_logit("rotation journal", "rotation du journal local")
        assert entry["served"] is True
        assert result["sources"][0]["score_text"] and result["sources"][0]["score_text"] > 0


def test_stopwords_and_metadata_never_form_lexical_candidates(settings):
    """Les mots vides FR/EN et les jetons produit/version ne forment JAMAIS de
    candidat lexical (sélection) ; le vecteur sélectionne le passage, et le
    reclassement décide sur la paire — ici : sous le seuil, aucune source."""
    from app.db import session_scope

    with session_scope() as db:
        document = _document(db, title="Bruit", product="Aster", versions=["10.10"])
        _chunk(db, document, seq=1, text_value="aster 10.10 de la journal", vector_index=0)
        db.commit()

        result = hybrid_search(
            db, settings, query="de la le pour dans Aster 10.10", query_vector=_one_hot(200)
        )
        assert result["status"] == "no_relevant_source"
        assert result["sources"] == []
        assert result["diagnostics"]["lexical_lexemes"] == []
        trace = _trace(result)
        assert trace and all(not entry["served"] for entry in trace.values())


def test_metadata_tokens_do_not_count_towards_coverage(settings):
    """Produit et version restent des jetons de métadonnées pour la sélection
    lexicale ; le reclassement, lui, juge la paire question/texte seule."""
    from app.db import session_scope

    with session_scope() as db:
        document = _document(db, title="Aster doc", product="Aster", versions=["10.10"])
        _chunk(db, document, seq=1, text_value="voyant ambre", vector_index=0)
        db.commit()

        result = hybrid_search(db, settings, query="Aster 10.10 voyant ambre", query_vector=_one_hot(200))
        diagnostics = result["diagnostics"]
        assert diagnostics["lexical_lexemes"] == ["voyant", "ambre"]
        entry = _trace(result)[result["sources"][0]["chunk_id"]]
        assert entry["served"] is True
        assert result["status"] == "ok"


def test_single_shared_token_stays_below_threshold(settings):
    """Une seule correspondance exacte dans le passage ne suffit plus : le
    logit (+1.0 en fixture) reste sous le seuil gelé ; deux correspondances le
    franchissent. (L'ancienne porte « question mono-lexème » n'existe plus :
    le classement dépend du reclassement seul.)"""
    from app.db import session_scope

    with session_scope() as db:
        document = _document(db, title="Couverture", product="Aster", versions=["10.10"])
        _chunk(db, document, seq=1, text_value="journal local", vector_index=0)
        db.commit()

        partial = hybrid_search(db, settings, query="journal alpha beta gamma", query_vector=_one_hot(200))
        assert partial["status"] == "no_relevant_source"
        assert any(entry["score_rerank"] == 1.0 and entry["served"] is False for entry in _trace(partial).values())

        full = hybrid_search(db, settings, query="journal local", query_vector=_one_hot(200))
        assert full["status"] == "ok"
        entry = _trace(full)[full["sources"][0]["chunk_id"]]
        assert entry["served"] is True
        assert entry["score_rerank"] == 3.0
