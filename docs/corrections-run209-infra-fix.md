# Corrections run209 — lot infra (défauts concrets après exécution)

Codeur ponctuel (DeepSeek opencode-go), tour non exécuté : aucun test lancé
côté codeur. Le principal relit et exécute. Preuve d'entrée :
`runtime/evidence/run209-infra-tests-fixtures.log` (54 passés / 5 échoués —
les 5 échecs venaient du mapping argparse de `delivery_state.py`).

## 1. `delivery_state.py` — mapping argparse

- `WRITE_REQUIRED_ARGS = ("image", "image_id", "compose", "env")` (dests
  argparse) séparé de `JSON_REQUIRED_FIELDS` (clés JSON
  `compose_snapshot`/`env_snapshot`) : plus d'`AttributeError` ; le mapping
  `--compose`/`--env` → `compose_snapshot`/`env_snapshot` est exercé.
- Écriture REFUSÉE si le snapshot Compose ou l'env référencé est absent/vide ;
  `--null-previous` EXCLUSIF (jamais mêlé à une référence d'image) ;
  `validate-refs` refuse `previous:null` + références d'image.
- Conservés : JSON atomique (temporaire + rename), 0700/0600.

## 2. Backticks dans les messages shell

- Retirées les substitutions involontaires (`` `healthy` ``, `` `running` ``)
  des messages de `deploy.sh` et `backup.sh` : plus de `command not found`.
- Les tests nominaux l'exigent explicitement (`assert "command not found" not
  in stderr`).
- Aucun wrapper ajouté.

## 3. Bootstrap précédent — RETIRÉ

- `--bootstrap-previous-env` refuse désormais AVANT toute mutation (message :
  capture automatique ≠ rollback fidèle ; préparer/vérifier un snapshot fidèle,
  docs/operations.md §12).
- Runtime existant sans `current.json` VALIDE ⇒ refus avant mutation (aucune
  fabrique d'état depuis un conteneur arbitraire) ; première installation
  réellement vide ⇒ `previous:null` conservé.
- Test adapté : exige le refus sans `up`/`run`/`stop` (couverture conservée).

## 4. Déploiement par ID / rollback

- Tag résolu UNE fois ; snapshot Compose RENDU (JSON) référençant l'ID immuable,
  produit et VÉRIFIÉ avant up/migrate ; CE snapshot est utilisé pour toutes les
  mutations (`up`, `run`, `up -d`).
- Image EFFECTIVE d'api/worker vérifiée après démarrage (`{{.Image}}`).
- Rollback : refuse avant up/run si le snapshot ne référence PAS l'ID attendu
  (ni autre image, ni tag mutable) ; `current.json` actualisé après succès,
  état précédent consommé ; pas de migrations descendantes ; pas de
  remove-orphans/prune.

## 5. `backup.sh`

- Runtime = SNAPSHOT COURANT validé (`current.json`) sinon `--env-file`
  EXPLICITE obligatoire (plus de fallback implicite vers `app.env`).
- Découverte `compose ps` FATALE avant pause/archive (idem vérification
  d'arrêt) ; arrêt/reprise des seuls api/worker initialement actifs ; reprise
  « healthy » obligatoire, rc non nul sinon.
- Comptes/refs avec `psql -v ON_ERROR_STOP=1` ; bundle unique sans `-p`
  (suffixe horodaté, pas d'écrasement même seconde) ; validations
  `restore_lib.py validate` sur le bundle avant succès ; référence DB sans
  fichier ⇒ rc non nul AVEC reprise.
- Doc de sensibilité : la base contient empreintes ET jetons hachés — bundle
  NON publiable.

## 6. `wallia-tls-activate.sh`

- `notBefore` COMPARÉ à maintenant (certificat futur refusé).
- Rollback exact : vhost (contenu + mode), entrée sites-enabled
  absence/lien relatif ou absolu/fichier + mode, lien `active` — restaurés à
  l'identique ; transaction armée avant la première mutation, rollback UNIQUE
  sur TOUT échec (install, nginx, probe), rc non nul conservé.
- Probe HTTP ACME : rollback + retries bornés (`WALLIA_TLS_HTTP_PROBE_*`).
- Conservés : 3 phases, racines fixes root, modèles root-owned
  `/etc/wallia/tls-templates`, verrou externe, timeout, aucune clé affichée.

## 7. `check-models` (`backend/app/cli.py`)

- Manifestes : fichiers ESSENTIELS exigés (miroir de `fetch_models.py`),
  chemins relatifs sûrs (absolus, `../`, non canoniques, cache refusés),
  identité/révision, tailles/SHA256 réels.
- Timeout de sonde = résultat non-ok explicite (variable
  `WALLIA_CHECK_STEP_TIMEOUT`, défaut 900 s) ; sondes réelles séquentielles
  E5/reranker/Docling conservées, hors réseau.

## 8. `restore.sh`

- Cleanup automatique (`trap rm -rf TMP_DIR`) supprimé ; sorties de
  vérification privées, rangées SOUS la cible isolée
  (`<cible>/verification/…`), CONSERVÉES (aucun autre nettoyage hôte ajouté).

## Tests (NON EXÉCUTÉS par le codeur)

- `tests/delivery/test_delivery_helpers.py` : mapping + refus renforcés,
  returncode de `write` désormais vérifié (tests qui l'ignoraient renforcés).
- `test_deploy_rollback_mocked.py` : refus `--bootstrap-previous-env` sans
  mutation, previous repris de `current.json`, snapshot avant up, mutations
  par le snapshot, image effective, rollback refusant snapshot incohérent /
  tag mutable, échec de santé sans consommation.
- `test_backup_flow_mocked.py` : snapshot courant utilisé, `--env-file`
  explicite, découverte cassée, arrêt non vérifiable, partial stop, reprise
  cassée, refs sans fichier, dry-run.
- `test_tls_transaction.py` (nouveau) : transaction TLS en bac à sable non
  privilégié (copie du script, racines réécrites, doubles `id`/`install`/
  `nginx`/`systemctl`/`curl`) — rollback unique, restauration EXACTE (fichier+
  mode, lien relatif, fichier enabled), absence, probe avec retries bornés,
  refus avant mutation si modèle manquant.
- `test_tls_self_check.py` : + certificat FUTUR refusé (`cryptography` si
  dispo, sinon ignoré).
- `test_check_models_manifest.py` (nouveau) : essentiels, chemins sûrs, cache,
  révision, SHA, manifeste vide, alignement des listes avec `fetch_models.py`
  (aucun réseau, aucun secret).

## Limites et points pour le principal

1. Tous les tests sont NON EXÉCUTÉS ici : ré-exécuter le harnais existant
   (mêmes garde-fous) et vérifier en premier les messages exacts attendus par
   les nouveaux tests TLS/CLI (assertions sur chaînes).
2. Le test TLS de transaction RÉÉCRIT des racines `_DIR`/`_ROOT` dans une COPIE
   du script (assertions de harnais) ; le script livré garde ses racines fixes
   et reste root-only non privilégiable par env.
3. Migration initiale runtime existant : la préparation VÉRIFIÉE du snapshot
   fidèle (image ID effectif, Compose rendu, env) reste à faire par le
   principal ; elle n'est PAS automatisée par ce lot.
4. `test_check_models_manifest.py` importe `backend/app/cli.py` : ignoré si
   l'import est impossible dans l'interpréteur (pas de DB/réseau ; aucun
   secret lu) ; à lancer dans l'image où les dépendances applicatives
   existent.
