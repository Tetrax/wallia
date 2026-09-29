"""Tests des commandes de sauvegarde AVEC DOUBLES (aucun Docker réel).

Le double `docker` (tests/delivery/conftest.py) journalise chaque appel et
simule stop/start/exec/inspect/ps selon un état JSON. Les garanties vérifiées :

- le runtime sélectionné est le SNAPSHOT COURANT validé (current.json) ; sans
  état suivi, `--env-file` EXPLICITE est exigé (jamais de fallback implicite) ;
- la découverte des services (compose ps) et la vérification d'arrêt sont
  FATALES en cas d'échec (jamais assimilées à « zéro service ») ;
- manifeste calculé PENDANT la pause (arrêt → dump → manifeste → reprise) ;
- seuls les services api/worker INITIALEMENT actifs sont arrêtés puis repris ;
- arrêt non confirmé ⇒ échec explicite ET reprise quand même déclenchée ;
- reprise impossible (healthcheck « healthy » non atteint) ⇒ code de sortie non nul ;
- validations restore_lib sur le bundle + référence DB sans fichier ⇒ échec
  non nul AVEC reprise des services ;
- bundle : archives/empreintes cohérentes, aucun fichier temporaire résiduel.
"""
from __future__ import annotations

import hashlib
import json

from conftest import DeliverySandbox  # bac à sable fourni par le socle

TABLE_KEYS = {
    "users",
    "conversations",
    "messages",
    "attachments",
    "documents",
    "chunks",
    "ingestion_jobs",
    "schema_migrations",
}


def test_backup_utilise_le_snapshot_courant_valide(delivery: DeliverySandbox) -> None:
    current = delivery.write_current_state()
    snapshot = json.loads(current.read_text(encoding="utf-8"))["compose_snapshot"]
    proc = delivery.run("backup.sh")
    assert proc.returncode == 0, proc.stderr
    assert "command not found" not in proc.stderr

    calls = delivery.calls()
    # Le runtime est le snapshot validé, jamais le Compose courant du dépôt
    # (aucun app.env implicite).
    assert any(str(snapshot) in call for call in calls)
    assert not any("docker-compose.yml" in call for call in calls)
    assert not any("secrets/app.env" in call for call in calls)

    bundle = delivery.newest_bundle()
    manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    assert set(manifest["tables"]) == TABLE_KEYS
    paths = {item["path"] for item in manifest["files"]}
    assert {"documents/doc1.pdf", "uploads/conv1/note.txt"} <= paths
    assert len(manifest["db_files"]) == 2
    assert manifest["db_files_missing"] == []

    for name in ("db.sql.gz", "data.tar.gz"):
        digest = hashlib.sha256((bundle / name).read_bytes()).hexdigest()
        assert digest == manifest["archives"][name]["sha256"]

    # Aucun fichier temporaire de collecte n'est laissé dans le bundle.
    assert not (bundle / ".counts.json").exists()
    assert not (bundle / ".db-files.json").exists()

    # Ordre réel : arrêt → dump → reprise (manifeste écrit entre les deux).
    stop_api = next(i for i, call in enumerate(calls) if call.endswith(" stop api"))
    dump = next(i for i, call in enumerate(calls) if "pg_dump" in call)
    start_api = next(i for i, call in enumerate(calls) if call.endswith(" start api"))
    assert stop_api < dump < start_api
    # Comptes et références exigent ON_ERROR_STOP.
    assert any("ON_ERROR_STOP=1" in call and "count(*)" in call for call in calls)
    assert any("ON_ERROR_STOP=1" in call and "stored_relpath" in call for call in calls)


def test_backup_runtime_historique_exige_env_file_explicite(delivery: DeliverySandbox) -> None:
    # Aucun état suivi ET aucun --env-file : refus AVANT toute pause/archive.
    proc = delivery.run("backup.sh")
    assert proc.returncode != 0
    assert "--env-file" in proc.stderr
    assert "fallback" in proc.stderr
    assert not any(" stop " in f" {call} " for call in delivery.calls())
    assert not any("pg_dump" in call for call in delivery.calls())
    assert not list((delivery.runtime / "backups").glob("wallia-bundle-*"))

    # Avec un --env-file explicite, le runtime historique est sauvegardé.
    env_file = delivery.write_env_file(name="app.historique.env")
    proc = delivery.run("backup.sh", "--env-file", str(env_file))
    assert proc.returncode == 0, proc.stderr
    calls = delivery.calls()
    assert any("docker-compose.yml" in call and str(env_file) in call for call in calls)
    assert delivery.newest_bundle().is_dir()


