#!/usr/bin/env python3
"""État de déploiement Wallia — lectures, écritures ATOMIQUES et validations.

Utilisé par scripts/deploy.sh, scripts/rollback.sh et scripts/backup.sh (jamais
en exploitation directe). L'état ne contient AUCUN secret : uniquement l'image
de référence (tag), son ID immuable `sha256:…`, et les chemins du snapshot
Compose rendu et du fichier d'environnement correspondant, plus des métadonnées
de journal.

Sous-commandes :
  write         <chemin> [--stamp S] [--image TAG] [--image-id sha256:…]
                [--sha SHA] [--compose FICHIER] [--env FICHIER]
                [--note TEXTE] [--null-previous]
                Écrit l'état par REMPLACEMENT ATOMIQUE (temporaire + rename),
                répertoire parent 0700, fichier 0600. Dans le cas image, les
                cinq références (--image, --image-id, --compose, --env +
                format sha256 de l'ID) sont exigées ET les fichiers référencés
                doivent exister et être non vides : une référence incohérente
                est refusée à l'écriture. `--null-previous` est EXCLUSIF : il
                exclut toute référence d'image (jamais un `previous: null`
                mêlé à un état image).
  read          <chemin> <champ>   Affiche la valeur (chaîne), vide si absente
                ou null.
  validate-refs <chemin>           Vérifie la cohérence d'un état : soit
                `previous: null` explicite ET AUCUNE référence d'image
                (première installation), soit image + image_id (format
                sha256) + snapshot Compose + env présents, non vides et
                effectivement sur disque. Échoue (code non nul) sinon.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

IMAGE_ID_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
# Champs de l'ÉTAT JSON (clés écrites dans le fichier).
JSON_REQUIRED_FIELDS = ("image", "image_id", "compose_snapshot", "env_snapshot")
# Champs de la ligne de commande `write` équivalents (l'option publique est
# `--compose` / `--env`, la clé JSON `compose_snapshot` / `env_snapshot`).
WRITE_REQUIRED_ARGS = ("image", "image_id", "compose", "env")
# Références d'image interdites avec `--null-previous` / `previous: null`.
IMAGE_REF_KEYS = ("image", "image_id", "sha", "compose_snapshot", "env_snapshot")


def _load(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise SystemExit(f"état illisible ({path}): {exc}") from exc
    if not isinstance(payload, dict):
        raise SystemExit(f"état invalide ({path}): objet JSON attendu")
    return payload


def _string(payload: dict, key: str) -> str:
    value = payload.get(key)
    return value if isinstance(value, str) else ""


def _require_readable(path: Path, what: str) -> None:
    if not path.is_file():
        raise SystemExit(f"{what} absent: {path}")
    if path.stat().st_size == 0:
        raise SystemExit(f"{what} vide: {path}")


def cmd_write(args: argparse.Namespace) -> int:
    path = Path(args.path)
    if args.null_previous:
        present = [name for name in WRITE_REQUIRED_ARGS + ("sha",) if getattr(args, name)]
        if present:
            raise SystemExit(
                "--null-previous exclut toute référence d'image ("
                + ", ".join("--" + name.replace("_", "-") for name in present)
                + ") : previous: null jamais mêlé à un état image"
            )
        payload = {"stamp": args.stamp or "", "previous": None, "note": args.note or ""}
    else:
        missing = [name for name in WRITE_REQUIRED_ARGS if not getattr(args, name)]
        if missing:
            raise SystemExit(
                "champs requis manquants pour l'état: "
                + ", ".join("--" + name.replace("_", "-") for name in missing)
            )
        if not IMAGE_ID_RE.match(args.image_id):
            raise SystemExit(f"image_id invalide (attendu sha256:<64 hex>): {args.image_id!r}")
        # Références incohérentes refusées DÈS L'ÉCRITURE : le snapshot Compose
        # et l'env doivent être de vrais fichiers non vides.
        _require_readable(Path(args.compose), "snapshot Compose référencé")
        _require_readable(Path(args.env), "fichier d'environnement référencé")
        payload = {
            "stamp": args.stamp or "",
            "image": args.image,
            "image_id": args.image_id,
            "sha": args.sha or "",
            "compose_snapshot": args.compose,
            "env_snapshot": args.env,
            "note": args.note or "",
        }
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    tmp = path.with_name(path.name + f".tmp-{os.getpid()}")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)  # renommage atomique : jamais d'état partiel
    print(json.dumps({"ok": True, "path": str(path)}))
    return 0


def cmd_read(args: argparse.Namespace) -> int:
    payload = _load(Path(args.path))
    value = payload.get(args.field)
    print(value if isinstance(value, str) else "")
    return 0


def cmd_validate_refs(args: argparse.Namespace) -> int:
    path = Path(args.path)
    if not path.is_file():
        raise SystemExit(f"état absent: {path}")
    payload = _load(path)
    if "previous" in payload:
        if payload["previous"] is None:
            leftovers = [key for key in IMAGE_REF_KEYS if _string(payload, key).strip()]
            if leftovers:
                raise SystemExit(
                    f"état invalide ({path}): previous=null incohérent avec des "
                    f"références d'image: {', '.join(leftovers)}"
                )
            print(json.dumps({"ok": True, "path": str(path), "previous": "null"}))
            return 0
        raise SystemExit(f"état invalide ({path}): clé `previous` inattendue")
    missing = [key for key in JSON_REQUIRED_FIELDS if not _string(payload, key).strip()]
    if missing:
        raise SystemExit(f"état incomplet ({path}): champs manquants: {', '.join(missing)}")
    if not IMAGE_ID_RE.match(_string(payload, "image_id")):
        raise SystemExit(f"état invalide ({path}): image_id non conforme (sha256:<64 hex>)")
    for key in ("compose_snapshot", "env_snapshot"):
        ref = Path(_string(payload, key))
        if not ref.is_file() or ref.stat().st_size == 0:
            raise SystemExit(f"état invalide ({path}): {key} absent ou vide ({ref})")
    print(
        json.dumps(
            {
                "ok": True,
                "path": str(path),
                "image": _string(payload, "image"),
                "image_id": _string(payload, "image_id"),
            }
        )
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="État de déploiement Wallia (atomique, 0600)")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("write")
    p.add_argument("path")
    p.add_argument("--stamp", default="")
    p.add_argument("--image", default="")
    p.add_argument("--image-id", default="")
    p.add_argument("--sha", default="")
    p.add_argument("--compose", default="")
    p.add_argument("--env", default="")
    p.add_argument("--note", default="")
    p.add_argument("--null-previous", action="store_true")
    p.set_defaults(func=cmd_write)

    p = sub.add_parser("read")
    p.add_argument("path")
    p.add_argument("field")
    p.set_defaults(func=cmd_read)

    p = sub.add_parser("validate-refs")
    p.add_argument("path")
    p.set_defaults(func=cmd_validate_refs)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
