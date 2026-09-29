"""Sonde : la recherche hybride (en cours de code) sert-elle le passage EN ?"""
from __future__ import annotations

import sys

sys.path.insert(0, "/app")

from app.config import get_settings  # noqa: E402
from app.db import session_scope  # noqa: E402
from app.embeddings import get_embedding_service  # noqa: E402
from app.retrieval import hybrid_search  # noqa: E402

QUESTION = "Combien de jours de journaux la rotation conserve-t-elle dans Aster 10.10 ?"
NEGATIVE = "Quel est le tarif de la licence annuelle en euros et les conditions commerciales de revente ?"

settings = get_settings()
service = get_embedding_service()
with session_scope() as db:
    for label, question in (("positif", QUESTION), ("négatif", NEGATIVE)):
        vector = list(service.encode([question], kind="query")[0])
        result = hybrid_search(db, settings, query=question, query_vector=vector)
        print(f"=== {label}: {result['status']} | {result['diagnostics']}", flush=True)
        for source in result.get("sources") or []:
            print(
                f"    [{source['title'][:40]}] p.{source['page_start']} "
                f"cos={source['score_vector']} txt={source['score_text']}",
                flush=True,
            )
