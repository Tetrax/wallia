# Wallia — manuel d'exploitation (prototype)

Application : assistant de support technique privé, francophone, non officiel
WALLIX. Corpus de démonstration **entièrement fictif**. Aucune procédure
constructeur réelle n'est embarquée ; ne pas utiliser les réponses comme
documentation officielle.

## 1. Composants et exposition

| Service | Image | Exposition |
|---|---|---|
| `db` | `pgvector/pgvector:pg17` | aucun port hôte, volume `wallia_pgdata` |
| `api` | image Wallia (frontend statique inclus) | `127.0.0.1:13745` uniquement |
| `worker` | même image, `python -m app.worker` | aucun port |
| `fake-upstream` | même image, profil `testtools` | réseau compose uniquement (tests) |

Réseau dédié `wallia_net` (172.31.245.0/24). Aucun accès à Hermes, Docker socket
ou SSH depuis les conteneurs (`read_only`, `cap_drop: ALL`, `no-new-privileges`).

## 2. Cycle de vie

```bash
scripts/build.sh --test          # construit wallia:test-<sha> (+ wallia:local), journal runtime/evidence/
scripts/init.sh                  # dossiers, secrets, migrations, premier admin (aucun affichage)
scripts/dev_up.sh                # db + api + worker, attend /healthz
scripts/smoke.sh                 # smoke HTTP authentifié (identifiants lus, jamais affichés)
scripts/acceptance.sh all        # tests, frontend, corpus réel, recherche, streaming, Playwright
scripts/down.sh                  # arrêt ; --purge pour supprimer aussi les volumes
```

`scripts/build.sh --deliver` et `scripts/deploy.sh` refusent un arbre sale ou un
HEAD non poussé (vérification EN LIGNE) : c'est le mode livraison, toujours sur
le HEAD courant — aucune sélection de SHA arbitraire (le retour vers un état
ancien passe UNIQUEMENT par `scripts/rollback.sh`). Le déploiement contrôle
l'environnement Compose EFFECTIF (rendu, variables héritées incluses), exige la
santé `healthy` de db/api/worker et enregistre l'état précédent (voir §12).

## 2 bis. Modèles embarqués (vérification hors réseau)

```bash
docker run --rm --network none \
  --memory 2500m --cpus 2 --read-only --tmpfs /tmp:size=768m --cap-drop ALL \
  --entrypoint python wallia:<sha> -m app.cli check-models
```

`check-models` vérifie les manifestes (fichiers/tailles/empreintes, identité et
révision attendues) puis exécute SÉQUENTIELLEMENT trois sondes réelles — E5,
reclassement, Docling (2 pages avec tableau) — chacune dans son processus :
les modèles ne sont jamais chargés simultanément. Un échec — y compris un
DÉPASSEMENT DE DÉLAI d'une sonde, rapporté explicitement en résultat non-ok
(jamais une traceback à interpréter) — rend l'image non conforme (le build
`--deliver` l'exécute automatiquement). Les manifestes exigent les fichiers
ESSENTIELS de chaque modèle (alignés sur `scripts/fetch_models.py`), des
chemins relatifs sûrs (ni `../`, ni absolu, ni cache) et l'identité, la
révision et la licence attendues.

## 3. Secrets et configuration

- `runtime/secrets/` (uid 1002, 0600/0750) : `db_password`, `session_secret`,
  `worker_token`, `provider_api_key`, `app.env` (configuration non secrète).
- `runtime/initial-access.txt` (0600, propriété tetrax si possible) : URL locale,
  e-mail et mot de passe du premier administrateur. Jamais recopié dans Git, un
  log ou une réponse HTTP.
- Clé fournisseur : write-only via l'interface (ou dépôt de fichier par
  l'opérateur). Jamais relue par l'API, jamais affichée ; champ vide = conservation.
- Aucune clé Hermes, aucun credential d'abonnement de développement n'est monté.

## 4. Authentification et sécurité applicative

- Argon2id ; sessions opaques hachées en base, expiration, révocation à la
  déconnexion et au changement de mot de passe.
- Cookie `HttpOnly`, `SameSite=Lax` ; `Secure` en production
  (`WALLIA_COOKIE_SECURE=1`).
