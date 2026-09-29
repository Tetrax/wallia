# Run209 — vérification du snapshot et reprise requise

## Verdict

Le prototype reste **NON LIVRÉ**. Les écarts infra et web de la revue préalable ont reçu des corrections fichiers-only, relues puis exercées par le principal. Les résultats ci-dessous sont de vraies exécutions du run209, pas une réutilisation des preuves run163. Ils valident les chemins couverts dans des harnais isolés ; ils ne constituent ni un déploiement ni une recette fournisseur native.

Branche `feat/prototype`, HEAD `11265a5b8ac14ed001eda8c9c806006d03b80cdb` + WIP conservé. Aucun nouveau commit/push/PR/merge ou build d'image de livraison pendant ce run. Tests exécutés dans l'image existante `sha256:73928914ea411f6c775022f6f6c3edd3dd538f021402e25898cb708401b82adc`, code courant monté en lecture seule ; ce n'est pas l'image immuable finale.

## Routage et restriction vérifiés

Principal `20260929_101606_def0bf` : `gpt-6-astra`, `openai-codex`, configuration `reasoning_config={enabled:true,effort:max}`, maximum150tours. Dernier codeur `20260929_121208_bb861b` / `proc_04ce0b0e54f9` : `deepseek-v4.1-flash`, `opencode-go`, endpoint Go `/zen/go/v1`, même effort configuré max, maximum60tours, terminé exit0 après59appels API. Outils persistés : read_file77/search_files37/patch24/write_file13, aucun outil d'exécution ou d'orchestration. Les métadonnées configurées et appels réels ne mesurent pas le calcul interne du fournisseur.

Aucun codeur actif à ce checkpoint. Aucun nouveau lot natif lancé. Les contrôles files-only restent une restriction des outils du modèle, PAS un confinement OS. Le principal a relu les scripts de reproduction et a exécuté les tests lui-même.

## Preuves actuelles

| Contrôle | Résultat réel | Preuve |
|---|---|---|
| Livraison/exploitation isolée | 93PASS / 1SKIP, 44.90s, runner exit0/OOMfalse | `runtime/evidence/run209-infra-tests-finalfix.log` |
| TLS notBefore futur | Vraie paire OpenSSL datée2030 refusée explicitement, exit1 attendu | `runtime/evidence/run209-tls-future-certificate-check.log` |
| Web/chat ciblés | 107PASS, 25warnings, 25.16s, exit0/OOMfalse | `runtime/evidence/run209-web-tests-after-fix.log` |
| Backend complet | 251PASS, 31warnings, 112.10s, exit0/OOMfalse | `runtime/evidence/run209-all-backend-final.log` |
| Types/build frontend | tsc + Vite PASS, exit0 | `runtime/evidence/run209-web-front-build-after-fix.log` |
| SSR réel des composants | Gardes URL, provenance et statuts web PASS, exit0 | `runtime/evidence/run209-web-ssr-after-fix.log` |
| Navigateur réel, flux SSE groupé | App réelle montée ; meta/sources/done dans un chunk ; statut et requête conservés dans le message final ; aucune erreur JS, exit0 | `runtime/evidence/run209-web-stream-after-fix.log` |
| RAG intégré réel, sans LLM | Tous les contrôles PASS : FR→EN p2/rang1/logit5.75806, versions, absence, génération/statut, calibration et mémoire | `runtime/evidence/run209-rag-integrated-final.log`, `runtime/tests-isolated/evidence/run209/acceptance-rag-fr-en-isolated.json` |

Le SKIP infra concerne uniquement la génération d'un certificat futur avec cryptography absent ; la paire OpenSSL réelle complète ce contrôle. Les warnings backend concernent les dépréciations HTTP et fork **dans les doubles de test** ; production utilise spawn, dont un chemin inoffensif sans réseau a aussi été exercé.

Les107 tests web font partie des251 tests backend : ne PAS additionner ces totaux. Les tests web/LLM utilisent des doubles locaux, pas Firecrawl/DeepSeek réels. La reproduction navigateur couvre le bug de batching du statut web, pas la recette desktop/mobile complète.

