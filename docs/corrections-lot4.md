# Lot4 — corrections réelles (final)

Statut : les périmètres ci-dessous ont été EXÉCUTÉS et leurs résultats sont
réels ; tout point non rejoué est explicitement listé comme NON VALIDÉ.
Aucun critère non exécuté n'est déclaré vert. Aucun commit/push/merge.
Aucun nouveau runner/framework. Pile LIVE (13745/worker/DB) jamais touchée.

Fichiers modifiés ce lot (tous dans `/home/tetrax/workspace/wallia`, branche
`feat/prototype`, diff non committé) :
`tests/e2e/wallia_e2e.py`, `frontend/src/App.tsx`, `backend/app/declarations.py`,
`backend/app/llm.py`, `backend/tests/test_declarations.py`,
`backend/tests/test_chat_continuity.py`,
`backend/tests/acceptance/repro_pre_headers_close.py` (étendu TLS),
`backend/tests/acceptance/memory_batches_isolated.py` (nouveau, borné),
`docs/corrections-lot4.md`. Hors dépôt : script one-shot de restauration du
mot de passe synthétique (aucun secret affiché).

## 1. Correctifs runner E2E (`tests/e2e/wallia_e2e.py`)

- `wait_visible` : cause réelle conservée dans le diagnostic (classe
  d'exception), sans secret ni détail du sélecteur.
- `_logout` : attend un contrôle SPÉCIFIQUE de l'écran de connexion (champ
  email + bouton « Se connecter »). L'ancien `input[type=password]` était
  ambigu sur la page Administration (strict mode) — ce n'était PAS un défaut
  de déconnexion : le vrai écran login est bien affiché.
- `_p20_admin` : attend le badge serveur « clé configurée » (chargement
  asynchrone) avant toute lecture — cause du FAIL à 60 ms.
- `_p14_race_create` : barrière explicite (POST retenu par interception,
  clic B + DOM B confirmé, PUIS libération de la réponse) + horodatage d'ordre
  asserté (`t_request/t_b_click/t_b_shown/t_release`). Preuve réelle : la
  course existait → RED « l'ancien créateur a écrasé B ».
