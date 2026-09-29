"""Recherche hybride SQL (pgvector384 + plein texte) puis reclassement CPU.

Une seule chaîne, dans cet ordre :
  1. filtres AVANT sélection : statut `ready`, génération courante, périmètre
     (demo/official), produit, version explicite (une version demandée
     n'inclut jamais les autres documents) ;
  2. deux branches SQL — vecteur (cosinus) et lexical (lexèmes significatifs de
     la question ; mots vides FR/EN, jetons de métadonnées et versions exclus)
     — qui ne servent QU'À SÉLECTIONNER des candidats : aucune barrière
     cosinus/lexicale d'éligibilité n'existe plus (elle éliminait le rappel
     multilingue avant tout classement) ;
  3. fusion RRF des deux listes, puis pool BORNÉ (WALLIA_RETRIEVAL_CANDIDATES,
     30 par défaut, borne dure `MAX_CANDIDATES`) : le reclassement ne voit
     jamais tout le corpus ;
  4. reclassement de chaque paire (question, texte du passage) par le
     cross-encoder réel — score INDIVIDUEL : aucun passage n'est qualifié ni
     entraîné par les autres ; les titres/métadonnées restent de l'attribution,
     jamais une porte d'éligibilité ;
  5. logit brut ≥ seuil GELÉ (1.1491, docs/reranker-probe.md) → servi ; tri par
     logit décroissant, départage déterministe (chunk_id), au plus `top_k`.

Indisponibilité du reclassement (absent, altéré, saturé, échec) :
`status = "retrieval_unavailable"`, sources vides, erreur technique typée avec
message sûr — JAMAIS de repli silencieux vers l'ancienne barrière, jamais un
`no_relevant_source` factice, aucun web automatique. Les signaux bruts
(cosinus, correspondances, rangs, score RRF, score de reclassement) restent
exposés par passage dans `diagnostics["candidates"]` ; `score` des sources est
le logit de reclassement (`score_kind = "rerank_logit"`), pas une probabilité.
"""
from __future__ import annotations

import math
import re
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from .config import Settings
from .reranking import (
    RERANK_LOGIT_THRESHOLD,
    RerankerFailure,
    RerankerUnavailable,
    get_reranker_service,
)

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

# Correspondances DU PASSAGE : information de sélection/diagnostic, jamais une
# porte d'éligibilité (l'éligibilité est le logit de reclassement du passage).
_HITS_EXPR = "(SELECT count(*) FROM lex WHERE c.tsv @@ to_tsquery('simple', quote_literal(lex.lexeme)))"

_VECTOR_SQL = f"""
    WITH lex AS (
        SELECT unnest(CAST(:lex AS text[])) AS lexeme
    )
    SELECT {_BASE_COLUMNS}, 1 - (c.embedding <=> CAST(:qv AS vector)) AS cosine,
           {_HITS_EXPR} AS lexeme_hits
    FROM chunks c
    JOIN documents d ON d.id = c.document_id
    WHERE {_FILTERS}
    ORDER BY c.embedding <=> CAST(:qv AS vector)
    LIMIT :cand
"""

# OR explicite des lexèmes significatifs, sans aucune coupure de fréquence
# documentaire (régression lot3 conservée) : la branche lexicale sélectionne
# des candidats, elle ne qualifie personne.
_LEXICAL_SQL = f"""
    WITH lex AS (
        SELECT unnest(CAST(:lex AS text[])) AS lexeme
    ),
    expr AS (
        SELECT string_agg(quote_literal(lexeme), ' | ') AS q FROM lex
    )
    SELECT {_BASE_COLUMNS}, 1 - (c.embedding <=> CAST(:qv AS vector)) AS cosine,
           ts_rank_cd(c.tsv, to_tsquery('simple', expr.q)) AS rank_text,
           {_HITS_EXPR} AS lexeme_hits
    FROM chunks c
    JOIN documents d ON d.id = c.document_id
    CROSS JOIN expr
    WHERE {_FILTERS} AND expr.q IS NOT NULL AND c.tsv @@ to_tsquery('simple', expr.q)
    ORDER BY rank_text DESC
    LIMIT :cand
"""

