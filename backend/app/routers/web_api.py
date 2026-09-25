"""Endpoints web complémentaires (désactivés tant que non validés opérateur)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field

from ..deps import AuthContext, csrf_guard, require_user
from ..web import WebUnavailable, anon_query, search_web, web_status

router = APIRouter(prefix="/api/web", tags=["web"])


class WebSearchIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    product: str | None = Field(default=None, max_length=100)
    version: str | None = Field(default=None, max_length=64)
    terms: list[str] = Field(default_factory=list, max_length=6)


@router.get("/status")
def web_status_endpoint(auth: AuthContext = Depends(require_user)):
    return web_status(auth.db, auth.settings)


@router.post("/search")
def web_search(body: WebSearchIn, auth: AuthContext = Depends(csrf_guard)):
    query = anon_query(body.product, body.version, [t for t in body.terms])
    try:
        return search_web(query, auth.db, auth.settings)
    except WebUnavailable as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc))
