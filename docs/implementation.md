# Wallia — dossier d'implémentation (lot initial)

Auteur : implémenteur unique (DeepSeek V4.1 Flash via OpenCode Go, effort max),
session `20260925_212925_bacc3f`. Contrats de référence : `docs/architecture.md`
(Astra). Ce document décrit ce qui a réellement été construit, les décisions
prises, les écarts constatés et les preuves d'exécution.

## 1. Structure livrée

```
backend/            API FastAPI + worker + migrations SQL + tests
  app/              modules applicatifs (voir carte ci-dessous)
  migrations/       0001_init.sql (pgvector, tables, index HNSW + plein texte)
  tests/            pytest (DB pgvector réelle) + smoke HTTP + acceptance + faux fournisseur
frontend/           React 18 + TypeScript + Vite, thème sombre FR, build dans l'image
fixtures/           générateur de PDF fictifs (FR/EN, tableaux, versions, piège d'injection)
resources/          4 méthodes métier versionnées (qualification, logs, sources, escalade)
scripts/            lib, init, build, deploy, dev_up, down, smoke, backup, restore, acceptance
deployment/         vhost Nginx modèle + hook TLS (application par le principal)
tests/e2e/          Playwright 1.63.0 (desktop + mobile), captures et contrôles console/débordement
.github/workflows/  check requis « quality » (DB pgvector, aucun appel payant)
docs/               operations.md, implementation.md (acceptance/state/architecture = Astra)
Dockerfile          image unique API/worker + frontend, CPU uniquement, read-only en runtime
docker-compose.yml  projet « wallia », API 127.0.0.1:13745, réseau 172.31.245.0/24
```

Carte des modules backend :

| Module | Rôle |
|---|---|
| `config.py` | configuration par variables d'environnement, secrets par fichiers, URL DB construite à la demande |
| `db.py` / `models.py` / `migrate.py` | SQLAlchemy 2, UUID opaques, migrations SQL versionnées avec checksum SHA-256 |
| `security.py` | Argon2id, sessions opaques hachées, CSRF, rate-limit (jeton + durable), IP fiable, Origin strict |
| `deps.py` | dépendances d'authentification (utilisateur, admin, garde CSRF) |
| `chunking.py` | passages depuis items Docling, fenêtre du tokenizer E5, pages réelles, tableaux intacts |
| `docling_runner.py` | extraction Docling en sous-processus borné (CPU, do_ocr=False, tableaux), sortie JSON+Markdown |
| `embeddings.py` | E5 `intfloat/multilingual-e5-small` (révision épinglée) + backend `fixture` réservé aux tests |
| `retrieval.py` | recherche hybride SQL (cosinus + plein texte, fusion RRF), filtres appliqués avant classement |
| `prompts.py` | prompt système, méthodes versionnées, encadrement `<donnees_non_fiables>`, détection d'injection |
| `llm.py` | client OpenAI-compatible (SSE, annulation, erreurs assainies, validation d'endpoint/domaines) |
| `jobs.py` / `worker.py` | file durable SQL, leases, reprises, un job documentaire à la fois, publication par génération |
| `app_settings.py` | réglages persistés (fournisseur, barrière de pertinence), heartbeat worker |
| `routers/` | auth, conversations, chat (SSE), attachments, documents+jobs, search, settings, internal, status, web |

## 2. Décisions d'implémentation

1. **Une seule image** API/worker (frontend statique intégré) : moins de surface,
   même code pour les deux rôles, `read_only`, `cap_drop: ALL`,
   `no-new-privileges`, tmpfs.
2. **Worker et modèle unique** : l'API expose `/internal/embeddings` (jeton
   worker) ; le worker appelle cet endpoint, ce qui évite deux copies du modèle
   en mémoire — conforme à `architecture.md` et adapté aux 4,2 Gio disponibles.
