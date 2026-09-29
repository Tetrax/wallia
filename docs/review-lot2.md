# Wallia — checkpoint suspendu du 26 septembre 2026

## Verdict du principal

**NON LIVRÉ — reprise nécessitant une autorisation ciblée et des corrections RAG.** Carte `t_0e0d0075`, run162. Aucun push, PR, merge ou déploiement HTTPS n'a été effectué pendant cette reprise. Le lot de livraison préparé n'a pas été lancé. Le prototype et les corrections restent conservés dans `/home/tetrax/workspace/wallia`, branche `feat/prototype`, HEAD `11265a5`, avec diff et fichiers nouveaux non commités.

## Blocage d'autorisation effectivement vérifié

Dans la session DeepSeek `20260926_072358_00b812`, le résultat natif du tool terminal (**message219282**, call`chatcmpl-tool-96a82faddde4261c`) indique :

> BLOCKED: Command flagged as dangerous (docker compose restart/stop/kill/down (container lifecycle)) but single-query mode (-q) runs without a user present to approve it.

La commande contenait le redémarrage ciblé :

```sh
docker compose -p wallia -f docker-compose.yml -f docker-compose.tests.yml restart api
```

Le principal a lu le résultat et l'appel exact dans `state.db`, et non seulement le résumé enfant. Aucun nouvel essai de ce redémarrage, aucune substitution par un autre chemin et aucun assouplissement des approvals. La reprise doit fournir une capacité d'approbation interactive ou une autorisation outillée strictement limitée aux opérations Wallia nécessaires. Ne pas débloquer simplement pour répéter le même refus ; ne pas passer les approvals globalement à `approve`.

## Travail conservé, sans acceptance finale

Lot1 : corrections de sécurité/prompts/streams/jobs/UI ; le principal avait constaté90 tests passés, build TypeScript/Vite vert et intégrité des secrets.

Lot2 : CLI Go/DeepSeek Flash effort max, session `20260926_072358_00b812`, processus suivi `proc_a487e622e2d9`, terminé exit0. Corrections supplémentaires appliquées : annulation transport silencieux, gardes des jobs, parsers en sous-processus, budget de contexte, déclarations et isolation frontend. Le rapport enfant `docs/corrections-lot2.md` est PROVISOIRE et conserve des sections incomplètes. Il ne constitue pas une validation. L'enfant a modifié ensuite le retrieval après sa dernière suite complète verte et n'a exécuté ni Playwright ni commit.

### Vérifications directes sur le snapshot final

| Contrôle | Résultat réel |
|---|---|
| `npm run build` dans frontend | PASS : tsc puis Vite,294 modules, sortie983ms ; log `runtime/evidence/lot2-final-frontend-build.log` |
| `pytest -q tests/test_retrieval.py -p no:cacheprovider` dans l'API/override, environnement de test isolé | **FAIL :3 failed,3 passed en1,00s** |
| Suite complète relancée par le principal | **INCOMPLÈTE : exit137**, journal `runtime/evidence/lot2-final-pytest.log`, pas de total final utilisable |
| Kernel/runtime lors de cette relance | OOM cgroup Wallia API à08:43:36UTC, processus uvicorn tué ; API redémarrée automatiquement par sa politique Docker. Observation08:45UTC : healthy, RestartCount5 au total, mémoire configurée1258291200octets. Ne pas confondre redémarrage automatique subi et opération de lifecycle approuvée. |
| Preuve RAG réelle relue | **FAIL** : `runtime/evidence/lot2-rag-fr-en.json`, positif sans passage EN attendu ; négatif commercial retournant des sources ; `ok:false` |
| Secrets runtime | `app.env`, `db_password`, `session_secret`, `worker_token` inchangés par comparaison SHA256 avec relevé avant ; aucune valeur affichée |
| Services voisins | Tous ceux ayant un healthcheck restent healthy, uptimes20h au dernier contrôle ; aucun redémarrage/volume/configuration étranger modifié |
| Sonde temporaire | Uvicorn port8123 dans le worker, PID hôte1313936, identifié puis arrêté par SIGTERM ; `docker top` confirme qu'il ne reste que le worker normal |

