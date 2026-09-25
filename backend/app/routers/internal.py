"""Endpoints internes (réseau compose uniquement, jeton worker dédié)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field

from ..config import Settings, get_settings
from ..embeddings import EmbeddingsUnavailable, get_embedding_service
from ..security import constant_time_eq

router = APIRouter(prefix="/internal", tags=["internal"])

MAX_TEXTS = 64
MAX_TEXT_CHARS = 8000


class EmbeddingsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: str = Field(pattern="^(query|passage)$")
    texts: list[str] = Field(min_length=1, max_length=MAX_TEXTS)


def require_worker_token(x_internal_token: str = Header(default=""), settings: Settings = Depends(get_settings)) -> None:
    expected = settings.worker_token()
    if not x_internal_token or not constant_time_eq(expected, x_internal_token):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="jeton interne invalide")


@router.post("/embeddings", dependencies=[Depends(require_worker_token)])
def internal_embeddings(body: EmbeddingsIn):
    texts = [t[:MAX_TEXT_CHARS] for t in body.texts]
    try:
        vectors = get_embedding_service().encode(texts, kind=body.kind)
    except EmbeddingsUnavailable as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc))
    info = get_embedding_service().info
    return {"vectors": vectors, **info}
