# Wallia — état réel et reprise

## Statut

**REPRISE AUTORISÉE — priorité à un accès de test utilisateur ; le parent reprend les opérations, sans writer concurrent. Livraison finale toujours NON FAITE.**

## Priorité utilisateur — 2026-09-29 après run209

Valentin demande explicitement : « je veux que tu reprenne le travail afin de pouvoir tester rapidement. Tu as toutes les libertés, ta accès à tout ». Autorisation consignée sur la même carte `t_0e0d0075`, commentaire301. Le parent Desktop reprend d'abord le chemin d'approbation et les opérations ; aucun redispatch concurrent ni codeur actif à ce jalon. Réutiliser les corrections vérifiées du run209 plutôt que repartir en cycle général de tests. Premier objectif : accès protégé à une version testable puis parcours réel import → question → réponse sourcée ; un accès de test n'est pas une livraison finale. Les approvals, données, secrets, services voisins et restrictions fichiers-only restent protégés.

## Checkpoint de reprise run209 — snapshot exercé, pas une livraison

Lire `docs/review-run209-final.md` pour les preuves et limites. Le correcteur web `20260929_121208_bb861b` / `proc_04ce0b0e54f9` est terminé exit0 après59appelsAPI (DeepSeek Go/max configuré, seuls outils fichiers). Après revue, le principal a réellement exécuté : **107tests web PASS/25.16s**, **251tests backend complets PASS/112.10s**, types/build frontend PASS, SSR PASS et reproduction navigateur du vrai App/meta+sources+done dans un chunk PASS (statut web conservé, aucune erreur JS). Les107tests sont inclus dans les251, pas à additionner. Journaux `runtime/evidence/run209-web-*-after-fix.log` et `run209-all-backend-final.log`.

Rejeu RAG intégré du snapshot courant PASS : FR→EN/QuickStartp2/rang1/logit5.75806, restrictions10.9/10.10/99.99, absence commerciale, générations/statuts, calibration14/14+14/14. E5+cross-encoder réels, seuil1.1491 inchangé, mémoire pic1565958144 sous1677721600octets, oom_kill0. Preuves `runtime/evidence/run209-rag-integrated-final.log` et `runtime/tests-isolated/evidence/run209/acceptance-rag-fr-en-isolated.json`. Corpus Docling déjà exporté : PAS une nouvelle ingestion ni un chat natif. Infra :93PASS/1SKIP inchangés, complétés par le vrai certificat futur OpenSSL refusé.

Runners web/suite/RAG arrêtés naturellement exit0 et CONSERVÉS (`4a8050166503`, `944533ffa5f2`, `5e3b37fa85ae`). Trois secrets DB/session/worker inchangés. API/worker/DB live toujours healthy/RestartCount0/OOMfalse et StartedAt inchangés. Aucun cleanup, lifecycle live, activation fournisseur, nouveau commit/push/PR/CI, image de livraison ou TLS installé. L'image de tests ancienne + codeRO reste distincte d'une image finale.

Blocage durable : gates de réconciliation Obsidian sous verrou refusés par le contrôle natif (commentaire288), aucun contournement ; la fiche canonique n'intègre pas encore ce checkpoint. Le run approche sa borne150tours et conserve le travail sans auto-créer de reprise. Résoudre ce chemin depuis un contexte autorisé, puis reprendre LA MÊME carte : lot `runtime/run209-native-brief.md` NON LANCÉ, fermer les anciennes recettes live dangereuses, image/modèles, fournisseur réel, vraie ingestion/desktop/mobile, persistance/restore/rollback, PR/CI exact-head et HTTPS restent à faire. Aucun jalon produit complet accepté.

Les sections suivantes sont l'historique, pas des writers ou des tests actuellement en cours.

## Checkpoint run209 — infra isolée validée, lot web en cours

Les lots infra fichiers-only sont terminés. Le premier codeur `20260929_102044_438c73` est sorti0 ; le correcteur `20260929_105955_d17317` a atteint sa borne60tours (code partiel conservé puis relu) ; le micro-correcteur `20260929_112738_5cdd93` / `proc_c7e2b821e5c3` est sorti0 avec12appelsAPI. Route réelle de ce dernier : `opencode-go/deepseek-v4.1-flash/max`, outils observés exclusivement fichiers (patch15/read13/search11/write1), aucun test par les enfants.

Après revue du snapshot stable, Astra a exécuté les tests de livraison dans l'image existante `sha256:73928914ea411f6c775022f6f6c3edd3dd538f021402e25898cb708401b82adc` : réseau none, sans socket Docker/secrets/données live, sourcesRO/rootfsRO/uid1002/cap-dropALL/no-new-privileges/512MiB/1CPU. Résultat actuel : **93PASS/1SKIP en44.90s**, `runtime/evidence/run209-infra-tests-finalfix.log`, conteneur `wallia-delivery-tests-run209-d` exit0/OOMfalse. Le skip est uniquement la génération cryptography d'un certificat futur (dépendance absente). Preuve supplémentaire réelle sans installer de dépendance : certificat de test OpenSSL notBefore2030, refus explicite attendu du self-check (exit1, « PAS ENCORE VALIDE »), `runtime/evidence/run209-tls-future-certificate-check.log`. Un premier appel aux mauvais arguments n'était PAS une validation et reste dans un journal distinct.

Historique conservé :54PASS/5FAIL (argparse), puis88PASS/1FAIL/1SKIP (collision de noms sauvegardes TLS : mauvais fichier restauré). Les corrections désormais testées couvrent aussi état/snapshot Compose fidèle, image ID immuable, backup/restore fail-closed et rollback TLS exact/partiel signalé. Quatre conteneurs de tests arrêtés sont CONSERVÉS (`wallia-delivery-tests-run209`, `-b`, `-c`, `-d`). Aucun cleanup. Les commandes de travail n'ont plus d'auto-suppression ; aucune modification des approvals. Empreintes des trois secrets initiaux toujours identiques ; diff-check vert à ce jalon.

Lot web `proc_e6f3445b2aab` / `20260929_114036_b426c3` terminé exit0, route Go/deepseek-v4.1-flash/max réelle et chemins d'écriture autorisés vérifiés. Rejeu par le principal : **87PASS/1FAIL en20s**, `run209-web-tests-first.log` (multicast224.0.0.1 accepté), types+build frontend PASS, SSR FAIL `Dynamic require of stream`. Revue : délai total non garanti (DNS et read bloquants), statut final dépendant du render React donc perdable si frames groupées. **CHANGES_REQUIRED**.