3. **Migrations SQL maison** avec checksum : pas de dépendance Alembic,
   application idempotente et transactionnelle, dérive détectée.
4. **Publication par générations** : la réindexation insère une nouvelle
   génération puis bascule `current_generation` ; l'ancienne n'est supprimée
   qu'après succès. La recherche ne lit que la génération courante.
5. **Barrière de pertinence mesurée** (cosinus minimal + secours lexical) au
   lieu d'un seuil naïf : les scores E5 se concentrent vers 0,7–1. Valeur par
   défaut 0,84, réglable dans l'administration, diagnostics exposés par l'API.
6. **Arrêt et déconnexion** : bouton stop (colonne `stop_requested`, sondage
   borné) et fermeture cliente (GeneratorExit) produisent tous deux un message
   `cancelled` persisté ; l'appel upstream est coupé dans les deux cas.
7. **Recovery au démarrage** : jobs expirés requeutés ou échoués, messages
   `streaming` marqués `interrupted` (jamais laissés « en cours » à vie).
8. **Secrets par fichiers** uniquement (`runtime/secrets/`), jamais en ligne de
   commande ni dans un log ; la clé fournisseur est write-only.
9. **Tests** : DB PostgreSQL + pgvector réelles, serveur uvicorn réel (SSE et
   déconnexions vraies), faux fournisseur local pour streaming/stop/erreurs ;
   backend `fixture` d'embeddings explicitement interdit en production.
10. **Corpus fictif** marqué « non officiel » sur chaque page des PDF et par un
    badge permanent dans l'interface.

## 3. Écarts et points de vigilance

| Point | Architecture | Livré | Raison |
|---|---|---|---|
| Dossier de déploiement | `deploy/` | `deployment/` | nom demandé par le mandat ; contenu identique |
| `AGENTS.md` | fichier de règles racine | **non versionné** (blocage runtime) | l'écriture du fichier est refusée par une protection du runtime en session non assistée ; contournement interdit par le mandat — contenu proposé en annexe A |
| Reprise de jobs | reprise des jobs abandonnés | ✅ `recover_stale_jobs` + `recover_stale_streams` au démarrage et toutes les 60 s | — |
| Historique de citations après suppression d'un document | « explicitement indisponibles » | l'API renvoie 404 à l'ouverture du PDF supprimé ; les passages historiques restent affichés tels quels (jamais remplacés) | compromis prototype, à durcir si l'opérateur le juge nécessaire |
| Web Search | après recette RAG seule | code présent, **désactivé** (`WALLIA_WEB_ENABLED=0`) | décision du mandat |
| Vision | inactive | **désactivée**, mention explicite dans l'interface | décision du mandat |
| Nginx/TLS | application cible | fichiers modèles dans `deployment/` | interdiction de modifier `/etc` |

## 4. Résultats d'exécution

Constats réels, mesurés dans cette session (journaux dans `runtime/evidence/`, aucun
résultat inventé ; les runs intermédiaires en échec sont conservés : pytest-run1..10).

| Élément | Résultat | Preuve |
|---|---|---|
| Frontend | `tsc --noEmit && vite build` vert (exit 0) | `npm run build` (frontend/) |
| Build image | `wallia:test-c11842fff201` = `wallia:local`, 12,9 Go, CPU only | `runtime/evidence/build-*.log` |
| Bootstrap | secrets générés, base migrée (0001, puis 0002), admin créé, `runtime/initial-access.txt` 0600 tetrax | `scripts/init.sh` |
| Pile locale | db + api (127.0.0.1:13745) + worker, tous `healthy` | `docker compose ps` |
| Smoke HTTP | 10/10 PASS (santé, login, session, état, conversation, SSE, admin doc, recherche, logout, révocation) | `scripts/smoke.sh` |
| Tests backend | **65 passés, 0 échec** (PostgreSQL 17 + pgvector réels, serveur HTTP réel, faux fournisseur SSE) | `runtime/evidence/pytest-run10.log` |
| Migrations | 0001 puis 0002 appliquées, idempotence + détection de checksum altéré testées | `tests/test_migrations_schema.py` |
| Ingestion Docling réelle + recherche E5 | **non exécutée dans ce lot** (étapes `acceptance.sh corpus` / `rag` à jouer — voir §6) | — |

