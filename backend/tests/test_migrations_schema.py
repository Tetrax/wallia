"""Schéma et migrations : extension pgvector, index HNSW, idempotence, checksums."""
from __future__ import annotations

import pytest
from sqlalchemy import text


def test_pgvector_extension_and_indexes(migrated):
    from app.db import session_scope

    with session_scope() as db:
        extension = db.execute(
            text("SELECT extname FROM pg_extension WHERE extname = 'vector'")
        ).scalar_one()
        assert extension == "vector"
        index_defs = {
            row[0]
            for row in db.execute(
                text("SELECT indexdef FROM pg_indexes WHERE schemaname = 'public'")
            ).fetchall()
        }
        # Index ANN réel sur les embeddings (HNSW, opérateur cosine).
        assert any("hnsw" in d.lower() and "vector_cosine_ops" in d.lower() for d in index_defs), index_defs
        assert any("gin" in d.lower() and "tsv" in d.lower() for d in index_defs), index_defs
        tables = {
            row[0]
            for row in db.execute(
                text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
            ).fetchall()
        }
        for table in (
            "users",
            "sessions",
            "login_attempts",
            "conversations",
            "messages",
            "attachments",
            "documents",
            "chunks",
            "ingestion_jobs",
            "settings",
            "schema_migrations",
        ):
            assert table in tables, table


def test_migrations_are_idempotent_and_checksummed(migrated):
    from app.db import session_scope
    from app.migrate import run_migrations

    assert run_migrations(verbose=False) == []  # rien à appliquer
    with session_scope() as db:
        rows = db.execute(text("SELECT version, name, checksum FROM schema_migrations ORDER BY version")).fetchall()
    assert rows, "migrations absentes"
    assert rows[0][0] == 1
    assert len(rows[0][2]) == 64  # SHA-256


def test_checksum_drift_is_detected(migrated):
    from app.db import session_scope
    from app.migrate import run_migrations

    with session_scope() as db:
        original = db.execute(text("SELECT checksum FROM schema_migrations WHERE version = 1")).scalar_one()
        db.execute(text("UPDATE schema_migrations SET checksum = :c WHERE version = 1"), {"c": "0" * 64})
    with pytest.raises(RuntimeError):
        run_migrations(verbose=False)
    with session_scope() as db:
        db.execute(text("UPDATE schema_migrations SET checksum = :c WHERE version = 1"), {"c": original})


def test_embedding_dimension_is_enforced(migrated):
    from app.db import session_scope

    with session_scope() as db:
        user_row = db.execute(text("SELECT id FROM users LIMIT 1")).fetchone()
        # La colonne est typée vector(384) : une dimension incorrecte doit être refusée.
        db.execute(
            text(
                "INSERT INTO documents (id, title, origin, versions, language, demo, scope, status,"
                " checksum_sha256, stored_relpath, original_filename, content_type, size_bytes, page_count)"
                " VALUES (gen_random_uuid(), 't', 't', '{}', 'fr', true, 'demo', 'ready',"
                " 'test-dimension', '', 'x.pdf', 'application/pdf', 1, 1)"
            )
        )
        document_id = db.execute(text("SELECT id FROM documents ORDER BY created_at DESC LIMIT 1")).scalar_one()
        with pytest.raises(Exception) as excinfo:
            db.execute(
                text(
                    "INSERT INTO chunks (document_id, generation, seq, text, token_count, embedding)"
                    " VALUES (:doc, 1, 1, 'x', 1, CAST(:emb AS vector))"
                ),
                {"doc": str(document_id), "emb": "[" + ",".join(["0.1"] * 10) + "]"},
            )
        # Le refus doit venir de la dimension du vecteur (384), pas d'autre chose.
        assert "384" in str(excinfo.value) or "dimension" in str(excinfo.value).lower()
        db.rollback()
    assert user_row is None or True
