"""Tests des helpers de livraison (AUCUN Docker, aucun réseau, aucun root).

Couvre scripts/delivery_env_check.py (environnement Compose EFFECTIF strict :
origine exacte, endpoint natif, aucun backend de test, aucun service de test,
aucune valeur non secrète affichée indûment) et scripts/delivery_state.py
(écriture atomique 0600, previous=null explicite, validate-refs).
"""
from __future__ import annotations

import json
import stat
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
ENV_CHECK = REPO_ROOT / "scripts" / "delivery_env_check.py"
STATE = REPO_ROOT / "scripts" / "delivery_state.py"
IMAGE = "wallia:" + "0" * 40


def strict_config(image: str = IMAGE) -> dict:
    env = {
        "WALLIA_ENV": "production",
        "WALLIA_COOKIE_SECURE": "1",
        "WALLIA_ALLOWED_ORIGINS": "https://wallia.valdev.me",
        "WALLIA_PROVIDER_ENDPOINT": "https://api.deepseek.com/v1",
    }
    return {
        "name": "wallia",
        "services": {
            "db": {"image": "pgvector/pgvector:pg17"},
            "api": {"image": image, "environment": dict(env)},
            "worker": {"image": image, "environment": dict(env)},
            # Le faux fournisseur de test vit sous profil `testtools` : il ne
            # peut jamais démarrer via `up db api worker`.
            "fake-upstream": {"image": image, "profiles": ["testtools"]},
        },
    }


def run_env_check(config: dict, image: str = IMAGE) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(ENV_CHECK), "--image", image],
        input=json.dumps(config),
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_environnement_strict_accepte() -> None:
    proc = run_env_check(strict_config())
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["ok"] is True


def test_origine_par_prefixe_refusee() -> None:
    # L'ancien contrôle par préfixe acceptait cette origine : refus explicite.
    config = strict_config()
    config["services"]["api"]["environment"]["WALLIA_ALLOWED_ORIGINS"] = "https://wallia.valdev.me.attaquant.tld"
    proc = run_env_check(config)
    assert proc.returncode != 0
    assert "WALLIA_ALLOWED_ORIGINS" in proc.stderr


def test_origine_supplementaire_refusee() -> None:
    config = strict_config()
    config["services"]["worker"]["environment"]["WALLIA_ALLOWED_ORIGINS"] = "https://wallia.valdev.me,https://evil.tld"
    assert run_env_check(config).returncode != 0


@pytest.mark.parametrize(
    "key,value",
    [
        ("WALLIA_ENV", "staging"),
        ("WALLIA_COOKIE_SECURE", "0"),
        ("WALLIA_PROVIDER_ENDPOINT", "http://api.deepseek.com/v1"),
        ("WALLIA_PROVIDER_ENDPOINT", "https://api.deepseek.com/v2"),
    ],
)
def test_valeurs_de_garde_refusees(key: str, value: str) -> None:
    config = strict_config()
    config["services"]["api"]["environment"][key] = value
    assert run_env_check(config).returncode != 0


def test_valeur_absente_refusee() -> None:
    config = strict_config()
    config["services"]["api"]["environment"]["WALLIA_ENV"] = None
    assert run_env_check(config).returncode != 0


def test_backend_de_test_refuse() -> None:
    config = strict_config()
    config["services"]["worker"]["environment"]["WALLIA_EMBEDDING_BACKEND"] = "fixture"
    assert run_env_check(config).returncode != 0
    config = strict_config()
    config["services"]["worker"]["environment"]["WALLIA_RERANKER_BACKEND"] = "fixture"
    assert run_env_check(config).returncode != 0


def test_service_de_test_refuse() -> None:
    # Un service de test SANS profil serait un service de runtime : refusé.
    config = strict_config()
    config["services"]["fake-upstream"] = {"image": IMAGE}
    assert run_env_check(config).returncode != 0


def test_service_sous_profil_tolere() -> None:
    # Profil `testtools` : jamais démarré par `up db api worker` — accepté.
    config = strict_config()
    assert run_env_check(config).returncode == 0


def test_projet_incorrect_refuse() -> None:
    config = strict_config()
    config["name"] = "wallia-e2e"
    assert run_env_check(config).returncode != 0


def test_image_differente_refusee() -> None:
    assert run_env_check(strict_config(image="wallia:local")).returncode != 0


def test_aucune_valeur_etrangere_imprimee() -> None:
    config = strict_config()
    config["services"]["worker"]["environment"]["SENTINELLE_INTERNE"] = "DO-NOT-PRINT-123"
    config["services"]["api"]["environment"]["WALLIA_ALLOWED_ORIGINS"] = "https://wallia.valdev.me.attaquant.tld"
    proc = run_env_check(config)
    assert proc.returncode != 0
    assert "DO-NOT-PRINT-123" not in proc.stdout + proc.stderr


# --- delivery_state.py ------------------------------------------------------

def run_state(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(STATE), *args], capture_output=True, text=True, timeout=60
    )


