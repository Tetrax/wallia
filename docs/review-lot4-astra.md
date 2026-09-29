# Checkpoint de revue et recette — run163

## Verdict

RAG et corrections applicatives VALIDÉS EN HARNAIS SÉPARÉS. Mission Wallia NON LIVRÉE. Le principal arrive à sa borne de 150 tours ; reprendre la même carte avec un nouveau budget d'exécution, sans nouveau bootstrap ni changement de modèle/approvals. Aucun worker de code ne reste actif.

## Exécutions directes Astra sur le dernier snapshot

- Suite backend isolée : 144 passed, 6 avertissements de dépréciation, 87.81 s, exit0 (`runtime/evidence/lot4-astra-suite.log`).
- RAG E5 + PostgreSQL/pgvector + reranker réels : 11/11, exit0 (`lot4-astra-rag.log`). Question FR → Quick Start EN page2, rang1/logit5.75806 ; aucun résultat commercial, absence/filtres10.9/10.10/99.99, documents non prêts et générations périmées exclus. Calibration indépendante14/14 positifs et14/14 négatifs ; seuil1.1491 inchangé.
- TLS réellement vérifié avec CA éphémère : stop et déconnexion avant en-têtes ferment l'amont en0.108s ; délai6s observé en6.038s. Threads terminés, appel fournisseur suivant réussi. Contrôle HTTP hérité sans suivi de socket reste volontairement rouge : c'est le témoin du défaut, pas le mécanisme livré. DNS/handshake restent bornés par connect_timeout, pas de promesse d'annulation instantanée.
- Mémoire : batch16 E5 et batch8 reranker sur textes longs, deux tours ; cgroup max2097152000octets, peak1457303552octets, oom/oom_kill0. Dans la recette RAG intégrée : peak1631932416octets sous la même borne. Aucun modèle additionnel chargé dans l'API vivante.
- Frontend tsc/Vite : PASS. Repro Markdown : cinq cas PASS. `git diff --check` : exit0.
- Playwright Python réel REJOUÉ par Astra : desktop1440x900 24/24 et mobile390x844 tactile24/24 ; aucune erreur console/HTTP inattendue. Résumé daté2026-09-26T13:13:46.500Z dans `runtime/evidence/astra-e2e/lot4-e2e-summary.json`, journal `runtime/evidence/lot4-astra-e2e.log`. Connexion, erreurs, état du cas, sources/original, PJ/image, stop/retry, Markdown/XSS, courses inter-cas, logout pendant requêtes, bibliothèque/imports, administration, changement/restauration du mot de passe synthétique et suppression contrôlée des seuls cas du harnais passent. Captures dans ce même répertoire ; les captures desktop/mobile du lot ont également été inspectées visuellement par Astra.

## Corrections revues directement

- Reranking CPU local après génération des candidats SQL, identité/modèle/seuil figés et défaillance fermée ; pas de seuil réglé sur le jeu d'acceptance.
- Suivi du nouveau flux TLS renvoyé par httpcore.start_tls ; registre fermable en cas d'annulation concurrente.
- Déclarations ambiguës/douteuses non promues en version certaine, produit explicite normalisé ; tests d'état persisté et de filtre du tour.
- Création tardive ne remplace plus le cas ouvert plus récemment. La recette possède maintenant une vraie barrière et vérifie l'ordre des événements ; l'ancien test ne prouvait pas cette course.
- Attacher Markdown corrigé ; menu mobile accessible hors chat ; attentes/sélecteurs E2E corrigés sans maquiller les erreurs. Le défaut supposé de logout était un sélecteur password ambigu ; la capture montrait bien le retour au login.

## Limites des preuves — ne pas les effacer à la reprise

Les recettes UI utilisent un faux amont et des fixtures de recherche, explicitement affichés : elles prouvent la mécanique de l'interface, pas le chat DeepSeek natif. La pile E2E n'a pas été redémarrée : son backend reste celui chargé à son démarrage ; le front final est servi, tandis que les derniers correctifs backend sont prouvés séparément par la suite isolée, le RAG réel et la sonde TLS. Une recette intégrée sur la nouvelle image reste obligatoire après livraison. Les specs TypeScript historiques n'ont pas été exécutées ; leur installation a été refusée et n'a pas été contournée. Playwright Python déjà installé a été utilisé sans installation.

La cible principale conserve l'image prototype et ses binds backend ; aucune nouvelle image publiée/déployée. Aucun nouveau commit/push/PR dans ce run. Branche feat/prototype, base locale11265a5, bootstrap publié c11842f ; préserver tout le diff et tous les non-suivis. Le compte admin initial n'a pas été réinitialisé. Le compte E2E synthétique est restauré par la recette réussie.

Read-back final : API StartedAt2026-09-26T08:57:16.750788302Z, worker07:00:47.022315679Z, DB2026-09-25T22:22:36.733936174Z ; tous healthy, RestartCount0 et OOMKilledfalse. Healthchecks des voisins observés healthy ; aucun service étranger modifié.

## Reprise exacte

1. Lire ce rapport, la tête de docs/state.md, docs/acceptance.md et le cahier des charges original. Vérifier absence de writer ; dernier lot code20260926_122707_0d1166 / proc_a841a7dc97b2 terminé exit0.
2. Lancer UNIQUEMENT le lot livraison déjà préparé `/home/hermes/.hermes/cache/scratch/wallia-delivery-brief.md`, DeepSeek V4.1 Flash / OpenCode Go / max vérifié, un auteur, budget borné. Ne pas relancer les anciens briefs de correction ; déclaration/HTTPS sont traités.
3. Inclure modèles à révisions immuables et manifestes, limites RAM mesurées ; image sans binds de développement/doubles ; scripts build/deploy/backup/restore/rollback et TLS à faire/revoir. Web Firecrawl borné après RAG validé, harness application native distinct du coding. Aucun lifecycle live par le leaf ; aucun contournement des refus de down/restart/rm ni modification de protections.
4. Publication PR/CI quality exact-head requis, protection main ciblée après identification du check réel, merge/imageSHA, HTTPS vérifié, vrais dialogues et navigateur sur l'image finale, persistance/recréation et restauration isolée, voisins, docs/Obsidian. Aucun de ces critères finaux n'est actuellement acquis.

Modèles de travail conservés : Astra/openai-codex/max pour contrats/revue/replays ; DeepSeek-v4.1-flash/opencode-go/max pour tout code. Aucune sélection automatique d'un modèle supplémentaire.
