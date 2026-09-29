# Wallia — reprise encadrée après l'incident du run198

## Décision humaine et objectif

Le 2026-09-29, après présentation de l'incident et de ses limites, Valentin a répondu **« oui va si »** à la reprise proposée : revue des modifications conservées, codeur limité techniquement aux outils de fichiers, commandes relues et exécutées uniquement par le principal avec les approbations natives. Décision consignée sur `default/t_0e0d0075`, commentaire279.

Cette décision autorise la reprise de la même mission de livraison ; elle n'approuve pas rétroactivement le nettoyage global, n'efface pas l'incident et n'autorise aucun contournement des contrôles. Aucun autre service ni modèle ne change de périmètre. Aucun nouveau job/carte ou profil Builder/Reviewer.

**Lire ce document avant les anciens briefs.** Il remplace les instructions de lancement du codeur dans les reprises précédentes, mais conserve le cahier des charges, `docs/architecture.md`, `docs/delivery-contracts.md` et les critères de livraison. L'application reste NON LIVRÉE.

## Contrôle technique vérifié avant reprise

- Mécanisme natif documenté : CLI `hermes chat --toolsets file`. Aucune mutation de la configuration globale, du routage global ou des approvals.
- Sélection native vérifiée : exactement `read_file`, `write_file`, `patch`, `search_files`. Aucun terminal, gestion de processus, Python programmatique, navigateur/CDP, MCP, passerelle de recherche d'outils, délégation, Kanban ou job exposé au codeur.
- Validation ad hoc réellement exécutée sur la boucle native : appels synthétiques vers `terminal`, `process_manage`, `execute_code`, `delegate_task`, `cronjob_manage`, `browser_cdp`, `tool_call`, `kanban_create` rejetés avant dispatch. Aucun handler exécuté par ce test. Sonde : `runtime/verify_files_only_tools.py`.
- Les premières invocations diagnostiques via l'ancien Python sans bootstrap ont échoué sur les imports (ce ne sont pas des tests passés). Exécution correcte et verte via l'environnement natif : depuis la racine source Hermes, son Python de compatibilité avec `import hermes_bootstrap` puis `runpy.run_path` de la sonde. Ne pas installer de dépendances dans Hermes pour ce contrôle.
- Smoke réel CLI terminé exit0 : session `20260929_100637_cf8f94`, modèle `deepseek-v4.1-flash`, provider `opencode-go`, URL `https://opencode.ai/zen/go/v1`, effort configuré `max`, 3 appels API et 3 appels outils persistés. Outils réellement employés : deux `read_file`, un `write_file`. Aucune modification applicative. Rapport et log : `runtime/evidence/files-only-smoke-20260929.{md,log}`.
- Le lanceur contrôlé est `runtime/launch_files_only_leaf.py` : CLI officiel, sélection `file` fixe, environnement d'identité/session/Kanban nettoyé seulement dans la copie enfant, verrou `runtime/delivery-leaf.lock`, logs créés sans écrasement, budgets bornés. Il s'agit d'orchestration locale, pas d'un script de déploiement. Ne PAS relancer `runtime/launch_delivery_run198.py` ni le brief ancien tel quel.

**Limite à ne pas masquer :** il s'agit d'une restriction native des outils accessibles au modèle, PAS d'un bac à sable OS ni d'une garantie de confinement des chemins. Les outils de fichiers et leurs contrôles natifs restent actifs. Le principal définit les chemins autorisés dans chaque lot, ne confie aucun secret/configuration Hermes, vérifie les chemins effectivement modifiés et relit tout script avant de l'exécuter. Ne pas prétendre que les écritures sont confinées par les permissions système.

## Routage et responsabilité

- Un principal durable unique : `openai-codex/gpt-6-astra`, effort `max` vérifié, même carte et workspace. Le parent Desktop cesse les opérations applicatives après constat du démarrage.
- Un codeur ponctuel maximum : `opencode-go/deepseek-v4.1-flash`, effort `max`, **uniquement `--toolsets file`**. Aucun autre toolset ni fallback d'exécution. Aucun enfant récursif. Aucune reprise d'une session ayant eu des outils hôte.
- Le principal écrit les contrats/rapports, arbitre et revoit. DeepSeek reste l'auteur du code applicatif, des tests, des scripts et des correctifs. Le codeur prépare les fichiers/tests mais indique clairement **NON EXÉCUTÉ** ; le principal seul les exerce après revue.
- Pour chaque nouveau lot, utiliser le lanceur contrôlé avec brief/log propres, maximum80tours et3600secondes. Préférer des lots courts et bornés par chemins ; un auteur par fichier, aucune recette contre un arbre en cours d'édition. Vérifier argv, provider/modèle/effort et traces outils ; un outil inattendu impose l'arrêt.
- Le principal conserve les limites natives150tours/21600secondes. Garder un budget pour revue, preuves et checkpoint ; pas de retry identique, pas de nouveaux jobs pour contourner une limite ou une intervention humaine.

## Séquence de reprise obligatoire

1. Lire `docs/incident-run198.md`, tête de `docs/state.md`, `docs/delivery-contracts.md`, `docs/delivery-handoff.md` et le mandat complet conservé sur la carte. Les paragraphes historiques ne sont pas une observation actuelle.
2. **Revue seule d'abord** : comparer le WIP aux preuves avant/après du run198 et examiner les24chemins du manifeste `runtime/evidence/run198-leaf-written-paths.json`. Préserver tous les fichiers ; pas de reset/clean/restore aveugle. Ne pas exécuter les nouveaux scripts avant revue. Les144tests/RAG11/11/UI24+24 du run163 NE VALIDENT PAS le nouveau snapshot.
3. Identifier les écarts concrets aux contrats et donner à DeepSeek un lot fichiers-only minimal avec OBJECTIF, CONTEXTE, CONTRAINTES, CHEMINS et CRITÈRES. Le codeur ne modifie ni `runtime/launch_files_only_leaf.py`, ni les garde-fous, ni les secrets, ni les fichiers hors lot. Besoin de commande/doc externe : il le remonte au principal, sans chercher un autre moyen d'exécution.
4. Après fin du codeur : inspecter le diff stable et les chemins, puis lancer les tests pertinents et recettes isolées uniquement via les outils natifs du principal. Un refus reste un refus ; pas de wrapper de contournement, pas de modification d'approvals. Conserver les ressources si leur cleanup est refusé.
5. Continuer les critères originaux de livraison (image reproductible, vrai fournisseur distinct du coding, sauvegarde/restauration, PR/CI exact-head requise, HTTPS, parcours desktop/mobile, persistance et rollback) seulement après validation du snapshot. Les contrôles sensibles et les frontières de services restent inchangés.
6. Actualiser `docs/state.md`, matrice d'acceptation et note canonique Obsidian aux jalons. Faire les gates vault avec racine explicite et verrou ; préserver les changements étrangers. Ne terminer la carte que si tous les critères sont réellement vérifiés.

## Précautions runtime

L'ancienne API est saine mais son bind backend historique rend les fichiers nouveaux visibles sans reload. Aucune nouvelle ingestion ou recette live avant revue et reconstruction contrôlée ; les anciens healthchecks ne valident pas le nouveau code. Avant build lourd, revérifier RAM/disque et concurrence (dernier préflight parent :2010MiB disponibles et66GiB libres). Pas de nettoyage Docker global, pas de modifications des services voisins. Nginx/TLS et vault utilisent leurs verrous existants.