La preuve enfant `lot2-pytest-full3.log` contient bien **108 passed,6 warnings,76,67s**, mais sur une version antérieure au dernier changement `retrieval.py`. Elle ne peut pas être réutilisée pour déclarer le snapshot actuel vert.

### Défaillances précisément reproduites

- `test_filters_are_applied_before_ranking` : le seul document du produit Boreal disparaît malgré une correspondance lexicale.
- `test_cosine_barrier_and_lexical_rescue` : une correspondance lexicale forte avec un vecteur orthogonal est rejetée.
- `test_rrf_favours_chunks_present_in_both_lists` : score lexical absent alors que le passage doit figurer dans les deux listes.

Cause locale vérifiable : la nouvelle branche `_LEXICAL_SQL` filtre les lexèmes par `df * 2 < total` dans le corpus déjà filtré. Avec un seul passage ou un terme présent dans la moitié des passages, tous les lexèmes utiles peuvent être éliminés. Les essais précédents en OR de tous les mots favorisaient au contraire les mots communs et produisaient des faux positifs. Ne pas résoudre cela par une baisse ad hoc du seuil ou par l'abandon du scénario FR→EN.

Le positif de recette est : « Combien de jours de journaux la rotation conserve-t-elle dans Aster 10.10 ? » ; cible : Quick Start anglais, page2, « seven days ». L'enfant a mesuré un cosinus0,7921 pour ce passage contre seuil0,84. Il faut établir un protocole de calibration séparé, une requête hybride pertinente et une validation tenue à l'écart, plutôt que surajuster l'unique test d'acceptance. Les filtres produit/version doivent être exercés explicitement.

## Non exécuté / non actif

- Recette navigateur desktop/mobile et inspection des captures.
- Chat Wallia avec l'API DeepSeek native, dialogues complets, vision ; la connexion préflight du fournisseur seul n'est pas la recette de l'application.
- Recherche web applicative : non activée ; ne pas s'en servir pour masquer l'échec RAG.
- Publication du prototype/PR/CI requise exact-head et image finale par SHA.
- Vhost/TLS Wallia valide, activation/renouvellement de la paire gérée.
- Persistance après un redémarrage CONTRÔLÉ/recréation, sauvegarde/restauration et rollback réellement testés.
- Audit final complet du diff, de Git/bundle et des futurs scripts de livraison.

Le fournisseur fictif et l'image `wallia:local` restent des outils de test, pas un runtime livré. Le corpus est synthétique/non officiel ; aucune compétence métier WALLIX n'est établie.

## Reprise bornée

1. Résoudre l'approbation ciblée du cycle de vie Wallia dans un contexte capable de demander une décision humaine. Revérifier qu'aucun auteur code n'est vivant avant relance.
2. Reprendre UNIQUEMENT la correction retrieval et sa calibration avec un CLI DeepSeek Go/max borné ; conserver les autres changements. Isoler aussi les ressources des tests : ne plus lancer la suite complète ou un second serveur modèle dans l'API vivante limitée à1200Mio. Corriger la cause mémoire avant nouvelle recette complète.
3. Prouver RAG FR→EN/absence/versions puis E2E desktop/mobile et terminer le rapport/commit local. Astra revoit réellement le snapshot.
4. Seulement ensuite : lot de livraison `docs/delivery-contracts.md` ; brief déjà préparé sous `/home/hermes/.hermes/cache/scratch/wallia-delivery-brief.md`, à adapter à l'état de reprise, pas à lancer aveuglément.
5. Native API/web si validé, PR/CI/HTTPS, persistance/restauration, revue finale et compte rendu. Aucun autre job/carte/worker automatique n'a été créé pour contourner le blocage.

Routage du travail : principal `openai-codex/gpt-6-astra/max` (session `20260926_061950_76403f`) ; code `opencode-go/deepseek-v4.1-flash/max` (lots `20260926_062919_428c64` et `20260926_072358_00b812`). L'effort et la route ont été vérifiés dans les métadonnées ; aucune configuration globale de modèles ni protection modifiée.