### 4.1 Défauts réels trouvés par les tests et corrigés

1. `str(URL)` masque le mot de passe SQLAlchemy → connexion de test refusée en masse ;
   corrigé par `render_as_string(hide_password=False)` (tests uniquement).
2. `GET /api/documents` renvoyait l'objet ORM au lieu de l'identifiant vers `_last_job` → 500 ;
   corrigé (`d.id`).
3. Dérive schéma/code : la file d'ingestion écrivait `updated_at` absent de
   `ingestion_jobs` → échecs `claim/renew/finish` en boucle côté worker ; corrigé par la
   migration **0002** (jamais en modifiant 0001 déjà appliquée) + alignement du modèle.
4. PDF illisible en pièce jointe → 500 ; désormais 415 explicite (« PDF illisible »).
5. `chunking` : un titre/`section_header` ne nommait pas la section des passages suivants ;
   corrigé (`HEADER_LABELS`).
6. `retrieval` : quand la barrière était franchie par un bon candidat, des candidats
   faibles étaient quand même servis en remplissage ; corrigé — seuls les candidats
   réellement pertinents (lexical exact ou cosine ≥ barrière) sont cités.
7. Pièce jointe/section vide, champ `case_state` non canonique à la création d'une
   conversation (réponse partielle) → corrigé (`normalize_case_state({})`).
8. **Sous-processus Docling** : lancé avec `cwd=<dossier de travail>`, il ne trouvait
   plus le paquet `app` (`ModuleNotFoundError: No module named 'app'`) → **les 4 premiers
   jobs ont échoué en réel**. Invisible en test (le parseur est remplacé par une sortie
   contrôlée) : c'est exactement ce qu'apporte la recette réelle. Corrigé par un
   `PYTHONPATH` explicite dans l'environnement du sous-processus. Après correction :
   4 documents `ready`, 4 à 6 passages chacun (voir §4.3).
9. Scripts d'acceptance : `python tests/acceptance/x.py` (script) ne trouvait pas le paquet
   `tests` → exécution en module (`python -m tests.acceptance.x`) ; `credentials()`
   relisait stdin après le premier appel (identifiants « introuvables ») → lecture
   désormais idempotente ; statut de job « succeeded » non compté comme terminé.

### 4.3 Recette réelle du corpus (Docling + E5, image livrée)

| Étape | Résultat réel |
|---|---|
| Génération du corpus fictif | 4 PDF (FR ×3, EN ×1), reproductibles octet pour octet (`rl_config.invariant`) |
| Import API + file durable | 4 jobs créés, exécutés par le worker, déduplication (même empreinte → 409) |
| Extraction Docling réelle | OK par document : 10.9 FR = 4 passages, 10.10 FR = 6, Quick Start EN = 5, piège d'injection = 1 |
| Statuts finaux | 4 documents `ready`, `current_generation` ≥ 1, 0 job en échec |
| Preuves machine | `runtime/evidence/container/acceptance-ingestion.json` (+ `acceptance-recherche.json`, `acceptance-streaming.json`) |

### 4.3.1 Recherche réelle (embeddings E5, filtres, encadrement) — 8 mesures, 8 OK

