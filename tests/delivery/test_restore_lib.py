"""Tests unitaires du manifeste et des archives de sauvegarde (AUCUN Docker).

Couvre scripts/restore_lib.py : schéma STRICT du manifeste (exactement 8
tables, types entiers, chemins canoniques sans traversée, pas de doublon),
SHA256 des archives avant parsing, sûreté tar (liens/absolus/traversées),
cible d'extraction NEUVE, détection des fichiers manquants/extra/altérés,
comptes des tables et références DB→fichiers.
"""
from __future__ import annotations

import gzip
import hashlib
import importlib.util
import io
import json
import tarfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
TABLE_KEYS = (
    "users",
    "conversations",
    "messages",
    "attachments",
    "documents",
    "chunks",
    "ingestion_jobs",
    "schema_migrations",
)


def _load_restore_lib():
    spec = importlib.util.spec_from_file_location("wallia_restore_lib", REPO_ROOT / "scripts" / "restore_lib.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


restore_lib = _load_restore_lib()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def write_tar(path: Path, members: list[dict]) -> None:
    with tarfile.open(path, "w:gz") as tar:
        for member in members:
            info = tarfile.TarInfo(member["name"])
            kind = member.get("type", "file")
            if kind == "dir":
                info.type = tarfile.DIRTYPE
                info.mode = 0o755
                tar.addfile(info)
            elif kind == "symlink":
                info.type = tarfile.SYMTYPE
                info.linkname = member.get("linkname", "cible")
                info.mode = 0o777
                tar.addfile(info)
            elif kind == "fifo":
                info.type = tarfile.FIFOTYPE
                info.mode = 0o644
                tar.addfile(info)
            else:
                content = member.get("content", b"")
                info.size = len(content)
                info.mode = 0o644
                tar.addfile(info, io.BytesIO(content))


def canonical(name: str) -> str:
    return name[2:] if name.startswith("./") else name


def make_bundle(root: Path, members: list[dict] | None = None, manifest_overrides: dict | None = None) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    if members is None:
        members = [{"name": "documents/a.pdf", "type": "file", "content": b"pdf"}]
    data_tar = root / "data.tar.gz"
    write_tar(data_tar, members)
    db_file = root / "db.sql.gz"
    with gzip.open(db_file, "wb") as handle:
        handle.write(b"-- fake wallia dump\n")

    files = []
    for member in members:
        if member.get("type", "file") != "file":
            continue
        content = member.get("content", b"")
        files.append({"path": canonical(member["name"]), "size": len(content), "sha256": sha256_bytes(content)})

    manifest = {
        "stamp": "20260930T000000Z",
        "archives": {
            "db.sql.gz": {"size": db_file.stat().st_size, "sha256": hashlib.sha256(db_file.read_bytes()).hexdigest()},
            "data.tar.gz": {"size": data_tar.stat().st_size, "sha256": hashlib.sha256(data_tar.read_bytes()).hexdigest()},
        },
        "tables": {key: 0 for key in TABLE_KEYS},
        "files": files,
        "files_count": len(files),
        "db_files": [],
        "db_files_missing": [],
    }
    manifest.update(manifest_overrides or {})
    (root / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    return root


def expect_failure(args: list[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        restore_lib.main(args)
    assert excinfo.value.code not in (None, 0), "échec attendu avec code non nul"


# --- validate ---------------------------------------------------------------

def test_validate_accepte_un_bundle_conforme(tmp_path: Path) -> None:
    bundle = make_bundle(tmp_path / "bundle")
    assert restore_lib.main(["validate", str(bundle)]) == 0


def test_validate_accepte_les_noms_prefixes_point_slash(tmp_path: Path) -> None:
    members = [
        {"name": "./", "type": "dir"},
        {"name": "./documents/a.pdf", "type": "file", "content": b"pdf"},
    ]
    bundle = make_bundle(tmp_path / "bundle", members)
    assert restore_lib.main(["validate", str(bundle)]) == 0


def test_validate_refuse_un_lien_symbolique(tmp_path: Path) -> None:
    members = [
        {"name": "documents/a.pdf", "type": "file", "content": b"pdf"},
        {"name": "documents/link", "type": "symlink"},
    ]
    bundle = make_bundle(tmp_path / "bundle", members)
    expect_failure(["validate", str(bundle)])


def test_validate_refuse_une_traversee_dans_le_tar(tmp_path: Path) -> None:
    members = [{"name": "dir/../evil", "type": "file", "content": b"x"}]
    bundle = make_bundle(tmp_path / "bundle", members)
    expect_failure(["validate", str(bundle)])


def test_validate_refuse_un_membre_non_regulier(tmp_path: Path) -> None:
    members = [{"name": "documents/fifo", "type": "fifo"}]
    bundle = make_bundle(tmp_path / "bundle", members, manifest_overrides={"files": [], "files_count": 0})
    expect_failure(["validate", str(bundle)])


@pytest.mark.parametrize("mutation", ["manquante", "extra"])
def test_validate_exige_les_huit_tables_exactement(tmp_path: Path, mutation: str) -> None:
    tables = {key: 0 for key in TABLE_KEYS}
    if mutation == "manquante":
        tables.pop("users")
    else:
        tables["secrets"] = 0
    bundle = make_bundle(tmp_path / "bundle", manifest_overrides={"tables": tables})
    expect_failure(["validate", str(bundle)])


def test_validate_refuse_un_compte_non_entier(tmp_path: Path) -> None:
    tables = {key: 0 for key in TABLE_KEYS}
    tables["users"] = "1"
    bundle = make_bundle(tmp_path / "bundle", manifest_overrides={"tables": tables})
    expect_failure(["validate", str(bundle)])


@pytest.mark.parametrize("bad_path", ["../escape", "/etc/passwd", "./documents/a.pdf", "a//b"])
def test_validate_refuse_un_chemin_non_canonique_ou_traversant(tmp_path: Path, bad_path: str) -> None:
    files = [{"path": bad_path, "size": 3, "sha256": sha256_bytes(b"pdf")}]
    bundle = make_bundle(tmp_path / "bundle", manifest_overrides={"files": files, "files_count": 1})
    expect_failure(["validate", str(bundle)])


def test_validate_refuse_un_chemin_duplique(tmp_path: Path) -> None:
    entry = {"path": "documents/a.pdf", "size": 3, "sha256": sha256_bytes(b"pdf")}
    bundle = make_bundle(tmp_path / "bundle", manifest_overrides={"files": [entry, dict(entry)], "files_count": 2})
    expect_failure(["validate", str(bundle)])


def test_validate_refuse_un_files_count_incoherent(tmp_path: Path) -> None:
    bundle = make_bundle(tmp_path / "bundle", manifest_overrides={"files_count": 5})
    expect_failure(["validate", str(bundle)])


def test_validate_refuse_une_archive_corrompue(tmp_path: Path) -> None:
    bundle = make_bundle(tmp_path / "bundle")
    with gzip.open(bundle / "db.sql.gz", "ab") as handle:
        handle.write(b"corruption")
    expect_failure(["validate", str(bundle)])


def test_validate_refuse_une_section_db_files_absente(tmp_path: Path) -> None:
    bundle = make_bundle(tmp_path / "bundle", manifest_overrides={"db_files": None})
    expect_failure(["validate", str(bundle)])


def test_validate_refuse_une_reference_db_hors_prefixe(tmp_path: Path) -> None:
    db_files = [{"source": "documents", "path": "evil/a.pdf"}]
    bundle = make_bundle(tmp_path / "bundle", manifest_overrides={"db_files": db_files})
    expect_failure(["validate", str(bundle)])


# --- extract / verify-files -------------------------------------------------

def test_extract_exige_une_cible_neuve(tmp_path: Path) -> None:
    bundle = make_bundle(tmp_path / "bundle")
    target = tmp_path / "cible"
    target.mkdir()
    (target / "occupé.txt").write_text("déjà là", encoding="utf-8")
    expect_failure(["extract", str(bundle), str(target)])
    assert not (target / "documents").exists()


def test_extract_puis_verify_files_ok(tmp_path: Path) -> None:
    bundle = make_bundle(tmp_path / "bundle")
    target = tmp_path / "cible"
    assert restore_lib.main(["extract", str(bundle), str(target)]) == 0
    assert (target / "documents" / "a.pdf").read_bytes() == b"pdf"
    assert restore_lib.main(["verify-files", str(bundle), str(target)]) == 0


def test_verify_files_detecte_extra_altere_et_manquant(tmp_path: Path) -> None:
    bundle = make_bundle(tmp_path / "bundle")
    target = tmp_path / "cible"
    restore_lib.main(["extract", str(bundle), str(target)])

    extra = target / "documents" / "extra.txt"
    extra.write_bytes(b"intrus")
    expect_failure(["verify-files", str(bundle), str(target)])
    extra.unlink()

    (target / "documents" / "a.pdf").write_bytes(b"pdq")  # même taille, empreinte différente
    expect_failure(["verify-files", str(bundle), str(target)])

    (target / "documents" / "a.pdf").unlink()
    expect_failure(["verify-files", str(bundle), str(target)])


# --- verify-counts / verify-refs -------------------------------------------

def _counts_file(tmp_path: Path, counts: dict[str, int]) -> Path:
    path = tmp_path / "counts.txt"
    path.write_text("".join(f"{key}={value}\n" for key, value in counts.items()), encoding="utf-8")
    return path


def test_verify_counts_ok_et_ecart(tmp_path: Path) -> None:
    bundle = make_bundle(tmp_path / "bundle")
    assert restore_lib.main(["verify-counts", str(bundle), str(_counts_file(tmp_path, {key: 0 for key in TABLE_KEYS}))]) == 0
    bad = {key: 0 for key in TABLE_KEYS}
    bad["messages"] = 7
    expect_failure(["verify-counts", str(bundle), str(_counts_file(tmp_path, bad))])


def test_verify_refs_ok_puis_ecarts(tmp_path: Path) -> None:
    db_files = [{"source": "documents", "path": "documents/a.pdf"}]
    bundle = make_bundle(tmp_path / "bundle", manifest_overrides={"db_files": db_files})
    target = tmp_path / "cible"
    restore_lib.main(["extract", str(bundle), str(target)])

    refs = tmp_path / "refs.txt"
    refs.write_text("documents=a.pdf\n", encoding="utf-8")
    assert restore_lib.main(["verify-refs", str(bundle), str(target), str(refs)]) == 0

    refs.write_text("documents=a.pdf\nattachments=jamais-vu.txt\n", encoding="utf-8")
    expect_failure(["verify-refs", str(bundle), str(target), str(refs)])

    refs.write_text("documents=../evil\n", encoding="utf-8")
    expect_failure(["verify-refs", str(bundle), str(target), str(refs)])

    refs.write_text("documents=a.pdf\n", encoding="utf-8")
    (target / "documents" / "a.pdf").unlink()
    expect_failure(["verify-refs", str(bundle), str(target), str(refs)])
