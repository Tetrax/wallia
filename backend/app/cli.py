"""CLI opérateur Wallia (exécution locale dans le conteneur API).

Aucun endpoint public de bootstrap : le premier administrateur est créé ici,
avec un mot de passe lu depuis un fichier (jamais en argument, jamais affiché).
"""
from __future__ import annotations

import argparse
import getpass
import json
import subprocess
import sys
import tempfile
from pathlib import Path

from sqlalchemy import func, select

from .config import get_settings
from .db import session_scope
from .migrate import run_migrations
from .models import Chunk, Conversation, Document, IngestionJob, Message, User
from .security import hash_password, utcnow, verify_password


def _read_password(path: str | None) -> str:
    if path:
        return Path(path).read_text(encoding="utf-8").strip()
    return getpass.getpass("mot de passe: ")


def cmd_migrate(_args: argparse.Namespace) -> int:
    applied = run_migrations()
    print(json.dumps({"applied": applied}, ensure_ascii=False))
    return 0


def cmd_create_admin(args: argparse.Namespace) -> int:
    password = _read_password(args.password_file)
    if len(password) < 12:
        print("ERREUR: mot de passe trop court (>=12 caractères)", file=sys.stderr)
        return 2
    email = args.email.strip().lower()
    with session_scope() as db:
        existing = db.execute(select(User).where(func.lower(User.email) == email)).scalar_one_or_none()
        if existing is not None:
            print(json.dumps({"status": "exists", "email": existing.email}))
            return 0
        user = User(email=email, password_hash=hash_password(password), is_admin=True)
        db.add(user)
        db.flush()
        user.password_changed_at = utcnow()
    print(json.dumps({"status": "created", "email": email}))
    return 0


def cmd_reset_password(args: argparse.Namespace) -> int:
    password = _read_password(args.password_file)
    if len(password) < 12:
        print("ERREUR: mot de passe trop court (>=12 caractères)", file=sys.stderr)
        return 2
    email = args.email.strip().lower()
    with session_scope() as db:
        user = db.execute(select(User).where(func.lower(User.email) == email)).scalar_one_or_none()
        if user is None:
            print(json.dumps({"status": "not_found", "email": email}))
            return 1
        user.password_hash = hash_password(password)
        user.password_changed_at = utcnow()
        from sqlalchemy import text

        db.execute(text("UPDATE sessions SET revoked_at = now() WHERE user_id = :uid AND revoked_at IS NULL"), {"uid": str(user.id)})
    print(json.dumps({"status": "reset", "email": email, "sessions_revoked": True}))
    return 0


def cmd_info(_args: argparse.Namespace) -> int:
    settings = get_settings()
    with session_scope() as db:
        def count(model) -> int:
            return int(db.execute(select(func.count()).select_from(model)).scalar_one())

        payload = {
            "app": {"version": settings.app_version, "env": settings.env, "embedding_backend": settings.embedding_backend},
            "provider_key_configured": settings.provider_api_key() is not None,
            "db_endpoint": settings.db_endpoint,
            "counts": {
                "users": count(User),
                "conversations": count(Conversation),
                "messages": count(Message),
                "documents": count(Document),
                "chunks": count(Chunk),
                "jobs": count(IngestionJob),
            },
        }
    print(json.dumps(payload, ensure_ascii=False, indent=1))
    return 0


def cmd_check_models(_args: argparse.Namespace) -> int:
    from .embeddings import get_embedding_service

    settings = get_settings()
    service = get_embedding_service()
    vectors = service.encode(["vérification du modèle wallia"], kind="query")
    dim = len(vectors[0]) if vectors else 0
    ok_embedding = dim == settings.embedding_dim
    report: dict = {
        "embedding": {"ok": ok_embedding, "dim": dim, "expected": settings.embedding_dim, **service.info}
    }
    # Docling : conversion réelle d'un PDF minuscule généré à la volée.
    docling_ok = False
    detail = ""
    try:
        from reportlab.pdfgen import canvas

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            probe = tmp_path / "probe.pdf"
            pdf = canvas.Canvas(str(probe))
            pdf.drawString(72, 720, "Wallia docling probe 10.10")
            pdf.showPage()
            pdf.save()
            out_dir = tmp_path / "out"
            proc = subprocess.run(
                [sys.executable, "-m", "app.docling_runner", "--input", str(probe), "--output", str(out_dir)],
                capture_output=True,
                text=True,
                timeout=600,
            )
            if proc.returncode == 0 and (out_dir / "extracted.json").is_file():
                payload = json.loads((out_dir / "extracted.json").read_text(encoding="utf-8"))
                docling_ok = bool(payload.get("items"))
                detail = f"items={len(payload.get('items') or [])} pages={payload.get('page_count')}"
            else:
                detail = (proc.stderr or proc.stdout or "")[-300:]
    except Exception as exc:  # noqa: BLE001
        detail = f"{exc.__class__.__name__}: {exc}"
    report["docling"] = {"ok": docling_ok, "detail": detail}
    print(json.dumps(report, ensure_ascii=False, indent=1))
    return 0 if (ok_embedding and docling_ok) else 1


def cmd_verify_password(args: argparse.Namespace) -> int:
    """Vérification locale d'un mot de passe (usage opérateur, jamais la valeur affichée)."""
    password = _read_password(args.password_file)
    email = args.email.strip().lower()
    with session_scope() as db:
        user = db.execute(select(User).where(func.lower(User.email) == email)).scalar_one_or_none()
        ok = bool(user) and verify_password(user.password_hash, password)
    print(json.dumps({"ok": ok}))
    return 0 if ok else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.cli", description="CLI opérateur Wallia")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("migrate", help="applique les migrations en attente")
    p.set_defaults(func=cmd_migrate)

    p = sub.add_parser("create-admin", help="crée le premier administrateur (idempotent)")
    p.add_argument("--email", required=True)
    p.add_argument("--password-file", required=True)
    p.set_defaults(func=cmd_create_admin)

    p = sub.add_parser("reset-password", help="réinitialise un mot de passe et révoque les sessions")
    p.add_argument("--email", required=True)
    p.add_argument("--password-file", required=True)
    p.set_defaults(func=cmd_reset_password)

    p = sub.add_parser("verify-password", help="vérifie un mot de passe (sans l'afficher)")
    p.add_argument("--email", required=True)
    p.add_argument("--password-file", required=True)
    p.set_defaults(func=cmd_verify_password)

    p = sub.add_parser("info", help="résumé non secret de l'état applicatif")
    p.set_defaults(func=cmd_info)

    p = sub.add_parser("check-models", help="vérifie embeddings + Docling en local")
    p.set_defaults(func=cmd_check_models)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
