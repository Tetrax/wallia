"""Recherche web complémentaire — intégration préparée, DÉSACTIVÉE par défaut.

Règles : activation seulement après recette RAG seule et validation opérateur ;
requêtes anonymisées (produit/version publics uniquement, jamais de log, question
brute, nom interne) ; domaine officiel wallix.com prioritaire ; aucune récupération
massive. Tant que ce n'est pas activé, les endpoints répondent « indisponible ».
"""
from __future__ import annotations

import httpx
from sqlalchemy.orm import Session

from .config import Settings

FIRECRAWL_ENDPOINT = "https://api.firecrawl.dev/v1/search"


class WebUnavailable(RuntimeError):
    pass


def web_activation(db: Session, settings: Settings) -> dict:
    from .app_settings import get_row

    row = get_row(db, "web") or {}
    return {
        "env_enabled": settings.web_enabled,
        "operator_activated": bool(row.get("activated")),
        "rag_validated": bool(row.get("rag_validated")),
        "key_present": settings.read_secret("firecrawl_api_key") is not None,
    }


def web_status(db: Session, settings: Settings) -> dict:
    state = web_activation(db, settings)
    if not state["env_enabled"]:
        return {"available": False, "reason": "désactivé par configuration (WALLIA_WEB_ENABLED=0)"}
    if not state["operator_activated"] or not state["rag_validated"]:
        return {"available": False, "reason": "en attente d'activation opérateur après recette RAG"}
    if not state["key_present"]:
        return {"available": False, "reason": "clé de service absente"}
    return {"available": True, "reason": None}


def anon_query(product: str | None, version: str | None, topic_terms: list[str]) -> str:
    """Requête publique minimale : produit/version publics + mots contrôlés."""
    parts = [p for p in [product, version] if p]
    parts.extend(t[:40] for t in topic_terms[:6])
    return " ".join(parts)[:200]


def search_web(query_public: str, db: Session, settings: Settings) -> dict:
    status = web_status(db, settings)
    if not status["available"]:
        raise WebUnavailable(status["reason"] or "indisponible")
    key = settings.read_secret("firecrawl_api_key")
    assert key is not None
    try:
        with httpx.Client(timeout=httpx.Timeout(connect=10.0, read=30.0, write=10.0, pool=10.0)) as client:
            response = client.post(
                FIRECRAWL_ENDPOINT,
                headers={"Authorization": f"Bearer {key}"},
                json={"query": query_public, "limit": 5, "sources": ["web"]},
            )
    except httpx.HTTPError as exc:
        raise WebUnavailable(f"service indisponible ({exc.__class__.__name__})") from exc
    if response.status_code >= 400:
        raise WebUnavailable(f"service HTTP {response.status_code}")
    try:
        payload = response.json()
    except ValueError as exc:
        raise WebUnavailable("réponse illisible") from exc
    results = []
    for item in (payload.get("data") or [])[:5]:
        url = str(item.get("url") or "")
        if not url.startswith("https://"):
            continue
        results.append({"url": url, "title": str(item.get("title") or "")[:200], "snippet": str(item.get("description") or "")[:400]})
    return {"query": query_public, "results": results}
