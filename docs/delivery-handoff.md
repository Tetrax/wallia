# Wallia — reprise du lot livraison

> **Reprise du 2026-09-29 après incident : lire `docs/resume-files-only.md` en premier.** Autorisation humaine obtenue ; revue du snapshot run198 avant toute correction. Le codeur doit être lancé avec le seul toolset `file`, sans outil d'exécution hôte. Les tests et opérations appartiennent au principal et restent soumis aux approvals. Le checkpoint run163 ci-dessous est historique et ne valide pas les modifications partielles du run198.

## Autorisation et objectif

Valentin a explicitement choisi « Oui, reprendre la livraison prévue » dans la session Desktop, puis demandé « reprends ». Cette décision autorise la sortie de triage de `default/t_0e0d0075` et la reprise bornée de la même mission, publication et déploiement inclus si les validations et contrôles natifs le permettent. Aucun changement de modèle, profil, limite globale ou approval. Le présent document remplace le brief de livraison temporaire disparu du scratch ; il ne remplace ni le cahier des charges original ni les contrats du dépôt.

**Objectif : livrer le prototype utilisable sur https://wallia.valdev.me à partir du travail existant. Ne pas relancer les anciens lots de correction.**

## Contexte utile à lire

1. `AGENTS.md`, tête de `docs/state.md`, `docs/review-lot4-astra.md` : checkpoint final du run163 et limites des preuves.
2. `docs/delivery-contracts.md`, `docs/review-infra.md`, `docs/architecture.md`, `docs/acceptance.md`, fichiers existants de build/deploy/backup/restore, Compose et CI.
3. Cahier des charges original conservé en pièce jointe de la mission, `/home/hermes/.hermes/attachments/Contenu collé (25.4 KB)` ; contexte et autorisations complets sur la carte. Les préflights historiques ne sont pas des observations actuelles.
4. Note canonique Obsidian Wallia et règles de son vault avant sa mise à jour. `docs/state.md` reste l'état technique de reprise.

Checkpoint : branche `feat/prototype`, base locale `11265a5`, diff et fichiers non suivis conservés. Run163 clos, aucun writer Wallia actif au contrôle de reprise. Résultats conservés : 144 tests backend, RAG réel 11/11, Playwright Python desktop 24/24 et mobile 24/24, build frontend, sondes TLS d'annulation et mémoire. Ces résultats sont en harnais séparés : UI avec faux fournisseur et backend E2E déjà chargé, PAS une preuve de nouvelle image ni de dialogue natif. Nouvelle image, PR/CI finale, HTTPS, persistance/recréation/restauration restent à prouver.

## Routage et bornes

- Un principal unique Astra `openai-codex/gpt-6-astra`, effort `max` vérifié. Il cadre, arbitre, relit les diffs et exerce les validations ; il ne code pas l'application.
- Un seul auteur de code ponctuel DeepSeek `opencode-go/deepseek-v4.1-flash`, effort `max` vérifié via le CLI officiel déjà employé. Aucun profil Builder/Reviewer Wallia, enfant récursif, nouveau job/carte, substitution de modèle ni réglage global de délégation.
- Périmètre du lot DeepSeek : compléter les fichiers existants de livraison, modèles figés et manifestes, scripts, tests et harness natif, web complémentaire selon contrat, et leurs rapports. Pas de lifecycle live, publication, installation root ou action sur d'autres applications par le leaf. Le principal effectue les opérations finales revues via les outils natifs.
- Préserver la borne native de 150 tours du principal et le plafond de carte de 21600 secondes. Donner au leaf un budget borné adapté ; conserver assez de tours au principal pour revue/opérations et checkpoint. Ne pas consommer tout le budget à rejouer les lots déjà acceptés ni confondre fin de budget et réussite.
- Une opération lourde à la fois ; revérifier RAM/disque et les autres travaux actifs. Les recettes restent isolées de l'API vivante. Ne pas lancer une seconde copie des modèles dans son cgroup.

## Travail attendu

1. Inspecter l'implémentation existante et ne compléter que les contrats encore non exécutés. Préserver tous les changements antérieurs. Faire implémenter le lot de livraison unique, puis relire le diff stable et tester les composants modifiés.
2. Image reproductible par SHA avec dépendances verrouillées et modèles/licences/révisions/manifeste requis ; aucune dépendance aux binds backend de développement ou à un téléchargement en inférence. Pas de nettoyage global de Docker.
3. Build/deploy/backup/restore/rollback réels selon `docs/delivery-contracts.md`. Sauvegarde cohérente, restauration isolée vérifiée (DB ET fichiers), rollback prêt. Aucun effacement des données ou secrets existants.
4. Fournisseur applicatif DeepSeek natif DISTINCT d'OpenCode Go : utiliser le résolveur autorisé et un stockage Wallia dédié, jamais les credentials Hermes montés dans l'application. Revalider la route avant de la déclarer active ; ne pas journaliser clés, cookies, mots de passe ou raisonnement privé. Web/vision actifs seulement après preuves réelles, sans masquer l'absence de source du corpus.
5. Commit/PR/CI exact-head, protections requises, publication et image finale ; Nginx/TLS dédiés sous verrou infra et avec rollback, contrôles HTTPS sans `-k`. Tout changement de cycle de vie reste soumis aux approvals natifs : arrêter seulement le chemin refusé et consigner commande/preuve, jamais contourner par un wrapper, un autre outil ou `--yolo`.
6. Recette intégrée sur la NOUVELLE image avec vrais dialogues, provenance/pages FR→EN, limites de version, pièces jointes et état du cas, streaming/arrêt/erreurs, auth et UI desktop/mobile. Vérifier persistance après redémarrage/recréation, restauration isolée, TLS et état des voisins. Les preuves artificielles, les validations locales et les appels natifs restent distingués.
7. Mettre à jour matrice d'acceptation, `docs/state.md`, opérations/rollback et note canonique Obsidian ; exécuter les gates du vault sans prendre possession des modifications étrangères. Rapports utiles conservés dans le dépôt ou dans les pièces jointes durables, jamais uniquement dans le scratch.

## Critères d'acceptation

Application réellement utilisable en HTTPS, commit/image traçables, CI requise verte sur le SHA livré, fonctions du cahier des charges exercées sur la nouvelle image, données/secrets conservés, sauvegarde/restauration et rollback vérifiés, services voisins non dégradés. Compte rendu français avec URL, chemin sécurisé d'accès (pas de secret), tests réellement exécutés, fonctions non actives et limites explicites. Ne pas terminer la carte tant qu'un critère reste non vérifié ; produire un checkpoint honnête si une capacité humaine manque.
