"""Tests de déploiement/rollback AVEC DOUBLES (aucun Docker réel, aucun git réel).

Garanties vérifiées : refus de `--sha` arbitraire, contrôle STRICT de
l'environnement effectif AVANT toute mutation, projet Compose forcé à wallia
(même avec WALLIA_COMPOSE_PROJECT hostile), santé « healthy » obligatoire
(jamais « running »), état précédent jamais fabriqué (previous=null explicite
ou reprise de current.json — l'option --bootstrap-previous-env est RETIRÉE et
refusée avant mutation), snapshot Compose immuable référençant l'ID résolu et
produit AVANT up/migrate, mutations passant par CE snapshot, image réellement
portée par api/worker vérifiée après démarrage, rollback qui rejoue UNIQUEMENT
le snapshot précédent (refus si le snapshot ne référence pas l'ID attendu),
actualise current.json et consomme l'état précédent.
"""
from __future__ import annotations

import json
from pathlib import Path

from conftest import DeliverySandbox

IMAGE_ID = "sha256:" + "a" * 64


def head(delivery: DeliverySandbox) -> str:
    return delivery.state()["head_sha"]


def state_path(delivery: DeliverySandbox, name: str) -> Path:
    return delivery.runtime / "deploy-state" / name


def deploy_ready_state(delivery: DeliverySandbox) -> None:
    state = delivery.state()
    delivery.prepare_image(state)
    state["config_json"] = delivery.strict_config()
    delivery.deployed_containers(state, image_id=IMAGE_ID)
    delivery.state_path.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")