RAG : E5 et cross-encoder réels chargés avec poids locaux vérifiés, SQL/pgvector réel dans les bases de test dédiées, seuil1.1491 inchangé ; calibration14positifs/14négatifs validés. Mémoire cgroup : maximum1677721600octets, pic1565958144octets, oom_kill0. Le corpus Docling déjà exporté et ses vecteurs sont réutilisés, avec contre-vérification E5 fraîche : **aucune nouvelle ingestion Docling** ni génération LLM n'est prouvée par ce replay. L'avertissement tokenizer sur une entrée2590tokens précède la troncature testée ; ne pas le masquer.

## Périmètre préservé

Conteneurs de test terminés conservés : infra jusqu'à `wallia-delivery-tests-run209-d`, web `wallia-tests-runner-run-4a8050166503`, suite complète `wallia-tests-runner-run-944533ffa5f2`, RAG `wallia-tests-runner-run-5e3b37fa85ae`. Aucun `rm`, `down`, prune ou nettoyage global. Les messages Compose suggérant de supprimer les orphelins n'ont pas été suivis.

API/worker/DB Wallia observés healthy, RestartCount0/OOMfalse, StartedAt respectivement2026-09-26T08:57:16.750788302Z,2026-09-26T07:00:47.022315679Z et2026-09-25T22:22:36.733936174Z. Les trois secrets DB/worker/session sont identiques au manifeste initial par `sha256sum -c`. D'autres services ont leurs propres changements d'uptime : aucune attribution à Wallia ni affirmation d'immobilité globale ; leurs healthchecks observés restent healthy.

Le rattachement historique `runtime/deploy-state/current.json` est une préparation de rollback, PAS un rollback exécuté ; voir `review-run209-legacy-snapshot.md`. L'ancien bind backend mutable est toujours présent dans le runtime historique. Aucun test live, aucune nouvelle ingestion live, aucun secret applicatif copié/activé.

## Blocage et reprise

Le chemin de réconciliation Obsidian sous le verrou `valdev-obsidian.lock` a été refusé par le contrôle natif dans ce contexte one-shot (`flock … vault.py dashboard`, commentaire288). Ne pas le relancer sous une autre forme, désactiver les approvals ou contourner le verrou. La note canonique reste en retard sur ce checkpoint : réconciliation et gates à effectuer depuis un contexte autorisé. Le refus d'execute_code et d'une commande composée d'audit n'a produit aucune exécution ; les outils simples autorisés ont suffi aux tests ci-dessus.

Le run approche sa borne native150tours. Le checkpoint est conservé sans créer une nouvelle carte, un nouveau job ou un codeur qu'il ne pourrait plus revoir. La mission ne peut pas être marquée terminée ni simplement envoyée en review : il reste de l'implémentation et des recettes réelles.

Reprise sur **la même carte** après résolution explicite du chemin d'approbation :

1. Vérifier l'absence de writer, le WIP et les preuves ci-dessus ; ne pas repartir du bootstrap ou effacer l'incident.
2. Faire implémenter le lot natif borné décrit dans `runtime/run209-native-brief.md`, via `runtime/launch_files_only_leaf.py`, outils file uniquement ; relire avant toute exécution. En particulier fermer l'ancien chemin `scripts/acceptance.sh streaming` qui modifie le connecteur live. Ne pas lancer ce chemin ancien.
3. Construire et vérifier l'image finale/modèles depuis un commit traçable, puis exercer les vraies recettes fournisseur, ingestion, UI desktop/mobile, persistance, sauvegarde/restauration et rollback selon les contrats existants.
4. Publier selon le contrat PR/CI exact-head, provisionner et tester HTTPS sans `-k`, puis réconcilier les preuves et le vault. Aucun succès fictif, aucune nouvelle facturation ou mutation de services voisins.

Le corpus reste synthétique/non officiel et aucune compétence métier WALLIX n'est attestée par ces fixtures.
