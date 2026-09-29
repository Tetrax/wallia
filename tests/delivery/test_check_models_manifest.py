"""Tests de la validation des manifestes de modèles (hors réseau, aucun secret).

Couvre `backend/app/cli.py::_verify_manifest_dir` : identité/révision/licence,
chemins relatifs sûrs (ni `../`, ni absolu, ni cache), fichiers ESSENTIELS
listés DANS le manifeste ET présents sur disque, tailles/SHA256 réels — aligné
sur scripts/fetch_models.py. Aucune sonde réelle, aucun réseau, aucun modèle
téléchargé ; aucun secret n'est lu.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "backend"))

try:
    from app import cli  # noqa: E402 — import après ajout de backend/ au chemin
except ImportError as exc:  # dépendances applicatives absentes de cet interpréteur
    pytest.skip(f"import de l'application impossible ({exc})", allow_module_level=True)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def build_model_dir(
    root: Path,
    required: tuple[str, ...] = cli.LAYOUT_REQUIRED,
    repo: str = cli.LAYOUT_REPO,
    revision: str = cli.LAYOUT_REVISION,
    license_id: str = "Apache-2.0",
    extra_files: dict | None = None,
) -> Path:
    target = root / "model"
    target.mkdir(parents=True, exist_ok=True)
    manifest: dict = {"repo": repo, "revision": revision, "license": license_id, "files": {}}
    for rel in required:
        content = f"contenu {rel}\n".encode()
        path = target / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        manifest["files"][rel] = {"size": len(content), "sha256": sha256(content)}
    for rel, meta in (extra_files or {}).items():
        manifest["files"][rel] = meta
    (target / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    return target


def verify(target: Path):
    return cli._verify_manifest_dir(target, cli.LAYOUT_REPO, cli.LAYOUT_REVISION, "layout", cli.LAYOUT_REQUIRED)


def test_manifeste_conforme_accepte(tmp_path: Path) -> None:
    target = build_model_dir(tmp_path)
    report = verify(target)
    assert report["files_checked"] == len(cli.LAYOUT_REQUIRED)
    assert report["repo"] == cli.LAYOUT_REPO


def test_fichier_essentiel_supprime_du_manifeste_refuse(tmp_path: Path) -> None:
    target = build_model_dir(tmp_path)
    manifest = json.loads((target / "manifest.json").read_text(encoding="utf-8"))
    del manifest["files"]["model.safetensors"]  # supprimé du manifeste, encore sur disque
    (target / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(cli.CheckModelsError, match="ABSENT DU MANIFESTE"):
        verify(target)


def test_fichier_essentiel_absent_du_disque_refuse(tmp_path: Path) -> None:
    target = build_model_dir(tmp_path)
    (target / "model.safetensors").unlink()
    with pytest.raises(cli.CheckModelsError, match="absent du disque"):
        verify(target)


@pytest.mark.parametrize("bad_path", ["../evil.safetensors", "/etc/passwd", "./config.json", "a//b"])
def test_chemin_non_sur_refuse(tmp_path: Path, bad_path: str) -> None:
    content = b"x"
    target = build_model_dir(tmp_path, extra_files={bad_path: {"size": 1, "sha256": sha256(content)}})
    with pytest.raises(cli.CheckModelsError, match="manifeste"):
        verify(target)


def test_entree_de_cache_refusee(tmp_path: Path) -> None:
    content = b"x"
    target = build_model_dir(tmp_path, extra_files={".cache/secret": {"size": 1, "sha256": sha256(content)}})
    with pytest.raises(cli.CheckModelsError, match="cache"):
        verify(target)


def test_revision_differente_refusee(tmp_path: Path) -> None:
    target = build_model_dir(tmp_path, revision="0" * 40)
    with pytest.raises(cli.CheckModelsError, match="révision"):
        verify(target)


def test_empreinte_alteree_refusee(tmp_path: Path) -> None:
    target = build_model_dir(tmp_path)
    (target / "config.json").write_bytes(b"altere")
    with pytest.raises(cli.CheckModelsError, match="taille|empreinte"):
        verify(target)


def test_manifeste_vide_refuse(tmp_path: Path) -> None:
    target = build_model_dir(tmp_path)
    (target / "manifest.json").write_text(json.dumps({"repo": cli.LAYOUT_REPO, "revision": cli.LAYOUT_REVISION, "files": {}}), encoding="utf-8")
    with pytest.raises(cli.CheckModelsError, match="vide"):
        verify(target)


def test_essentiels_en_phase_entre_fetch_models_et_cli() -> None:
    """Les listes essentielles du CLI restent alignées sur fetch_models.py."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("wallia_fetch_models", REPO_ROOT / "scripts" / "fetch_models.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert set(cli.E5_REQUIRED) == set(module.E5_REQUIRED)
    assert set(cli.RERANKER_REQUIRED) == set(module.RERANKER_REQUIRED)
    assert set(cli.LAYOUT_REQUIRED) == set(module.LAYOUT_REQUIRED)
    assert set(cli.TABLEFORMER_REQUIRED) == set(module.TABLEFORMER_REQUIRED)
    assert cli.LAYOUT_REVISION == module.LAYOUT_REVISION
    assert cli.TABLEFORMER_REVISION == module.TABLEFORMER_REVISION


def test_lien_sortant_essentiel_refuse_avant_lecture(tmp_path: Path) -> None:
    """Fichier essentiel lié hors du répertoire : refusé (lien sortant, pas de lecture)."""
    target = build_model_dir(tmp_path)
    anodin = tmp_path / "anodin-hors-modele.txt"
    anodin.write_text("fichier de test anodin — aucun secret réel\n", encoding="utf-8")
    (target / "config.json").unlink()
    os.symlink(anodin, target / "config.json")
    with pytest.raises(cli.CheckModelsError, match="lien sortant"):
        verify(target)


def test_manifest_json_lien_sortant_refuse(tmp_path: Path) -> None:
    """manifest.json résolu hors du répertoire cible : refusé avant lecture."""
    target = build_model_dir(tmp_path)
    dehors = tmp_path / "manifest-ailleurs.json"
    dehors.write_text((target / "manifest.json").read_text(encoding="utf-8"), encoding="utf-8")
    (target / "manifest.json").unlink()
    os.symlink(dehors, target / "manifest.json")
    with pytest.raises(cli.CheckModelsError, match="lien sortant"):
        verify(target)


def test_entree_manifeste_lien_sortant_refusee(tmp_path: Path) -> None:
    """Entrée du manifeste liée hors du répertoire : refusée avant taille/SHA."""
    contenu = b"x"
    dehors = tmp_path / "extra-externe.bin"
    dehors.write_bytes(contenu)
    target = build_model_dir(tmp_path, extra_files={"extra.bin": {"size": len(contenu), "sha256": sha256(contenu)}})
    os.symlink(dehors, target / "extra.bin")
    with pytest.raises(cli.CheckModelsError, match="lien sortant"):
        verify(target)