- `_p21_password` : changement + restauration avec `finally` idempotent
  (restauration de secours via l'API de test), reconnexion de l'ApiClient
  indépendant après chaque changement (les autres sessions sont révoquées).
- `_p23_cleanup` : détecte/répare les 401, exige le résultat réel pour CHAQUE
  cas créé par le harnais et vérifie le 404 serveur après suppression — plus
  de « 0/9 » silencieux.
- Boutons « Nouvelle conversation » : sélecteur `exact=True` (collision
  aria-label titre vs bouton).
- `_p12_race_get` : réouverture du tiroir entre les deux clics latéraux
  (mobile : le clic ferme le tiroir).

## 2. Correctifs app (prouvés par la recette)

- `frontend/src/App.tsx` — `handleCreateConversation` : garde de génération
  (`openRequestRef` capturé au départ) — une réponse de création tardive
  n'écrase plus une ouverture plus récente. RED prouvé par la recette avec la
  barrière réelle, puis GREEN.
- `frontend/src/App.tsx` — bouton « Ouvrir le menu » mobile aussi dans
  `.global-strip` pour les vues Bibliothèque/Administration (impossible de
  rouvrir le tiroir hors vue chat) — cause des FAIL mobile 20/21/22.
- PRESERVÉ (lot4b, non retouché) : correctif Markdown `remarkCitations`
  (attacher retournant transformer) — repro node Astra 5/5 PASS.

## 3. Déclarations incertaines (`backend/app/declarations.py`)

Tests d'abord (RED), puis correctif local. 3 bugs prouvés corrigés :
1. « version 10.9 ou 10.10 » (sans préfixe répété) choisissait 10.9 →
   spans d'alternative « ou » : aucune promotion, aucun filtre ;
2. « pas sûr … Aster 10.9 » devenait un fait affirmatif → marqueurs de doute
   d'assertion (_DOUBT_ASSERTION_RE) : ni fait ni filtre ;
3. « Produit : Aster version 10.10 » créait un faux produit composite →
   normalisation vers le produit CONNU exact (_normalize_product_candidate).
Plus : « je ne sais pas si … version X » (doute de version).
Détail technique : les candidats produit→version utilisent la position du
CHIFFRE (le span d'alternative commence au mot-clé, pas à l'espace de tête).

- RED observé : 4 failed / 19 passed — dont le test chat isolé montrant que le
  filtre `'10.9'` atteignait la recherche (défaut réel de bout en bout).
- GREEN : 23/23 (`test_declarations.py` + `test_chat_continuity.py`), incluant
  état persévéré + filtre du tour.
- Replay : `scripts/tests-isolated.sh -- python -m pytest -q -p no:cacheprovider tests/test_declarations.py tests/test_chat_continuity.py`

## 4. Angle mort HTTPS pré-en-têtes (TLS) — RED puis GREEN

- Repro étendue : `backend/tests/acceptance/repro_pre_headers_close.py` —
  scénarios `https-stop` / `https-disconnect` / `https-timeout` avec CA +
  certificat ÉPHÉMÈRES VÉRIFIÉS (SSL_CERT_FILE, httpx trust_env ; la
  vérification n'est JAMAIS désactivée), attente que l'amont ait REÇU la
  requête DANS TLS puis silence AVANT en-têtes.
- RED (avant patch) : `https-stop`/`https-disconnect` →
  `close_observed_s=null`, `provider_thread_finished=false` — le registre ne
  suivait que le TCP, détaché après `start_tls` (httpcore renvoie un NOUVEAU
  flux TLS).
- Correctif borné : `backend/app/llm.py` — `_TrackedStream` (proxy qui
  enregistre AUSSI le flux renvoyé par `start_tls`), registre « fermable »
  (course annulation/register traitée : un flux enregistré après fermeture est
  fermé immédiatement), `_close_stream` partagé. Aucun nouveau client/service.
- GREEN : stop 0.107 s, disconnect 0.107 s, timeout 6.057 s (déclencheur 6 s),
  threads terminés, `health_after=true` (appel réel suivant OK), `tls_ok=true`,
  exit 0. HTTP suivi OK ; HTTP « legacy » (client.close seul) reste rouge —
  référence du défaut.
- Évidence : `runtime/tests-isolated/evidence/lot4-repro-pre-headers.json` ;
  logs `runtime/evidence/tests-isolated-20260926T125517Z.log` (RED) et
  `…T125804Z.log` (GREEN).
- Limite : DNS/handshake restent bornés par le connect timeout — aucune
  annulation instantanée n'est revendiquée pour ces phases.

## 5. Recettes réelles desktop + mobile

- Desktop 1440x900 : **24/24 PASS**, 0 erreur console/HTTP inattendue
  (`runtime/evidence/lot4-e2e-desktop.json`).
- Mobile 390x844 tactile : **24/24 PASS** (`runtime/evidence/lot4-e2e-mobile.json`),
  après correction des 2 défauts mobiles ; le rejeu précédent avait été bloqué
  par le rate-limit login durable (429) puis rejoué proprement après la
  fenêtre. Captures : `runtime/evidence/lot4-*.png` (runs finaux).
- Limite explicite : la pile e2e 13746 sert le backend chargé à SON démarrage
  (pas de restart effectué) ; les correctifs backend de CE lot (déclarations,
  llm) sont prouvés par la suite isolée + la repro TLS, PAS par la recette UI.
  Le front servi est bien le `frontend/dist` final.
- Specs TS historiques : NON exécutées — jamais déclarées vertes.

## 6. Vérifications finales

- Suite isolée COMPLÈTE après TOUTES éditions : **144 passed** (87.72 s),
  exit 0 — log `runtime/evidence/tests-isolated-20260926T130146Z.log`.
- RAG réel E5+pgvector+CE : **11/11** (seuil 1.1491 non retouché), cgroup
  peak 1652703232 (1576 Mio), oom_kill 0 — JSON
  `runtime/tests-isolated/evidence/acceptance-rag-fr-en-isolated.json`,
  log `…T125852Z.log`.
- Mémoire (complément borné) : E5 16 textes longs 2.78/2.90 s ; CE 8 paires
  longues 1.26/1.32 s ; peak 1537875968 (1466.7 Mio) < 2000 Mio ; oom/oom_kill
  0 — JSON `runtime/tests-isolated/evidence/lot4-memory-batches.json`,
  log `…T130106Z.log`. Script `tests/acceptance/memory_batches_isolated.py`
  (réutilise les services réels ; aucun OOM mesuré → aucune modif app).
- Build front : `npm --prefix frontend run build` OK (tsc + Vite) avant les
  recettes finales ; aucune édition frontend après.
- Mot de passe du compte synthétique : restauré via API de test
  (`change=200`, `login_original_after=200`), état sain vérifié ; le fichier
  admin live n'a jamais été lu.
- Docker inspect live (StartedAt/RestartCount/OOM/healthy) : NON rejoué ce lot
  (budget) — aucune opération lifecycle n'a été tentée ; replay lecture seule :
  `scripts/tests-isolated/final-checks.sh`.

## 7. Checklist finale

- [x] Correctifs runner + app (RED/GREEN réels, ci-dessus).
- [x] Desktop 24/24 + Mobile 24/24 sur le front final.
- [x] TLS pré-en-têtes : RED → correctif → GREEN (stop/disconnect/délai).
- [x] Déclarations : RED → correctif → GREEN (23/23, chat inclus).
- [x] Suite isolée complète 144/144 ; RAG 11/11 ; mémoire batchs max OK ;
      build front OK.
- [ ] Docker inspect live avant/après — NON rejoué (budget) : non validé.
- [ ] Specs TS — non exécutées : non validées (jamais vertes).
- [ ] Rechargement backend de la pile e2e (restart) — non effectué : la
      recette UI couvre le front final + le backend d'alors.

## 8. Limites et honnêteté

- Les doubles UI (fake-upstream, embeddings/reranker `fixture`) prouvent la
  mécanique UI, PAS le chat natif ni le RAG réel (couverts séparément par la
  suite isolée + acceptance RAG).
- Rien n'a été committé/pushé ; le diff de travail est dans l'arbre.
- `docs/state.md` et le vault Obsidian restent réservés au principal.