def test_backup_decouverte_cassee_est_fatale_avant_pause(delivery: DeliverySandbox) -> None:
    delivery.write_current_state()
    delivery.update_state(fail_ps_running=True)
    proc = delivery.run("backup.sh")
    assert proc.returncode != 0
    assert "découverte des services en échec" in proc.stderr
    assert not any(" stop " in f" {call} " for call in delivery.calls())
    assert not any("pg_dump" in call for call in delivery.calls())
    # Rien n'est créé : ni pause, ni archive, ni bundle.
    assert not list((delivery.runtime / "backups").glob("wallia-bundle-*"))


def test_backup_verification_arret_impossible_est_fatale(delivery: DeliverySandbox) -> None:
    # `compose ps` fonctionne à la découverte mais échoue après le stop : l'état
    # des services est INCONNU — jamais supposé « arrêté ».
    delivery.write_current_state()
    delivery.update_state(fail_ps_running="after_stop")
    proc = delivery.run("backup.sh")
    assert proc.returncode != 0
    assert "vérification d'arrêt IMPOSSIBLE" in proc.stderr
    # La reprise a bien eu lieu (trap armé avant l'arrêt).
    assert {"api", "worker"} <= set(delivery.state()["running"])
    bundle = delivery.newest_bundle()
    assert not (bundle / "manifest.json").exists()


def test_backup_ne_reprend_que_les_services_initialement_actifs(delivery: DeliverySandbox) -> None:
    delivery.write_current_state()
    delivery.update_state(running=["api"])
    proc = delivery.run("backup.sh")
    assert proc.returncode == 0, proc.stderr
    joined = " || ".join(delivery.calls())
    assert " stop api" in joined
    assert " start api" in joined
    assert " stop worker" not in joined
    assert " start worker" not in joined


def test_backup_arret_non_confirme_echoue_et_reprend_les_services(delivery: DeliverySandbox) -> None:
    delivery.write_current_state()
    delivery.update_state(fail_stop=["worker"])
    proc = delivery.run("backup.sh")
    assert proc.returncode != 0
    assert "NON CONFIRMÉ" in proc.stderr
    # Le trap armé AVANT l'arrêt a bien repris les services initialement actifs.
    running = set(delivery.state()["running"])
    assert {"api", "worker"} <= running
    # Sauvegarde interrompue : aucun manifeste publié.
    bundle = delivery.newest_bundle()
    assert not (bundle / "manifest.json").exists()


def test_backup_reprise_en_echec_donne_un_rc_non_nul(delivery: DeliverySandbox) -> None:
    delivery.write_current_state()
    delivery.update_state(fail_start=["api"], health={"db": "healthy", "api": "starting", "worker": "healthy"})
    proc = delivery.run("backup.sh", overrides={"WALLIA_HEALTH_TIMEOUT": "2"})
    assert proc.returncode != 0
    assert "reprise" in proc.stderr.lower()
    # La sauvegarde elle-même était terminée (manifeste publié) : c'est la
    # reprise qui impose le code non nul, jamais un faux succès.
    bundle = delivery.newest_bundle()
    assert (bundle / "manifest.json").is_file()


def test_backup_reference_db_sans_fichier_non_valide_avec_reprise(delivery: DeliverySandbox) -> None:
    delivery.write_current_state()
    delivery.update_state(refs_sql="attachments=conv1/note.txt\ndocuments=doc1.pdf\nattachments=conv1/ghost.txt")
    proc = delivery.run("backup.sh")
    assert proc.returncode != 0
    assert "NON valide" in proc.stderr
    # Reprise des services malgré l'échec de validation.
    assert {"api", "worker"} <= set(delivery.state()["running"])
    bundle = delivery.newest_bundle()
    manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["db_files_missing"] == ["uploads/conv1/ghost.txt"]


def test_backup_dry_run_ne_touche_a_rien(delivery: DeliverySandbox) -> None:
    proc = delivery.run("backup.sh", "--dry-run")
    assert proc.returncode == 0
    assert delivery.calls() == []
