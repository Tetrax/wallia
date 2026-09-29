# Revue lot3 — Astra, run163

## Verdict RAG intégré après3d — accepté en harnais isolé, pas une livraison

Replay DIRECT Astra après fin du writer3d : `scripts/tests-isolated.sh` →128PASS,6warnings,78.08s ; `WALLIA_TESTS_RUNNER_MEM=2000m scripts/tests-isolated.sh python tests/acceptance/rag_fr_en_isolated.py` →11/11contrôles,rc0 ; `npm --prefix frontend run build` →tsc/Vite rc0. Logs `runtime/evidence/lot3d-astra-{pytest,rag,frontend}.log`. Les nouveaux tests de scoring sont des doubles unitaires explicitement déclarés ; la recette intégrée utilise au contraire E5 et le cross-encoder REELS et le SQL/pgvector de l'application.

Résultats : FR exact → ENp2rang1/logit5.75806, produit/version/langue/page conservés, avec/sans filtre ; commercial0source ;10.9ne laisse pas fuir10.10, question10.9répondable retourne200Mo ;99.99sanssubstitution ; générationpérimée/documentnonprêt exclus. Calibration distincte rejouée14/14positifs +14/14négatifs. Coexistence E5+CE sous2097152000octets :peak1439666176,oom/oom_kill0 ; entrée2590tokens réellement tronquée, logitfini. APIStartedAt08:57:16.750788302Z/RestartCount0/healthy et worker/DBinchangés. Corpus fictif : aucune compétenceWALLIX revendiquée.

Revue directe du code : une seule chaîne sélectionSQL/RRF bornée→scoring individuel→seuilgelé1.1491, plus de barrièrecollective ni de filtrecosinus préalable. Lecture des scripts d'acceptance, tests et propagationconfig/search/status/settings réalisée ; absence de nouveau service et d'appel externe. Le lot3d a atteint son plafond90tours (CLIexit1), mais ses checks rapportés ont été refaits directement et passent. Image/runtime vivant n'ont PAS encore ce code/poids.

À corriger avec le lot applicatif suivant (ne remettent pas en cause les scores réels vérifiés) :
- `retrieval.hybrid_search` capture les diagnostics `service.info` AVANT lazyload ; premier succès peut conserver `unloaded`. Actualiser après score ; les erreurs d'inférence doivent rendre l'état réellement observable, et rejeter des logits non finis comme échec technique plutôt que no-source/JSON invalide.
- L'identité reranker est dite fixe mais `config.py` autorise encore les overrides model/revision/SHA. Les constantes de la décision doivent être fixes ou les divergences refusées ; chemin local configurable oui, substitution silencieuse non.
- Le plafond mémoire est prouvé avec un passage long, pas encore le batchlong maximal (8CE/16E5). Compléter la mesure en runner isolé avant de retenir le plafond de l'API de livraison ; pas d'essai lourd dans APIvivante. Ne pas ajouter de composant tant qu'une pression n'est pas mesurée.

Le lot app/E2E décrit dans `docs/review-run163-app.md` peut maintenant démarrer. Livraison/native/web/TLS/persistance/CI encore NON VALIDÉS.

## Historique — verdict provisoire du lot3 : corrections ciblées requises

Le lot `20260926_090422_dc6d7b` a terminé exit0. Les fichiers et preuves ont été relus directement. Le rapport annonce113PASS et une recette5/5 ; les journaux et données existent, mais le principal n'a PAS encore rejoué ce snapshot. Ne pas transformer cet exit0 en acceptance du prototype.

### Progrès réels constatés en lecture

- Runner/DB séparés, pas de montage des secrets/données du runtime vivant, limites1600MiB/2CPU ; StartedAt API08:57:16.750788302Z, RestartCount0/OOMfalse au contrôle09:34.
- Retrait de la coupure lexicale par fréquence cassant les petits corpus. Filtrage explicite des mots vides et des produits/versions ; filtres métier toujours avant ranking.
- Calibration distincte14positifs/14négatifs avec vrais vecteurs E5 exportés, marge négative honnêtement documentée ;11/14positifs servis,12/12négatifs gated rejetés. Ce n'est pas une garantie générale, ni une preuve de capacité WALLIX.
- Recette sur copie du corpus réel Docling/pgvector : le passage EN p2 avec seven days est présent dans les6sources ; négatif commercial vide ; versions exercées. Le positif principal est NON FILTRÉ produit/version : sa liste contient aussi10.9. La validation du premier tour naturel reste donc au lot applicatif.

