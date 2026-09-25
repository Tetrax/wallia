#!/usr/bin/env python3
"""Téléchargement explicite et pinné des modèles nécessaires à Wallia.

- Embeddings : intfloat/multilingual-e5-small à la révision épinglée (CPU, dim 384).
- Docling : modèles de mise en page/tableaux CPU uniquement.

Exécuté au build de l'image ; aucune variante CUDA, aucun modèle inutile.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

DEFAULT_REPO = "intfloat/multilingual-e5-small"
DEFAULT_REVISION = os.environ.get("EMBEDDING_MODEL_REVISION", "614241f622f53c4eeff9890bdc4f31cfecc418b3")


def fetch_embedding(model_dir: Path, repo: str, revision: str) -> dict:
    from huggingface_hub import snapshot_download

    model_dir.mkdir(parents=True, exist_ok=True)
    path = snapshot_download(
        repo_id=repo,
        revision=revision,
        local_dir=str(model_dir),
        allow_patterns=[
            "*.json",
            "*.txt",
            "*.model",
            "*.bin",
            "*.safetensors",
            "*.spm",
            "*.tiktoken",
            "1_Pooling/*",
            "2_Dense/*",
        ],
    )
    files = sorted(p.name for p in model_dir.iterdir())
    return {"repo": repo, "revision": revision, "path": str(path), "files": files[:20]}


def fetch_docling(docling_dir: Path) -> dict:
    docling_dir.mkdir(parents=True, exist_ok=True)
    errors: list[str] = []
    # 1) API Python explicite
    try:
        from docling.utils import model_downloader

        if hasattr(model_downloader, "download_models"):
            model_downloader.download_models(output_dir=docling_dir)
            return {"mode": "python-api", "path": str(docling_dir), "files": len(list(docling_dir.rglob("*")))}
    except Exception as exc:  # noqa: BLE001
        errors.append(f"python-api: {exc.__class__.__name__}: {exc}")
    # 2) CLI docling-tools
    for cmd in (
        ["docling-tools", "models", "download", "layout", "tableformer", "--output-dir", str(docling_dir)],
        ["docling-tools", "models", "download", "--output-dir", str(docling_dir)],
    ):
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"cli: {exc.__class__.__name__}: {exc}")
            continue
        if proc.returncode == 0:
            return {"mode": "cli", "path": str(docling_dir), "files": len(list(docling_dir.rglob("*"))), "cmd": " ".join(cmd)}
        errors.append(f"cli rc={proc.returncode}: {(proc.stderr or '')[-200:]}")
    # 3) préchauffage par conversion réelle (HOME jetable), puis copie du cache
    try:
        from reportlab.pdfgen import canvas

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            home = tmp_path / "home"
            home.mkdir()
            probe = tmp_path / "probe.pdf"
            pdf = canvas.Canvas(str(probe))
            pdf.drawString(72, 720, "wallia model probe")
            pdf.showPage()
            pdf.save()
            env = dict(os.environ)
            env["HOME"] = str(home)
            proc = subprocess.run(
                [sys.executable, "-m", "app.docling_runner", "--input", str(probe), "--output", str(tmp_path / "out")],
                capture_output=True,
                text=True,
                timeout=1800,
                env=env,
            )
            if proc.returncode != 0:
                errors.append(f"probe rc={proc.returncode}: {(proc.stderr or '')[-300:]}")
            else:
                cache = home / ".cache" / "docling"
                if cache.is_dir():
                    shutil.copytree(cache, docling_dir, dirs_exist_ok=True)
                    return {"mode": "probe-cache", "path": str(docling_dir), "files": len(list(docling_dir.rglob("*")))}
                errors.append("probe ok mais cache docling introuvable")
    except Exception as exc:  # noqa: BLE001
        errors.append(f"probe: {exc.__class__.__name__}: {exc}")
    raise SystemExit("échec du téléchargement des modèles Docling:\n- " + "\n- ".join(errors))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model-dir", default="/opt/models/e5-small")
    parser.add_argument("--docling-dir", default="/opt/docling-models")
    parser.add_argument("--repo", default=DEFAULT_REPO)
    parser.add_argument("--revision", default=DEFAULT_REVISION)
    parser.add_argument("--skip-docling", action="store_true")
    args = parser.parse_args()

    report = {"embedding": fetch_embedding(Path(args.model_dir), args.repo, args.revision)}
    if not args.skip_docling:
        report["docling"] = fetch_docling(Path(args.docling_dir))
    print(json.dumps(report, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