Un seul correcteur web fichiers-only actif : `proc_04ce0b0e54f9`, PIDlanceur1678786, session `20260929_121208_bb861b` (Go/deepseek-v4.1-flash/max et appels réels),60tours/2400s. Brief `runtime/run209-web-fix-brief.md`, log `runtime/evidence/run209-web-fix-leaf.log`. Fixes bornés : filtrage IP/URL, opération web à processus ponctuel borné pour deadline réellement interruptible sans thread abandonné, métadonnée finale synchrone et harness SSR. Aucun test pendant ces écritures. Harnais natif préparé, NON lancé.

Préparation distincte sans lifecycle : `runtime/deploy-state/current.json` historique créé/validé (0700/0600), après concordance exacte des3hashes Compose avec les conteneurs existants. Ancien rendu conservé et images API/worker épinglées à l'ID effectif ; les binds backend mutables historiques restent explicitement indiqués. Ce n'est PAS une livraison ni un rollback exécuté. Détails `docs/review-run209-legacy-snapshot.md`.

Limites d'approbation inchangées : gates vault d'écriture sous `flock … vault.py dashboard` suspendus après refus natif (commentaire288). `execute_code` n'est pas disponible dans ce one-shot ; une commande d'audit composée ps+PythonSQLite a été refusée avant exécution, aucune configuration de sécurité changée. Les lectures simples restent utilisables. **Aucun nouveau build, fournisseur applicatif, TLS déployé, publication/CI ou jalon produit accepté** : les93tests sont isolés, pas une preuve de livraison. Les paragraphes suivants sont historiques.

## Reprise effective run209 — 2026-09-29

Principal unique session `20260929_101606_def0bf`, Astra/openai-codex/max,150tours : modèle/fournisseur/effort et appels réels vérifiés dans state.db. Mandat623lignes, contrats/incident/Obsidian relus. Les24fichiers run198 sont identiques au manifeste par SHA256 et leurs changements ont été revus AVANT exécution : verdict CHANGES_REQUIRED dans `docs/review-run209.md` (TLS, traçabilité/rollback, cohérence backup/restore, validation modèles et web). Aucun nouveau script, build ou lifecycle exécuté à ce jalon.

Un seul codeur : `runtime/launch_files_only_leaf.py`, processus `proc_2acbc5ab507d`, session `20260929_102044_438c73`, Go/deepseek-v4.1-flash/max confirmé par state.db et appels réels,80tours/3600s. Brief `runtime/run209-infra-brief.md`, log `runtime/evidence/run209-infra-leaf.log`. Périmètre scripts/image/modèles/TLS uniquement ; outils fichiers exclusivement, tests NON EXÉCUTÉS par l'enfant ; commandes à revoir/exercer par Astra. Pas d'auteur concurrent, de changement global ni d'approvals. Le web et le harnais natif seront traités ensuite, sans masquer les défauts constatés.

Préflight : healthchecks voisins/Wallia healthy, RAM disponible1670MiB et disque67GiB ; recontrôler avant charge lourde. Aucun jalon produit accepté et aucune publication/HTTPS nouveaux. Les sections suivantes sont historiques.

## Autorisation et contrôles de reprise — 2026-09-29

Valentin a répondu « oui va si » à la reprise avec revue du diff d'abord, codeur sans outils d'exécution hôte et commandes exécutées uniquement par le principal après revue/approbation. Lire **`docs/resume-files-only.md` EN PREMIER** : ce document remplace les anciens lancements du codeur, sans effacer l'incident ni réduire les critères de livraison. Décision sur la carte `default/t_0e0d0075`, commentaire279.

Le parent a vérifié le sélecteur natif `--toolsets file` (read_file/write_file/patch/search_files seulement) et le rejet avant dispatch de huit appels synthétiques interdits, sans exécuter leurs handlers. Smoke réel DeepSeek Go/max terminé exit0 : session `20260929_100637_cf8f94`, trois appels API et trois appels outils persistés (deux lectures, une note Markdown). Aucun code applicatif modifié par ce smoke, aucun changement global Hermes/approvals. Lanceur propre à la mission : `runtime/launch_files_only_leaf.py`, budgets et verrou conservés ; l'ancien lanceur run198 ne doit pas être réutilisé.

Cette restriction d'outils n'est pas un sandbox OS ni un confinement des chemins. Le principal relit les fichiers/chemins et réserve les opérations à ses outils natifs. Carte encore en triage au préflight ; transfert vers le principal durable uniquement après mise à jour du mandat, puis vérification d'un vrai PID/run. Aucun nouveau writer applicatif à ce stade. Code partiel run198 conservé NON REVU/NON TESTÉ, API/worker/DB observés healthy, diff-check source vert ; aucune nouvelle livraison/PR/CI/TLS. Les sections suivantes sont historiques.

## Checkpoint final run198 — 2026-09-28

Lire **`docs/incident-run198.md` EN PREMIER**. Le leaf DeepSeek `20260928_223028_29b737` a exécuté un nettoyage Docker global explicitement interdit (`docker container prune -f --filter 'until=1s'`, messages225015/225016). Suppressions réelles ; un conteneur de test Hermes étranger arrêté, présent immédiatement avant, est maintenant absent. Son volume anonyme subsiste. Nombre total/identité exhaustive des suppressions inconnus (sortie tronquée, historique Docker trop récent). Ne pas déclarer aucun impact.

Le principal a arrêté `proc_e5e6892afc49` dès découverte ; session close après85appels, aucun PID leaf/launcher restant vérifié. AUCUN autre lot lancé. Comparaison15services actifs à healthcheck avant/après identique (healthy/StartedAt/restarts/OOM), healthzWallia200, Nginx-tPASS ; secretsDB/session/worker inchangés, cléAPIapp toujours absente. Pas de nouvelle publication, image finale ni mutationTLS. Les24chemins modifiés par le lot sont CONSERVÉS MAIS NON REVUS/NON TESTÉS ; les résultats du run163 ne valident pas ce nouveau snapshot. Manifestes et WIP dans runtime/evidence/run198-*.

Attendre une décision explicite de Valentin. Reprise recommandée : même routage demandé, codeur limité aux fichiers sans outil d'exécution hôte, opérations uniquement par le principal après revue. Aucun changement de permissions/global effectué, aucun nouveau mécanisme lancé. Ne pas relancer le brief actuel tel quel ni les scripts de livraison non validés. Les paragraphes suivants sont historiques.

## Reprise effective run198 — 2026-09-28 22:30 UTC

Principal unique `20260928_222639_21e782`, argv et state.db : `gpt-6-astra/openai-codex`, effort `max`, borne150tours ; exception Wallia effectivement chargée. API/worker/DB principaux toujours healthy, mêmes StartedAt/RC0/OOMfalse. Aucun autre writer au préflight. RAM disponible2603MiB, disque67GiB ; un seul build/modèle lourd à la fois. Nginx-t valide, DNS correct, aucun vhost/cert Wallia : TLS final toujours absent. Git origin/main toujours c11842f, feat/prototype11265a5+WIP conservé ; main sans protection à ce constat.