def make_refs(tmp_path: Path) -> tuple[Path, Path]:
    compose = tmp_path / "compose.yml"
    compose.write_text("name: wallia\n", encoding="utf-8")
    env = tmp_path / "env.snapshot"
    env.write_text("WALLIA_ENV=production\n", encoding="utf-8")
    return compose, env


def test_state_ecriture_atomique_et_lecture(tmp_path: Path) -> None:
    compose, env = make_refs(tmp_path)
    path = tmp_path / "state" / "current.json"
    proc = run_state(
        "write", str(path),
        "--stamp", "20260930T000000Z",
        "--image", "wallia:" + "1" * 40,
        "--image-id", "sha256:" + "a" * 64,
        "--sha", "1" * 40,
        "--compose", str(compose),
        "--env", str(env),
        "--note", "test",
    )
    assert proc.returncode == 0, proc.stderr
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    read = run_state("read", str(path), "image")
    assert read.stdout.strip() == "wallia:" + "1" * 40
    # La clé JSON du snapshot Compose est bien `compose_snapshot` (mapping
    # --compose/--env → compose_snapshot/env_snapshot, jamais AttributeError).
    assert run_state("read", str(path), "compose_snapshot").stdout.strip() == str(compose)
    assert run_state("read", str(path), "env_snapshot").stdout.strip() == str(env)
    assert run_state("validate-refs", str(path)).returncode == 0


def test_state_null_previous_explicite(tmp_path: Path) -> None:
    path = tmp_path / "previous.json"
    proc = run_state("write", str(path), "--stamp", "x", "--null-previous", "--note", "première installation")
    assert proc.returncode == 0
    assert run_state("read", str(path), "image").stdout.strip() == ""
    assert run_state("validate-refs", str(path)).returncode == 0
    # previous=null n'accepte pas d'image recopiée par erreur
    bad = run_state("write", str(path), "--null-previous", "--image", "wallia:" + "1" * 40)
    assert bad.returncode != 0
    bad = run_state("write", str(path), "--null-previous", "--compose", str(path))
    assert bad.returncode != 0


def test_state_refs_manquantes_refusees(tmp_path: Path) -> None:
    compose, env = make_refs(tmp_path)
    path = tmp_path / "previous.json"
    written = run_state(
        "write", str(path),
        "--image", "wallia:" + "1" * 40,
        "--image-id", "sha256:" + "b" * 64,
        "--compose", str(compose),
        "--env", str(env),
    )
    assert written.returncode == 0, written.stderr  # write nominal réellement exercé
    compose.unlink()
    proc = run_state("validate-refs", str(path))
    assert proc.returncode != 0
    assert "compose_snapshot" in proc.stderr


def test_state_image_id_non_conforme_refuse(tmp_path: Path) -> None:
    compose, env = make_refs(tmp_path)
    proc = run_state(
        "write", str(tmp_path / "previous.json"),
        "--image", "wallia:" + "1" * 40,
        "--image-id", "wallia:local",
        "--compose", str(compose),
        "--env", str(env),
    )
    assert proc.returncode != 0


def test_state_write_refuse_des_references_incoherentes(tmp_path: Path) -> None:
    compose, env = make_refs(tmp_path)
    missing = tmp_path / "jamais-creee.yml"
    proc = run_state(
        "write", str(tmp_path / "previous.json"),
        "--image", "wallia:" + "1" * 40,
        "--image-id", "sha256:" + "c" * 64,
        "--compose", str(missing),
        "--env", str(env),
    )
    assert proc.returncode != 0
    assert "snapshot Compose référencé" in proc.stderr
    assert not (tmp_path / "previous.json").exists()
    # Champs manquants : les options de l'état sont bien --image-id/--compose/--env.
    proc = run_state("write", str(tmp_path / "previous.json"), "--image", "wallia:" + "1" * 40)
    assert proc.returncode != 0
    for option in ("--image-id", "--compose", "--env"):
        assert option in proc.stderr
    # Compose présent mais env absent : refusé aussi (aucune référence partielle).
    proc = run_state(
        "write", str(tmp_path / "previous.json"),
        "--image", "wallia:" + "1" * 40,
        "--image-id", "sha256:" + "c" * 64,
        "--compose", str(compose),
        "--env", str(tmp_path / "absent.env"),
    )
    assert proc.returncode != 0
    assert "fichier d'environnement référencé" in proc.stderr


def test_state_previous_null_jamais_melange_a_un_etat_image(tmp_path: Path) -> None:
    path = tmp_path / "mixed.json"
    path.write_text(
        json.dumps(
            {
                "stamp": "x",
                "previous": None,
                "image": "wallia:" + "1" * 40,
                "image_id": "sha256:" + "a" * 64,
            }
        ),
        encoding="utf-8",
    )
    proc = run_state("validate-refs", str(path))
    assert proc.returncode != 0
    assert "previous=null incohérent" in proc.stderr
