# Calibration retrieval (lot3 → lot3b)

> **STATUT : HISTORIQUE.** Le contrat d'éligibilité de cette page (barrière
> cosinus 0.87 + éligibilité lexicale PAR PASSAGE ≥2 correspondances) a été
> REMPLACÉ en lot3d par le reclassement cross-encoder au seuil de logit gelé
> 1.1491 (décision et preuve : `docs/reranker-probe.md`). Les jeux de données
> et les résultats lot3/lot3b ci-dessous restent conservés comme PREUVES
> HISTORIQUES de la démarche de calibration et comme jeu d'évaluation rejoué
> sur le chemin intégré réel (recette isolée lot3d). Aucun paramètre de cette
> page n'est actif en production.

Objet : calibrer la barrière vectorielle et l'éligibilité lexicale PAR PASSAGE
du retrieval sur un JEU DÉDIÉ, avant et séparément de la recette d'acceptance
métier. Aucun texte du jeu d'acceptance (Aster / Quick Start « seven days » /
négatif commercial) n'a servi à choisir un paramètre, et ces textes n'ont pas
été modifiés. Preuves machine (lot3b) :

- `runtime/evidence/lot3b-calibration.json` — sélection déterministe sur les
  vecteurs E5 réels (réanalyse lot3b) ;
- `runtime/evidence/lot3b-calibration-runner.json` — exécution SQL RÉELLE de
  l'implémentation par passage (pytest isolé, vecteurs E5 réels ;
  `texts_sha256` dans l'évidence) ;
- lot3 : `runtime/evidence/lot3-calibration.json`,
  `runtime/evidence/lot3-calibration-runner.json` (conservés, non écrasés).

## Jeux de données

- **Calibration** : `fixtures/calibration/calibration.json` (« calibration-nova-boreal » v1,
  contenus fictifs inoffensifs, sous-dossier dédié distinct du corpus démo) —
  13 passages, **14 positifs** (FR→FR, FR→EN, EN→FR, EN→EN ; petits corpus ;
  questions dont produit/version ne suffisent pas) et **14 négatifs** =
  12 « gated » (10 hors-sujet + 2 pièges métadonnées produit/version) + 2
  « proches » (aucune réponse dans le corpus, mais vocabulaire proche).
  Les TEXTES sont des fixtures ; les VECTEURS utilisés sont des embeddings
  **E5-small réels** exportés via l'endpoint interne de l'API vivante — les
  deux couches sont distinctes et jamais confondues.
- **Acceptance** : jeu métier existant, textes INCHANGÉS, tenu à l'écart de la
  sélection ; joué séparément après le gel des paramètres.

## Politique retenue (figée AVANT l'acceptance)

Éligibilité évaluée **par passage**, sur ses seuls signaux, avant la troncature
top_k (aucune accumulation de correspondances à travers le corpus) :

- **vecteur** : cosinus du passage ≥ `WALLIA_RETRIEVAL_MIN_COSINE=0.87` ;
- **lexical fort** : ≥ `WALLIA_RETRIEVAL_LEXICAL_MIN_MATCHES=2` lexèmes
  significatifs de la question présents DANS le passage (mots vides FR/EN et
  jetons de métadonnées produit/version exclus) ;
- **mono-lexème corroboré** : une seule correspondance dans le passage est
  admissible si le cosinus du passage ≥
  `WALLIA_RETRIEVAL_LEXICAL_CORROBORATION_COSINE=0.85` (porte translangue) ;