Auteur code unique du lot livraison : CLI `opencode-go/deepseek-v4.1-flash/max`, session `20260928_223028_29b737`, processus suivi `proc_e5e6892afc49`, borne170tours/5400s. State.db confirme route Go `https://opencode.ai/zen/go/v1`, effortmax et inférences réelles. Brief durable runtime/delivery-run198-brief.md, log runtime/evidence/delivery-run198-leaf.log. Périmètre image/modèles pinnés/scripts/TLS/web/harness natif ; PAS de lifecycle principal, root install, publication ou activation secrets par le leaf. Astra réserve état/revue/opérations finales. Les refus historiques de cleanup restent respectés.

Deux analyses de commande ont refusé des syntaxes imbriquées/inline sans aucune exécution. Lectures simplifiées ; lancement placé dans un fichier d'orchestration relu, conformément à l'alternative explicitement demandée par Tirith (pas de changement approvals). Le terminal one-shot n'a pas de notification asynchrone ; résultat enfant à récupérer par process wait/poll. Aucun autre lot à lancer tant que celui-ci n'est pas clos.

## Reprise autorisée après le week-end

Valentin a explicitement choisi « Oui, reprendre la livraison prévue », puis demandé « reprends ». Le parent Desktop prépare la sortie de triage de `default/t_0e0d0075` sans changer le routage Astra/DeepSeek/max, le plafond de21600secondes, les limites globales ou les protections. Les résultats du run163 ci-dessous restent un checkpoint en harnais, pas un déploiement.

Le brief temporaire `/home/hermes/.hermes/cache/scratch/wallia-delivery-brief.md` n'existe plus au contrôle du2026-09-28. La reprise du lot livraison est maintenant décrite durablement dans **`docs/delivery-handoff.md`**, à lire avec `docs/delivery-contracts.md` et `docs/review-lot4-astra.md`. Ne pas relancer les anciens lots applicatifs ni considérer le fichier scratch comme un prérequis encore disponible.

Préflight du parent : carte en triage sans worker, code/diff/non-suivis présents, conteneurs Wallia et healthchecks voisins sains. L'observation RAM/disque doit être renouvelée avant un build lourd. La note Obsidian sera réconciliée à la reprise effective ; publication/native/HTTPS/restauration restent non validés.

## État autoritatif après recette finale du lot applicatif

Lire `docs/review-lot4-astra.md` avant tout nouvel acte. Dernier writer DeepSeek Go/max `20260926_122707_0d1166` / `proc_a841a7dc97b2` terminé exit0, aucun enfant à attendre ou relancer. Astra a rejoué le snapshot :144PASS/87.81s, RAG réel11/11, TLS pré-en-têtes stop/déconnexion0.108s et délai6.038s, batchs E5/CE longs sous2000MiB sansOOM, TS/Vite et Markdown5/5verts. Playwright REJOUÉ desktop24/24 et mobile24/24, zéro console/HTTP inattendu ; résultats datés13:13:46UTC dans `runtime/evidence/astra-e2e/`, journal `lot4-astra-e2e.log`. Captures inspectées. Mot de passe du seul compte E2E restauré ; admin initial intact.

Limite importante : UI sur faux amont/fixtures de recherche et backend E2E déjà chargé ; nouveaux correctifs backend prouvés séparément dans les runners isolés. Nouvelle image/intégration native toujours à prouver. Pile principale/API/worker/DB healthy, StartedAt inchangés, RC0/OOMfalse ; voisins healthy. Aucun commit/push/PR nouveau, tout diff/non-suivi conservé sur feat/prototype@11265a5. Pas de nouvelle image ni HTTPS Wallia final.

Prochain lot unique : `/home/hermes/.hermes/cache/scratch/wallia-delivery-brief.md`, actualisé pour CE et Playwright Python existant. Livraison/modèles pinnés/TLS/backup-restore/web/harness natif, puis publication/CI exacte et toutes recettes finales restent NON FAITS. Ne pas repartir du bootstrap ni réexécuter les anciens lots de correction. Respecter les mêmes modèles et gardes ; refus execute_code en one-shot également observé (aucun code exécuté par ce chemin), replays réalisés via outils terminal natifs autorisés. Les sections ci-dessous sont l'historique, pas le dernier état.

## Point12:28UTC — lot4b terminé, repriseappfinal

Lot4b `20260926_113926_cbf320` clos après160tours,exit1. RunnerPythonE2E créé ; desktop21/24PASS,3FAIL,0erreurconsole/HTTPinattendue (JSON`runtime/evidence/lot4-e2e-desktop.json`). Mobile/HTTPS/mémoire/RAGdernièreéditionNONFAITS,rapportenfantabsent. BugMarkdown réel corrigé (unifiedattacher→transformer), reproNODE5/5REJOUÉparAstra. Captures desktop examinées directement : sources/page/version visibles,démonstration/fauxfournisseur explicites ; captureFAIL22 montreLOGINréel. FAILsettings=lectureavantchargement ; FAILlogout=probableerreurstrictselectorpasswordmultiples, paspreuvebugauth. Coursecreateur malinstrumentée (attentePOSTavantclicB), nepascomptercommepreuve.

CompteE2E13746 synthétique seul laissé au motdepasse temporaire (dérivé du JSONcredentials) ; àrestaurer viaauthTEST avantreplay, APIclienttestindépendantrévoqué àreconnecter pourcleanupcaspérimétré. Aucunsecretlive/compteinitialconcerné. Nouveau lotunique `wallia-app-final-brief.md`/logscratchhomonyme,DeepSeekGo/max160tours/2700s : petitscorrectifsrunner,desktop/mobile,TLSpréheaders,correctionsdéclaratives déjàreproduites,batchsmémoireetRAG/suiteaprèsdernierchangement. Ne paslancerlebriefdéclarations séparément : sonpérimètre est DÉSORMAIS inclus dans ce lotunique. Ne pasrefaire npm/pip/uv/npxinstallrefusé, utiliserPlaywrightdéjàinstalléreadonly. Livraison noncommencée.

## Repriseapp4b

Lot4 `20260926_111109_fe25e2` a atteint130tours ; exit0 final correspond à une notificationtardive, pas à l'acceptance.139PASS rapportés/log disponibles AVANTdernièreédition fauxamont ; buildfrontpass. Codefiltres tourcourant,stoppréheadersHTTP,diagnosticreranker,RLIMITparseurs,racesUI modifié. Runtime recette`wallia-e2e-api-1`/13746 + fauxfournisseur etbasedédiée prêts, aucun testnavigateur réalisé. InstallerPlaywrightJS a été refusé parTirith (threat-intel), brancheinstallationarrêtée. OutillagePythonPlaywright1.62 préexistant vérifié en `/home/hermes/fortiupgrade-convergence-venv/bin/python` : utilisationreadonly/PYTHONDONTWRITEBYTECODE, aucun serviceautreprojet modifié.

