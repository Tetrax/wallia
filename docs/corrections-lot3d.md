# Corrections lot3d — intégration du reclassement cross-encoder (chemin intégré, reranker réel)

Objet : intégrer MINIMALEMENT le reranker CPU validé (lot3c, `docs/reranker-probe.md`)
au moteur RAG en remplaçant les barrières d'éligibilité cosinus/lexicales, et
prouver le chemin application réel (SQL + E5/pgvector 384 + reclassement réel
+ filtres) ainsi que la coexistence mémoire E5+CE, sans toucher à la pile
vivante. Aucun commit/push. Le lot ne livre PAS la production : l'image et le
déploiement relèvent du lot suivant.

## Décisions appliquées (contrat Astra, non redécidées)

- Chaîne unique : filtres SQL (statut `ready`, génération courante, scope/
  produit/version) AVANT sélection → présélection hybride pgvector384 + plein
  texte (RRF, pool borné 30 par défaut, borne dure `MAX_CANDIDATES=64`) →
  reclassement cross-encoder → top-k 6. Score INDIVIDUEL par passage (aucune
  accumulation inter-passages) ; seule l'appartenance au pool est collective.
- AUCUNE barrière cosinus/lexicale avant reclassement (elles perdaient le
  rappel FR→EN) : vecteur + lexical ne servent plus qu'à SÉLECTIONNER les
  candidats.
- Modèle `cross-encoder/mmarco-mMiniLMv2-L12-H384-v1`, révision
  `1427fd652930e4ba29e8149678df786c240d8825`, poids SHA256
  `5daeca…f359b4` (Apache-2.0), fichier locaux existants — aucun
  téléchargement, aucun nouveau modèle.
- Même tokenizer / `AutoModelForSequenceClassification` que le probe, logits
  bruts, CPU 2 threads, 512 tokens, paires (question, texte), batch 8. Seuil
  GELÉ `RERANK_LOGIT_THRESHOLD = 1.1491` ; tri par logits décroissants avec
  départage déterministe (`chunk_id`). Aucun réglage du seuil exposé (UI/API
  retirés).
- Échec/saturation : statut `retrieval_unavailable`, `sources=[]`, erreur
  typée (`reranker_unavailable|reranker_integrity|reranker_busy|reranker_failure`)
  + message sûr ; jamais de `no_relevant_source` factice, jamais de repli
  silencieux, jamais de web automatique. Sous indisponibilité, la feature
  n'est pas annoncée active (statut/`/api/status` + note de chat).
