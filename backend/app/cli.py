"""CLI opérateur Wallia (exécution locale dans le conteneur API).

Aucun endpoint public de bootstrap : le premier administrateur est créé ici,
avec un mot de passe lu depuis un fichier (jamais en argument, jamais affiché).

`check-models` vérifie l'image HORS RÉSEAU : manifestes des quatre modèles
(E5, reranker, layout Heron, tableformer) — fichiers, tailles, empreintes,
identité et révision — puis exécute SÉQUENTIELLEMENT trois sondes réelles
(embeddings E5, reclassement cross-encoder, conversion Docling de 2 pages avec
tableau), chacune dans SON processus : les modèles ne sont jamais chargés
simultanément.
"""
from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path, PurePosixPath

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


def cmd_verify_password(args: argparse.Namespace) -> int:
    """Vérification locale d'un mot de passe (usage opérateur, jamais la valeur affichée)."""
    password = _read_password(args.password_file)
    email = args.email.strip().lower()
    with session_scope() as db:
        user = db.execute(select(User).where(func.lower(User.email) == email)).scalar_one_or_none()
        ok = bool(user) and verify_password(user.password_hash, password)
    print(json.dumps({"ok": ok}))
    return 0 if ok else 1


# ---------------------------------------------------------------------------
# Vérification hors ligne des modèles embarqués (image de livraison).
# ---------------------------------------------------------------------------

# Identités layout/tableformer : miroir des constantes de scripts/fetch_models.py
# (révisions immuables vérifiées par l'API HF). Toute évolution doit rester
# alignée des deux côtés — jamais une substitution silencieuse.
LAYOUT_REPO = "docling-project/docling-layout-heron"
LAYOUT_REVISION = "8f39ad3c0b4c58e9c2d2c84a38465abf757272d8"
LAYOUT_DIRNAME = "docling-project--docling-layout-heron"
TABLEFORMER_REPO = "docling-project/docling-models"
TABLEFORMER_REVISION = "fc0f2d45e2218ea24bce5045f58a389aed16dc23"
TABLEFORMER_DIRNAME = "docling-project--docling-models"

# Fichiers ESSENTIELS par modèle : miroir STRICT des listes de
# scripts/fetch_models.py (un fichier essentiel disparu du manifeste — même
# présent sur disque — rend l'image non conforme).
E5_REQUIRED = (
    "README.md",
    "config.json",
    "model.safetensors",
    "modules.json",
    "sentencepiece.bpe.model",
    "special_tokens_map.json",
    "tokenizer.json",
    "tokenizer_config.json",
)
RERANKER_REQUIRED = (
    "README.md",
    "config.json",
    "model.safetensors",
    "sentencepiece.bpe.model",
    "special_tokens_map.json",
    "tokenizer.json",
    "tokenizer_config.json",
)
LAYOUT_REQUIRED = (
    "README.md",
    "config.json",
    "model.safetensors",
    "preprocessor_config.json",
)
TABLEFORMER_REQUIRED = (
    "README.md",
    "config.json",
    "model_artifacts/tableformer/accurate/tableformer_accurate.safetensors",
    "model_artifacts/tableformer/accurate/tm_config.json",
    "model_artifacts/tableformer/fast/tableformer_fast.safetensors",
    "model_artifacts/tableformer/fast/tm_config.json",
)
# Caches HF : jamais acceptés dans un manifeste de livraison (fetch_models les
# exclut ET les purge).
CACHE_DIRNAME = ".cache"


class CheckModelsError(RuntimeError):
    """Écart constaté lors de la vérification d'une image (message explicite)."""


def _sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(chunk), b""):
            digest.update(block)
    return digest.hexdigest()


def _safe_manifest_relpath(raw, label: str) -> str:
    """Chemin de manifeste relatif SÛR : ni absolu, ni traversant, ni cache."""
    if not isinstance(raw, str) or not raw:
        raise CheckModelsError(f"{label}: chemin de manifeste vide ou non textuel")
    if raw.startswith("/") or "\\" in raw:
        raise CheckModelsError(f"{label}: chemin absolu interdit dans le manifeste ({raw!r})")
    parts = PurePosixPath(raw).parts
    if str(PurePosixPath(raw)) != raw or any(part in ("..", ".") for part in parts):
        raise CheckModelsError(f"{label}: chemin non canonique ou traversant dans le manifeste ({raw!r})")
    if raw == "manifest.json" or CACHE_DIRNAME in parts:
        raise CheckModelsError(f"{label}: entrée de cache/auto-référence interdite dans le manifeste ({raw!r})")
    return raw