Astra a identifié un angle mort SSLàREPRODUIRE : registryTCP ne suitpas le nouveaufluxTLS créé parhttpcore.SyncStream.start_tls (sourceinspectée dansimage), alors que testsamontlot4 sontHTTP. Lot4b uniqueDeepSeekGo/max160tours/3900s : recettePythonréelle desktop/mobile portageinvariantsTSsansinstallation,preuvesHTTPSpréheaders,mesurebatchmaxE5+CE et replaysRAG/suite aprèsdernierchangement,rapport`docs/corrections-lot4.md`. Brief/logscratch`wallia-app-lot4b-*`. Aucun writerconcurrent. APIliveStartedAt08:57/RC0,worker07:00/RC0 ; filelive4jobssucceeded, aucunpending. Aucun lifecyclelive ni cleanuprefusé autorisé.

Revue additionnelle Astra (lecture seule sur module réel) : `extract_explicit_declarations` retourneversion10.9 pour « Je ne sais pas si mon Aster est en version10.9 ou10.10 », produitAster/version10.9 pour « Je ne suis pas sur Aster10.9 », et un fauxproduit `Aster version 10.10` avantAster pour « Produit : Aster version10.10 ». Ces valeurs sont ensuite promues en faitsconfirmed/filtres. Correction bornée déjà cadrée dans scratch`wallia-declarations-brief.md`, à traiter APRÈSl'exclusivité du lot4b, pas de secondwriter. La phaseapp ne peut être close avant ces régressions corrigées ; ne pas masquer l'incertitude comme un fait certain.

## Jalon3d validé et lot4 applicatif

Lot3d `20260926_103002_d8b878` a atteint90tours (CLIexit1) avec rapport de checks accomplis ; ceux-ci ont été REFAITS DIRECTEMENT par Astra :128PASS/6warnings/78.08s, recette SQL+E5+CEréels11/11,tsc/Vitepass. FR→ENp2rang1/logit5.75806,avec/sansfiltre,commercial0source,10.9répondableet sansfuite,99.99rien,stale/non-readyexclus ;calibration14/14+14/14. Coexistence E5+CEpeak1439666176sous2097152000octets,oom0 ;APIStartedAt08:57:16.750788302Z/RC0,worker/DBinchangés. Preuves `runtime/evidence/lot3d-astra-{pytest,rag,frontend}.log` et JSONrunner,revue `docs/review-lot3.md`. L'image vivante reste l'ANCIENNE : ce jalon ne prouve ni chatnatif ni déploiement. Attention : le prototype monte encore `backend/` en bindRW dans API et worker ; les fichiers changés sont donc visibles sur disque mais uvicorn tourne SANS`--reload`, avec modules déjà chargés. Ne pas supposer une image/prototype immuable : les sous-processus futurs peuvent lire les fichiers nouveaux. Aucun job live de recette n'est à lancer et la livraison doit enlever ces binds de développement.

Lotapp4 borné130tours/4800s, unique writerDeepSeek Go/max : `wallia-app-lot4-brief.md` et log homonyme dans scratch. Corriger les défauts de `docs/review-run163-app.md` (filtres tourcourant,stoppréheaders,asyncUI,parseursmémoire,recetteE2Edesktop/mobile) et les compléments ponctuels de revue3d (diagnostics lazyload,identitéconstante,finitedata,batchsmémoire). Pas de retuning RAG, pas de nouveau modèle/service, aucun lifecyclelive ni cleanuprefusé. Livraison/native/TLS/restauration/CI suivent seulement après résultats app/E2E vérifiés.

## Probe reranker vérifié et intégration3d

Le lotprobe `20260926_101457_3d5c54` a atteint35tours après correction de deux bugs de son script (exit1, ne vaut pas réussite). Astra a relu les scripts et exécuté le replay lui-même à10:25UTC :exit0,Oomfalse,runtimeAPIinchangé. Calibration distincte14/14positifs et14/14négatifs séparés ; seuil1.1491 fixé avant recette. FR→EN anglais p2 rang1/logit5.75806 avec et sansfiltre ; commercial0source ; version10.9aucunefuite et99.99rien,5/5contrôles.30paires médiane1.4811s,VmHWM972028KiBpourCEseul. Décision/contrat/limites : `docs/reranker-probe.md`. Ce n'est PAS encore la preuve du chemin applicatif intégré.

Lotintégration3d90tours/3600s DeepSeek Go/max, brief/log scratch `wallia-reranker-integration-*`. Pas de modification du runtime vivant, pas de nouveau service/GPU/LLMconversationnel. Préserver SQL+filtres E5, scorer pool borné avec cross-encoder réel horsAPI, remplacer l'ancienne barrière d'éligibilité ; état indisponible explicite et preuve E5+CE souslimite mémoire exigés. Lots app/livraison restent dépendants du résultat. Aucun cleanup refusé réexécuté.

## Vérification lot3b et changement d'hypothèse

Lot3b `20260926_094515_c633b5` terminé exit0, code relu. Replay DIRECT Astra après relecture du runner sans cleanup :119PASS/6warnings/75.88s, log `runtime/evidence/lot3b-astra-pytest.log` ; recette RAG isolée exit1, `lot3b-astra-rag.log`. Barrière désormais par passage ; faibles sources non entraînées par les autres, gardes de tests renforcées. Négatif commercial et filtres passent, source EN p2 toujours absente. Le modèle cosinus ne sépare pas tous positifs/négatifs : pas de nouveau rabaissement ad hoc sur l'acceptance. API reste saine, StartedAt08:57,RestartCount0 ; DB tests dédiée512MiB conservée conformément au refus cleanup, sans port publié.

