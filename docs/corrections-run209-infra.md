# Corrections livraison/exploitation — revue run209, points 1–5

Lot fichiers-only (auteur unique, quatre outils). **Aucune exécution dans ce
lot : tout test/commande listé ici est NON EXÉCUTÉ** — les scripts, tests et
Dockerfile/Compose seront relus puis exécutés par le principal/Astra.

## Fichiers livrés

- Réécrits : `scripts/{lib.sh,build.sh,deploy.sh,rollback.sh,backup.sh,restore.sh,restore_lib.py}`,
  `scripts/fetch_models.py` (durci), `deployment/{wallia-tls-activate.sh,tls-hook.sh,README.md}`,
  `backend/app/cli.py` (check-models hors réseau).
- Nouveaux : `deployment/nginx-wallia-le-bootstrap.conf`, `scripts/delivery_state.py`
  et `scripts/delivery_env_check.py` (les 2 helpers autorisés), `tests/delivery/*`
  (`conftest.py`, 4 fichiers de tests fonctionnels, `test_tls_self_check.py`, `README.md`).
- Patchés : `Dockerfile` (chmod lecture au lieu de chown massif), `docker-compose.yml`
  (mount evidence retiré du runtime final, UID/ports/DB/réseaux/secrets intacts),
  `backend/app/docling_runner.py` (options pipeline explicites), `docs/operations.md` (§2, §2 bis, §10, §12).
- Revus, contenu conservé : `deployment/nginx-wallia-{http-initial,final}.conf`.

## Point 1 — TLS

- `-pubout` des DEUX côtés pour comparer les clés publiques (l'ancienne
  comparaison rejetait toute paire valide) ; `checkhost`/`checkend`/`startdate`
  explicites ; sonde `openssl s_client` sous `timeout` + retries bornés (anciens workers).
- Trois phases : `--install-http` (webroot réel + probe dans le VRAI
  `.well-known/acme-challenge/`, nom alphanumérique accepté par le regex Nginx)
  → `--stage-le` (paire Let's Encrypt RÉELLEMENT SERVIE, vhost intermédiaire,
  empreinte SNI vérifiée) → `--activate` (paire gérée, lien relatif `active`
  remplacé par `rename` atomique, rollback exact y compris absence du lien et
  état `sites-enabled`, y compris premier amorçage).
