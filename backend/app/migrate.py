"""Exécuteur de migrations SQL versionnées (backend/migrations/NNNN_nom.sql).

Chaque fichier est appliqué une seule fois, dans l'ordre, dans une transaction.
Le checksum SHA-256 est vérifié pour détecter toute modification d'une migration
déjà appliquée.
"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path

from sqlalchemy import text

from .db import get_engine

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"

_BOOTSTRAP = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version integer PRIMARY KEY,
    name text NOT NULL,
    checksum text NOT NULL,
    applied_at timestamptz NOT NULL DEFAULT now()
);
"""


def _parse_version(path: Path) -> int:
    return int(path.name.split("_", 1)[0])


def discover() -> list[Path]:
    files = sorted(MIGRATIONS_DIR.glob("*.sql"), key=_parse_version)
    versions = [_parse_version(f) for f in files]
    if versions != sorted(set(versions)):
        raise RuntimeError("versions de migration en doublon")
    return files


def run_migrations(verbose: bool = True) -> list[str]:
    engine = get_engine()
    applied_now: list[str] = []
    with engine.begin() as conn:
        conn.execute(text(_BOOTSTRAP))
        rows = conn.execute(text("SELECT version, name, checksum FROM schema_migrations")).fetchall()
        applied = {int(r[0]): (r[1], r[2]) for r in rows}
        for path in discover():
            version = _parse_version(path)
            content = path.read_text(encoding="utf-8")
            checksum = hashlib.sha256(content.encode("utf-8")).hexdigest()
            if version in applied:
                name, recorded = applied[version]
                if recorded != checksum:
                    raise RuntimeError(
                        f"migration {path.name} modifiée après application (checksum divergent)"
                    )
                continue
            conn.execute(text(content))
            conn.execute(
                text("INSERT INTO schema_migrations (version, name, checksum) VALUES (:v, :n, :c)"),
                {"v": version, "n": path.name, "c": checksum},
            )
            applied_now.append(path.name)
            if verbose:
                print(f"[migrate] appliquée: {path.name}")
    if verbose and not applied_now:
        print("[migrate] base déjà à jour")
    return applied_now


def main() -> int:
    run_migrations()
    return 0


if __name__ == "__main__":
    sys.exit(main())