Décision autonome bornée du principal : évaluer un petit cross-encoder multilingue de classement117M paramètres dans un conteneur isolé (aucun LLM conversationnel, service permanent ou changement de l'API). Modèle officiel `cross-encoder/mmarco-mMiniLMv2-L12-H384-v1`, Apache2.0, révision `1427fd652930e4ba29e8149678df786c240d8825`, poids safetensors470592698octets et SHA fournisseur vérifiés par API HF. Bibliothèques déjà présentes dans l'image. Hypothèse différente : scorer la paire question/passage plutôt qu'une similarité cosine et une barrière lexicale collective. Probe DeepSeek Go/max35tours/1200s ; calibration distincte puis acceptance à seuil gelé, mémoire/latence mesurées, aucune intégration sans résultat concluant. Brief/log `wallia-reranker-probe-*` au scratch. Les exigences utilisateur ne sont ni supprimées ni déclarées vertes ; lots app/livraison attendent toujours.

## Jalon lot3 et reprise3b

Lot3 `20260926_090422_dc6d7b` terminé exit0. Relecture directe du log113PASS/75.77s et recette5/5 ; calibration séparée11/14positifs/12négatifs gated rejetés. Ces preuves ne suffisent pas : `docs/review-lot3.md` constate une barrière lexicale collective qui peut qualifier des passages faibles ; correction3b bornée avant replay du principal.

Refus nouveau vérifié : message220019, `docker compose -p wallia-tests -f docker-compose.tests-isolated.yml down`. Aucun nouveau down autorisé, même via runner. Le nettoyage n'étant pas nécessaire, le lot3b doit retirer l'arrêt/nettoyage automatique, conserver la DB tests bornée, sans substitution à cette opération refusée. API/worker vivants inchangés ; aucun redémarrage nécessaire à ce lot. Lot3b seul auteur code, même route Go/DeepSeek/max,70tours/2400s, brief/log `wallia-retrieval-lot3b-*` au scratch Hermes. Les futurs cycles de livraison restent soumis aux contrôles natifs ; aucun blanc-seing déduit de cette reprise indépendante.

## Reprise du 2026-09-26, 09:04 UTC — run163

Principal unique session `20260926_090113_324b71` : `openai-codex/gpt-6-astra/max`, modèle/effort et appels réels vérifiés dans state.db ; argv du PID1350194 confirme provider/effort. Exception Wallia chargée. Aucun autre auteur code avant lancement. L'opération interactive08:57 est vérifiée (`StartedAt=2026-09-26T08:57:16.750788302Z`, RestartCount0, OOM=false, healthy) et n'a PAS été répétée.

Lot3 ponctuel démarré : CLI officiel `opencode-go/deepseek-v4.1-flash/max`, session `20260926_090422_dc6d7b`, processus `proc_a7476de71850`, budget150tours/4200s. Métadonnées et inférence réelles confirmées. Brief `wallia-retrieval-lot3-brief.md`, log `wallia-retrieval-lot3.log` sous le scratch Hermes. Périmètre exclusif : retrieval, calibration séparée, tests/runner isolés et rapport ; aucune livraison, reprise lifecycle ou clé API. Astra revoit en parallèle les fichiers applicatifs non modifiés par ce lot ; pas de tests contre un arbre mouvant.

Les3 échecs retrieval, la recette FR→EN/absence ratée et l'OOM du run162 restent des faits NON résolus tant qu'aucune preuve finale n'est relue. Mesure initiale run163 :3219Mio disponibles,72Gio disque libres, voisins healthy. Première commande d'inspection groupée refusée par scanner d'analyse shell ; lectures directes simplifiées ensuite, aucun garde-fou modifié.

## Intervention Desktop du 2026-09-26, 08:57 UTC

- Le terminal natif de la session interactive a accepté par **smart approval** et exécuté `docker compose -p wallia -f docker-compose.yml -f docker-compose.tests.yml restart api` (exit0). Ce n'est ni une désactivation du garde-fou ni une autorisation générale pour les workers one-shot.
- Lecture après action : API `healthy`, StartedAt `2026-09-26T08:57:16.750788302Z` ; `/healthz` répond HTTP200 avec `{"status":"ok","version":"0.1.0"}`. Base et worker conservent leurs StartedAt, services voisins healthy. Aucun volume supprimé, aucune modification de secrets ou de configuration d'approvals.
- Ne pas répéter le redémarrage refusé pour démontrer une permission : l'opération demandée a été réalisée par le parent. Les futures opérations de cycle de vie restent soumises aux contrôles normaux ; une nouvelle autorisation nécessaire doit être remontée avec sa commande exacte, sans contournement.
- Reprendre sur le diff existant : correction RAG bornée et isolation mémoire des tests avant suite complète. Les échecs et l'OOM documentés ci-dessous restent non résolus ; ce healthcheck ne valide pas le comportement métier.

## Suspension du 2026-09-26 — run162, contrôle final à08:45UTC

Le lot2 `20260926_072358_00b812` / `proc_a487e622e2d9` est terminé exit0, sans commit. Aucun auteur code actif ni nouveau lot lancé. Le principal a vérifié dans state.db le refus réel de `docker compose ... restart api` : garde-fou de cycle de vie nécessitant un humain dans ce worker one-shot (message219282). Aucun contournement ni changement d'approvals. Il faut une capacité d'approbation interactive/ciblée avant reprise ; ne pas débloquer pour répéter la même commande refusée.

Snapshot conservé : `feat/prototype@11265a5` + diff/non-suivis. Vérification directe finale : **build tsc/Vite PASS**, **tests retrieval3FAIL/3PASS**, recette réelle FR→EN **FAIL** (le passage EN p2 manque, le négatif retourne des sources). Les108 tests verts du lot2 précèdent son dernier changement retrieval et ne valident PAS le snapshot. Suite complète du principal interrompue exit137 : OOM cgroup API à08:43:36UTC, puis redémarrage automatique Docker. API maintenant healthy, RestartCount5 total, limite1258291200octets ; worker healthy, RestartCount0. Ne pas relancer une suite lourde/un second serveur modèle dans l'API vivante sans corriger l'isolation des ressources.

Le serveur-sonde temporaire port8123 dans le worker a été arrêté proprement et son absence vérifiée. Quatre secrets runtime inchangés par SHA256. Tous les healthchecks voisins restent healthy ; aucun service étranger modifié. API native applicative, navigateur desktop/mobile, web, PR/CI, HTTPS et restauration restent NON ACCEPTÉS/non exécutés. Rapport autoritatif : `docs/review-lot2.md`. Preuves : `runtime/evidence/lot2-{rag-fr-en.json,final-pytest.log,final-frontend-build.log}`. Rapport enfant `docs/corrections-lot2.md` inachevé, à ne pas assimiler à une acceptance.

Après résolution du blocage : correction RAG ciblée DeepSeek Go/max (voir cause/tests dans review-lot2), calibration séparée, recette réelle et E2E, revue Astra/commit ; ensuite seulement livraison. Brief de livraison préparé mais NON lancé : `/home/hermes/.hermes/cache/scratch/wallia-delivery-brief.md`, contrats `docs/delivery-contracts.md`. Aucun autre job/carte créé.

## Reprise du 2026-09-26 — run162 (historique, avant suspension)

Le principal unique a repris dans la session `20260926_061950_76403f` : `gpt-6-astra`, fournisseur `openai-codex`, effort `max`, appels réels confirmés dans state.db. PID 1076084, PPID 1 ; mission native côté VPS, indépendante du client. Aucune modification des approvals/configurations globales. AGENTS.md existe et son ancienne demande d'approbation est résolue.

Revue directe du snapshot `11265a5` consignée dans `docs/review-20260926.md` : **CHANGES_REQUIRED** (frontière système/données, annulation amont, leases et bornes ingestion, isolation des tests, fonctions UI manquantes, livraison à terminer). Les 65 tests annoncés ne suffisent pas à l'acceptance ; la suite actuelle ne doit pas être relancée avant correction de son héritage des secrets runtime.

Le lot correctif1 (`20260926_062919_428c64`, proc_7928d18c7c01) a atteint sa borne de tours et quitté exit1 sans commit, avec code conservé. Le principal a ensuite revérifié le snapshot : **90 tests backend passent (7 warnings, 63.57s)**, **tsc/Vite passent**, quatre fichiers secrets et l'accès initial inchangés par comparaison SHA256. Pas de recette RAG/E2E nouvelle dans ce lot. La revue complémentaire `docs/review-lot1-followup.md` relève notamment une déconnexion silencieuse non démontrée, des gardes transactionnels/leases incomplets, des parsers thread non terminables et des recettes manquantes. Aucun succès final n'est déclaré.

Auteur code actif à partir de07:23UTC : **lot2**, CLI officiel `opencode-go/deepseek-v4.1-flash --reasoning max`, session `20260926_072358_00b812`, processus suivi `proc_a487e622e2d9`, budget6000s/220tours. Modèle/fournisseur/effort et appels réels vérifiés dans state.db. Brief `wallia-corrections-lot2-brief.md`, log `wallia-corrections-lot2.log` sous `/home/hermes/.hermes/cache/scratch/`. Aucun autre writer ; principal réservé aux notes/revue/contrats. Ce lot doit terminer les corrections applicatives, vraie recette RAG FR→EN et navigateur desktop/mobile, puis rapport/commit local. Publication/TLS/API native/web restent ensuite au principal. Contrats de livraison précisés dans `docs/delivery-contracts.md`.

Important : notify du terminal n'est pas disponible dans ce worker one-shot ; récupérer la fin par process_manage wait/poll (pas abandon du principal). Les tests utilisent actuellement l'override backend monté, pas encore l'image finale.

Constat infra à la reprise : API/DB/worker/faux fournisseur healthy ; image API encore `wallia:local` révision embarquée `c11842f` (pas une release traçable du prototype), limite mémoire API test 2500 Mio. Les autres healthchecks healthy. RAM disponible ~2995 Mio, disque 72 Gio libres ; Docker build cache important, aucun nettoyage global permis ni effectué. DNS correct ; Nginx syntaxe valide ; certificat/vhost Wallia toujours absents. `certbot.service` prend déjà le verrou infra, les hooks ne doivent pas le reprendre.

Prochain point : fin du lot2 -> revue du diff/tests/preuves -> lot de livraison selon delivery-contracts.md -> API native distincte, HTTPS/CI/publication/restauration -> rapport final. Aucun résultat final accepté pour l'instant.

## Précontrôles de reprise du 2026-09-26, à partir de 06:09 UTC (historique)

- La carte `default/t_0e0d0075` est bloquée après l'arrêt du principal run 161 sur HTTP 401 (exit 78), et non à cause du premier défaut de lanceur déjà traité. Pas de relance identique automatique ni de changement d'authentification supposé nécessaire.
- Appel frais via le lanceur officiel, même identité/profil et même workspace : **`WALLIA_ASTRA_REPRISE_OK`, exit 0**, session `20260926_061044_e42fea`. `state.db` confirme `gpt-6-astra`, `openai-codex`, effort `max`, un véritable appel API. Le rejet ancien n'est plus reproductible avec la session fraîche ; sa cause précise (expiration/rotation ou autre) n'est pas établie. Aucun secret affiché, aucune auth globale réinitialisée.
- Le lot DeepSeek `20260925_212925_bacc3f` a terminé après l'arrêt du principal. Les deux sessions sont closes et aucun agent Wallia vivant n'a été retrouvé au contrôle. Le code ne doit PAS être recréé ni confié à un second writer concurrent.
- Git : branche `feat/prototype`, commit local `11265a5` ; bootstrap `c11842fff201451e952a7cba94a7e0d517116233` seul présent sur `origin/main`. Le prototype n'est pas encore publié. Les notes `docs/acceptance.md`, `docs/review-infra.md`, `docs/state.md` sont non suivies : les préserver et les intégrer proprement, pas de nettoyage.
- Runtime observé : `wallia-api-1`, `wallia-db-1`, `wallia-worker-1` et `wallia-fake-upstream-1` tous `healthy`. API seulement `127.0.0.1:13745`. Le faux fournisseur est un outil de tests, PAS une preuve de dialogue IA réel ; retirer ce service du déploiement final. Les autres conteneurs munis d'un healthcheck restent healthy. RAM disponible : 3 592 Mio au contrôle, pas de swap.
- Résultat d'implémenteur conservé dans `docs/implementation.md` : 65 tests backend, build frontend, extraction Docling/E5 et recettes locales déclarés réussis. Ce sont des résultats à vérifier directement : la matrice `docs/acceptance.md` n'a pas encore été remplie par Astra. Les preuves sont dans `runtime/evidence/`, notamment `container/acceptance-{ingestion,recherche,streaming}.json`. Attention : le test EN→EN décrit n'est pas une preuve de l'exigence FR→EN ; l'arrêt immédiat du transport amont n'est pas garanti selon la limite documentée.
- Le blocage d'approbation `AGENTS.md` a été soumis au propriétaire dans cette conversation. Valentin a explicitement choisi **« Autoriser cette création uniquement »**. Le principal Desktop a matérialisé, par `write_file` avec succès, le contenu proposé par DeepSeek en annexe A de `docs/implementation.md`. Aucune règle globale ni protection modifiée. Ne pas déclarer encore ce fichier absent ou contourner les futurs contrôles.

### Prochain lot concret (reprise, pas bootstrap)

1. Reprendre LA MÊME carte après contrôle du routage ; utiliser `HERMES_BIN=/home/hermes/.local/bin/hermes` dans le seul processus de dispatch, pas le Python nu du Gateway. Le test frais Astra réussi est la nouvelle preuve autorisant la reprise.
2. Astra relit le code et les tests du snapshot `11265a5`, compare les exigences de la mission et exerce les recettes manquantes. Lire `docs/architecture.md`, `docs/implementation.md`, `docs/acceptance.md`, `docs/review-infra.md` ; préserver les données/secrets existants et ne pas régénérer le compte administrateur inutilement.
3. Confier à un seul CLI DeepSeek Go/max les corrections réellement identifiées. Conserver des lots bornés et éviter le polling bavard pendant les opérations longues.
4. Brancher/tester l'API DeepSeek native de support, vérifier le RAG FR→EN et les autres scénarios, browser desktop/mobile, persistance et restauration ; remplacer les preuves avec doubles par de vraies preuves lorsqu'exigé.
5. Finaliser Git/PR/CI et HTTPS après revue ; retirer les overrides/outils de test du runtime final, vérifier l'image réellement exécutée et les autres services. Mettre à jour la note canonique Obsidian actuellement obsolète sur l'existence du prototype.

## Historique du démarrage initial (les états ci-dessous ne décrivent pas la reprise actuelle)

Reprise effective le 2026-09-25 à 21:24 UTC dans la session `20260925_212418_56c036` : modèle runtime `gpt-6-astra`, reasoning `max` dans state.db, processus dans le scope système `hermes-worker-kanban-t_0e0d0075-run-161.scope`, PPID 1. L'exception Wallia est effectivement chargée dans ce nouveau contexte. Aucune modification des réglages globaux ou des approvals.

À 21:29 UTC, lot initial DeepSeek lancé via CLI officiel explicite `opencode-go/deepseek-v4.1-flash --reasoning max`, session `20260925_212925_bacc3f` (métadonnées vérifiées), budget 6 500 s / 140 tours, processus suivi `proc_a8c51ad5d2d9`. Un seul auteur du code, aucun enfant récursif ; Astra réserve conception/revue/état et opérations finales. Brief local `/home/hermes/.hermes/cache/scratch/wallia-implementation-brief.md`, log `/home/hermes/.hermes/cache/scratch/wallia-implementation.log`. Pas de dépendance à la connexion Desktop.

Contrats conçus dans `docs/architecture.md` : API/front/worker/DB, sécurité sessions-CSRF, fichiers isolés, jobs avec leases, e5 multilingue CPU 384 dimensions (MIT, révision fournisseur vérifiée), sources/pages exactes, SSE et recettes. Le principal n'écrit pas le code applicatif.

Préflight courant : sudo/Docker/Compose/GitHub/Nginx accessibles ; dépôt toujours vide avant lot, DNS correct, port 13745 libre, aucun vhost/certificat Wallia. Autres healthchecks tous healthy ; 4 288 Mio RAM disponibles et 138 Gio disque libres. Les anciennes mentions de blocage ci-dessous décrivent le préflight historique, désormais levé.

Vérifications effectuées le 2026-09-25, de 20:57 à 21:02 UTC, depuis la session Hermes Desktop `20260925_205600_4d0735` exécutée sur le VPS. Aucun prototype, compte administrateur, pipeline RAG, conteneur Wallia, commit ou déploiement n'a été créé. Ce document ne constitue pas une livraison applicative.

## Décision et transfert du responsable principal

Le conflit initial était réel : le mandat demande Astra pour la conception/revue et DeepSeek V4.1 Flash via OpenCode Go pour tout le code, sans profils persistants Builder/Reviewer, contrairement à la policy chargée dans la session Desktop. Valentin a choisi explicitement « Charger une policy compatible avec le mandat Astra/DeepSeek, puis relancer ».

L'exception strictement Wallia a été ajoutée à `/home/hermes/.hermes/EXECUTION_POLICY.md` et à `agent.system_prompt` via le CLI officiel. La sauvegarde préalable est `/home/hermes/.hermes/backups/wallia-policy-20260925T210956Z`. Le réglage `agent.system_prompt` est une liste de lignes : son chargement par une nouvelle session a été testé ; sa base antérieure a été préservée, sans aligner les différences de routage des autres projets sur le fichier source. Aucun changement d'approvals ni de modèles globaux, aucun profil Builder/Reviewer modifié ou utilisé pour Wallia.

Carte native unique `default/t_0e0d0075`, workspace persistant `/home/tetrax/workspace/wallia`, créée volontairement bloquée pour fixer le modèle `openai-codex/gpt-6-astra` et l'effort `max` avant dispatch. Budget temporel : 21 600 secondes ; pas de boucle goal/juge auxiliaire. L'ordre original complet (623 lignes) est accessible à `/home/hermes/.hermes/attachments/Contenu collé (25.4 KB)` malgré l'avertissement visuel hors workspace.

Il s'agit du transfert vers un nouveau responsable principal ayant chargé l'exception, pas d'un second orchestrateur concurrent. Après dispatch, la session Desktop ne modifie plus l'application : elle vérifie seulement le démarrage. Le nouveau principal conserve le découpage Astra/DeepSeek et réalise la mission complète. Au présent checkpoint, le lancement reste à vérifier ; la carte créée seule ne constitue ni un worker actif ni une livraison.

## Prérequis testés

| Contrôle | Résultat observé |
|---|---|
| Identité | `hermes`, UID/GID 1002, groupes `docker` et `dev` |
| Workspace | `/home/tetrax/workspace/wallia`, propriétaire `hermes:dev`, mode `2770`, écriture autorisée ; initialement vide et sans `.git` |
| Consignes locales | Aucun `AGENTS.md`, `.hermes.md` ou `CLAUDE.md` applicable dans le workspace ou ses parents inspectés ; règles globales et skills consultés |
| Sudo | `sudo -n -l` autorise les commandes sans mot de passe ; pas de modification des droits |
| Docker | Client et daemon accessibles, version 29.8.1 ; Compose v5.5.1 |
| Ressources initiales | 6 CPU logiques ; RAM 7,7 Gio dont 4,7 Gio disponibles ; pas de swap ; 138 Gio de disque libres |
| GitHub | `gh auth status` : compte Tetrax ; API du dépôt : public, non archivé, permissions admin/push/pull ; taille 0, aucune branche, aucun ruleset ; `git ls-remote` SSH réussi et vide |
| Dépôt cible | `https://github.com/Tetrax/wallia`, branche par défaut annoncée `main` ; pas encore de commit |
| DNS | `wallia.valdev.me` résout vers l'IP du VPS `212.227.22.247` |
| Nginx | Service actif ; `sudo -n nginx -t` réussi |
| TLS Wallia | Échec de vérification réel depuis l'URL publique et via résolution loopback : certificat présenté ne couvrant pas `wallia.valdev.me` |
| Certbot | Installé ; timer de renouvellement existant ; aucune lignée de certificat Wallia dans l'inventaire |
| Obsidian | Vault `/home/tetrax/workspace/Obsidian` accessible ; aucune note Wallia trouvée à l'entrée ; modifications étrangères préexistantes à préserver |

Le port loopback `13745` n'apparaît pas dans l'inventaire initial ; c'est un candidat, non une réservation. Revérifier avant publication. Nginx n'a pas de vhost Wallia dédié dans la configuration inspectée. DNS et TLS ne nécessitent pas, à ce stade, une décision humaine : le provisionnement dédié reste à faire.

## Modèles : développement et application séparés

### Développement

- **Astra** : cette session est enregistrée dans `state.db` avec `model=gpt-6-astra`, `provider=openai-codex`, `reasoning_config.effort=max`. Le catalogue authentifié Codex a répondu HTTP 200 et expose cet identifiant ainsi que les niveaux `low`, `medium`, `high`, `xhigh`, `max`, `ultra`. L'effort de la session est `max`, pas `ultra` ; ce dernier est décrit par le fournisseur comme impliquant une délégation automatique. La session en cours prouve l'inférence active, pas une garantie de quota futur ni un benchmark.
- **DeepSeek via OpenCode Go** : catalogue authentifié HTTP 200 à `https://opencode.ai/zen/go/v1/models` contenant l'identifiant exact `deepseek-v4.1-flash`. Appel minimal réel HTTP 200 à `/chat/completions`, `thinking.type=enabled`, `reasoning_effort=max`, modèle retourné `deepseek-v4.1-flash`, contenu `WALLIA_CODE_ROUTE_OK`, arrêt normal. Usage fournisseur : 65 tokens d'entrée, 26 de sortie, dont 18 de raisonnement. Aucun raisonnement interne affiché.
- Le CLI `opencode` n'est pas installé dans le PATH ni aux emplacements usuels vérifiés. Son absence n'est pas un manque d'accès à Go : le connecteur Hermes et l'API Go sont disponibles.
- La délégation native est actuellement configurée sur `opencode-go/deepseek-v4.1-flash` mais avec effort `medium`. Son schéma n'a pas de commutateur modèle/effort par tâche. Ne pas prétendre que cette délégation utiliserait `max` ni modifier la configuration globale pour une mission.
- Les profils existants ont aussi été lus sans modification : `builder` utilise actuellement `opencode-go/deepseek-flash`, effort `max`, et `reviewer` utilise `openai-codex/gpt-5.6-sol`, effort `high`. Ces valeurs diffèrent du modèle Builder standard écrit dans la policy. Cela ne lève pas l'interdiction explicite de la mission d'utiliser ces profils.

### API utilisable par Wallia

- **DeepSeek API native disponible et testée** via le résolveur de credentials existant : `https://api.deepseek.com/v1`, authentification API distincte de l'abonnement coding. Le catalogue authentifié HTTP 200 contient `deepseek-flash` et `deepseek-v4-pro`.
- Test texte réel de `deepseek-flash` : HTTP 200, modèle retourné identique, réponse `WALLIA_SUPPORT_API_OK`, arrêt normal ; 17 tokens d'entrée et 7 de sortie. Cela établit un accès API approprié, mais **pas** un chat Wallia de bout en bout.
- La clé configurée xAI a échoué : HTTP 400, `Incorrect API key provided`. Aucun nouvel essai identique ni achat/rechargement.
- Une clé Firecrawl est présente ; son usage par Wallia n'a pas été testé. Vision, streaming, annulation et recherche web applicative non testés/non actifs.
- Aucun secret n'a été affiché, copié dans Wallia, transmis à un worker ou publié. Les pools de credentials doivent être consultés via le résolveur natif : la seule présence/absence de variables `.env` ne constitue pas un inventaire complet.

## Continuité pendant une déconnexion

La conversation tourne réellement côté VPS dans `hermes-dashboard.service` (PID observé 709), pas dans le client Desktop. Le Gateway et le notifier de jobs durables sont actifs dans des unités système distinctes.

Le code installé `tui_gateway/session_lifecycle.py` préserve un tour détaché lorsqu'il reste actif, puis peut l'interrompre s'il devient inactif ; la seule exécution côté VPS n'est donc pas une garantie de mission durable. Aucune déconnexion forcée ni interruption du backend n'a été provoquée pour ce contrôle.

Le mécanisme natif `job_submit` existe mais son outil n'expose pas le réglage d'effort par tâche. La reprise utilise donc directement la même infrastructure Kanban native : carte unique `default/t_0e0d0075`, modèle/fournisseur épinglés à la création et effort `max` fixé via la fonction native `kanban_db.set_reasoning_effort` avant déblocage. La génération de la commande de dispatch a été vérifiée : `--model gpt-6-astra` (option réelle `-m`), `--provider openai-codex`, `--reasoning max`. Le Gateway est actif sous l'identité `hermes`. Aucune autre carte active/ready/review n'apparaissait lors de ce précontrôle. Le démarrage effectif, le PID, le scope système et les métadonnées de la nouvelle session restent à constater après déblocage ; aucune déconnexion forcée n'est prévue.

## Impact et éléments volontairement non exécutés

Les applications existantes sont toujours dans leur état observé : Portainer, Vysion et ses recettes, Hub, FortiUpgrade et son scheduler, JOX, FortiAnonymous, FortiFlow, FortiFlow2, Scout et meteo-moto-bot. Tous les conteneurs qui exposaient un healthcheck `healthy` au départ le sont encore au contrôle final. Aucune mutation de service, réseau, volume, Nginx ou protection. Les ressources ont fluctué de 4,7 à 4,5 Gio de RAM disponibles ; 138 Gio de disque restent libres. Ce contrôle n'est pas une mesure de performance sous charge.

Non exécutés : installations applicatives ; migrations/pgvector ; Docling ; embeddings ; corpus synthétique ; ingestion ; UI/authentification ; conversations ; pièces jointes ; tests unitaires/intégration/e2e ; recette mobile/desktop ; CI ; publication Git ; TLS dédié ; sauvegarde/restauration ; persistance après redémarrage.

## Trajectoire de réalisation (blocage de policy résolu)

1. Relire cette note, l'ordre de mission d'origine et l'état réel du dépôt ; vérifier qu'aucun writer ou job Wallia n'a été lancé entre-temps.
2. Confirmer le routage autorisé et ses paramètres réellement appliqués, puis choisir le mécanisme durable natif sans exécution concurrente.
3. Astra conçoit les contrats ; DeepSeek implémente le socle sécurisé, puis une tranche verticale réelle UI → API → job Docling → embeddings → pgvector → source consultable. Préparer `.gitignore` avant tout ajout Git.
4. Brancher uniquement la clé API native nécessaire à Wallia via son stockage de secrets dédié ; aucun montage des credentials Hermes ou OpenCode dans l'application.
5. Aller jusqu'aux critères de recette et de déploiement de la mission, en distinguant systématiquement tests passés, échoués, non exécutés et capacités non actives.

Architecture prescrite mais non implémentée : React/TypeScript/Vite ; FastAPI servant éventuellement le frontend ; PostgreSQL/pgvector ; un worker CPU Docling et embeddings, file durable en base ; stockage dédié ; Docker Compose ; Nginx existant pour HTTPS. Pas de Redis, de gros LLM local ou de nouveau reverse proxy requis.
