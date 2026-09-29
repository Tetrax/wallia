"""Endpoints web complémentaires : état effectif + recherche publique bornée.

La recherche manuelle emprunte EXACTEMENT le même chemin que le repli du chat :
barrière effective `web_status` AVANT toute recherche (drapeau d'environnement,
activation opérateur après recette RAG, clé — une clé seule ne suffit JAMAIS),
requête construite à partir du vocabulaire fermé public uniquement, appel v2
borné, sources au schéma contractuel (identifiants/scores/pages nuls). Aucun
fetch d'URL libre n'existe dans l'application ; aucune requête libre n'est
exposée.
"""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from ..deps import AuthContext, csrf_guard, require_user
from ..web import (
    WebUnavailable,
    build_public_query,
    search_web_public,
    web_source_from_result,
    web_status,
)

router = APIRouter(prefix="/api/web", tags=["web"])

# Chaque entrée de `terms` est un simple texte candidat BORNÉ : seuls les
# jetons appartenant au vocabulaire fermé peuvent sortir dans la requête.
WebSearchTerm = Annotated[str, StringConstraints(max_length=100)]


class WebSearchIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    product: str | None = Field(default=None, max_length=100)
    version: str | None = Field(default=None, max_length=64)
    terms: list[WebSearchTerm] = Field(default_factory=list, max_length=6)


@router.get("/status")
def web_status_endpoint(auth: AuthContext = Depends(require_user)):
    return web_status(auth.db, auth.settings)


@router.post("/search")
def web_search(body: WebSearchIn, auth: AuthContext = Depends(csrf_guard)):
    # Barrière EFFECTIVE AVANT toute construction de requête ou appel réseau :
    # si la fonction n'est pas active (web désactivé, recette RAG non validée,
    # clé absente), la réponse est un refus explicite 403 — jamais une
    # recherche tentée « parce qu'une clé existe ».
    state = web_status(auth.db, auth.settings)
    if not state.get("available"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=state.get("reason") or "recherche web indisponible",
        )
    # Les « terms » sont un texte candidat : seuls les jetons appartenant au
    # vocabulaire fermé peuvent sortir dans la requête (jamais le texte brut).
    query = build_public_query(" ".join(body.terms), body.product, body.version)
    if query is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="aucun terme public du vocabulaire fermé : recherche web non générée",
        )
    try:
        result = search_web_public(query, auth.settings)
    except WebUnavailable as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc))
    return {
        "query": result["query"],
        "results": [web_source_from_result(item) for item in result["results"]],
    }