- CSRF : jeton synchronisé renvoyé au login et par `/api/auth/me`, vérifié en
  temps constant sur toute mutation ; `Origin` strict y compris au login.
- Rate-limit login durable par IP fiable (`WALLIA_TRUSTED_PROXY_CIDRS`) + limite
  globale API ; `Retry-After` renvoyé en 429.
- En-têtes de sécurité (CSP `self`, nosniff, frame-ancestors none) posés par
  l'API ; téléchargements avec `Content-Disposition` et `nosniff`.
- `/internal/*` exige le jeton worker et n'est jamais exposé par Nginx.

## 5. Corpus documentaire

- Import administrateur (PDF ≤ 20 Mio, ≤ 100 pages), métadonnées obligatoires :
  titre, origine, produit, versions explicites, langue, périmètre.
- `scope = demo` (fictif, badge « démo non officiel ») vs `scope = official`
  (document réel fourni par l'opérateur). Un même contenu ne peut exister dans
  les deux périmètres (checksum + périmètre).
- Réindexation atomique : nouvelle génération publiée en cas de succès,
  ancienne conservée en cas d'échec. Suppression : passages et jobs supprimés,
  fichier supprimé, les citations historiques ne sont jamais remplacées.
- Les scans (PDF image) ne sont pas couverts : `do_ocr=False`. Un PDF sans texte
  exploitable échoue proprement (`aucun contenu exploitable extrait`).

## 6. Jobs d'ingestion

- File SQL durable : `queued/running/succeeded(done)/failed/cancelled`,
  `attempts ≤ 3`, `available_at` (backoff borné), lease renouvelé.
- Un seul job documentaire simultané (verrou d'instance) ; les requêtes
  utilisateur restent servies pendant l'ingestion (vidage SQL `SKIP LOCKED`).
- Reprise au démarrage : jobs expirés requeutés (ou échoués au-delà des
  tentatives), messages restés `streaming` marqués `interrupted`.
- Diagnostic : Bibliothèque → « Jobs d'ingestion » (progression, erreur sûre,
  relance manuelle bornée) ; journaux d'extraction conservés dans
  `runtime/data/ingestion/<document_id>/gen<N>/docling_stderr.log`.

## 7. Recherche et citations

- Embeddings `intfloat/multilingual-e5-small` (révision épinglée, 384 dimensions,
  CPU, préfixes `query:`/`passage:`, normalisation L2).
- Recherche hybride : cosinus pgvector + plein texte PostgreSQL, fusion RRF ;
  filtres produit/version/périmètre appliqués **avant** le classement.
- Une version demandée n'est jamais remplacée ; version inconnue → « aucune
  source pertinente ». Barrière de pertinence ajustable dans Administration.
- Chaque citation expose document, page réelle, extrait exact et scores ; le PDF
  source s'ouvre authentifié à la page voulue quand il est encore présent.

## 8. Fournisseur de génération

- Par défaut `https://api.deepseek.com/v1`, `deepseek-flash` (OpenAI-compatible).
- Sans clé : mode démonstration honnête (aucun texte inventé ; recherche réelle,
  passages listés). Aucune panne générale.
- Domaine autorisé explicite, HTTPS exigé hors test, redirections refusées,
  erreurs assainies (jamais la clé ni le corps complet du fournisseur).
- Streaming SSE : `meta`, `sources`, `status`, `delta`, `done`, `error`.
  Arrêt via bouton (persiste `cancelled`) ou fermeture cliente (détectée côté
  serveur, appel upstream coupé). Une génération à la fois par conversation.

## 9. Web et vision

- **Vision : inactive** (`WALLIA_VISION_ENABLED=0`). Les images sont stockées et
  prévisualisées, jamais analysées ; l'interface l'annonce explicitement.
- **Web : inactive** (`WALLIA_WEB_ENABLED=0`). Le code d'intégration est préparé
  (requête anonymisée produit/version, domaine contrôlé) mais ne doit être
  activé qu'après la recette RAG seule, sur décision de l'opérateur.

## 10. Sauvegarde, restauration

```bash
scripts/backup.sh                       # bundle cohérent (DB + fichiers + manifeste), pause brève api/worker
scripts/restore.sh --isolated <bundle>  # recette isolée : cible neuve, base jetable sans port, vérifications complètes
```

**Sauvegarde.** Le runtime sauvegardé est le SNAPSHOT COURANT VALIDÉ
(`runtime/deploy-state/current.json`) ; sans état suivi (runtime historique),
`--env-file <fichier réel>` devient EXPLICITE obligatoire — jamais de repli
implicite vers `runtime/secrets/app.env`. La découverte des services
(`docker compose ps`) et la vérification réelle de l'arrêt sont FATALES en cas
d'échec (une panne n'est jamais « zéro service ») : aucune sauvegarde à chaud.
Seuls api/worker INITIALEMENT actifs sont arrêtés (la base reste active ; le
trap de reprise est armé AVANT l'arrêt et reprend exactement ces services,
même sur échec partiel). Archive, comptes des 8 tables (erreurs SQL fatales,
`ON_ERROR_STOP`), références DB→fichiers et manifeste (SHA256) sont calculés
PENDANT la pause ; le bundle passe les validations EXISTANTES `restore_lib`
(manifeste strict, SHA256 des archives, sûreté tar : liens/absolus refusés) et
une référence DB sans fichier rend la sauvegarde NON valide (code de sortie
non nul AVEC reprise des services). La reprise exige la santé `healthy` de
api+worker et un échec de reprise donne un code de sortie NON NUL. Le répertoire
du bundle est créé SANS écrasement possible (même dans la même seconde).

