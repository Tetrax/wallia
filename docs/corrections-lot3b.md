# Corrections lot3b — réponse à `docs/review-lot3.md`

Périmètre : corrections ciblées des points bloquants de la revue lot3 sur le
snapshot existant, puis vérifications réelles. Aucune livraison, aucun lot4,
aucun commit. Le principal (Astra) conserve revue/état/publication.

## 1. Éligibilité PAR PASSAGE (revue §1)

Avant : `retrieval.py` comptait les lexèmes « présents quelque part dans le
corpus filtré » (CTE `matched`), ouvrait une barrière globale et servait TOUS
les passages ayant un `rank_text` — une source vectorielle forte pouvait
qualifier un bruit lexical faible, et deux termes répartis sur deux passages
orthogonaux pouvaient sembler forts.

Après (minimal, RRF/filtres inchangés) :

- `lexeme_hits` est compté **dans chaque passage** (`c.tsv @@ to_tsquery`
  par lexème, dans les DEUX branches SQL ; le comptage global est supprimé) ;
- une petite politique générale par candidat décide l'éligibilité sur les
  seuls signaux du passage, **avant** la troncature top_k ;
- raisons observables dans `diagnostics["candidates"]` (cosine, `lexeme_hits`,
  `text_rank`, `eligible`, `reason`) — aucun score composite inventé ;
- porte mono-lexème **corroborée** (cos ≥ 0.85) calibrée sur le jeu dédié ;
  porte « question réduite à un unique lexème » conservée par passage.

Paramètres choisis sur la calibration SEULE (voir `docs/retrieval-calibration.md`) :
`MIN_COSINE=0.87` (inchangé), `LEXICAL_MIN_MATCHES=2` par passage,
`LEXICAL_CORROBORATION_COSINE=0.85` (mesuré : max des négatifs à une
correspondance 0.8322). Aucun lexique de traductions, aucune whitelist,
aucun modèle ajouté.

Tests de régression ajoutés d'abord (revue §1, exemples exacts) :
`test_orthogonal_disjoint_matches_never_accumulate`,
`test_strong_vector_source_never_qualifies_weak_lexical_noise`,
`test_single_match_requires_corroboration_except_keyword_question`,
`test_generic_single_word_never_rescues_alone` + mise à jour des tests
existants (`backend/tests/test_retrieval.py`).

Politique appliquée par DeepSeek V4.1 Flash (opencode-go/deepseek-v4.1-flash,
effort max) ; aucun enfant/agent/job/cron/Kanban ; Astra principal vérifie.

## 2. Runner : plus aucun nettoyage automatique (revue §2)

- `scripts/tests-isolated.sh` : l'appel `... down --remove-orphans` est
  **retiré** (pas déguisé, pas remplacé ; aucun down/stop/restart/rm/Docker
  API/script). Le runner démarre désormais uniquement la DB dédiée si absente
  et exécute le conteneur de test éphémère (fin naturelle).
- Exécutions réelles lot3b : `up` (DB déjà là au 2e/3e run → « Running »),
  `pytest` puis acceptance, **aucune** opération de lifecycle supplémentaire ;
  l'opération refusée (resultat terminal 220019, session 20260926_090422_dc6d7b)
  n'a PAS été rejouée.
- État laissé en place et vérifié : conteneur `wallia-tests-db-1`
  (healthy, StartedAt 2026-09-26T10:00:34Z, limite mémoire 512 MiB,
  restarts 0, OOM false), réseau `wallia_tests_net`, volume
  `wallia_tests_pgdata`. Rien d'autre ; aucun nettoyage effectué ni à faire.

## 3. Garde-fous conftest (revue §3)

- `_refuse_delivery_container` : les contrôles production / secrets réels
  s'exécutent **avant** toute dérogation — `WALLIA_TEST_RUNNER=isolated` ne
  les court-circuite plus (la dérogation anticipée a été supprimée, pas
  déplacée).
- `_assert_isolated` : refus des **descendants** de `/secrets` et `/data`
  (pas seulement l'égalité), y compris si `WALLIA_TEST_TMP` est configuré à
  l'intérieur.
- Régressions ciblées : `test_assert_isolated_refuses_data_and_secrets_descendants`,
  `test_delivery_container_guard_is_not_bypassed_by_isolated_flag`
  (`backend/tests/test_isolation.py`). Aucune lecture/altération de secrets
  réels.
- Commentaires périmés alignés : conftest (≥1 → politique par passage),
  `test_retrieval.py` (tiers de couverture), `test_retrieval_calibration.py`
  (limites mesurées).

## 4. Recette isolée (revue §3)

- Scénarios d'origine conservés à l'identique ; ajout de
  `product=Aster/version=10.10` qui enregistre le périmètre du passage
  Quick Start EN p.2 (`product=Aster`, `versions=[10.10]`) et ses signaux par
  passage ; 10.9 et 99.99 conservés ; distinction filtrage / présence de la
  réponse matérialisée (le check 10.9 ne vaut que pour le filtrage).

## Résultats réels (après dernière modification)

Commandes reproductibles (harnais isolé, aucun cleanup) :

    scripts/tests-isolated.sh                       # suite backend complète
    scripts/tests-isolated.sh python tests/acceptance/rag_fr_en_isolated.py

- Suite backend complète : **119 passed** (log `runtime/evidence/lot3b-pytest-isolated.log` ;
  1er passage rouge par bug de TEST (contrôle positif après monkeypatch), corrigé
  — log conservé `lot3b-pytest-isolated-run1.log`).
- Calibration (SQL réel + vecteurs E5 réels) : **12/12 gated + 2/2 proches
  rejetés, 11/14 positifs servis**, limites pos7/pos8/pos14 documentées ;
  `pos4` désormais servi par corroboration (0.8536) —
  `runtime/evidence/lot3b-calibration-runner.json`.
- Acceptance isolée : **3 OK / 2 ÉCHEC** — négatif commercial (aucune source),
  filtrage 10.9, 99.99 sans substitution ; le passage EN « seven days »
  (cos 0.7921, 1 correspondance *rotation*) n'est plus servi, ni non filtré ni
  dans le périmètre Aster/10.10. Le servir exigerait un seuil ≤ 0.7921, qui
  admettrait le négatif gated neg6 (0.8061) : compromis constaté, pas de
  réglage sur l'acceptance —
  `runtime/evidence/lot3b-acceptance-rag-fr-en-isolated.json`.

## État / décisions attendues

- API vivante : StartedAt `2026-09-26T08:57:16.750788302Z`, RestartCount 0,
  OOM false, voisins healthy — aucune modification.
- Aucun commit/push ; worktree laissé tel quel. Preuves lot3b séparées
  (historique lot3 intact).
- Décision pour Astra/Tetrax : le contrat FR→EN « seven days » n'est pas
  couvert par la politique par passage calibrée. Options (hors périmètre
  lot3b) : lot4 applicatif (périmètre produit/version au premier tour), ou
  acceptation explicite du compromis ; le passage reste un candidat non prouvé.
