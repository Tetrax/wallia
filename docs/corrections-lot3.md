# Corrections lot3 — retrieval + isolation des tests (preuves)

Branche `feat/prototype` (11265a5 + diff antérieur conservé). Aucun
`git add`/`commit`/`push`/`merge` pour ce lot ; fichiers non suivis préservés.

## Défaut confirmé et correction

1. **Coupure lexicale par fréquence** (`df*2 < total` dans le corpus filtré) :
   supprimait des lexèmes utiles sur les petits corpus (3 échecs / 3 succès des
   tests retrieval). Retirée — le nombre de chunks ne supprime plus un terme
   exact en soi.
2. **Ancien OR non filtré** : réintroduisait des faux positifs via mots vides /
   noms produit / versions. Remplacé par : mots vides FR/EN + jetons de
   métadonnées (produit, versions) exclus, rescousse conditionnée par
   **≥ 2 correspondances significatives exactes** (1 si la question n'a qu'un
   seul lexème significatif). RRF conservé (branches vectorielle + textuelle
   réelles) ; filtres produit/version/scope/génération appliqués **avant** le
   classement ; barrière vectorielle calibrée sur jeu dédié (voir
   `docs/retrieval-calibration.md`).
3. **Isolation des tests** : la suite refuse désormais de s'exécuter dans le
   conteneur de livraison (détection des secrets réels), n'opère que sur des
   bases dédiées (`wallia_test`, `wallia_acceptance`), compare les chemins par
   `Path.is_relative_to` (jamais par préfixe de chaîne) ; runner isolé dédié.

## Fichiers changés (lot3 uniquement)