def _verify_manifest_dir(target: Path, repo: str, revision: str, label: str, required: tuple[str, ...]) -> dict:
    """Manifeste NON VIDE + identité/révision/licence attendues + chemins SÛRS
    + fichiers ESSENTIELS listés ET présents + tailles/SHA256 réels.

    Alignement minimal avec scripts/fetch_models.py : un manifeste qui a perdu
    un fichier essentiel (ou qui contient un chemin `../`) est REFUSÉ, même si
    les entrées restantes sont correctes.

    Bornage RÉEL : la résolution du manifeste et de chaque fichier doit rester
    SOUS target.resolve() — un lien sortant est refusé AVANT toute lecture de
    contenu ou calcul d'empreinte (fichiers essentiels compris).
    """
    target_root = target.resolve()
    manifest_path = target / "manifest.json"
    try:
        manifest_resolved = manifest_path.resolve()
    except (OSError, RuntimeError) as exc:
        raise CheckModelsError(f"{label}: manifeste non résolvable ({manifest_path})") from exc
    if target_root not in manifest_resolved.parents:
        raise CheckModelsError(f"{label}: lien sortant refusé (manifest.json résolu hors du répertoire cible)")
    if not manifest_path.is_file():
        raise CheckModelsError(f"{label}: manifeste absent ({manifest_path})")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise CheckModelsError(f"{label}: manifeste illisible ({exc})") from exc
    files = manifest.get("files") if isinstance(manifest, dict) else None
    if not isinstance(files, dict) or not files:
        raise CheckModelsError(f"{label}: manifeste vide ou sans fichiers")
    if manifest.get("repo") != repo:
        raise CheckModelsError(f"{label}: dépôt du manifeste ({manifest.get('repo')!r}) différent de l'attendu ({repo!r})")
    if manifest.get("revision") != revision:
        raise CheckModelsError(f"{label}: révision du manifeste ({manifest.get('revision')!r}) différente de l'attendue ({revision!r})")
    if not isinstance(manifest.get("license"), str) or not manifest["license"]:
        raise CheckModelsError(f"{label}: licence absente du manifeste")
    for rel in required:
        if rel not in files:
            raise CheckModelsError(f"{label}: fichier essentiel ABSENT DU MANIFESTE ({rel})")
        essential = target / rel
        try:
            essential_resolved = essential.resolve()
        except (OSError, RuntimeError) as exc:
            raise CheckModelsError(f"{label}: fichier essentiel non résolvable ({rel})") from exc
        if target_root not in essential_resolved.parents:
            raise CheckModelsError(f"{label}: lien sortant refusé ({rel} résolu hors du répertoire cible)")
        if not essential.is_file():
            raise CheckModelsError(f"{label}: fichier essentiel absent du disque ({rel})")
    checked = 0
    for raw_rel, meta in sorted(files.items()):
        rel = _safe_manifest_relpath(raw_rel, label)
        path = target / rel
        try:
            resolved = path.resolve()
        except (OSError, RuntimeError) as exc:
            raise CheckModelsError(f"{label}: fichier du manifeste non résolvable ({rel})") from exc
        if target_root not in resolved.parents:
            raise CheckModelsError(f"{label}: lien sortant refusé ({rel} résolu hors du répertoire cible)")
        if not isinstance(meta, dict) or not path.is_file():
            raise CheckModelsError(f"{label}: fichier du manifeste absent ({rel})")
        if path.stat().st_size != meta.get("size"):
            raise CheckModelsError(f"{label}: taille incorrecte ({rel})")
        if _sha256_file(path) != meta.get("sha256"):
            raise CheckModelsError(f"{label}: empreinte incorrecte ({rel})")
        checked += 1
    return {"dir": str(target), "repo": repo, "revision": revision, "files_checked": checked}