Bundle sous `runtime/backups/` (répertoire 0700, fichiers 0600) : il ne
contient **aucun secret** (les clés externes — `runtime/secrets/` — doivent
être sauvegardées séparément), mais la base embarque les empreintes de
comptes/sessions et les données applicatives (conversations, pièces jointes,
documents) : **ce n'est PAS un export publiable ni totalement dépourvu
d'information sensible** — à protéger comme les données de production. Aucun
contenu utilisateur n'est imprimé.

**Restauration isolée** (`--isolated`, seule exécution automatisée en V1).
Cible NEUVE garantie (`runtime/restore-isolated/<stamp>-<aléa>`), conteneur
`wallia-restore-<stamp>-<aléa>` : réseau none, aucun port publié, 512 Mo / 1
CPU, données PostgreSQL dans un volume dédié (jamais un tmpfs). Vérifications :
manifeste STRICT (exactement les 8 tables attendues, types entiers, chemins
canoniques sans traversée), SHA256 des archives AVANT tout parsing, erreurs
SQL fatales (`ON_ERROR_STOP`), fichiers manquants/extra/altérés/liens,
références DB→fichiers. **Aucun nettoyage automatique** : conteneur, volume et
cible sont conservés comme preuve et l'opérateur nettoie explicitement
(commandes affichées à la fin). Les sorties de vérification
(`verification/table-counts.txt`, `verification/db-file-refs.txt`) sont
privées et CONSERVÉES sous la cible isolée.

**Restauration EN PLACE — procédure opérateur CONTRÔLÉE** (retirée du script ;
jamais dans la recette de livraison) :

1. sauvegarde fraîche obligatoire : `scripts/backup.sh` (c'est le retour
   arrière) ; noter l'image courante (`runtime/deploy-state/current.json`) ;
2. valider le bundle : `python3 scripts/restore_lib.py validate <bundle>` ;
3. `docker compose -p wallia stop api worker` puis vérifier l'arrêt ;
4. base : `gunzip -c <bundle>/db.sql.gz | docker compose -p wallia exec -T db psql -v ON_ERROR_STOP=1 -U wallia -d wallia -q`;
5. fichiers : extraire vers un répertoire NEUF puis contrôler avant substitution
   (`python3 scripts/restore_lib.py extract <bundle> <neuf>`, `verify-files`,
   `verify-refs` avec les références DB restaurées) ; remplacer `runtime/data`
   en conservant l'ancien au moins jusqu'aux smoke tests ;
6. migrations puis reprise : `docker compose -p wallia run --rm api python -m app.migrate`,
   `docker compose -p wallia up -d api worker`, `scripts/smoke.sh`.