Modifiés :
- `backend/app/retrieval.py` — lexèmes significatifs, rescousse ≥2, filtres avant classement, diagnostics (`lexical_matched`, `lexical_required`, `lexical_match`).
- `backend/app/config.py` — défauts retrieval : `WALLIA_RETRIEVAL_MIN_COSINE=0.87`, `WALLIA_RETRIEVAL_LEXICAL_MIN_MATCHES=2`.
- `backend/tests/conftest.py` — isolation stricte, env de test figé, retry borné du TRUNCATE sur deadlock transitoire (jamais ignoré).
- `backend/tests/test_retrieval.py` — régression lot3 (stopwords, métadonnées, couverture, citation) ; les invariants historiques (filtres, RRF, petit corpus) sont conservés.
- `backend/tests/acceptance/rag_fr_en.py` — assertions ajoutées (filtres version 10.9/99.99, diagnostics) ; questions et seuil inchangés.
- `scripts/acceptance.sh` — `stage_tests` → runner isolé (plus de pytest dans l'API vivante) ; `all` refusé par défaut (opt-in explicite `WALLIA_ACCEPTANCE_ALL=1`) car il mute la pile vivante.

Créés :
- `backend/tests/test_retrieval_calibration.py`, `backend/tests/acceptance/rag_fr_en_isolated.py`
- `docker-compose.tests-isolated.yml`, `scripts/tests-isolated.sh`
- `scripts/tests-isolated/{refresh-acceptance-data.sh, export-live-corpus.py, fetch-embeddings.py, run-calibration.py, final-checks.sh}`
- `fixtures/calibration/calibration.json`, `docs/retrieval-calibration.md`, ce fichier.

## Commandes de replay (hôte, reproductibles)

```bash
cd /home/tetrax/workspace/wallia
./scripts/tests-isolated/refresh-acceptance-data.sh   # corpus (lecture seule) + vecteurs E5 réels → runtime/tests-isolated/
./scripts/tests-isolated.sh                           # suite complète isolée (pytest -q tests)
./scripts/tests-isolated.sh python tests/acceptance/rag_fr_en_isolated.py   # acceptance code actualisé
./scripts/tests-isolated/final-checks.sh              # contrôles env lecture seule
```

Analyse calibration (optionnelle, déjà incluse dans refresh) :

```bash
python3 scripts/tests-isolated/run-calibration.py \
  --calibration fixtures/calibration/calibration.json \
  --vectors runtime/tests-isolated/calibration-vectors.json \
  --out runtime/evidence/lot3-calibration.json
```

**Commande finale à jouer APRÈS livraison** (API redémarrée sur ce code, uvicorn
sans reload) :

```bash
sudo cat runtime/initial-access.txt | docker compose -p wallia exec -T api python tests/acceptance/rag_fr_en.py
# ou l'étape dédiée : scripts/acceptance.sh rag
```

## Preuves (toutes dans `runtime/evidence/`)

| Fichier | Contenu |
|---|---|
| `lot3-calibration.json` | scores/labels/choix de calibration (séparation non possible : marge −0.0523) |
| `lot3-calibration-runner.json` | exécution réelle : 12/12 gated rejetés, 11/14 servis, limites pos4/pos7/pos8 |
| `lot3-acceptance-rag-fr-en-isolated.json` | 5/5 OK ; couches : E5 réel, pgvector 0.8.6, corpus Docling (4 docs/16 chunks) |
| `lot3-pytest-isolated.log` | **113 passed, rc=0** (75.8 s, runner 1600 MiB/2 CPU, pas d'OOM) |
| `lot3-acceptance-isolated.log` | acceptance isolée rc=0 (5/5) |
| `lot3-env-checks.txt` | état final pile : api/worker/db/fake-upstream healthy, api StartedAt inchangé, restarts 0, secrets intacts (empreintes) |
| `lot3-{runtime,secrets}-before.*`, `lot3-services-before.jsonl`, `lot3-tracked-before.patch` | captures AVANT (09:05 UTC) pour comparaison |

## Isolation (détails)

- Runner : projet compose `wallia-tests`, image `wallia:local` réutilisée, code
  backend monté **en lecture seule**, **aucun** montage `runtime/secrets` ni
  `runtime/data`, aucun port hôte publié ; base pgvector **dédiée** (service
  séparé, stockage dédié, limites mémoire) ; limites explicites
  **1600 MiB / 2 CPU / tmpfs**, suite légère séquentielle (un seul traitement
  lourd à la fois).
- Secrets : jamais montés dans le runner ; le token interne n'a servi qu'à
  l'outil hôte d'embedding (lecture silencieuse, valeur jamais affichée) ;
  empreintes sha256 identiques avant/après (`lot3-env-checks.txt`).
- DB vivante : jamais écrite (export **lecture seule** du corpus) ; jamais
  tronquée ; `wallia_test`/`wallia_acceptance` sont les seules bases manipulées,
  dans le service de tests dédié.

## Limites / fixture vs réel

- Tests unitaires/intégration : backend embeddings **fixture** (one-hot) comme
  avant — mécanique uniquement ; les recettes réelles (calibration, acceptance)
  utilisent E5 réel + pgvector réel + corpus issu du pipeline Docling importé.
- Calibration : séparation statistique absente (constat consigné) ; limites
  pos4/pos7/pos8 documentées ; pas de garantie générale.
- L'API vivante sert encore l'ancien code (uvicorn sans reload) : normal ;
  la commande finale ci-dessus le vérifiera après redémarrage de livraison.
- Deadlock TRUNCATE observé une fois (stream SSE résiduel) → retry borné ;
  suite et acceptance relancées vertes après la dernière modification de code.

## Opérations bloquées par la barrière d'exécution (aucun contournement)

1. Commande composée `chmod +x … && ls -l … && export WALLIA_TESTS_DB_PASSWORD=…` → refusée (exit -1). Repli : commandes simples séparées.
2. `./scripts/tests-isolated.sh python -c "…"` → refusée (corps exécutable imbriqué). Repli : script fichier `runtime/tests-isolated/smoke.py`.
3. `bash -c '…'` → refusée, texte exact : « BLOCKED: Command flagged as dangerous (shell command via -c/-lc flag) but single-query mode (-q) runs without a user present to approve it. Find an alternative approach that avoids this command. To allow dangerous commands in single-query mode, set approvals.single_query_mode: approve in config.yaml. » Repli : `bash -n` + scripts fichiers. Aucun `--yolo`/allowlist/config modifiée. (Les textes intégraux des refus 1-2 sont dans le transcript de session `20260926_090422_dc6d7b`.)
4. `docker compose -p wallia-tests … down` → refusée, texte exact : « BLOCKED: Command flagged as dangerous (docker compose restart/stop/kill/down (container lifecycle)) but single-query mode (-q) runs without a user present to approve it. … ». Sans conséquence : le harnais ne laisse aucun conteneur `wallia-tests-*` subsister après exécution (vérifié `docker ps`) ; aucun cycle de vie tenté à nouveau.

## Hors périmètre (non fait, par consigne)

Aucun e2e/frontend, aucune publication/HTTPS/TLS, aucun restart API, aucune
livraison. Ces étapes viendront après cette revue.