### 1. La barrière lexicale est collective, pas propre au passage

`retrieval.py` lignes83-89 compte les lexèmes présents n'importe où dans le corpus filtré. Lignes220/302 ouvrent ensuite une barrière globale ; lignes312-315 acceptent TOUS les passages ayant un `rank_text`, même si chacun ne contient qu'un seul mot et a un vecteur sans rapport. Une bonne source vectorielle peut aussi entraîner un candidat lexical faible qui n'aurait jamais franchi la rescousse.

Cela ne démontre pas les affirmations des commentaires « un mot commun ne déclenche jamais à lui seul une fausse pertinence » et « candidats réellement pertinents ». Exemple déterministe à ajouter : question à deux termes distincts, deux passages disjoints avec un terme chacun et des vecteurs orthogonaux ; aucun passage ne doit être qualifié par simple accumulation des termes à travers les autres. Autre cas : une vraie source vectorielle et un bruit lexical faible ; la bonne source ne qualifie pas le bruit.

Correction minimale : évaluer la pertinence/éligibilité au niveau de chaque passage, avec scores observables, avant la troncature top_k. Ne pas juste passer à deux termes par passage en cachant la régression FR→EN (le passage EN attendu ne partage que rotation). Autoriser un seul terme sur un passage seulement avec une corroboration sémantique/lexicale générale calibrée sur le jeu indépendant, jamais par whitelist question/document. Relancer calibration puis acceptance sans en changer les textes ; rapporter les limites et échecs. Les deux négatifs proches doivent rester visibles, pas être omis du compte global.

### 2. Refus lifecycle : le replay actuel n'est plus autorisé

Refus natif confirmé dans state.db, sessionlot3, résultat terminal **message220019**, appel220018 :

`docker compose -p wallia-tests -f docker-compose.tests-isolated.yml down`

Motif : `docker compose restart/stop/kill/down (container lifecycle)` nécessite une approbation interactive absente du one-shot. Le refus arrive APRÈS les suites effectuées ; le transcript ne montre pas un nouveau down après ce résultat. La variante via le script existant n'est PAS une solution : `scripts/tests-isolated.sh` ligne57 contient précisément un down. Le principal n'a donc PAS lancé ce script après découverte du refus.

Le nettoyage n'est pas un prérequis aux tests : retirer l'arrêt/nettoyage automatique du runner (pas le déguiser, pas le remplacer par Docker API/rm/kill), conserver la DB/réseau isolés pour les replays et rapporter explicitement leur état. Pas de nouvelle opération down/stop/restart/rm sur cette pile sans décision outillée. Les conteneurs de tests sont absents au constat du lot3 ; aucun nettoyage immédiat nécessaire. Les futurs cycles de livraison restent soumis aux contrôles natifs et ne sont pas autorisés par cette note.

### 3. Petites cohérences du harnais

- conftest : `_refuse_delivery_container` ne doit pas ignorer la détection de secrets réels/production parce que la seule variable WALLIA_TEST_RUNNER vaut isolated.
- `_assert_isolated` doit refuser des descendants de /data ou /secrets, pas seulement leur égalité, même si TEST_TMP se trouve dedans ; ajouter régressions ciblées.
- Le test de calibration skippe explicitement sans vecteurs en CI (acceptable), mais ses commentaires ≥1 et celui du tiers de couverture dans test_retrieval sont périmés : aligner sur la politique finalement retenue.
- Ajouter au résultat de recette le filtre explicite product=Aster/version=10.10 et vérifier le passage EN de ce périmètre ; conserver le scénario non filtré pour diagnostic et10.9/99.99.

Aucun lot4/livraison lancé avant correction/replay. API vivante reste ancienne version, aucun redémarrage par le principal.