| Mesure | Résultat réel |
|---|---|
| FR → FR avec filtre version 10.10 | `ok`, sources uniquement 10.10, pages + extraits présents |
| Requête **anglaise** → documents EN | `ok`, doc EN retrouvé (multilingue réel) |
| Filtre version 10.9 | `ok`, aucune source 10.10 |
| **Version inconnue 9.9** | `no_relevant_source` — jamais substituée par 10.9/10.10 |
| Sujet hors corpus | `no_relevant_source` |
| Document piège retrouvé | `ok` ; bloc de données non fiables encadré `<donnees_non_fiables>` **et** la tentative d'instruction signalée au modèle |
| Dialogue, état de cas version 10.10 | `ok`, sources 10.10 uniquement |
| Dialogue, état de cas version 10.9 | `ok`, sources 10.9 uniquement (jamais 10.10) |

### 4.3.2 Streaming réel (faux fournisseur OpenAI-compatible local) — 5 scénarios, 5 OK

| Scénario | Résultat réel |
|---|---|
| Configuration fournisseur (clé en écriture seule) | 200, `key_configured: true`, aucun secret affiché |
| Streaming nominal | deltas reçus, trame `done` `complete` |
| Arrêt utilisateur en pleine génération | message serveur `cancelled` |
| Erreur fournisseur puis relance | erreur remontée (`retryable`), relance `complete` |
| Effacement de la clé → mode démonstration | `key_configured: false`, réponse honnête en mode démo |

Preuves : `runtime/evidence/container/acceptance-recherche.json`, `acceptance-streaming.json`.

### 4.4 Défauts d'outillage corrigés

- Healthcheck du worker : il testait un port HTTP inexistant (hérité de l'image) ;
  remplacé par un contrôle réel (processus `/proc/1/cmdline` **et** connexion PostgreSQL).
  Le worker est désormais `healthy`.
- Limite mémoire de l'API paramétrable (`WALLIA_API_MEM_LIMIT`) : la suite de tests démarre
  un serveur HTTP supplémentaire dans le conteneur ; un OOM cgroup tuait le serveur de test
  (limite 1200 Mo). L'acceptance relève la limite à 2500 Mo ; la production garde 1200 Mo.
- Override de test local `docker-compose.tests.yml` (code monté) — **jamais** utilisé pour la
  validation finale ; la validation tourne sur l'image.

### 4.5 Limites connues assumées (à trancher par le principal)

- Sur **déconnexion cliente**, l'état serveur passe bien à `cancelled` (vérifié), mais la
  fermeture immédiate du flux amont relève du ramasse-miettes du générateur : non garantie
  au niveau transport (best effort).
- `GET /api/documents/{id}/original` est ouvert à **tout utilisateur authentifié** (les
  extraits cités lui sont déjà visibles) ; la gestion du corpus reste admin. Décision à
  confirmer en revue.
- La suppression d'un document supprime ses jobs (cascade) : plus d'historique de job après
  suppression (choix, pas oubli).
- Image de 12,9 Go (torch CPU + Docling + modèles) : un jeu d'images ou une slim est possible
  si le poids devient un problème d'exploitation.

## 5. Reproductibilité et commandes

```bash
scripts/build.sh --test                # image CPU reproductible (tag SHA + labels OCI)
python fixtures/generate_corpus.py     # équivalent dans l'image : --out /data/fixtures/corpus
scripts/init.sh && scripts/dev_up.sh && scripts/smoke.sh
scripts/acceptance.sh all              # tests + frontend + corpus + recherche + streaming + e2e
```

## Annexe A — contenu `AGENTS.md` proposé (non écrit : protection runtime)

```markdown
# AGENTS.md — Wallia

- Périmètre : dépôt unique Wallia (API FastAPI + worker + frontend React/TS).
- Contrats : docs/architecture.md est autoritatif ; docs/state.md porte l'état.
- Ne jamais committer runtime/, des secrets, des données d'exécution ou des exports.
- La CI « quality » doit rester verte ; aucun appel payant dans la CI.
- Le corpus de démonstration est fictif : ne jamais le présenter comme officiel.
- Toute modification de sécurité (auth, sessions, CSRF, permissions) exige tests + revue.
- Un seul auteur par fichier ; pas de secrets dans les logs, prompts ou tests.
```
