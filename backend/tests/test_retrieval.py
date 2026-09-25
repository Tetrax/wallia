"""Recherche hybride : filtres avant classement, fusion RRF, barrières, provenance."""
from __future__ import annotations

import uuid

from sqlalchemy import text

from app.retrieval import hybrid_search


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


def test_filters_are_applied_before_ranking(settings):
    from app.db import session_scope

    with session_scope() as db:
        doc_109 = _document(db, title="Guide 10.9", product="Aster", versions=["10.9"])
        _chunk(db, doc_109, seq=1, text_value="procédure voyant ambre version 10.9", vector_index=0)
        doc_1010 = _document(db, title="Guide 10.10", product="Aster", versions=["10.10"])
        _chunk(db, doc_1010, seq=1, text_value="procédure voyant ambre version 10.10 : relever le journal local", vector_index=0)
        doc_official = _document(db, title="Note officielle", product="Aster", versions=["10.10"], scope="official", demo=False)
        _chunk(db, doc_official, seq=1, text_value="note officielle 10.10 sur la saturation du journal", vector_index=0)
        doc_other = _document(db, title="Autre produit", product="Boreal", versions=["10.10"])
        _chunk(db, doc_other, seq=1, text_value="autre produit", vector_index=5)
        db.commit()

        query = _one_hot(0)

        result = hybrid_search(db, settings, query="voyant ambre", query_vector=query, version="10.10")
        titles = {source["title"] for source in result["sources"]}
        assert titles == {"Guide 10.10", "Note officielle"}  # jamais la 10.9
        assert all("10.10" in source["versions"] for source in result["sources"])

        official = hybrid_search(db, settings, query="voyant ambre", query_vector=query, scope="official")
        assert {source["title"] for source in official["sources"]} == {"Note officielle"}

        by_product = hybrid_search(db, settings, query="autre", query_vector=query, product="Boreal")
        assert {source["title"] for source in by_product["sources"]} == {"Autre produit"}

        unknown = hybrid_search(db, settings, query="voyant", query_vector=query, product="ProduitInconnu")
        assert unknown["status"] == "no_relevant_source"
        assert unknown["sources"] == []


def test_unknown_version_is_never_substituted(settings):
    from app.db import session_scope

    with session_scope() as db:
        document = _document(db, title="Guide 10.9", product="Aster", versions=["10.9"])
        _chunk(db, document, seq=1, text_value="voyant ambre 10.9", vector_index=0)
        db.commit()
        result = hybrid_search(db, settings, query="voyant ambre", query_vector=_one_hot(0), version="9.9")
        assert result["status"] == "no_relevant_source"
        assert result["diagnostics"]["filters"] == {"product": None, "version": "9.9", "scope": "all"}


def test_cosine_barrier_and_lexical_rescue(settings):
    from app.db import session_scope

    with session_scope() as db:
        document = _document(db, title="Guide", product="Aster", versions=["10.10"])
        _chunk(db, document, seq=1, text_value="le journal local sature au-delà de 80 %", vector_index=0)
        db.commit()

        # Vecteur orthogonal sans correspondance lexicale : aucune source retenue.
        orthogonal = hybrid_search(db, settings, query="sujet totalement différent", query_vector=_one_hot(200))
        assert orthogonal["status"] == "no_relevant_source"
        assert orthogonal["diagnostics"]["best_cosine"] is not None

        # Correspondance lexicale forte : le passage est retenu malgré un vecteur faible.
        lexical = hybrid_search(db, settings, query="journal local sature", query_vector=_one_hot(200))
        assert lexical["status"] == "ok"
        assert lexical["sources"][0]["score_text"] and lexical["sources"][0]["score_text"] > 0
        assert lexical["diagnostics"]["lexical_match"] is True


def test_rrf_favours_chunks_present_in_both_lists(settings):
    from app.db import session_scope

    with session_scope() as db:
        strong = _document(db, title="Fort", product="Aster", versions=["10.10"])
        _chunk(db, strong, seq=1, text_value="saturation du journal local", vector_index=5)
        weak = _document(db, title="Faible", product="Aster", versions=["10.10"])
        _chunk(db, weak, seq=1, text_value="texte sans rapport", vector_index=6)
        db.commit()

        result = hybrid_search(db, settings, query="saturation journal", query_vector=_one_hot(5))
        assert result["status"] == "ok"
        assert result["sources"][0]["title"] == "Fort"
        # Le passage présent dans les deux listes (vecteur + lexical) obtient le meilleur score.
        assert result["sources"][0]["score_vector"] is not None
        assert result["sources"][0]["score_text"] is not None
        scores = [source["score"] for source in result["sources"]]
        assert scores == sorted(scores, reverse=True)


def test_only_current_generation_is_searchable(settings):
    from app.db import session_scope

    with session_scope() as db:
        document = _document(db, title="Réindexé", product="Aster", versions=["10.10"], generation=2)
        _chunk(db, document, seq=1, text_value="ancienne génération", vector_index=10, generation=1)
        _chunk(db, document, seq=1, text_value="nouvelle génération", vector_index=10, generation=2)
        db.commit()

        result = hybrid_search(db, settings, query="génération", query_vector=_one_hot(10))
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
        assert source["score"] > 0
        assert source["chunk_id"]
        assert uuid.UUID(source["chunk_id"])
