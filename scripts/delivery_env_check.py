#!/usr/bin/env python3
"""Contrôle STRICT de l'environnement Compose EFFECTIF de Wallia en production.

Lit sur stdin le JSON de `docker compose config --format json` — c'est-à-dire
l'environnement réellement rendu après fusion du fichier d'environnement ET des
variables héritées du shell (un simple `grep` d'un fichier ne prouverait rien).

Refuse si l'environnement effectif n'est pas exactement l'environnement de
production attendu :

  - projet Compose `wallia` (nom effectif du rendu) ;
  - services SANS PROFIL exactement {db, api, worker} — un service à profils
    (ex. `fake-upstream` sous `["testtools"]`) ne peut JAMAIS démarrer sans
    activation explicite du profil et n'est donc pas un service de runtime ;
  - api ET worker utilisent l'image `wallia:<sha-complet>` demandée ;
  - WALLIA_ENV=production, WALLIA_COOKIE_SECURE=1 ;
  - WALLIA_ALLOWED_ORIGINS == https://wallia.valdev.me (origine EXACTE, aucune
    origine supplémentaire, aucun préfixe accepté) ;
  - WALLIA_PROVIDER_ENDPOINT == https://api.deepseek.com/v1 (endpoint natif HTTPS) ;
  - aucun backend de test : WALLIA_EMBEDDING_BACKEND et WALLIA_RERANKER_BACKEND
    ne peuvent pas valoir `fixture`.

Sortie silencieuse : seuls les champs de garde (non secrets) sont nommés en
cas d'écart ; la valeur des autres variables n'est jamais imprimée.
"""
from __future__ import annotations

import argparse
import json
import re
import sys

EXPECTED_SERVICES = {"db", "api", "worker"}
EXPECTED_PROJECT = "wallia"
REQUIRED_IMAGE_RE = re.compile(r"^wallia:[0-9a-f]{40}$")
GUARD_EXPECTED = (
    ("WALLIA_ENV", "production"),
    ("WALLIA_COOKIE_SECURE", "1"),
    ("WALLIA_ALLOWED_ORIGINS", "https://wallia.valdev.me"),
    ("WALLIA_PROVIDER_ENDPOINT", "https://api.deepseek.com/v1"),
)
TEST_BACKENDS = (
    "WALLIA_EMBEDDING_BACKEND",
    "WALLIA_RERANKER_BACKEND",
)


def _env_map(entry: dict) -> dict[str, str]:
    """Environnement d'un service au format `docker compose config` (dict ou liste)."""
    env = entry.get("environment")
    if isinstance(env, dict):
        return {str(key): (value if isinstance(value, str) else "") for key, value in env.items()}
    if isinstance(env, list):
        parsed: dict[str, str] = {}
        for item in env:
            if not isinstance(item, str):
                continue
            key, _, value = item.partition("=")
            parsed[key] = value
        return parsed
    return {}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Contrôle de l'environnement Compose effectif Wallia")
    parser.add_argument("--image", required=True, help="tag d'image de livraison attendu (wallia:<sha40>)")
    args = parser.parse_args(argv)

    expected_image = args.image
    if not REQUIRED_IMAGE_RE.match(expected_image):
        print(f"[wallia] image attendue invalide: {expected_image!r} (format wallia:<sha 40 hex>)", file=sys.stderr)
        return 2

    try:
        config = json.load(sys.stdin)
    except ValueError as exc:
        print(f"[wallia] configuration Compose illisible: {exc}", file=sys.stderr)
        return 2

    services = config.get("services")
    if not isinstance(services, dict):
        print("[wallia] configuration Compose sans section services", file=sys.stderr)
        return 2

    problems: list[str] = []
    if config.get("name") != EXPECTED_PROJECT:
        problems.append(f"projet Compose attendu {EXPECTED_PROJECT!r}, obtenu {config.get('name')!r}")
    # Les services à profils ne sont jamais démarrés par `up db api worker` :
    # seuls les services SANS profil constituent le runtime (aucun service de
    # test ne peut démarrer en production).
    runtime_names = {
        name
        for name, entry in services.items()
        if not (isinstance(entry, dict) and entry.get("profiles"))
    }
    if runtime_names != EXPECTED_SERVICES:
        problems.append(f"services attendus {sorted(EXPECTED_SERVICES)}, obtenus {sorted(runtime_names)}")

    for service in sorted(EXPECTED_SERVICES - {"db"}):
        entry = services.get(service)
        if not isinstance(entry, dict):
            continue
        image = str(entry.get("image") or "")
        if image != expected_image:
            problems.append(f"services.{service}.image attendu {expected_image!r}, obtenu {image!r}")
        env = _env_map(entry)
        for key, expected in GUARD_EXPECTED:
            value = env.get(key)
            if value != expected:
                problems.append(f"services.{service}.{key} attendu {expected!r}, obtenu {value!r}")
        for key in TEST_BACKENDS:
            value = env.get(key)
            if value is not None and value.strip().lower() == "fixture":
                problems.append(f"services.{service}.{key}={value!r} : backend de test interdit en production")

    if problems:
        print("[wallia] environnement effectif NON CONFORME (aucune mutation effectuée) :", file=sys.stderr)
        for problem in problems:
            print(f"[wallia]   - {problem}", file=sys.stderr)
        return 2

    print(
        json.dumps(
            {
                "ok": True,
                "project": EXPECTED_PROJECT,
                "image": expected_image,
                "services": sorted(runtime_names),
                "guards": [key for key, _ in GUARD_EXPECTED],
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