_TOKEN_RE = re.compile(r"\w+", re.UNICODE)
_VERSION_RE = re.compile(r"^\d+(?:\.\d+)*$")
MAX_SIGNIFICANT_LEXEMES = 32
# Borne dure du pool de reclassement : aucun scan neural de tout le corpus,
# même si la configuration était poussée à l'extrême.
MAX_CANDIDATES = 64

# Mots vides FR/EN (interrogatifs, articles, prépositions, auxiliaires courants).
# Ils ne doivent jamais contribuer seuls à une correspondance lexicale.
_STOPWORDS = frozenset(
    """
    au aux avec ce ces cet cette dans de des du elle elles en et eux il ils je
    la le les leur leurs lui ma mais me mes moi mon ne nos notre nous on ou où
    par pas pour qu que quel quelle quelles quels qui quoi sa sans se ses son
    sont sous sur ta te tes toi ton tu un une vos votre vous c d j l m n s t y
    à ç été étée étées étés étant suis es est sommes êtes était étaient sera
    seront serais serait avons avez ont ai as eu eue eus eues ayant avoir fait
    faire plus moins très trop aussi alors donc car ni or quand comment
    pourquoi combien ainsi voici voilà
    a an the and or but if then than else of in on at to for with without from
    by as is are was were be been being am do does did done doing has have had
    having how what when where which who whom whose why this that these those
    it its he she they we you i me my our your their his her them us not no nor
    so such can could should would will shall may might must many much more
    most some any all each every both few less least own same other another
    """.split()
)


def _tokenize(text_value: str) -> list[str]:
    """Découpe une question en mots, comme le ferait `to_tsvector('simple', …)`
    (minuscules, séparateurs non alphanumériques ; les mots composés à tiret
    sont exposés par Postgres à la fois entiers et en parties — les parties
    suffisent ici puisque la recherche porte sur les lexèmes eux-mêmes)."""
    return [token.lower() for token in _TOKEN_RE.findall(text_value or "")]


def _significant_lexemes(db: Session, query: str, product: str | None) -> list[str]:
    """Lexèmes de la question réellement utiles à la branche lexicale.

    Exclus : mots vides FR/EN, jetons de métadonnées (nombres et versions,
    noms de produit du paramètre ou des documents indexés). Le nombre de
    passages du corpus n'intervient jamais dans cette sélection.
    """
    product_words: set[str] = set(_tokenize(product or ""))
    rows = db.execute(
        text("SELECT DISTINCT lower(product) FROM documents WHERE product IS NOT NULL AND status = 'ready'")
    ).scalars()
    for value in rows:
        product_words.update(_tokenize(value or ""))
    significant: list[str] = []
    seen: set[str] = set()
    for token in _tokenize(query):
        if len(token) < 2 or token in _STOPWORDS or token in product_words:
            continue
        if _VERSION_RE.match(token):
            continue
        if token in seen:
            continue
        seen.add(token)
        significant.append(token)
        if len(significant) >= MAX_SIGNIFICANT_LEXEMES:
            break
    return significant


def _vector_literal(vector: list[float]) -> str:
    return "[" + ",".join(f"{x:.7f}" for x in vector) + "]"


def _params(
    query_vector: list[float],
    product: str | None,
    version: str | None,
    scope: str,
    cand: int,
    lexemes: list[str],
) -> dict[str, Any]:
    return {
        "qv": _vector_literal(query_vector),
        "product": product,
        "version": version,
        "scope": scope,
        "cand": cand,
        "lex": lexemes,
    }


