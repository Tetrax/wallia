# Décision — reranking de passages après recherche hybride

## Statut : probe vérifié, intégration autorisée mais non encore livrée

Astra a relu les deux scripts produits par DeepSeek (`scripts/tests-isolated/probe-reranker{.sh,-eval.py}`), puis exécuté lui-même le probe complet. Le premier lot DeepSeek avait épuisé ses35tours après correction de deux erreurs de forme du script ; sa sortie exit1 n'était PAS une acceptance. Le replay direct à10:25UTC a terminé exit0 et tous les contrôles détaillés ci-dessous sont verts.

Le runner est séparé de l'API, non-root, réseau `none`, lecture seule,2CPU/1600MiB, sans secret ni socket Docker. Aucun cleanup ou lifecycle de la pile vivante n'est exécuté. Conteneur de preuve `wallia-reranker-probe-102527`, exit0/OOMfalse. API StartedAt08:57:16.750788302Z,RestartCount0,healthy avant/après.

## Problème observé

Les scores cosinus E5 du lot3/3b ne permettent pas de séparer tous les positifs FR→EN et les négatifs. Le seuil élevé et la barrière lexicale individuelle évitent des faux positifs, mais éliminent le passage anglais demandé par une question française. Une ancienne barrière collective donnait artificiellement du rappel en entraînant d'autres passages ; elle a été supprimée. Replay direct lot3b :119tests backend passent, mais la recette RAG isolée a3contrôles verts/3rouges.

## Alternative mesurée

Un petit cross-encoder multilingue classe directement la paire question/passage. Ce n'est ni un LLM conversationnel ni un service distant. Les embeddings restent E5-small384 ; aucune migration des vecteurs n'est requise.

- Modèle : `cross-encoder/mmarco-mMiniLMv2-L12-H384-v1`,117M paramètres, licence Apache-2.0 déclarée dans sa fiche officielle.
- Révision : `1427fd652930e4ba29e8149678df786c240d8825`.
- Poids : safetensors470592698octets, SHA256 `5daeca2481a76b5976a2bdc32f0a78532b6716da4f8cd3ff59460ef8d2f359b4` vérifié avant chargement.
- Sept fichiers sélectionnés, aucun code distant ou pickle exécuté. Bibliothèques déjà présentes dans l'image ; `local_files_only=True`, `trust_remote_code=False`, CPU,2threads, lots8,512tokens maximum.
- Sorties : logits BRUTS, jamais des probabilités.

Sources :
- https://huggingface.co/cross-encoder/mmarco-mMiniLMv2-L12-H384-v1
- https://huggingface.co/api/models/cross-encoder/mmarco-mMiniLMv2-L12-H384-v1
- https://huggingface.co/api/models/cross-encoder/mmarco-mMiniLMv2-L12-H384-v1/tree/1427fd652930e4ba29e8149678df786c240d8825?recursive=false

## Résultats réels du replay principal

Calibration DISTINCTE inchangée (`calibration-nova-boreal`,13passages,14positifs,14négatifs,364paires) :
- Bonne réponse positive minimale :3.499786.
- Meilleur score de TOUS les négatifs, proches et métadonnées inclus :-1.201679.
- Séparation :4.701465.
- Règle fixée AVANT exécution : milieu de ces deux bornes si elles sont séparées. Seuil gelé à1.1491 AVANT la recette.
- Résultat :14/14positifs gardés et14/14négatifs rejetés. Tous scores exposés ; pas de retrait des cas proches.

Recette sur l'export INTACT des4documents/16chunks Docling réels :
- Question FR exacte → unique passage anglais «seven days»,p2, rang1,logit5.75806 ; avec et sans filtre Aster/10.10.
- Absence commerciale :0source ; meilleur logit−6.204299.
- Périmètre10.9 :4candidats tous10.9,0passage retenu (la réponse demandée n'y existe pas), aucune substitution10.10.
- Version99.99 :0candidat/0source.
- Les5contrôles du probe passent. Il s'agit d'un probe de reranking des candidats, PAS encore du chemin SQL+service+chat intégré.

Performance mesurée : chargement modèle/tokenizer2.084s (cacheOS non neutralisé), classement30paires médiane1.4811s sur3exécutions,VmHWM972028KiB,cgroup peak816525312octets pour CE SEUL ; modèle/tokenizer492751475octets. La coexistence E5+CE reste à mesurer avant déploiement.

Preuves canoniques de ce replay : `runtime/tests-isolated/reranker-probe-out/reranker-probe-{summary,calibration,frozen-params,acceptance,perf}.json`, copies `runtime/evidence/lot3c-*`, log principal `runtime/evidence/lot3c-astra-replay.log`.

## Contrat d'intégration décidé par le principal

1. Garder l'unique recherche hybride SQL existante (vecteur384 + lexical, filtres produit/version/scope/génération/compatibilité AVANT sélection). Fusion RRF pour produire un pool borné de30candidats par défaut ; pas de seuil cosinus/lexical d'éligibilité avant le reranker réel. Sinon le passage FR→EN serait perdu avant son classement.
2. Scorer QUESTION + TEXTE du passage uniquement avec ce modèle/révision. Les titres/métadonnées restent une information d'attribution, pas une porte automatique d'éligibilité.
3. Retenir individuellement les logits>=1.1491, trier par logit décroissant (départage déterministe), retourner au plus6sources. Aucun changement sur la seule fixture. Exposer `score_rerank` et modèle/révision dans les diagnostics ; pas de prétendue probabilité.
4. Si le modèle est absent, incompatible ou saturé, état explicite `retrieval_unavailable`/sources vides, jamais repli silencieux vers l'ancien garde ou `no_relevant_source`. Les erreurs techniques ne déclenchent pas de web automatique. Aucun chargement distant pendant une requête.
5. Un singleton à chargement protégé, un scoring à la fois et entrées bornées ; pas de second service, GPU, LLM local, worker/framework supplémentaire. Aucun changement des dimensions E5, aucun réindexage forcé.
6. Backend réel par défaut et obligatoire en production. Un backend fixture explicitement déclaré est autorisé uniquement en tests ; aucune preuve métier avec ce backend. Adapter les tests existants au nouvel invariant sans effacer leurs protections.
7. Validations avant acceptation : suite isolée, calibration puis recette sur le chemin SQL+service intégré avec reranker RÉEL, contrôle des filtres/no-source, preuve E5+CE dans un conteneur borné avec mémoire/latence et runtime vivant inchangé. Ne pas modifier/déployer la pile vivante pour ces tests.

## Limites conservées

Corpus et calibration sont fictifs et petits, donc PAS une qualification WALLIX. Le succès du probe n'est pas encore le succès du chat/application, du pipeline réel complet ni d'une bibliothèque volumineuse. Le coût mémoire/latence supplémentaire est réel et justifié seulement par ce gain mesuré ; aucune autre sophistication n'est autorisée sans besoin observé.
