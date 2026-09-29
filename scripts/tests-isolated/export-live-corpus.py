#!/usr/bin/env python3
"""Outil hôte (lot3) : export LECTURE SEULE du corpus réel de la pile Wallia.

Lit documents + passages (avec leurs vecteurs E5 réels issus de l'ingestion
Docling) dans la base vivante, sans écrire ni modifier quoi que ce soit, et
écrit un JSON non secret dans l'espace de tests isolé
(runtime/tests-isolated/live-corpus.json). La base vivante n'est jamais
tronquée, modifiée ou migrée par ce script.

Usage : export-live-corpus.py --out runtime/tests-isolated/live-corpus.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import subprocess
import sys
from datetime import datetime, timezone

PSQL = "PGPASSWORD=$(cat /run/secrets/db_password) psql -U wallia -d wallia -tA -f -"

SQL = """
SELECT json_build_object(
  'vector_ext_version', (SELECT extversion FROM pg_extension WHERE extname = 'vector'),
  'documents', (
    SELECT json_agg(d) FROM (
      SELECT id::text AS id, title, product, versions, language, demo, scope, status,
             current_generation, page_count, embedding_model, embedding_revision, embedding_dim
        FROM documents ORDER BY created_at, id
    ) d
  ),
  'chunks', (
    SELECT json_agg(c) FROM (
      SELECT document_id::text AS document_id, generation, seq, text, page_start, page_end,
             section, kind, embedding::text AS embedding
        FROM chunks ORDER BY document_id, generation, seq
    ) c
  )
)::text;
"""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=pathlib.Path, default=pathlib.Path("runtime/tests-isolated/live-corpus.json"))
    args = parser.parse_args()

    proc = subprocess.run(
        ["docker", "exec", "-i", "wallia-db-1", "sh", "-c", PSQL],
        input=SQL.encode("utf-8"),
        capture_output=True,
    )
    if proc.returncode != 0:
        print(proc.stderr.decode()[-500:], file=sys.stderr)
        raise SystemExit(f"psql lecture seule rc={proc.returncode}")
    payload = json.loads(proc.stdout.decode("utf-8").strip())
    for chunk in payload["chunks"]:
        chunk["embedding"] = json.loads(chunk["embedding"])

    digest = hashlib.sha256(
        json.dumps(payload["chunks"], ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    document = {
        "source": "base vivante Wallia — LECTURE SEULE (psql via conteneur db, aucun secret exporté)",
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "vector_ext_version": payload["vector_ext_version"],
        "counts": {"documents": len(payload["documents"]), "chunks": len(payload["chunks"])},
        "chunks_sha256": digest,
        "documents": payload["documents"],
        "chunks": payload["chunks"],
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        f"[export] documents={document['counts']['documents']} chunks={document['counts']['chunks']} "
        f"pgvector={payload['vector_ext_version']} sha256={digest[:16]}… -> {args.out}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
