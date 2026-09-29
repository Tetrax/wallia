#!/usr/bin/env python3
"""Téléchargement explicite, pinné et vérifié des modèles de l'image Wallia.

Quatre dépôts uniquement, révisions IMMUABLES, un seul format de poids
(safetensors) — aucun fallback « télécharger tout », aucun ONNX/OpenVINO/bin,
aucun modèle OCR/enrichissement :

  - intfloat/multilingual-e5-small @614241f6… (MIT)          → /opt/models/e5-small
  - cross-encoder/mmarco-mMiniLMv2-L12-H384-v1 @1427fd65… (Apache-2.0)
                                                             → /opt/models/reranker
      poids `model.safetensors` vérifié contre l'empreinte FIXE du contrat
      (docs/reranker-probe.md, valeur reprise par app/reranking.py).
  - docling-project/docling-layout-heron @8f39ad3c… (Apache-2.0)
      → /opt/docling-models/docling-project--docling-layout-heron  (moteur layout réel)
  - docling-project/docling-models @fc0f2d45… (CDLA-Permissive-2.0)
      → /opt/docling-models/docling-project--docling-models        (tableformer)

Chaque répertoire de modèle reçoit un `manifest.json` (dépôt, révision, licence,
fichiers + SHA256 + taille) ; les README/licences présents sont conservés. Les
caches HF internes (`.cache`) ne sont jamais inclus au manifeste et sont
supprimés après téléchargement. Les fichiers ESSENTIELS (poids/config/
tokenizer) sont exigés : un manquant fait échouer le téléchargement.

`--verify` recalcule ces empreintes SANS RÉSEAU, exige un manifeste NON VIDE,
contrôle l'identité (dépôt) et la révision attendues ET les fichiers
essentiels — échec au premier écart. Utilisé au build après téléchargement, et
utilisable en exploitation pour contrôler une image.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Spécifications figées (docs/delivery-contracts.md). Ne pas dériver : toute
# divergence doit être une décision explicite, pas un défaut silencieux.
# ---------------------------------------------------------------------------
E5_REPO = "intfloat/multilingual-e5-small"
E5_REVISION = "614241f622f53c4eeff9890bdc4f31cfecc418b3"
E5_LICENSE = "MIT"

RERANKER_REPO = "cross-encoder/mmarco-mMiniLMv2-L12-H384-v1"
RERANKER_REVISION = "1427fd652930e4ba29e8149678df786c240d8825"
RERANKER_LICENSE = "Apache-2.0"
# Empreinte FIXE des poids (docs/reranker-probe.md) — même constante que
# app/reranking.py (WEIGHTS_SHA256) : jamais un override silencieux.
RERANKER_WEIGHTS_SHA256 = "5daeca2481a76b5976a2bdc32f0a78532b6716da4f8cd3ff59460ef8d2f359b4"

LAYOUT_REPO = "docling-project/docling-layout-heron"
LAYOUT_REVISION = "8f39ad3c0b4c58e9c2d2c84a38465abf757272d8"
LAYOUT_LICENSE = "Apache-2.0"
LAYOUT_DIRNAME = "docling-project--docling-layout-heron"

TABLEFORMER_REPO = "docling-project/docling-models"
TABLEFORMER_REVISION = "fc0f2d45e2218ea24bce5045f58a389aed16dc23"
TABLEFORMER_LICENSE = "CDLA-Permissive-2.0"
TABLEFORMER_DIRNAME = "docling-project--docling-models"

# Listes de fichiers EXPLICITES (un seul format de poids : safetensors).
# Aucun motif large type « * » ne doit pouvoir ramener bin/onnx/openvino.
# `LICENSE*` conserve la licence du dépôt quand elle est présente.
E5_PATTERNS = [
    "README.md",
    "LICENSE*",
    "config.json",
    "model.safetensors",
    "modules.json",
    "sentence_bert_config.json",
    "sentencepiece.bpe.model",
    "special_tokens_map.json",
    "tokenizer.json",
    "tokenizer_config.json",
    "1_Pooling/*",
]
RERANKER_PATTERNS = [
    "README.md",
    "LICENSE*",
    "config.json",
    "model.safetensors",
    "sentencepiece.bpe.model",
    "special_tokens_map.json",
    "tokenizer.json",
    "tokenizer_config.json",
]
LAYOUT_PATTERNS = [
    "README.md",
    "LICENSE*",
    "config.json",
    "docling_heron_400.png",
    "model.safetensors",
    "preprocessor_config.json",
]
TABLEFORMER_PATTERNS = [
    "README.md",
    "LICENSE*",
    "config.json",
    "model_artifacts/tableformer/accurate/*",
    "model_artifacts/tableformer/fast/*",
]

# Fichiers ESSENTIELS : sans eux l'image est inutilisable — échec explicite,
# jamais un succès partiel silencieux.
E5_REQUIRED = [
    "README.md",
    "config.json",
    "model.safetensors",
    "modules.json",
    "sentencepiece.bpe.model",
    "special_tokens_map.json",
    "tokenizer.json",
    "tokenizer_config.json",
]
RERANKER_REQUIRED = [
    "README.md",
    "config.json",
    "model.safetensors",
    "sentencepiece.bpe.model",
    "special_tokens_map.json",
    "tokenizer.json",
    "tokenizer_config.json",
]
LAYOUT_REQUIRED = [
    "README.md",
    "config.json",
    "model.safetensors",
    "preprocessor_config.json",
]
TABLEFORMER_REQUIRED = [
    "README.md",
    "config.json",
    "model_artifacts/tableformer/accurate/tableformer_accurate.safetensors",
    "model_artifacts/tableformer/accurate/tm_config.json",
    "model_artifacts/tableformer/fast/tableformer_fast.safetensors",
    "model_artifacts/tableformer/fast/tm_config.json",
]

MANIFEST_NAME = "manifest.json"
CACHE_DIRNAME = ".cache"  # caches HF : jamais inclus ni conservés


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            block = handle.read(chunk)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def _snapshot(repo: str, revision: str, target: Path, patterns: list[str]) -> None:
    """Téléchargement snapshot à révision immuable, fichiers explicitement listés."""
    from huggingface_hub import snapshot_download

    target.mkdir(parents=True, exist_ok=True)
    snapshot_download(
        repo_id=repo,
        revision=revision,
        local_dir=str(target),
        allow_patterns=patterns,
    )


def _purge_cache(target: Path) -> None:
    shutil.rmtree(target / CACHE_DIRNAME, ignore_errors=True)


def _manifest_files(target: Path) -> dict[str, dict]:
    files: dict[str, dict] = {}
    for path in sorted(target.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(target).as_posix()
        if rel == MANIFEST_NAME or rel == CACHE_DIRNAME or rel.startswith(CACHE_DIRNAME + "/"):
            continue
        files[rel] = {"size": path.stat().st_size, "sha256": sha256_file(path)}
    return files


def _check_required(target: Path, required: list[str]) -> None:
    missing = [rel for rel in required if not (target / rel).is_file()]
    if missing:
        raise SystemExit(f"échec: fichiers essentiels manquants dans {target}: {', '.join(missing)}")


def _load_manifest(target: Path) -> dict:
    manifest_path = target / MANIFEST_NAME
    if not manifest_path.is_file():
        raise SystemExit(f"échec: manifeste absent ({manifest_path})")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise SystemExit(f"échec: manifeste illisible ({manifest_path}: {exc})") from exc
    if not isinstance(manifest, dict):
        raise SystemExit(f"échec: manifeste non objet ({manifest_path})")
    files = manifest.get("files")
    if not isinstance(files, dict) or not files:
        raise SystemExit(f"échec: manifeste VIDE ou sans fichiers ({manifest_path})")
    return manifest


def _write_manifest(target: Path, repo: str, revision: str, license_id: str) -> dict:
    payload = {
        "repo": repo,
        "revision": revision,
        "license": license_id,
        "files": _manifest_files(target),
    }
    (target / MANIFEST_NAME).write_text(
        json.dumps(payload, ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
    )
    return payload


def download_all(model_dir: Path, reranker_dir: Path, docling_dir: Path) -> dict:
    report: dict = {}

    _snapshot(E5_REPO, E5_REVISION, model_dir, E5_PATTERNS)
    _purge_cache(model_dir)
    _check_required(model_dir, E5_REQUIRED)
    e5_manifest = _write_manifest(model_dir, E5_REPO, E5_REVISION, E5_LICENSE)
    report["e5-small"] = {"repo": E5_REPO, "revision": E5_REVISION, "files": len(e5_manifest["files"])}

    _snapshot(RERANKER_REPO, RERANKER_REVISION, reranker_dir, RERANKER_PATTERNS)
    _purge_cache(reranker_dir)
    _check_required(reranker_dir, RERANKER_REQUIRED)
    weights = reranker_dir / "model.safetensors"
    got = sha256_file(weights)
    if got != RERANKER_WEIGHTS_SHA256:
        raise SystemExit("échec: empreinte des poids du reranker différente de l'attendue (contrat)")
    reranker_manifest = _write_manifest(reranker_dir, RERANKER_REPO, RERANKER_REVISION, RERANKER_LICENSE)
    report["reranker"] = {
        "repo": RERANKER_REPO,
        "revision": RERANKER_REVISION,
        "files": len(reranker_manifest["files"]),
        "weights_sha256_verified": got,
    }

    layout_dir = docling_dir / LAYOUT_DIRNAME
    _snapshot(LAYOUT_REPO, LAYOUT_REVISION, layout_dir, LAYOUT_PATTERNS)
    _purge_cache(layout_dir)
    _check_required(layout_dir, LAYOUT_REQUIRED)
    layout_manifest = _write_manifest(layout_dir, LAYOUT_REPO, LAYOUT_REVISION, LAYOUT_LICENSE)

    tableformer_dir = docling_dir / TABLEFORMER_DIRNAME
    _snapshot(TABLEFORMER_REPO, TABLEFORMER_REVISION, tableformer_dir, TABLEFORMER_PATTERNS)
    _purge_cache(tableformer_dir)
    _check_required(tableformer_dir, TABLEFORMER_REQUIRED)
    tableformer_manifest = _write_manifest(
        tableformer_dir, TABLEFORMER_REPO, TABLEFORMER_REVISION, TABLEFORMER_LICENSE
    )

    report["docling"] = {
        "layout": {"repo": LAYOUT_REPO, "revision": LAYOUT_REVISION, "files": len(layout_manifest["files"])},
        "tableformer": {
            "repo": TABLEFORMER_REPO,
            "revision": TABLEFORMER_REVISION,
            "files": len(tableformer_manifest["files"]),
        },
    }
    return report


def verify_dir(target: Path, repo: str, revision: str, required: list[str]) -> dict:
    """Vérifie un répertoire de modèle contre son manifeste (aucun réseau) :
    manifeste non vide, identité (dépôt) et révision attendues, fichiers
    essentiels présents, puis tailles + SHA256 de tous les fichiers listés."""
    manifest = _load_manifest(target)
    problems: list[str] = []
    if manifest.get("repo") != repo:
        problems.append(f"dépôt du manifeste ({manifest.get('repo')!r}) différent de l'attendu ({repo!r})")
    if manifest.get("revision") != revision:
        problems.append(f"révision du manifeste ({manifest.get('revision')!r}) différente de l'attendue ({revision!r})")
    if not isinstance(manifest.get("license"), str) or not manifest["license"]:
        problems.append("licence absente du manifeste")
    for rel in required:
        if not (target / rel).is_file():
            problems.append(f"essentiel manquant: {rel}")
    checked = 0
    for rel, meta in sorted(manifest.get("files", {}).items()):
        if not isinstance(meta, dict):
            problems.append(f"entrée de manifeste non objet: {rel}")
            continue
        path = target / rel
        if not path.is_file():
            problems.append(f"absent: {rel}")
            continue
        size = path.stat().st_size
        if size != meta.get("size"):
            problems.append(f"taille incorrecte: {rel}")
            continue
        if sha256_file(path) != meta.get("sha256"):
            problems.append(f"empreinte incorrecte: {rel}")
            continue
        checked += 1
    if problems:
        raise SystemExit(f"échec: manifeste non conforme pour {target} — " + "; ".join(problems[:12]))
    return {
        "path": str(target),
        "repo": manifest.get("repo"),
        "revision": manifest.get("revision"),
        "files_checked": checked,
    }


def verify_all(model_dir: Path, reranker_dir: Path, docling_dir: Path) -> dict:
    report = {
        "e5-small": verify_dir(model_dir, E5_REPO, E5_REVISION, E5_REQUIRED),
        "reranker": verify_dir(reranker_dir, RERANKER_REPO, RERANKER_REVISION, RERANKER_REQUIRED),
        "docling-layout": verify_dir(
            docling_dir / LAYOUT_DIRNAME, LAYOUT_REPO, LAYOUT_REVISION, LAYOUT_REQUIRED
        ),
        "docling-tableformer": verify_dir(
            docling_dir / TABLEFORMER_DIRNAME, TABLEFORMER_REPO, TABLEFORMER_REVISION, TABLEFORMER_REQUIRED
        ),
    }
    weights = reranker_dir / "model.safetensors"
    got = sha256_file(weights)
    if got != RERANKER_WEIGHTS_SHA256:
        raise SystemExit("échec: empreinte des poids du reranker différente de l'attendue (contrat)")
    report["reranker"]["weights_sha256_verified"] = got
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Modèles Wallia — téléchargement pinné / vérification")
    parser.add_argument("--model-dir", default="/opt/models/e5-small")
    parser.add_argument("--reranker-dir", default="/opt/models/reranker")
    parser.add_argument("--docling-dir", default="/opt/docling-models")
    parser.add_argument("--verify", action="store_true", help="vérifie les manifestes locaux (sans réseau)")
    args = parser.parse_args(argv)

    model_dir = Path(args.model_dir)
    reranker_dir = Path(args.reranker_dir)
    docling_dir = Path(args.docling_dir)

    if args.verify:
        report = verify_all(model_dir, reranker_dir, docling_dir)
        print(json.dumps({"mode": "verify", "ok": True, **report}, ensure_ascii=False, indent=1))
        return 0

    report = download_all(model_dir, reranker_dir, docling_dir)
    print(json.dumps({"mode": "download", "ok": True, **report}, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
