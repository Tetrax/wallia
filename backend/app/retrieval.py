"""Recherche hybride (vecteur cosine + plein texte PostgreSQL) avec fusion RRF.

Filtres appliqués AVANT le classement : périmètre (demo/official), produit,
version explicite (une version demandée n'inclut jamais les autres documents).
Une version inconnue n'est jamais inventée : si le filtre ne correspond à rien,
la recherche retourne « aucune source pertinente ».
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from .config import Settings

_BASE_COLUMNS = """
    c.id AS chunk_id,
    c.document_id,
    c.text,
    c.page_start,
    c.page_end,
    c.section,
    c.kind,
    c.generation,
    d.title,
    d.product,
    d.versions,
    d.demo,
    d.scope,
    d.language,
    d.original_filename
"""

_FILTERS = """
    d.status = 'ready'
    AND c.generation = d.current_generation
    AND (CAST(:scope AS text) = 'all' OR d.scope = CAST(:scope AS text))
    AND (CAST(:product AS text) IS NULL OR d.product = CAST(:product AS text))
    AND (CAST(:version AS text) IS NULL OR d.versions @> ARRAY[CAST(:version AS text)])
"""

_VECTOR_SQL = f"""
    SELECT {_BASE_COLUMNS}, 1 - (c.embedding <=> CAST(:qv AS vector)) AS cosine
    FROM chunks c
    JOIN documents d ON d.id = c.document_id
    WHERE {_FILTERS}
    ORDER BY c.embedding <=> CAST(:qv AS vector)
    LIMIT :cand
"""

_LEXICAL_SQL = f"""
    SELECT {_BASE_COLUMNS}, ts_rank_cd(c.tsv, q.query) AS rank_text
    FROM chunks c
    JOIN documents d ON d.id = c.document_id
    CROSS JOIN (SELECT plainto_tsquery('simple', CAST(:q AS text)) AS query) q
    WHERE {_FILTERS} AND c.tsv @@ q.query
    ORDER BY rank_text DESC
    LIMIT :cand
"""


def _vector_literal(vector: list[float]) -> str:
    return "[" + ",".join(f"{x:.7f}" for x in vector) + "]"


def _params(query: str, query_vector: list[float], product: str | None, version: str | None, scope: str, cand: int) -> dict[str, Any]:
    return {
        "q": query,
        "qv": _vector_literal(query_vector),
        "product": product,
        "version": version,
        "scope": scope,
        "cand": cand,
    }


def hybrid_search(
    db: Session,
    settings: Settings,
    *,
    query: str,
    query_vector: list[float],
    product: str | None = None,
    version: str | None = None,
    scope: str = "all",
    top_k: int | None = None,
) -> dict[str, Any]:
    top_k = top_k or settings.retrieval_top_k
    cand = max(settings.retrieval_candidates, top_k * 3)
    params = _params(query, query_vector, product, version, scope, cand)

    rows_vector = db.execute(text(_VECTOR_SQL), params).mappings().all()
    rows_lexical = db.execute(text(_LEXICAL_SQL), params).mappings().all()

    fused: dict[str, dict[str, Any]] = {}
    for rank, row in enumerate(rows_vector, start=1):
        entry = fused.setdefault(
            str(row["chunk_id"]),
            {
                "chunk_id": str(row["chunk_id"]),
                "document_id": str(row["document_id"]),
                "text": row["text"],
                "page_start": row["page_start"],
                "page_end": row["page_end"],
                "section": row["section"],
                "kind": row["kind"],
                "title": row["title"],
                "product": row["product"],
                "versions": list(row["versions"] or []),
                "demo": bool(row["demo"]),
                "scope": row["scope"],
                "language": row["language"],
                "original_filename": row["original_filename"],
                "rank_vector": rank,
                "rank_text": None,
                "cosine": float(row["cosine"]) if row["cosine"] is not None else None,
                "text_rank": None,
            },
        )
        entry["rank_vector"] = rank
    for rank, row in enumerate(rows_lexical, start=1):
        entry = fused.setdefault(
            str(row["chunk_id"]),
            {
                "chunk_id": str(row["chunk_id"]),
                "document_id": str(row["document_id"]),
                "text": row["text"],
                "page_start": row["page_start"],
                "page_end": row["page_end"],
                "section": row["section"],
                "kind": row["kind"],
                "title": row["title"],
                "product": row["product"],
                "versions": list(row["versions"] or []),
                "demo": bool(row["demo"]),
                "scope": row["scope"],
                "language": row["language"],
                "original_filename": row["original_filename"],
                "rank_vector": None,
                "rank_text": rank,
                "cosine": None,
                "text_rank": None,
            },
        )
        entry["rank_text"] = rank
        entry["text_rank"] = float(row["rank_text"]) if row["rank_text"] is not None else None

    rrf_k = settings.retrieval_rrf_k
    for entry in fused.values():
        score = 0.0
        if entry["rank_vector"]:
            score += 1.0 / (rrf_k + entry["rank_vector"])
        if entry["rank_text"]:
            score += 1.0 / (rrf_k + entry["rank_text"])
        entry["score"] = score

    ranked = sorted(fused.values(), key=lambda e: e["score"], reverse=True)
    best_cosine = max((e["cosine"] or 0.0) for e in fused.values()) if fused else None
    best_text_rank = max((e["text_rank"] or 0.0) for e in fused.values()) if fused else None
    lexical_hit = any(e["rank_text"] for e in fused.values())

    diagnostics = {
        "candidates_vector": len(rows_vector),
        "candidates_lexical": len(rows_lexical),
        "fused": len(fused),
        "best_cosine": best_cosine,
        "best_text_rank": best_text_rank,
        "min_cosine": settings.retrieval_min_cosine,
        "lexical_match": lexical_hit,
        "filters": {"product": product, "version": version, "scope": scope},
    }

    above_barrier = bool(fused) and (
        lexical_hit or (best_cosine is not None and best_cosine >= settings.retrieval_min_cosine)
    )
    if not above_barrier:
        return {"status": "no_relevant_source", "sources": [], "diagnostics": diagnostics}

    # Cohérence statut/liste : quand la barrière est franchie, on ne sert que des
    # candidats réellement pertinents — correspondance lexicale exacte, ou
    # similarité vectorielle au-dessus de la barrière. Aucun candidat faible en
    # remplissage : une source citée doit pouvoir être défendue.
    relevant = [
        e
        for e in ranked
        if e["rank_text"] or (e["cosine"] is not None and e["cosine"] >= settings.retrieval_min_cosine)
    ]
    if relevant:
        ranked = relevant
    diagnostics["kept"] = len(ranked)

    sources = [
        {
            "chunk_id": e["chunk_id"],
            "document_id": e["document_id"],
            "title": e["title"],
            "product": e["product"],
            "versions": e["versions"],
            "demo": e["demo"],
            "scope": e["scope"],
            "language": e["language"],
            "page_start": e["page_start"],
            "page_end": e["page_end"],
            "section": e["section"],
            "kind": e["kind"],
            "text": e["text"][:1600],
            "score": round(e["score"], 6),
            "score_vector": round(e["cosine"], 4) if e["cosine"] is not None else None,
            "score_text": round(e["text_rank"], 4) if e["text_rank"] is not None else None,
        }
        for e in ranked[:top_k]
    ]
    return {"status": "ok", "sources": sources, "diagnostics": diagnostics}
