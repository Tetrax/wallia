# Corrections run209 — point 6 (recherche web complémentaire)

Lot borné : activation effective du web, anonymisation/validation de la requête,
métadonnée SSE et information honnête du LLM et de l'UI. **Aucune exécution**
(compilation, tests, Git, Docker) dans ce lot : le principal (Astra) relit puis
exécute. Aucun modèle/schéma/migration DB touché, aucun réglage de seuil RAG,
de calibration ou de reranker modifié.

## Fichiers modifiés

Backend
- `backend/app/web.py` — réécrit : barrière `web_status` (drapeau + activation
  opérateur après recette RAG + clé EFFECTIVEMENT présente), vocabulaire fermé
  `PRODUCT_TERMS`/`THEME_TERMS`, version `WEB_VERSION_RE` (ASCII `fullmatch`,
  invalides OMISES), endpoint Firecrawl v2 FIXE, appel borné (200 seul,
  `success: true` booléen exact, `data.web` liste, types `str` contrôlés),
  `_allowed_public_url` (HTTPS, wallix.com ou sous-domaine, sans userinfo/port
  hors 443/fragment/backslash/contrôle, DNS ASCII strict, résolution
  entièrement globale), `_read_bounded` (256 Kio + échéance au fil de l'eau),
  `web_fallback_for_chat`, `web_source_from_result`, `WebUnavailable`.
- `backend/app/routers/web_api.py` — réécrit : `GET /api/web/status`,
  `POST /api/web/search` (auth + CSRF conservés), `web_status` consulté AVANT
  toute recherche (403 sinon), `terms` (≤ 6, ≤ 100 chars) → requête publique
  ≤ 160, 422 si aucun terme public, 503 sur `WebUnavailable` (message sûr).
- `backend/app/routers/chat.py` — `_prepare` : sources corpus + web fusionnées,
  notes d'honnêteté (corpus ET web) construites AVANT `build_provider_messages`
  et passées en `extra_notes` (réellement transmises dans le message de
  données) ; la métadonnée SSE `web` est émise dans le frame `sources`.
  Les anciennes notes ajoutées APRÈS l'appel fournisseur sont supprimées.
- `backend/tests/test_web.py` — nouveau (NON EXÉCUTÉ).
- `backend/tests/test_web_chat.py` — nouveau (NON EXÉCUTÉ).

Frontend
- `frontend/src/types.ts` — `WebFallbackMeta` + `RetrievalPayload.web`.
- `frontend/src/api.ts` — `onSources` transporte `web` ; `isAllowedWebUrl`
  (mêmes gardes syntaxiques que le serveur, résolution DNS côté serveur).
- `frontend/src/App.tsx` — `web` conservé dans le `StreamingState`, statut final
  conservé PAR MESSAGE en fin de flux (`messageWeb`), garde `isCurrent`
  inchangée, remise à zéro aux fins de session.
- `frontend/src/components/Chat.tsx` — `WebStatusNote` affiché honnêtement
  (ok / no_results / unavailable explicites, rien pour not_needed/disabled),
  indépendamment du statut corpus ; libellé de source web
  « web public — version non vérifiée ».
- `frontend/src/components/Panels.tsx` — badges « Web constructeur public » /
  « version non vérifiée » ; lien rendu ouvrable uniquement si
  `isAllowedWebUrl` (sinon mention « URL publique non conforme », jamais
  d'`href`).
- `tests/e2e/web_status_repro.mjs` — nouveau (NON EXÉCUTÉ) : rendu SSR local
  sans nouvelle dépendance (esbuild + react-dom de `frontend/node_modules`).

Documentation
- `docs/corrections-run209-web.md` — ce rapport.

## Contrats couverts

1. **Activation effective** : `POST /api/web/search` appelle `web_status` avant
   toute recherche ; clé seule jamais suffisante ; même contrôle pour le repli
   chat (`web_fallback_for_chat` → `web_status`). Auth/CSRF conservés.
2. **Requête anonymisée** : `terms` = texte candidat borné ; seuls les jetons du
   vocabulaire fermé et une version `[0-9]{1,4}(?:\.[0-9]{1,4}){1,3}`
   (fullmatch ASCII, sans strip) peuvent sortir ; produit par mapping fermé ;
   thèmes ≤ 4 ; requête ≤ 160 chars ; aucune requête libre exposée.
3. **Moteur** : endpoint v2 fixe HTTPS, `follow_redirects=False`,
   `verify=True`, `trust_env=False` ; 200 seul ; `success is True` ; `data.web`
   liste ; élément URL/titre/description `str` sinon refus (aucune conversion
   objet→str) ; ≤ 3 résultats ; ≤ 256 Kio ; titre ≤ 200 / extrait ≤ 400 ;
   erreurs sûres (jamais en-têtes, clé ou corps fournisseur) ; échéance totale
   ≤ 30 s vérifiée au fil de l'eau.
4. **URL** : HTTPS, domaine wallix.com (ou sous-domaine), hostname ASCII strict,
   sans userinfo/port hors 443/fragment/backslash/contrôle ; résolution bornée
   (`_result_host_addresses`) refusant toute adresse non globale ; aucune URL
   résultat n'est jamais appelée.
5. **Repli** : uniquement après gates (`no_relevant_source`/`empty_corpus`) ;
   jamais à la place du corpus `ok` ni d'une indisponibilité technique ;
   identifiants/scores/pages `null`, `source_type="web"`,
   `version_state="non_verifiee"` ; texte de résultat = données, jamais système.
6. **LLM/SSE/UI** : notes corpus + web transmises AVANT l'appel (vérifié sur les
   messages réellement capturés par le double fournisseur) ; frame `sources`
   porte `web {status, reason, query}` ; type `WebFallbackMeta` ; UI affiche
   statut et provenance sans spinner fictionnel ; statut final conservé par
   message en session, non promis après reload (seules les sources persistées
   gardent la provenance) ; barre globale = état connecteur, pas preuve d'appel.

## Tests écrits (NON EXÉCUTÉS)

`backend/tests/test_web.py` (doubles HTTP/DNS locaux, aucun réseau réel) :
- requête fermée : jetons/noms internes/secrets factices exclus, bornes,
  versions valides acceptées, versions invalides (suffixe, underscore, Unicode,
  exposant, jeton) omises ;
- URL : acceptées (HTTPS, sous-domaines, port 443) et refusées (userinfo,
  port, fragment, backslash, contrôle, domaines trompeurs, point final,
  souligné, host littéral) ; résolutions privées/loopback/link-local/réservées
  et échec DNS refusés ;
- moteur : payload v2 valide et bornes, filtrage des items non conformes,
  302 refusé même avec corps valide, 500 message sûr exact, JSON illisible,
  `success:false`, `data.web` non-liste, corps > 256 Kio, deadline
  (`slow`), clé absente = aucun appel ;
- endpoints : 401 sans auth, 403 CSRF, refus sans drapeau, sans activation
  opérateur, sans recette RAG et sans clé (jamais d'appel réseau) ; recherche
  bornée (422 terms > 6, > 100 chars, aucun terme public) ; statut exposé sans
  secret ; repli : disabled/not_needed/no_results/unavailable et indépendance
  du statut corpus.

`backend/tests/test_web_chat.py` (chat réel sur serveur de test, doubles) :
- `ok` : frame `sources.web` complet, source web persistée avec provenance,
  prompt capturé contenant `extrait_web_public`, URL publique, « version non
  vérifiée », notes corpus + web transmises, rien de web dans le message
  système ;
- `disabled` : aucun appel, statut explicite, note corpus toujours transmise ;
- `unavailable` : raison sûre, statut corpus intact, note d'indisponibilité
  transmise.

`tests/e2e/web_status_repro.mjs` : rendu SSR réel de `ChatView`/`SourcesPanel`
via le frontend bundlé localement — statuts ok/no_results/unavailable affichés,
not_needed/disabled silencieux, aucune note inventée sans métadonnée, liens
conformes ouvrables (`noopener noreferrer`), URL non conformes jamais rendues
ouvrables.

## Commandes exactes proposées (à exécuter par le principal, NON EXÉCUTÉES ici)

```bash
python -m compileall -q backend/app backend/tests
```

```bash
cd backend && python -m pytest -q tests/test_web.py tests/test_web_chat.py
```

```bash
cd backend && python -m pytest -q tests   # suite complète (pgvector requis)
```

```bash
cd frontend && npm run build   # tsc --noEmit + vite build
```

```bash
node tests/e2e/web_status_repro.mjs   # rendu local, sans installation
```

Les tests backend exigent le harnais isolé habituel (PostgreSQL `wallia_test`,
`WALLIA_TEST_*`), comme le reste de la suite ; aucun réseau réel dans les
nouveaux tests.

## Limites et points ouverts

- Aucune exécution dans ce lot : compilation, tests backend, build frontend et
  script e2e restent à lancer par le principal ; les résultats annoncés sont à
  confirmer par exécution.
- Le statut web n'est pas persisté en base (contrat SSE uniquement, pas de
  migration) : après reload, l'opérationnel n'est pas promis — seule la
  provenance web des sources est conservée.
- `terms` reste un texte candidat borné côté API : la garantie d'anonymisation
  repose sur le vocabulaire fermé de `web.py` (mapping produit + thèmes +
  version validée), pas sur une liste blanche d'entrées.
- La résolution DNS des URL de résultat reste dépendante du résolveur du
  déploiement ; un échec de résolution fait refuser l'URL (comportement voulu,
  couvert par les doubles de test).
- Le double HTTP des tests utilise `http://127.0.0.1:<port>` (endpoint
  monkeypatché) : l'endpoint fixe HTTPS de production n'est pas exercé par ces
  tests locaux.