def _check_manifests() -> dict:
    from .reranking import MODEL_ID as RERANKER_REPO
    from .reranking import MODEL_REVISION as RERANKER_REVISION

    settings = get_settings()
    return {
        "e5-small": _verify_manifest_dir(
            settings.model_dir, settings.embedding_model, settings.embedding_revision, "e5-small", E5_REQUIRED
        ),
        "reranker": _verify_manifest_dir(
            settings.reranker_model_dir, RERANKER_REPO, RERANKER_REVISION, "reranker", RERANKER_REQUIRED
        ),
        "docling-layout": _verify_manifest_dir(
            settings.docling_models_dir / LAYOUT_DIRNAME, LAYOUT_REPO, LAYOUT_REVISION, "docling-layout", LAYOUT_REQUIRED
        ),
        "docling-tableformer": _verify_manifest_dir(
            settings.docling_models_dir / TABLEFORMER_DIRNAME,
            TABLEFORMER_REPO,
            TABLEFORMER_REVISION,
            "docling-tableformer",
            TABLEFORMER_REQUIRED,
        ),
    }


def cmd_check_embedding(_args: argparse.Namespace) -> int:
    """Sonde E5 réelle, hors réseau (un seul modèle en mémoire)."""
    from .embeddings import get_embedding_service

    settings = get_settings()
    report: dict = {"step": "embedding", "ok": False}
    if settings.embedding_backend != "e5":
        report["detail"] = "backend d'embeddings de test interdit dans la vérification d'image (e5 requis)"
    else:
        try:
            service = get_embedding_service()
            vectors = service.encode(["vérification du modèle wallia"], kind="query")
            dim = len(vectors[0]) if vectors else 0
            report.update(service.info)
            report.update({"dim": dim, "expected": settings.embedding_dim})
            report["ok"] = dim == settings.embedding_dim
        except Exception as exc:  # noqa: BLE001 - rapport explicite, jamais un faux succès
            report["detail"] = f"{exc.__class__.__name__}: {exc}"
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report["ok"] else 1


def cmd_check_reranker(_args: argparse.Namespace) -> int:
    """Sonde cross-encoder réelle, hors réseau (fichiers + empreinte + scoring)."""
    from .reranking import get_reranker_service

    settings = get_settings()
    report: dict = {"step": "reranker", "ok": False}
    if settings.reranker_backend != "transformers":
        report["detail"] = "backend de reclassement de test interdit dans la vérification d'image (transformers requis)"
    else:
        try:
            service = get_reranker_service()
            verified = service.verify_model_files()  # tailles + empreinte FIXE des poids, sans réseau
            scores = service.score("vérification du modèle wallia", ["passage de vérification technique"])
            report.update(
                {
                    "files": len(verified["files"]),
                    "weights_sha256_verified": verified["weights_sha256_verified"],
                    "scores": scores,
                }
            )
            report["ok"] = len(scores) == 1
        except Exception as exc:  # noqa: BLE001 - rapport explicite, jamais un faux succès
            report["detail"] = f"{exc.__class__.__name__}: {exc}"
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report["ok"] else 1


def _build_probe_pdf(path: Path) -> None:
    """PDF de sonde : 2 pages dont un tableau réel (exerce tableformer)."""
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    styles = getSampleStyleSheet()
    story = [
        Paragraph("Wallia — sonde Docling (page 1)", styles["Title"]),
        Spacer(1, 12),
        Paragraph("Texte de la première page de la sonde locale.", styles["BodyText"]),
        PageBreak(),
        Paragraph("Wallia — tableau de vérification (page 2)", styles["Heading2"]),
        Table(
            [["Produit", "Version", "État"], ["Aster", "10.10", "OK"], ["Bastion", "3.6", "OK"]],
            style=TableStyle(
                [
                    ("GRID", (0, 0), (-1, -1), 0.5, colors.black),
                    ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
                ]
            ),
        ),
    ]
    SimpleDocTemplate(str(path), pagesize=A4).build(story)


