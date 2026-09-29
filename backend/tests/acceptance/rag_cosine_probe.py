"""Sonde : scores cosinus réels du chunk EN « seven days » pour la question FR."""
from __future__ import annotations

import json
import sys

sys.path.insert(0, "/app")

import numpy as np  # noqa: E402
from sqlalchemy import text  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.db import session_scope  # noqa: E402
from app.embeddings import get_embedding_service  # noqa: E402

QUESTION = "Combien de jours de journaux la rotation conserve-t-elle dans Aster 10.10 ?"
settings = get_settings()
service = get_embedding_service()
query_vector = np.asarray(service.encode([QUESTION], kind="query")[0], dtype=np.float32)

with session_scope() as db:
    rows = db.execute(
        text(
            """
            SELECT d.title, c.page_start, c.text, c.embedding
              FROM chunks c JOIN documents d ON d.id = c.document_id
             WHERE c.embedding IS NOT NULL
            """
        )
    ).fetchall()

scored = []
for title, page, chunk_text, embedding in rows:
    if isinstance(embedding, str):
        vector = np.asarray(json.loads(embedding), dtype=np.float32)
    else:
        vector = np.asarray(embedding, dtype=np.float32)
    cosine = float(np.dot(query_vector, vector) / (np.linalg.norm(query_vector) * np.linalg.norm(vector) + 1e-12))
    scored.append((cosine, title, page, chunk_text))
scored.sort(reverse=True)
print(f"seuil figé min_cosine = {settings.retrieval_min_cosine}")
for cosine, title, page, chunk_text in scored:
    marker = "  <-- EN seven days" if "seven days" in chunk_text else ""
    print(f"cos={cosine:.4f} p.{page} {title[:44]!r} {str(chunk_text)[:60]!r}{marker}")