**Migrations ascendantes uniquement** : il n'existe aucune migration
descendante. Après une restauration (isolée ou en place), rejouer
`app.migrate` sur la base restaurée ; si un retour d'image est nécessaire avec
une base déjà migrée par une version incompatible, restaurer d'abord la
sauvegarde correspondante, puis `scripts/rollback.sh`.

## 11. Limites connues du prototype

- Mon-administrateur (pas de gestion multi-comptes dans l'interface, mais
  l'isolation par compte est implémentée et testée).
- Scans/OCR non pris en charge ; pas d'analyse d'image ; web désactivé.
- Les citations de documents supprimés affichent une indisponibilité (fichier
  absent) plutôt que des passages de substitution.
- Nginx/TLS : fichiers préparés dans `deployment/`, application par le principal.
- Le smoke/acceptance suppose la pile démarrée localement (`dev_up.sh`).

## 12. Déploiement, état et retour arrière

- `scripts/build.sh --deliver` : arbre propre + HEAD réellement présent sur
  origin (en ligne) ; image `wallia:<sha complet>` (label OCI ET variable
  embarquée `WALLIA_GIT_SHA` concordants) ; sonde modèles bornée (2500 Mo,
  2 CPU, tmpfs borné, lecture seule, caps abandonnées) hors réseau. Les tags
  de test (`wallia:test-*`, `wallia:local`) ne peuvent jamais usurper le SHA
  livré.
- `scripts/deploy.sh --env-file <env-production>` : contrôle STRICT de
  l'environnement Compose EFFECTIF (production, cookie secure, origine exacte,
  endpoint natif HTTPS, aucun backend de test — fichier ET variables héritées,
  jamais un `grep` de fichier) ; image par SHA complet, ID immuable résolu UNE
  fois ; snapshot Compose RENDU (JSON auto-contenu) référençant cet ID, produit
  et vérifié AVANT up/migrate, puis utilisé pour TOUTES les mutations ;
  démarrage db/api/worker du projet `wallia` uniquement ; santé `healthy`
  OBLIGATOIRE des trois services et image EFFECTIVE d'api/worker vérifiée
  après démarrage ; aucun remove-orphans.
- État de déploiement : `runtime/deploy-state/` (0700, fichiers 0600) —
  `current.json` (image, image ID immuable `sha256:…`, snapshot Compose RENDU,
  fichier d'environnement) ; écritures ATOMIQUES (temporaire + rename).
- Première transition depuis un runtime Wallia existant sans état suivi :
  `--bootstrap-previous-env` est RETIRÉE — un déploiement sur un runtime
  existant SANS `current.json` valide est REFUSÉ avant toute mutation (la
  capture automatique d'un conteneur arbitraire ne serait pas un rollback
  fidèle : anciens mounts/env perdus). Le principal prépare et VÉRIFIE
  manuellement un snapshot fidèle de la configuration réellement active
  (image ID effectif `docker inspect`, Compose rendu, env), l'installe comme
  `current.json` puis déploie. Première installation réellement vide :
  `previous=null`, rollback explicitement impossible. Jamais d'état fabriqué.
- `scripts/rollback.sh` : rejoue UNIQUEMENT le snapshot précédent (Compose
  rendu JSON + env + image ID immuable), échoue AVANT toute mutation si l'état
  est absent/invalide OU si le snapshot ne RÉFÉRENCE PAS l'ID immuable attendu
  pour api/worker (jamais une autre image ni un tag mutable « parce que l'ID
  existe »), exige `healthy`, vérifie l'image effective d'api/worker, puis
  actualise `current.json` (env/mounts du snapshot conservés) et consomme
  l'état précédent.
- TLS : trois phases explicites (HTTP ACME → paire LE RÉELLEMENT SERVIE →
  paire gérée + vhost final) via `deployment/wallia-tls-activate.sh`, modèles
  root-owned sous `/etc/wallia/tls-templates` installés par l'opérateur (voir
  `deployment/README.md`) ; bascule atomique du lien `active` ; hook de
  renouvellement no-op hors lignée Wallia, sans reprise du verrou infra.