- Le script root ne lit QUE `/etc/wallia/tls-templates` (root-owned, installés
  par l'opérateur — `deployment/README.md`) ; il ne s'auto-installe pas, ne
  prend jamais le verrou infra externe (appel manuel sous `flock`) ; hook
  no-op hors lignée `wallia.valdev.me`, activateur à chemin fixe.

## Point 2 — Déploiement

- `--sha` arbitraire supprimé : déploiement TOUJOURS sur HEAD, arbre propre et
  HEAD réellement sur origin (vérification EN LIGNE) ; image `wallia:<sha
  complet>` dont le label OCI est vérifié.
- Garde-fous sur l'environnement Compose EFFECTIF (`docker compose config
  --format json` : fichier + variables héritées) : projet `wallia`, services
  sans profil == {db, api, worker} (un service à profils ne peut pas démarrer
  via `up db api worker`), origine EXACTE `https://wallia.valdev.me`, endpoint
  natif HTTPS, cookie secure, aucun backend `fixture`. Sortie silencieuse
  (aucune valeur de variable imprimée) ; refus AVANT toute mutation.
- Projet Compose forcé à `wallia` (jamais substituable par variable).
- Santé : `healthy` OBLIGATOIRE (un conteneur `running`/`starting`/sans
  healthcheck est refusé) ; pas de `remove-orphans`.
- État `runtime/deploy-state/` (0700, fichiers 0600, écritures atomiques) :
  image (tag), image ID immuable `sha256:…`, snapshot Compose RENDU, env.
  Première installation VIDE : `previous=null` explicite (rollback impossible).
  Transition depuis un runtime existant : `--bootstrap-previous-env` exigé et
  vérifié contre les conteneurs réellement présents — le nouvel env n'est
  JAMAIS recopié comme ancien ; jamais d'état fabriqué.
- `rollback.sh` : rejoue UNIQUEMENT le snapshot précédent, échoue AVANT
  mutation si absent/invalide, exige `healthy`, puis actualise `current.json`
  et consomme l'état précédent. Migrations AVANT-only (documentées §10).

## Point 3 — Sauvegarde

- Trap de reprise armé AVANT l'arrêt ; seuls les services api/worker
  INITIALEMENT ACTIFS sont arrêtés (db reste active) et repris — y compris
  après échec partiel du `stop` ; arrêt vérifié réellement (jamais supposé).
- Archive, comptes des 8 tables, références DB→fichiers et manifeste (SHA256)
  calculés PENDANT la pause (fichier temporaires retirés après).
- Reprise exigeant api+worker `healthy` ; rc NON NUL si la reprise échoue,
  même si la sauvegarde a réussi. Bundle 0700/0600 ; aucun secret, aucune
  sortie de contenu utilisateur ; note de sensibilité (empreintes
  comptes/sessions + données applicatives ⇒ pas un export publiable ; clés
  externes exclues, à sauvegarder séparément).

## Point 4 — Restauration

- V1 : `--isolated` UNIQUEMENT ; `--inplace`/`--yes` retirés (message
  explicite) ; procédure opérateur CONTRÔLÉE documentée (`docs/operations.md` §10).
- Manifeste strict : exactement les 8 tables, types entiers, chemins
  canoniques sans traversée/absolu/doublon ; SHA256 des archives vérifiés
  AVANT tout parsing ; tar : liens/absolus/membres non réguliers refusés.
- Conteneur `wallia-restore-<stamp>-<aléa>` : réseau none, aucun port,
  512 Mo/1 CPU, données PostgreSQL en volume dédié (jamais tmpfs) ; cible
  NEUVE garantie ; vérifications fichiers (manquants/extra/altérés/liens),
  comptes, références DB→fichiers, `ON_ERROR_STOP` ; AUCUN cleanup
  automatique (cible/conteneur/volume conservés comme preuve, commandes de
  nettoyage affichées).

## Point 5 — Image et modèles

- Dockerfile : plus de `chown -R` des poids ; `chmod -R a+rX` (lecture/traversée
  par uid 1002, y compris sources copiées en 0600) ; poids root-owned immuables.
- `fetch_models.py` : 4 dépôts aux révisions IMMUABLES, listes de fichiers
  explicites (safetensors only), README/licences conservés, fichiers
  ESSENTIELS exigés, caches HF `.cache` purgés et exclus des manifestes ;
  `--verify` hors réseau contrôle manifeste NON VIDE + dépôt + révision +
  licences + tailles/empreintes + essentiels. Tableformer : `model_artifacts/
  tableformer/{accurate,fast}/{…safetensors,tm_config.json}` ; Heron :
  README/config/model.safetensors/preprocessor_config (inventaires revérifiés).
- `check-models` (hors réseau) : manifestes des 4 modèles PUIS trois sondes
  réelles SÉQUENTIELLES — E5 (384), reparcours cross-encoder (empreinte FIXE
  du contrat), Docling 2 pages avec tableau — chacune dans son processus
  (jamais de chargement simultané), rc non nul au moindre écart.
- Sonde de build bornée : `--network none --memory 2500m --cpus 2 --read-only
  --tmpfs /tmp:size=768m --cap-drop ALL` (aussi documentée §2 bis d'operations.md).
- Testabilité : aucun test n'existait pour ces scripts → `tests/delivery/`
  (socle jetable, doubles `docker`/`git` pilotés par un état JSON,
  clé/cert TLS éphémères pour `--self-check`).

## Tests — NON EXÉCUTÉS (commandes exactes pour Astra)

```bash
cd /home/tetrax/workspace/wallia
python3 -m pytest tests/delivery -q          # aucun Docker réel, aucun réseau, aucun root
python3 -m compileall -q scripts             # les 2 helpers Python (déjà validés par lint à l'écriture)
```

Séquence réelle proposée (Astra seul, après relecture) :

```bash
scripts/build.sh --deliver              # arbre propre + HEAD sur origin ; sonde check-models incluse (bornée)
scripts/deploy.sh --env-file runtime/secrets/app.production.env   # bootstrap explicite si runtime existant sans état
scripts/backup.sh                       # puis :
scripts/restore.sh --isolated runtime/backups/wallia-bundle-<stamp>
scripts/rollback.sh                     # retour par le snapshot précédent uniquement
# TLS en 3 phases sous verrou infra : deployment/README.md §0-1
```

## Limites et vérifications restantes

- Aucune exécution dans ce lot : les doubles simulent Docker/git ; `tar`/`gzip`/`openssl`
  réels sont requis pour les tests ; `check-models` réel exige l'image construite (RAM, hors réseau).
- `delivery_env_check` : la tolérance « services à profils » suit le Compose réel
  (`fake-upstream` sous `testtools`) ; à confirmer sur un vrai `docker compose config
  --format json` (champ `name` + `profiles` présents).
- Harnais d'acceptance sur le Compose de base : l'evidence atterrit désormais sous
  `runtime/data/evidence` (exclu du manifeste de sauvegarde ; `runtime/data` chown 1002
  par `init.sh`) — à confirmer au lot harnais natif ; `init.sh` crée encore
  `runtime/evidence/container` (inoffensif, hors périmètre).
- `tests/delivery` n'est pas câblé dans la CI (`quality.yml` hors périmètre) —
  décision à prendre par le principal.
- Lock Python / formats HF dans la vraie image, RAM réelle des sondes et de l'API,
  HTTPS natif et recette native : restent à mesurer/exécuter par le principal.
