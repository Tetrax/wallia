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
HEAD non poussé : c'est le mode livraison. Le mode test local est explicite.

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
scripts/backup.sh                                  # dump SQL.gz + données + SHA-256
scripts/restore.sh --isolated runtime/backups/wallia-db-<stamp>.sql.gz   # test jetable
scripts/restore.sh --inplace  runtime/backups/wallia-db-<stamp>.sql.gz --yes
```

Les sauvegardes ne contiennent pas les secrets (à sauvegarder séparément).

## 11. Limites connues du prototype

- Mon-administrateur (pas de gestion multi-comptes dans l'interface, mais
  l'isolation par compte est implémentée et testée).
- Scans/OCR non pris en charge ; pas d'analyse d'image ; web désactivé.
- Les citations de documents supprimés affichent une indisponibilité (fichier
  absent) plutôt que des passages de substitution.
- Nginx/TLS : fichiers préparés dans `deployment/`, application par le principal.
- Le smoke/acceptance suppose la pile démarrée localement (`dev_up.sh`).