- **question réduite à un unique lexème significatif** : une correspondance
  dans le passage suffit (couverture 1/1 — porte calibrée lot3, conservée par
  passage ; aucun négatif mesuré n'en bénéficie).

### Sélection mesurée (aucune acceptation en boucle)

| Mesure | Valeur |
|---|---|
| cos min des positifs (cible) | 0.8038 |
| cos max des négatifs gated (meilleur) | 0.8561 |
| marge | **−0.0523 → non séparé** (constat honnête, pas de garantie générale) |
| barrière retenue | `ceil2(0.8561)+0.01 = 0.87` (rejeter d'abord) |
| cos max d'un négatif sur un passage à UNE correspondance | 0.8322 (neg6 « service » → p4) |
| corroboration retenue | `ceil2(0.8322)+0.01 = 0.85` (rejeter d'abord TOUS les négatifs mesurés, proches compris) |

### Exécution SQL réelle (runner isolé, pgvector)

`backend/tests/test_retrieval_calibration.py` (pytest, harnais isolé, base
dédiée, vecteurs E5 réels exportés) — résultat mesuré lot3b :

- **12/12 négatifs gated rejetés** (aucune source) ;
- **2/2 négatifs proches rejetés** (neg13, neg14) — absence maintenue ;
- **11/14 positifs servis** ; limites documentées :

| Cas | Cause (mesurée) |
|---|---|
| pos7 FR→EN sans lexème partagé | cos 0.8236 < 0.87 → sous la barrière, aucune correspondance |
| pos8 « LED bleue clignotante » vs « blue flashing LED » | 1 correspondance (*led*), cos 0.8256 < corroboration 0.85 |
| pos14 « port de gestion » vs « management port » | 1 correspondance (*port*), cos 0.8038 < 0.85 |

`pos4` (« purger manuellement » vs « purge manuelle ») n'est **plus** une
limite : sa correspondance unique (*historique*) est corroborée par un cosinus
mesuré de 0.8536 ≥ 0.85. Comparaison lot3 (politique collective) : 11/14
servis aussi, mais `pos4` perdu et des passages bruités pouvaient être servis ;
la correction par passage change la composition, pas le compte.

## Acceptance (distincte, après gel) — résultats réels lot3b

`backend/tests/acceptance/rag_fr_en_isolated.py` (runner isolé) : corpus réel
importé (4 documents / 16 passages, extraction Docling), vecteurs réels,
pgvector 0.8.6, filtres produit/version exercés. Scénarios d'origine conservés
à l'identique + scénario filtré `product=Aster/version=10.10` ajouté.
Preuve : `runtime/evidence/lot3b-acceptance-rag-fr-en-isolated.json`.

Résultat : **3 OK / 2 ÉCHEC** —

- OK : négatif commercial sans aucune source (meilleur cosinus 0.7841) ;
  version 10.9 : filtrage uniquement 10.9 ; 99.99 → aucune substitution ;
- ÉCHEC (honnête, non contourné) : le passage Quick Start (EN) p.2
  « seven days » n'est **plus servi**, ni sans filtre ni dans le périmètre
  `product=Aster/version=10.10`. Signaux réels du passage : cosinus **0.7921**,
  une seule correspondance (*rotation*, `lexeme_hits=1`) → sous la
  corroboration 0.85. Le servir exigerait un seuil ≤ 0.7921, ce qui admettrait
  neg6 (gated, 0.8061 sur une correspondance de même nature) : compromis
  consigné, aucun réglage sur l'acceptance. Le filtrage 10.9 reste correct ;
  la PRÉSENCE de la réponse « seven days » dans le périmètre n'est pas établie
  sous la politique par passage (lot3 la servait via la couverture collective
  du corpus — précisément le défaut corrigé ici).

## Limites (pas de garantie générale)

- Jeu de 28 questions, E5-small, distribution de scores concentrée ; la
  séparation est **constatée impossible** sur ce jeu — le point de
  fonctionnement est un compromis rejet>rappel, pas une preuve statistique.
- La porte mono-lexème corroborée est une porte de rappel (translangue), pas
  une preuve de pertinence ; le passage servi reste un candidat à vérifier.
- Reproductibilité : `scripts/tests-isolated/refresh-acceptance-data.sh`
  (endpoint embeddings réel, lecture seule) puis
  `scripts/tests-isolated.sh` (suite + calibration) et
  `scripts/tests-isolated.sh python tests/acceptance/rag_fr_en_isolated.py`
  (acceptance). Toute re-calibration doit rejouer l'analyse AVANT de rejouer
  l'acceptance. Le runner ne nettoie jamais le harnais (lot3b) : la DB de test
  `wallia-tests-db-1` (limite 512 MiB), le réseau `wallia_tests_net` et le
  volume `wallia_tests_pgdata` restent en place pour les replays.