def write_previous_state(delivery: DeliverySandbox, old_sha: str, old_id: str) -> Path:
    """État précédent dont le snapshot JSON référence VRAIMENT l'ID attendu."""
    state_dir = delivery.runtime / "deploy-state"
    snapshot = state_dir / "compose-ancien.json"
    snapshot.write_text(
        json.dumps(
            {"name": "wallia", "services": {"api": {"image": old_id}, "worker": {"image": old_id}}},
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    prev_env = state_dir / "env-ancien.env"
    prev_env.write_text("WALLIA_ENV=production\n", encoding="utf-8")
    written = delivery.run_python(
        "scripts/delivery_state.py", "write", str(state_dir / "previous.json"),
        "--stamp", "x", "--image", f"wallia:{old_sha}", "--image-id", old_id,
        "--compose", str(snapshot), "--env", str(prev_env),
    )
    assert written.returncode == 0, written.stderr
    return snapshot


def test_deploy_refuse_sha_arbitraire(delivery: DeliverySandbox) -> None:
    env_file = delivery.write_env_file()
    proc = delivery.run("deploy.sh", "--env-file", str(env_file), "--sha", "deadbeef")
    assert proc.returncode != 0
    assert "argument inconnu" in proc.stderr
    assert delivery.calls() == []  # rien n'est exécuté : retour uniquement par rollback.sh


def test_deploy_refuse_un_environnement_effectif_non_strict(delivery: DeliverySandbox) -> None:
    config = delivery.strict_config()
    # Préfixe d'origine : l'ancien contrôle par préfixe l'aurait accepté.
    config["services"]["api"]["environment"]["WALLIA_ALLOWED_ORIGINS"] = "https://wallia.valdev.me.attaquant.tld"
    delivery.update_state(config_json=config)
    env_file = delivery.write_env_file()
    proc = delivery.run("deploy.sh", "--env-file", str(env_file))
    assert proc.returncode != 0
    assert "NON CONFORME" in proc.stderr
    assert not state_path(delivery, "current.json").exists()
    assert not state_path(delivery, "previous.json").exists()
    assert not any(" up " in f" {call} " for call in delivery.calls())


def test_deploy_refuse_un_arbre_sale_avant_toute_mutation(delivery: DeliverySandbox) -> None:
    delivery.update_state(dirty=True)
    deploy_ready_state(delivery)
    env_file = delivery.write_env_file()
    proc = delivery.run("deploy.sh", "--env-file", str(env_file))
    assert proc.returncode != 0
    assert "arbre de travail sale" in proc.stderr
    assert not any("config" in call for call in delivery.calls())
    assert not state_path(delivery, "current.json").exists()


def test_deploy_nominal_projet_force_et_etat_enregistre(delivery: DeliverySandbox) -> None:
    deploy_ready_state(delivery)
    env_file = delivery.write_env_file()
    sha = head(delivery)
    proc = delivery.run("deploy.sh", "--env-file", str(env_file), overrides={"WALLIA_COMPOSE_PROJECT": "evil"})
    assert proc.returncode == 0, proc.stderr
    # Les backticks des messages shell ne doivent JAMAIS devenir des commandes.
    assert "command not found" not in proc.stderr

    calls = delivery.calls()
    compose = [call for call in calls if call.startswith("compose ")]
    assert compose, "aucun appel compose enregistré"
    assert all("-p wallia" in call for call in compose), "le projet doit être forcé à wallia"
    assert not any("evil" in call for call in calls)
    assert not any("remove-orphans" in call for call in calls)

    # Contrôle de l'environnement rendu AVANT le démarrage.
    first_config = next(i for i, call in enumerate(calls) if "config --format json" in call)
    first_up = next(i for i, call in enumerate(calls) if " up " in f" {call} ")
    assert first_config < first_up

    current = json.loads(state_path(delivery, "current.json").read_text(encoding="utf-8"))
    assert current["image"] == f"wallia:{sha}"
    assert current["image_id"] == IMAGE_ID
    snapshot = Path(current["compose_snapshot"])
    assert snapshot.is_file()
    assert Path(current["env_snapshot"]).is_file()

    # Le snapshot RENDU référence l'ID immuable (jamais le tag mutable) et il a
    # été produit AVANT up/migrate.
    snapshot_json = json.loads(snapshot.read_text(encoding="utf-8"))
    assert snapshot_json["services"]["api"]["image"] == IMAGE_ID
    assert snapshot_json["services"]["worker"]["image"] == IMAGE_ID
    json_render_calls = [i for i, call in enumerate(calls) if "config --format json" in call]
    assert len(json_render_calls) >= 2  # env effectif + snapshot du nouvel état
    assert json_render_calls[1] < first_up

    # TOUTES les mutations passent par CE snapshot (jamais le tag + env-file
    # pendant que l'ID n'est enregistré qu'après).
    mutations = [call for call in compose if " up " in f" {call} " or " run " in f" {call} "]
    assert mutations
    assert all(str(snapshot) in call for call in mutations)
    assert all(str(env_file) not in call for call in mutations)

    # L'image EFFECTIVEMENT portée par api/worker est contrôlée après démarrage.
    assert any("{{.Image}}" in call and "cid-api" in call for call in calls)
    assert any("{{.Image}}" in call and "cid-worker" in call for call in calls)

    previous = json.loads(state_path(delivery, "previous.json").read_text(encoding="utf-8"))
    assert previous["previous"] is None  # première installation : jamais un faux état


def test_deploy_exige_healthy_jamais_running(delivery: DeliverySandbox) -> None:
    deploy_ready_state(delivery)
    delivery.update_state(health={"db": "healthy", "api": "running", "worker": "healthy"})
    env_file = delivery.write_env_file()
    proc = delivery.run("deploy.sh", "--env-file", str(env_file), overrides={"WALLIA_HEALTH_TIMEOUT": "2"})
    assert proc.returncode != 0
    assert "healthy" in proc.stderr
    assert not state_path(delivery, "current.json").exists()


def test_deploy_echoue_si_image_effective_differente(delivery: DeliverySandbox) -> None:
    deploy_ready_state(delivery)
    # api démarre sur une AUTRE image que celle du snapshot : refus après constat.
    delivery.update_state(
        containers={"cid-api": {"image_id": "sha256:" + "b" * 64}, "cid-worker": {"image_id": IMAGE_ID}}
    )
    env_file = delivery.write_env_file()
    proc = delivery.run("deploy.sh", "--env-file", str(env_file))
    assert proc.returncode != 0
    assert "image effective" in proc.stderr
    assert not state_path(delivery, "current.json").exists()


def test_deploy_refuse_un_runtime_existant_sans_etat_suivi(delivery: DeliverySandbox) -> None:
    deploy_ready_state(delivery)
    delivery.update_state(project_containers=["wallia-api-1"])
    env_file = delivery.write_env_file()
    proc = delivery.run("deploy.sh", "--env-file", str(env_file))
    assert proc.returncode != 0
    assert "refus de fabriquer un état" in proc.stderr
    assert "snapshot fidèle" in proc.stderr  # préparation manuelle exigée (docs/operations.md §12)
    assert not state_path(delivery, "previous.json").exists()
    assert not state_path(delivery, "current.json").exists()
    calls = " || ".join(delivery.calls())
    assert " up " not in f" {calls} "
    assert " run " not in f" {calls} "
    assert " stop " not in f" {calls} "


def test_deploy_bootstrap_previous_env_retire_refuse_sans_mutation(delivery: DeliverySandbox) -> None:
    deploy_ready_state(delivery)
    delivery.update_state(project_containers=["wallia-api-1"])
    bootstrap = delivery.runtime / "secrets" / "bootstrap-prev.env"
    bootstrap.write_text("WALLIA_ENV=local\n", encoding="utf-8")
    env_file = delivery.write_env_file()
    proc = delivery.run("deploy.sh", "--env-file", str(env_file), "--bootstrap-previous-env", str(bootstrap))
    assert proc.returncode != 0
    assert "RETIRÉE" in proc.stderr
    assert "snapshot fidèle" in proc.stderr
    # Refus pendant l'analyse des arguments : AUCUN appel docker/git, aucune mutation.
    assert delivery.calls() == []
    assert not state_path(delivery, "previous.json").exists()
    assert not state_path(delivery, "current.json").exists()


def test_deploy_previous_repris_de_current_sans_faux_etat(delivery: DeliverySandbox) -> None:
    old_sha = "d" * 40
    old_id = "sha256:" + "d" * 64
    delivery.write_current_state(image=f"wallia:{old_sha}", image_id=old_id)
    old_snapshot = delivery.runtime / "deploy-state" / "compose-courant.json"
    old_env = delivery.runtime / "deploy-state" / "env-courant.env"
    deploy_ready_state(delivery)
    env_file = delivery.write_env_file()
    proc = delivery.run("deploy.sh", "--env-file", str(env_file))
    assert proc.returncode == 0, proc.stderr

    previous = json.loads(state_path(delivery, "previous.json").read_text(encoding="utf-8"))
    current = json.loads(state_path(delivery, "current.json").read_text(encoding="utf-8"))
    # L'état précédent est EXACTEMENT l'ancien (jamais le nouvel env recopié).
    assert previous["image"] == f"wallia:{old_sha}"
    assert previous["image_id"] == old_id
    assert previous["compose_snapshot"] == str(old_snapshot)
    assert previous["env_snapshot"] == str(old_env)
    # Le nouvel état est distinct et référence le nouveau snapshot/l'ID résolu.
    assert current["image_id"] == IMAGE_ID
    assert current["compose_snapshot"] != previous["compose_snapshot"]


def test_rollback_sans_etat_precedent_refuse_sans_mutation(delivery: DeliverySandbox) -> None:
    proc = delivery.run("rollback.sh")
    assert proc.returncode != 0
    assert "état précédent introuvable" in proc.stderr
    assert delivery.calls() == []


def test_rollback_previous_null_refuse_sans_mutation(delivery: DeliverySandbox) -> None:
    written = delivery.run_python(
        "scripts/delivery_state.py", "write", "runtime/deploy-state/previous.json",
        "--null-previous", "--stamp", "x",
    )
    assert written.returncode == 0, written.stderr
    proc = delivery.run("rollback.sh")
    assert proc.returncode != 0
    assert "previous=null" in proc.stderr
    assert delivery.calls() == []


def test_rollback_rejoue_le_snapshot_et_actualise_current(delivery: DeliverySandbox) -> None:
    old_sha = "e" * 40
    old_id = "sha256:" + "f" * 64
    state_dir = delivery.runtime / "deploy-state"
    snapshot = write_previous_state(delivery, old_sha, old_id)
    delivery.update_state(
        image_ids={old_id: old_id},
        containers={"cid-api": {"image_id": old_id}, "cid-worker": {"image_id": old_id}},
    )

    proc = delivery.run("rollback.sh")
    assert proc.returncode == 0, proc.stderr
    assert "command not found" not in proc.stderr
    calls = delivery.calls()
    # Le snapshot précédent est rejoué TEL QUEL : jamais le Compose courant.
    assert any(f"-f {snapshot}" in call for call in calls)
    assert not any("docker-compose.yml" in call for call in calls)
    # L'image réellement portée par api/worker est contrôlée après démarrage.
    assert any("{{.Image}}" in call and "cid-api" in call for call in calls)
    assert any("{{.Image}}" in call and "cid-worker" in call for call in calls)

    current = json.loads((state_dir / "current.json").read_text(encoding="utf-8"))
    assert current["image"] == f"wallia:{old_sha}"
    assert current["image_id"] == old_id
    assert current["compose_snapshot"] == str(snapshot)
    assert not (state_dir / "previous.json").exists()  # état précédent consommé


def test_rollback_refuse_snapshot_incoherent_avant_mutation(delivery: DeliverySandbox) -> None:
    old_sha = "e" * 40
    old_id = "sha256:" + "f" * 64
    state_dir = delivery.runtime / "deploy-state"
    snapshot = write_previous_state(delivery, old_sha, old_id)
    # Le snapshot référence une AUTRE image : refus AVANT up/run.
    snapshot.write_text(
        json.dumps(
            {
                "name": "wallia",
                "services": {"api": {"image": "sha256:" + "9" * 64}, "worker": {"image": old_id}},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    delivery.update_state(image_ids={old_id: old_id})

    proc = delivery.run("rollback.sh")
    assert proc.returncode != 0
    assert "ne référence PAS l'ID immuable" in proc.stderr
    assert not any(" up " in f" {call} " for call in delivery.calls())
    assert not any(" run " in f" {call} " for call in delivery.calls())
    assert (state_dir / "previous.json").exists()  # état NON consommé
    assert not (state_dir / "current.json").exists()


def test_rollback_refuse_snapshot_a_tag_mutable(delivery: DeliverySandbox) -> None:
    old_sha = "e" * 40
    old_id = "sha256:" + "f" * 64
    state_dir = delivery.runtime / "deploy-state"
    snapshot = write_previous_state(delivery, old_sha, old_id)
    # Le snapshot référence un TAG mutable (pas l'ID) : refus AVANT up/run —
    # l'existence locale de l'ID ne suffit jamais.
    snapshot.write_text(
        json.dumps(
            {
                "name": "wallia",
                "services": {"api": {"image": f"wallia:{old_sha}"}, "worker": {"image": f"wallia:{old_sha}"}},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    delivery.update_state(image_ids={old_id: old_id})

    proc = delivery.run("rollback.sh")
    assert proc.returncode != 0
    assert "ne référence PAS l'ID immuable" in proc.stderr
    assert not any(" up " in f" {call} " for call in delivery.calls())
    assert not any(" run " in f" {call} " for call in delivery.calls())
    assert (state_dir / "previous.json").exists()


def test_rollback_sante_non_atteinte_echoue_sans_consommer(delivery: DeliverySandbox) -> None:
    old_sha = "e" * 40
    old_id = "sha256:" + "f" * 64
    state_dir = delivery.runtime / "deploy-state"
    write_previous_state(delivery, old_sha, old_id)
    delivery.update_state(
        image_ids={old_id: old_id},
        containers={"cid-api": {"image_id": old_id}, "cid-worker": {"image_id": old_id}},
        health={"db": "healthy", "api": "starting", "worker": "healthy"},
    )
    proc = delivery.run("rollback.sh", overrides={"WALLIA_HEALTH_TIMEOUT": "2"})
    assert proc.returncode != 0
    assert "santé" in proc.stderr
    assert (state_dir / "previous.json").exists()  # échec : état conservé
    assert not (state_dir / "current.json").exists()
