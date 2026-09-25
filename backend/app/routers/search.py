"""Recherche documentaire exposée (indépendante du chat)."""
from __future__ import annotations

import dataclasses

from fastapi import APIRouter, Depends, HTTPException, status

from ..app_settings import retrieval_config
from ..deps import AuthContext, require_user
from ..embeddings import EmbeddingsUnavailable, get_embedding_service
from ..retrieval import hybrid_search
from ..schemas import SearchIn

router = APIRouter(prefix="/api", tags=["search"])


@router.post("/search")
def search(body: SearchIn, auth: AuthContext = Depends(require_user)):
    settings = auth.settings
    config = retrieval_config(auth.db, settings)
    try:
        vector = get_embedding_service().encode([body.query.strip()], kind="query")[0]
    except EmbeddingsUnavailable as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"service d'embeddings indisponible: {exc}",
        )
    effective = dataclasses.replace(settings, retrieval_min_cosine=float(config["min_cosine"]))
    result = hybrid_search(
        auth.db,
        effective,
        query=body.query.strip(),
        query_vector=vector,
        product=body.product,
        version=body.version,
        scope=body.scope,
        top_k=body.top_k,
    )
    return {
        "status": result["status"],
        "sources": result["sources"],
        "diagnostics": result["diagnostics"],
        "embedding": get_embedding_service().info,
    }