def cmd_check_docling(_args: argparse.Namespace) -> int:
    """Sonde Docling réelle : 2 pages avec tableau, options explicites, hors réseau."""
    settings = get_settings()
    report: dict = {"step": "docling", "ok": False}
    try:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            probe = tmp_path / "probe.pdf"
            _build_probe_pdf(probe)
            out_dir = tmp_path / "out"
            proc = subprocess.run(
                [
                    sys.executable, "-m", "app.docling_runner",
                    "--input", str(probe),
                    "--output", str(out_dir),
                    "--models", str(settings.docling_models_dir),
                ],
                capture_output=True,
                text=True,
                timeout=900,
            )
            payload = None
            if proc.returncode == 0 and (out_dir / "extracted.json").is_file():
                payload = json.loads((out_dir / "extracted.json").read_text(encoding="utf-8"))
        if payload is None:
            report["detail"] = (proc.stderr or proc.stdout or "")[-300:]
        else:
            pages = int(payload.get("page_count") or 0)
            items = payload.get("items") or []
            tables = sum(1 for item in items if item.get("kind") == "table")
            report.update({"pages": pages, "items": len(items), "tables": tables})
            report["ok"] = pages >= 2 and tables >= 1
    except Exception as exc:  # noqa: BLE001 - rapport explicite, jamais un faux succès
        report["detail"] = f"{exc.__class__.__name__}: {exc}"
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report["ok"] else 1


def cmd_check_models(_args: argparse.Namespace) -> int:
    """Vérifie l'image complète, sans réseau : manifestes puis sondes SÉQUENTIELLES."""
    report: dict = {"steps": {}, "ok": False}
    ok = True

    # 1) Manifestes : hachage seul, aucun modèle chargé.
    try:
        report["steps"]["manifests"] = {"ok": True, "models": _check_manifests()}
    except CheckModelsError as exc:
        ok = False
        report["steps"]["manifests"] = {"ok": False, "detail": str(exc)}

    # 2) Sondes réelles, chacune dans SON processus : E5, reranker et Docling
    #    ne sont jamais chargés simultanément (mémoire de la sonde bornée).
    #    Un DÉPASSEMENT DE DÉLAI est un résultat explicite non-ok (jamais une
    #    traceback laissant l'opérateur deviner).
    timeout = int(os.environ.get("WALLIA_CHECK_STEP_TIMEOUT", "900"))
    for step in ("check-embedding", "check-reranker", "check-docling"):
        try:
            proc = subprocess.run(
                [sys.executable, "-m", "app.cli", step], capture_output=True, text=True, timeout=timeout
            )
        except subprocess.TimeoutExpired:
            ok = False
            report["steps"][step] = {
                "ok": False,
                "detail": f"délai de {timeout} s dépassé : sonde interrompue, résultat non exploitable",
            }
            continue
        except OSError as exc:
            ok = False
            report["steps"][step] = {"ok": False, "detail": f"exécution de la sonde impossible: {exc}"}
            continue
        payload: dict = {}
        if proc.stdout.strip():
            try:
                payload = json.loads(proc.stdout.strip().splitlines()[-1])
            except ValueError:
                payload = {}
        step_ok = proc.returncode == 0 and payload.get("ok") is True
        ok = ok and step_ok
        entry: dict = {"ok": step_ok}
        entry.update(payload if payload else {"tail": (proc.stderr or proc.stdout or "")[-300:]})
        report["steps"][f"{step}"] = entry

    report["ok"] = ok
    print(json.dumps(report, ensure_ascii=False, indent=1))
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

    p = sub.add_parser("check-models", help="vérifie manifestes + embeddings + reclassement + Docling (hors réseau, séquentiel)")
    p.set_defaults(func=cmd_check_models)

    p = sub.add_parser("check-embedding", help="sonde E5 réelle hors réseau (usage interne à check-models)")
    p.set_defaults(func=cmd_check_embedding)

    p = sub.add_parser("check-reranker", help="sonde cross-encoder réelle hors réseau (usage interne à check-models)")
    p.set_defaults(func=cmd_check_reranker)

    p = sub.add_parser("check-docling", help="sonde Docling 2 pages/tableau hors réseau (usage interne à check-models)")
    p.set_defaults(func=cmd_check_docling)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