- Backend `fixture` = mécanique de TEST déclarée (déterministe, sans
  sémantique), refusé si `WALLIA_ENV=production` (comme l'embedding) ; le
  backend `transformers` est le seul réel.

## Fichiers

- `backend/app/reranking.py` (nouveau) : service singleton, cache/reset
  explicite, chargement local protégé (`local_files_only=True`,
  `trust_remote_code=False`), vérification taille+empreinte de chaque fichier
  AVANT chargement, une seule inférence à la fois (verrou, attente bornée 30 s
  → `RerankerBusy`), aucun réseau, aucun GPU, aucun appel fournisseur.
- `backend/app/retrieval.py` : réécrit — chaîne ci-dessus, `score_kind:
  "rerank_logit"`, diagnostics par passage (`cosine`, `lexeme_hits`,
  `text_rank`, `rank_vector/rank_text`, `score_rrf`, `score_rerank`,
  `served`) + bloc `reranker` (backend/modèle/révision/état).
- `backend/app/config.py` : champs `reranker_backend|model|revision|
  weights_sha256|model_dir` (`WALLIA_RERANKER_MODEL_DIR=/opt/models/reranker`
  par défaut) ; garde prod ; suppression des anciens champs de barrière
  (`retrieval_min_cosine`, `…_lexical_*`).
- Propagation stricte : `routers/chat.py` (retrieval réelle + note
  `no_relevant_source`/`retrieval_unavailable` + bloc sources neutre),
  `routers/search.py`, `routers/status.py` (`retrieval.top_k` + bloc
  `reranker`), `app_settings.py` + `routers/settings_api.py` +
  `schemas.py` (retrait `RETRIEVAL_KEY`/`min_cosine` des réglages),
  `main.py` (warmup embeddings + reranker), `routers/settings_api.py`
  conservé en sortie « booléens de secrets uniquement » (`key_configured`).
- Frontend (strictement anti-affichage mensonger) : `types.ts`
  (`score_kind`, statut `retrieval_unavailable`, bloc `reranker`) ;
  `Panels.tsx` (libellé du statut, badge « logit … · cos … · texte … » avec
  title explicite « jamais une probabilité ») ; `Settings.tsx` (carte
  « Recherche hybride » : modèle/révision/état/seuil gelé, plus de knob).
- Tests : `conftest.py` (fixture reranker fixture + reset), `test_reranking.py`
  (nouveau, 8 tests), `test_retrieval.py` (réécrit, 14 tests), `test_retrieval_calibration.py`
  (réécrit, exécution réelle isolée), `test_chat_continuity.py` (2 tests de
  continuité remplacés : chemin reranké + indisponibilité propagée),
  `tests/acceptance/rag_fr_en_isolated.py` (recette intégrée réelle).

## Changement de sensibilité (documenté)

- AVANT : barrière cosinus 0.87 + éligibilité lexicale par passage (≥2
  correspondances) — perdait le rappel multilingue FR→EN et pouvait laisser
  passer des passages « corroborés » sans pertinence réelle.
- APRÈS : sélection des candidats (RRF borné) puis jugement par cross-encoder
  réel (logit ≥ 1.1491). Les protections des anciens tests de barrière ont été
  CONVERTIES en invariants reranker (faux passage entraîné par accumulation
  interdit, indépendance par passage, filtres avant sélection, pool borné,
  saturation bornée, statuts distincts) — pas effacées pour afficher du vert.
  Bilan des tests : réécrits — `test_retrieval.py` (14 tests reranker à la
  place des tests de barrière), `test_retrieval_calibration.py` (1 test),
  `test_chat_continuity.py` (2 tests remplacés) ; ajoutés — 8 tests
  (`test_reranking.py`).

## Preuves exécutées (rc réels)

1. Suite backend isolée complète : `./scripts/tests-isolated.sh` →
   **128 passed, rc 0** (`runtime/evidence/tests-isolated-20260926T105457Z.log`) ;
   ciblé reranker/retrieval : 23 passed rc 0 (log `…T105447Z.log`).
   (Contexte de lot : 119 tests passaient avant ; le lot ajoute 8 tests
   `test_reranking.py` et réécrit les tests de barrière.)
2. Recette d'acceptance INTÉGRÉE (SQL + E5 réel + reranker RÉEL, runner borné
   2000 Mio) : `WALLIA_TESTS_RUNNER_MEM=2000m ./scripts/tests-isolated.sh python
   tests/acceptance/rag_fr_en_isolated.py` → **OK, 11/11 checks** (log
   `runtime/evidence/tests-isolated-20260926T105704Z.log` ; évidence
   `runtime/tests-isolated/evidence/acceptance-rag-fr-en-isolated.json`) :
   - question FR exacte → passage EN « seven days » p.2, provenance réelle
     (document/produit/version/langue/page), **rang 1, logit 5.75806 — écart
     0.0 avec la trace du probe réel** ;
   - avec/sans filtre Aster 10.10 : conforme ; négatif commercial : 0 source ;
   - restriction 10.9 : aucune fuite 10.10, candidats tous 10.9, et contrôle
     indépendant « question positive 10.9 répondable » (plafond journal
     « 200 Mo ») ; 99.99 : zéro candidat, aucune substitution ;
   - génération périmée + document non prêt : jamais candidats ni servis ;
   - calibration rejouée sur le chemin intégré : **14/14 positifs, 14/14
     négatifs** ; seuil gelé et identité reranker (modèle/révision/SHA)
     recalculés dans le runner.
3. Coexistence E5 + CE dans le MÊME runner borné (jamais dans l'API) :
   cgroup max=2097152000 octets (2000 Mio), **peak=1570029568 (1497,3 Mio),
   oom_kill=0** ; VmHWM 1339,4 Mio ; torch 2.6.0+cpu. Latences : E5 charge
   7,38 s / CE 2,83 s ; reclassement 16 paires 0,95 s ; entrée LONGUE
   (11 564 chars, 2 590 tokens → tronquée à 512) : E5 0,209 s, CE 0,161 s,
   logit fini ; cross-check vecteur exporté vs E5 du runner : cosinus 1.0
   (vecteurs réels, non inventés).
4. Frontend touché : `npm run build` (tsc + vite) → **rc 0** (dist généré).
5. Pile vivante : `wallia-api-1` StartedAt=2026-09-26T08:57:16.750788302Z,
   RestartCount=0 — IDENTIQUE avant/après tous les runs ; worker idem (07:00:47Z,
   RC=0) ; aucun `down/stop/restart/rm` (politique lot3b : harnais conservé,
   DB `wallia-tests-db-1` 512 Mio sans port intacte) ; aucun secret réel lu ou
   modifié ; réglages secrets uniquement en booléens (`key_configured`).

## Limites / non-fait (assumé)

- La pile VIVANTE n'exécute PAS encore ce moteur : l'image `wallia:local`
  n'embarque ni le module de reclassement ni la copie locale du modèle
  (`/opt/models/reranker`) ; câblage Dockerfile/image + rebuild + acceptance
  live = lot suivant (hors périmètre).
- Scripts/calibration lot3/lot3b conservés comme preuves HISTORIQUES (voir
  bandeau dans `docs/retrieval-calibration.md`) ; le contrat de barrière n'est
  plus actif nulle part.
- La borne mémoire a été mesurée à 2000 Mio (limite du runner fournie
  explicitement pour cette preuve) ; le défaut du compose isolé reste 1600m.
- `tests/rag_fr_en.py` (acceptance live) n'a pas été rejoué : il décrit encore
  l'ancienne API (`min_cosine`) et sera aligné au lot de déploiement.
- Aucune revendication « Wallia livrée » ni expertise WALLIX : corpus de
  démonstration fictif, recettes en harnais isolé uniquement.
