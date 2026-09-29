#!/usr/bin/env python3
"""Validation et extraction SÛRES des bundles de sauvegarde Wallia.

Appelé par scripts/restore.sh — jamais directement en exploitation.

Sous-commandes :
  validate       <bundle>                       manifeste STRICT + SHA256 archives + sûreté tar
  extract        <bundle> <target>              extraction (membres réguliers uniquement, cible NEUVE)
  verify-files   <bundle> <target>              fichiers manquants/extra/altérés/liens vs manifeste
  verify-counts  <bundle> <comptes-psql.txt>    comptes des 8 tables exactement conformes au manifeste
  verify-refs    <bundle> <target> <refs.txt>   références DB→fichiers (attachments/documents) vs fichiers restaurés

Garanties :
  - les SHA256 des archives sont vérifiés AVANT toute lecture/parsing des
    archives ; le manifeste est validé strictement (exactement les 8 tables
    attendues, types entiers, chemins relatifs canoniques sans traversée,
    pas de doublon, fichiers déclarés cohérents) ;
  - aucun chemin absolu, aucune traversée `..`, aucun lien symbolique ou
    matériel : tout membre tar non régulier fait ÉCHOUER la validation ;
  - la cible d'extraction doit être NEUVE (absente ou vide) ;
  - chaque référence DB→fichier du bundle doit correspondre exactement aux
    références de la base restaurée ET pointer un fichier présent ;
  - sortie JSON sans secret ; code de sortie non nul à la moindre anomalie.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import tarfile
from pathlib import Path, PurePosixPath
from typing import NoReturn

MANIFEST = "manifest.json"
ARCHIVE_NAMES = ("db.sql.gz", "data.tar.gz")
EXPECTED_TABLES = frozenset(
    ("users", "conversations", "messages", "attachments", "documents", "chunks", "ingestion_jobs", "schema_migrations")
)
# Référence DB → répertoire des données (chemins stockés relatifs à ces racines).
REF_SOURCES = {"attachments": "uploads", "documents": "documents"}
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(chunk), b""):
            digest.update(block)
    return digest.hexdigest()


def _fail(message: str) -> NoReturn:
    raise SystemExit(message)


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _safe_relpath(raw, what: str) -> str:
    if not isinstance(raw, str) or not raw:
        _fail(f"manifeste invalide : {what} vide ou non textuel")
    if raw.startswith("/"):
        _fail(f"manifeste invalide : {what} absolu interdit ({raw!r})")
    canonical = str(PurePosixPath(raw))
    if canonical != raw or any(part in ("..", ".") for part in PurePosixPath(raw).parts):
        _fail(f"manifeste invalide : {what} non canonique ou traversant ({raw!r})")
    return raw


def load_manifest(bundle: Path) -> dict:
    manifest_path = bundle / MANIFEST
    if not manifest_path.is_file():
        _fail(f"bundle invalide : manifeste absent ({manifest_path})")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except ValueError as exc:
        _fail(f"bundle invalide : manifeste illisible ({exc})")
    if not isinstance(manifest, dict):
        _fail("bundle invalide : manifeste non objet")
    return manifest


def validate_manifest(manifest: dict) -> dict:
    """Validation STRICTE du schéma du manifeste (aucune tolérance)."""
    if not isinstance(manifest.get("stamp"), str) or not manifest["stamp"]:
        _fail("manifeste invalide : stamp absent")
    archives = manifest.get("archives")
    if not isinstance(archives, dict) or set(archives) != set(ARCHIVE_NAMES):
        _fail(f"manifeste invalide : archives inattendues ({archives!r})")
    for name, meta in archives.items():
        if not isinstance(meta, dict) or not _is_int(meta.get("size")) or meta["size"] < 0:
            _fail(f"manifeste invalide : taille de {name} non entière")
        if not isinstance(meta.get("sha256"), str) or not SHA256_RE.match(meta["sha256"]):
            _fail(f"manifeste invalide : empreinte de {name} non conforme")
    tables = manifest.get("tables")
    if not isinstance(tables, dict) or set(tables) != set(EXPECTED_TABLES):
        _fail(f"manifeste invalide : les 8 tables attendues doivent être présentes exactement ({sorted(tables) if isinstance(tables, dict) else tables!r})")
    for key, value in tables.items():
        if not _is_int(value) or value < 0:
            _fail(f"manifeste invalide : compte de {key} non entier positif")
    files = manifest.get("files")
    if not isinstance(files, list):
        _fail("manifeste invalide : section files absente")
    seen: set[str] = set()
    for item in files:
        if not isinstance(item, dict):
            _fail("manifeste invalide : entrée de fichier non objet")
        rel = _safe_relpath(item.get("path"), "chemin de fichier")
        if rel in seen:
            _fail(f"manifeste invalide : chemin de fichier en double ({rel!r})")
        seen.add(rel)
        if not _is_int(item.get("size")) or item["size"] < 0:
            _fail(f"manifeste invalide : taille de {rel} non entière")
        if not isinstance(item.get("sha256"), str) or not SHA256_RE.match(item["sha256"]):
            _fail(f"manifeste invalide : empreinte de {rel} non conforme")
    if not _is_int(manifest.get("files_count")) or manifest["files_count"] != len(files):
        _fail("manifeste invalide : files_count incohérent avec la liste des fichiers")
    db_files = manifest.get("db_files")
    if not isinstance(db_files, list):
        _fail("manifeste invalide : section db_files absente")
    for ref in db_files:
        if not isinstance(ref, dict) or ref.get("source") not in REF_SOURCES:
            _fail(f"manifeste invalide : référence DB de source inconnue ({ref!r})")
        expected_prefix = f"{REF_SOURCES[ref['source']]}/"
        path = _safe_relpath(ref.get("path"), "chemin de référence DB")
        if not path.startswith(expected_prefix):
            _fail(f"manifeste invalide : référence DB hors de {expected_prefix} ({path!r})")
    missing = manifest.get("db_files_missing")
    if not isinstance(missing, list) or any(not isinstance(entry, str) for entry in missing):
        _fail("manifeste invalide : db_files_missing absent")
    return manifest


def check_archives(bundle: Path, manifest: dict) -> dict:
    archives = manifest["archives"]
    for name, meta in archives.items():
        path = bundle / name
        if not path.is_file():
            _fail(f"bundle invalide : archive absente ({name})")
        if path.stat().st_size != meta["size"]:
            _fail(f"bundle invalide : taille de {name} différente du manifeste")
        if sha256_file(path) != meta["sha256"]:
            _fail(f"bundle corrompu : SHA256 de {name} différent du manifeste")
    return archives


def _normalize_member(name: str) -> str:
    """Normalise un nom de membre tar : accepte « x » et « ./x », refuse le reste."""
    stripped = name[2:] if name.startswith("./") else name
    if stripped == ".":
        return stripped
    if name != stripped and name != "./" + stripped:
        _fail(f"archive refusée : nom de membre non canonique ({name!r})")
    return stripped


def _check_member(member: tarfile.TarInfo) -> None:
    name = member.name
    if name in (".", "./"):
        if not member.isdir():
            _fail(f"archive refusée : racine non répertoire ({name!r})")
        return
    canonical = _normalize_member(name)
    if canonical.startswith("/") or not canonical:
        _fail(f"archive refusée : chemin non sûr ({name!r})")
    parts = PurePosixPath(canonical).parts
    if any(part in ("..", ".") for part in parts):
        _fail(f"archive refusée : traversée de chemin ({name!r})")
    if member.issym() or member.islnk():
        _fail(f"archive refusée : lien détecté ({name!r})")
    if not (member.isdir() or member.isfile()):
        _fail(f"archive refusée : membre non régulier ({name!r})")


def _iter_safe_tar(bundle: Path) -> tarfile.TarFile:
    tar = tarfile.open(bundle / "data.tar.gz", "r:gz")
    try:
        for member in tar:
            _check_member(member)
    except Exception:
        tar.close()
        raise
    return tar


def cmd_validate(bundle: Path) -> int:
    manifest = validate_manifest(load_manifest(bundle))
    archives = check_archives(bundle, manifest)
    members = 0
    tar = _iter_safe_tar(bundle)
    try:
        with tar:
            members = sum(1 for _ in tar.getmembers())
    except tarfile.TarError as exc:
        _fail(f"bundle invalide : archive tar illisible ({exc})")
    print(
        json.dumps(
            {
                "ok": True,
                "bundle": str(bundle),
                "stamp": manifest["stamp"],
                "archives": {name: archives[name]["sha256"] for name in ARCHIVE_NAMES},
                "tables": manifest["tables"],
                "files_count": manifest["files_count"],
                "db_files": len(manifest["db_files"]),
                "tar_members": members,
            },
            ensure_ascii=False,
            indent=1,
        )
    )
    return 0


def cmd_extract(bundle: Path, target: Path) -> int:
    manifest = validate_manifest(load_manifest(bundle))
    check_archives(bundle, manifest)
    # Cible NEUVE garantie : absente ou strictement vide.
    if target.exists():
        if not target.is_dir():
            _fail(f"extraction refusée : cible existante non répertoire ({target})")
        if any(target.iterdir()):
            _fail(f"extraction refusée : cible non neuve ({target})")
    target.mkdir(parents=True, exist_ok=True)
    with _iter_safe_tar(bundle) as tar:
        tar.extractall(path=target, filter="data")  # filtre standard : liens/absolus refusés
        files = sum(1 for member in tar.getmembers() if member.isfile())
    print(json.dumps({"ok": True, "target": str(target), "files": files}, ensure_ascii=False, indent=1))
    return 0


def _expected_files(manifest: dict) -> dict[str, dict]:
    expected: dict[str, dict] = {}
    for item in manifest["files"]:
        expected[item["path"]] = item
    return expected


def cmd_verify_files(bundle: Path, target: Path) -> int:
    manifest = validate_manifest(load_manifest(bundle))
    expected = _expected_files(manifest)
    if not target.is_dir():
        _fail(f"vérification refusée : cible absente ({target})")
    problems: list[str] = []
    seen: set[str] = set()
    for root, dirs, filenames in os.walk(target, followlinks=False):
        root_path = Path(root)
        for name in list(dirs):
            path = root_path / name
            if path.is_symlink():
                problems.append(f"lien symbolique: {path.relative_to(target).as_posix()}")
                dirs.remove(name)
        for name in filenames:
            path = root_path / name
            rel = path.relative_to(target).as_posix()
            seen.add(rel)
            if path.is_symlink():
                problems.append(f"lien symbolique: {rel}")
                continue
            if not path.is_file():
                problems.append(f"type non régulier: {rel}")
                continue
            item = expected.get(rel)
            if item is None:
                problems.append(f"fichier EXTRA (absent du manifeste): {rel}")
                continue
            if path.stat().st_size != item["size"]:
                problems.append(f"taille incorrecte: {rel}")
                continue
            if sha256_file(path) != item["sha256"]:
                problems.append(f"empreinte incorrecte: {rel}")
                continue
    for rel in sorted(set(expected) - seen):
        problems.append(f"fichier manquant: {rel}")
    if problems:
        _fail("fichiers restaurés non conformes — " + "; ".join(problems[:12]))
    print(json.dumps({"ok": True, "files_checked": len(expected), "extra": 0}, ensure_ascii=False, indent=1))
    return 0


def cmd_verify_counts(bundle: Path, counts_file: Path) -> int:
    manifest = validate_manifest(load_manifest(bundle))
    expected = manifest["tables"]
    got: dict[str, int] = {}
    if not counts_file.is_file():
        _fail(f"comptes illisibles : {counts_file}")
    for line in counts_file.read_text(encoding="utf-8").splitlines():
        key, _, value = line.strip().partition("=")
        if key and value.isdigit():
            got[key] = int(value)
    if got != expected:
        detail = {key: (expected.get(key), got.get(key)) for key in sorted(set(expected) | set(got)) if expected.get(key) != got.get(key)}
        _fail(f"comptes de tables non conformes au manifeste: {detail}")
    print(json.dumps({"ok": True, "tables": got}, ensure_ascii=False))
    return 0


def _parse_refs(lines: list[str]) -> list[tuple[str, str]]:
    refs: list[tuple[str, str]] = []
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        source, sep, rel = line.partition("=")
        if not sep or source not in REF_SOURCES or not rel:
            _fail(f"référence DB illisible: {line!r}")
        rel = _safe_relpath(rel, "référence DB (fichier)")
        refs.append((source, f"{REF_SOURCES[source]}/{rel}"))
    return refs


def cmd_verify_refs(bundle: Path, target: Path, refs_file: Path) -> int:
    manifest = validate_manifest(load_manifest(bundle))
    if not refs_file.is_file():
        _fail(f"références DB illisibles : {refs_file}")
    restored = _parse_refs(refs_file.read_text(encoding="utf-8").splitlines())
    declared = [(ref["source"], ref["path"]) for ref in manifest["db_files"]]
    if sorted(restored) != sorted(declared):
        only_restored = sorted(set(restored) - set(declared))
        only_manifest = sorted(set(declared) - set(restored))
        _fail(
            "références DB→fichiers non conformes au bundle — "
            f"absentes du manifeste: {only_restored[:5]} ; absentes de la base restaurée: {only_manifest[:5]}"
        )
    problems = [f"référence DB sans fichier restauré: {path}" for _, path in restored if not (target / path).is_file()]
    if problems:
        _fail("; ".join(problems[:10]))
    print(json.dumps({"ok": True, "db_refs_checked": len(restored)}, ensure_ascii=False, indent=1))
    return 0


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    usage = "usage: restore_lib.py validate|extract|verify-files|verify-refs <bundle> [...]"
    if len(args) < 2:
        _fail(usage)
    command, bundle = args[0], Path(args[1])
    if command == "validate":
        return cmd_validate(bundle)
    if command == "extract" and len(args) == 3:
        return cmd_extract(bundle, Path(args[2]))
    if command == "verify-files" and len(args) == 3:
        return cmd_verify_files(bundle, Path(args[2]))
    if command == "verify-counts" and len(args) == 3:
        return cmd_verify_counts(bundle, Path(args[2]))
    if command == "verify-refs" and len(args) == 4:
        return cmd_verify_refs(bundle, Path(args[2]), Path(args[3]))
    _fail(f"commande inconnue ou incomplète: {command}")


if __name__ == "__main__":
    sys.exit(main())