def _entry_from_row(row: Any) -> dict[str, Any]:
    """Entrée de fusion construite depuis une ligne (vecteur ou lexicale) ;
    chaque entrée porte ses signaux bruts — cosinus, correspondances, rangs —
    jamais un score inventé."""
    return {
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
        "rank_text": None,
        "cosine": float(row["cosine"]) if row["cosine"] is not None else None,
        "text_rank": None,
        "lexeme_hits": int(row["lexeme_hits"] or 0),
        "score_rrf": 0.0,
        "score_rerank": None,
    }


def _reranker_diagnostics(service: Any) -> dict[str, Any]:
    info = service.info
    return {
        "backend": info.get("backend"),
        "model": info.get("model"),
        "revision": info.get("revision"),
        "threshold": info.get("threshold"),
        "state": info.get("state"),
        "error_type": info.get("error_type"),
    }


def _candidate_diagnostics(entries: list[dict[str, Any]], *, with_rerank: bool) -> list[dict[str, Any]]:
    """Trace brute par passage : signaux de sélection (vecteur/lexical/RRF) et,
    si le reclassement a abouti, logit et verdict individuel — sans faux score
    ni moyenne opaque ; `served` signifie « logit ≥ seuil gelé »."""
    ordered = sorted(
        entries,
        key=(lambda e: (-e["score_rerank"], e["chunk_id"])) if with_rerank else (lambda e: (-e["score_rrf"], e["chunk_id"])),
    )
    trace: list[dict[str, Any]] = []
    for entry in ordered:
        item: dict[str, Any] = {
            "chunk_id": entry["chunk_id"],
            "document_id": entry["document_id"],
            "cosine": round(entry["cosine"], 4) if entry["cosine"] is not None else None,
            "lexeme_hits": entry["lexeme_hits"],
            "text_rank": round(entry["text_rank"], 4) if entry["text_rank"] is not None else None,
            "rank_vector": entry["rank_vector"],
            "rank_text": entry["rank_text"],
            "score_rrf": round(entry["score_rrf"], 6),
        }
        if with_rerank:
            item["score_rerank"] = round(entry["score_rerank"], 6)
            item["served"] = entry["score_rerank"] >= RERANK_LOGIT_THRESHOLD
        trace.append(item)
    return trace


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
    reranker: Any | None = None,
) -> dict[str, Any]:
    top_k = top_k or settings.retrieval_top_k
    cand = min(max(settings.retrieval_candidates, top_k * 3), MAX_CANDIDATES)
    significant = _significant_lexemes(db, query, product)
    params = _params(query_vector, product, version, scope, cand, significant)

    rows_vector = db.execute(text(_VECTOR_SQL), params).mappings().all()

    rows_lexical: list[Any] = []
    if significant:
        rows_lexical = (
            db.execute(text(_LEXICAL_SQL), params).mappings().all()
        )

    fused: dict[str, dict[str, Any]] = {}
    for rank, row in enumerate(rows_vector, start=1):
        entry = fused.setdefault(str(row["chunk_id"]), _entry_from_row(row))
        entry["rank_vector"] = rank
        entry["cosine"] = float(row["cosine"]) if row["cosine"] is not None else entry["cosine"]
        entry["lexeme_hits"] = max(entry["lexeme_hits"], int(row["lexeme_hits"] or 0))
    for rank, row in enumerate(rows_lexical, start=1):
        entry = fused.setdefault(str(row["chunk_id"]), _entry_from_row(row))
        entry["rank_text"] = rank
        entry["text_rank"] = float(row["rank_text"]) if row["rank_text"] is not None else None
        entry["cosine"] = float(row["cosine"]) if row["cosine"] is not None else entry["cosine"]
        entry["lexeme_hits"] = max(entry["lexeme_hits"], int(row["lexeme_hits"] or 0))

    rrf_k = settings.retrieval_rrf_k
    for entry in fused.values():
        score = 0.0
        if entry["rank_vector"]:
            score += 1.0 / (rrf_k + entry["rank_vector"])
        if entry["rank_text"]:
            score += 1.0 / (rrf_k + entry["rank_text"])
        entry["score_rrf"] = score

    # Pool BORNÉ : le reclassement ne voit jamais plus de `cand` passages, et
    # aucun score n'y est entraîné par les autres passages.
    pool = sorted(fused.values(), key=lambda e: (-e["score_rrf"], e["chunk_id"]))[:cand]

    best_cosine = max((e["cosine"] or 0.0) for e in fused.values()) if fused else None
    best_text_rank = max((e["text_rank"] or 0.0) for e in fused.values()) if fused else None

    service = reranker or get_reranker_service()

    diagnostics: dict[str, Any] = {
        "candidates_vector": len(rows_vector),
        "candidates_lexical": len(rows_lexical),
        "fused": len(fused),
        "pool": len(pool),
        "pool_limit": cand,
        "best_cosine": best_cosine,
        "best_text_rank": best_text_rank,
        "lexical_lexemes": significant,
        "filters": {"product": product, "version": version, "scope": scope},
        # Rempli APRÈS la tentative de scoring : l'état reflète le chargement
        # paresseux réel (unloaded → ready) et la panne éventuelle, jamais
        # l'état d'avant l'inférence.
        "reranker": None,
        "candidates": [],
        "kept": 0,
    }

    if not pool:
        diagnostics["reranker"] = _reranker_diagnostics(service)
        return {"status": "no_relevant_source", "sources": [], "diagnostics": diagnostics}

    try:
        logits = service.score(query, [e["text"] or "" for e in pool])
        if len(logits) != len(pool):
            raise RerankerUnavailable("réponse de reclassement incomplète")
        floats = [float(value) for value in logits]
        if any(not math.isfinite(value) for value in floats):
            # Sortie non finie = panne technique typée, jamais no-source.
            raise RerankerFailure(RerankerFailure.safe_message)
    except RerankerUnavailable as exc:
        # Typé et sûr : jamais un repli silencieux, jamais no_relevant_source.
        # Les candidats du pool restent tracés (signaux de SÉLECTION bruts,
        # sans logit) pour l'exploitation ; l'état du service est relu APRÈS
        # l'échec pour rester réellement observable.
        diagnostics["reranker"] = _reranker_diagnostics(service)
        diagnostics["candidates"] = _candidate_diagnostics(pool, with_rerank=False)
        diagnostics["error"] = {
            "type": exc.error_type,
            "message": exc.safe_message,
        }
        return {"status": "retrieval_unavailable", "sources": [], "diagnostics": diagnostics}

    diagnostics["reranker"] = _reranker_diagnostics(service)

    for entry, logit in zip(pool, floats):
        entry["score_rerank"] = float(logit)

    served = [entry for entry in pool if entry["score_rerank"] >= RERANK_LOGIT_THRESHOLD]
    ranked = sorted(served, key=lambda e: (-e["score_rerank"], e["chunk_id"]))

    # Diagnostic par passage : signaux bruts et verdict individuel.
    diagnostics["candidates"] = _candidate_diagnostics(pool, with_rerank=True)

    if not ranked:
        return {"status": "no_relevant_source", "sources": [], "diagnostics": diagnostics}

    sources = [
        {
            # Discriminant de provenance : les sources corpus restent seules à
            # porter identifiants/scores/pages ; le web (ajouté par le chat)
            # utilise le même champ avec ses propres garanties.
            "source_type": "corpus",
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
            # `score` = logit brut du reclassement (classement réel) — jamais
            # une probabilité ; `score_kind` le déclare explicitement.
            "score": round(e["score_rerank"], 6),
            "score_kind": "rerank_logit",
            "score_vector": round(e["cosine"], 4) if e["cosine"] is not None else None,
            "score_text": round(e["text_rank"], 4) if e["text_rank"] is not None else None,
        }
        for e in ranked[:top_k]
    ]
    diagnostics["kept"] = len(sources)
    return {"status": "ok", "sources": sources, "diagnostics": diagnostics}
