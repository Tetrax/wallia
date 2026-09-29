# Corrections run209-web — après revue et rejeux Astra

Statut : fichiers écrits, **NON EXÉCUTÉ** (contrainte du lot : un codeur,
fichiers uniquement — aucun test ni script lancé par le codeur).
Périmètre : les cinq corrections décidées par Astra, sans refonte produit.

## Fichiers modifiés / créés

| Fichier | Nature |
| --- | --- |
| `backend/app/web.py` | refus IP explicites ; recherche exécutée dans un processus isolé borné (spawn) ; autorité brute contrôlée avant normalisation ; bornes de traitement (12 candidats / 3 résultats) |
| `backend/tests/test_web.py` | fixture `web_mp_fork` (autouse) ; cas IP/URL ajoutés ; borne MESURÉE (deadline, corps qui stagne, DNS bloqué) ; vrai chemin spawn inoffensif, IPC, saturation/récupération ; mode `stall` du double |
| `backend/tests/test_web_chat.py` | les 3 tests chat utilisent la fixture `web_mp_fork` (doubles hérités, aucun réseau réel) |
| `frontend/src/api.ts` | `isAllowedWebUrl` réécrit pour la parité complète avec le backend (autorité brute, labels DNS, host ≤ 253) |
| `frontend/src/App.tsx` | `streamingWebRef` synchronisée DIRECTEMENT dans les callbacks validés (init, `onMeta`, `onSources`) ; effacement pour le bon flux uniquement ; toutes les gardes `isCurrent`/session/cas conservées |
| `tests/e2e/web_status_repro.mjs` | banner esbuild `createRequire` (SSR) ; bundle conservé sous TMPDIR ; cas URL ajoutés ; toutes les assertions conservées |
| `tests/e2e/web_stream_repro.mjs` | **nouveau** : repro navigateur réelle du batching (meta+sources+done en un chunk), via le navigateur Playwright déjà installé, `--dump-dom`, aucun réseau |
| `docs/corrections-run209-web-fix.md` | ce rapport |

## Par point Astra

### 1. Refus IP explicitement unicast global
`_host_is_public` refuse désormais EXPLICITEMENT `is_multicast`, `is_reserved`,
`is_unspecified`, `is_loopback`, `is_link_local` et `is_private` en plus de
`is_global` (sur Python 3.12, `is_global` seul accepte encore 224.0.0.1).
Un seul résultat non conforme suffit à rejeter. Tests ajoutés : IPv6 multicast
(`ff02::1`) et mélange global/multicast (`[PUBLIC_IP, "224.0.0.1"]`) ; tous les
cas existants conservés.

### 2. Deadline réelle — processus de recherche isolé
`search_web_public` → `_search_web_public_bounded` : l'opération s'exécute dans
un processus enfant **spawn** (`_search_child_main`, cible top-level), résultat/
erreur par pipe privé, entrées minimales (requête publique + clé) uniquement par
IPC — jamais argv/stdout/log, pas de DB/session/config dans l'enfant. Parent :
`BoundedSemaphore(2)` non bloquant (saturé ⇒ `WebUnavailable` immédiat), attente
`WEB_CHILD_WAIT_S` puis terminate/kill avec join borné, pipes fermés, slot
libéré ; budget pire cas 27 s + 2 s de marge ≤ 30 s. Dans l'opération : échéance
vérifiée entre étapes et entre candidats (≤ 12 examinés, ≤ 3 résultats), lecture
par blocs bornés ; aucune URL résultat suivie ; aucun appel réseau tardif après
timeout (l'enfant est tué).

Tests : fixture autouse `web_mp_fork` (fork Linux uniquement, hérite les doubles
HTTP/DNS — jamais du vrai réseau ; aucune option de production ne change le
contexte). Bornes MESURÉES : `test_search_deadline_is_bounded_and_measured`,
`test_stalled_body_is_killed_within_budget` (corps qui ne repart jamais),
`test_blocked_dns_resolution_is_bounded` — durée de retour < 2 s et zéro
sous-processus survivant. Vrai chemin spawn : `test_real_spawn_path_...`
(clé vide, sans réseau), `test_child_inputs_travel_as_process_args_not_argv`,
`test_saturated_slots_refuse_immediately_and_recover`.

### 3. Parité URL backend/frontend
`isAllowedWebUrl` (api.ts) applique les mêmes règles que le backend : autorité
BRUTE ASCII contrôlée AVANT toute normalisation URL/IDNA (Unicode, `@` même
userinfo vide, `%`, espace brut refusés), un seul `:` au plus, labels DNS
stricts (tiret en début/fin refusé, ≤ 63, host ≤ 253, label vide refusé),
HTTPS/port 443/domaine exacts conservés, backslash/contrôles/fragments refusés.
La résolution DNS reste côté serveur. Cas ajoutés des deux côtés (tests pytest +
`web_status_repro.mjs`) : `https://@wallix.com/`, `wallix%2Ecom`, `wallix。com`,
espace brut, `-wallix.com`/`wallix-.com`, label > 63, host > 253, label vide.

### 4. Métadonnée de fin de flux
La ref `streamingWebRef` n'est plus écrite au rendu : elle est initialisée
SYNCHRONEMENT au démarrage du flux (par `seq`), puis mise à jour dans `onMeta`
(id) et `onSources` (web, la valeur observée gagne) — uniquement après les
gardes `isCurrent()`. Le finaliseur consomme puis efface la ref du BON flux
(`streamId`) ; logout/expiration et suppression du cas actif l'effacent aussi.
Repro : `web_stream_repro.mjs` monte le VRAI App avec un stub fetch qui livre
meta+status+sources+delta+done en UN chunk (les callbacks précèdent tout render
React) et exige la note « Web constructeur public — version non vérifiée » dans
le message final après la fin du flux — sans la synchro directe, le statut est
perdu et le cas échoue. SSR seul ne couvrait pas ce défaut.

### 5. SSR — passerelle createRequire
`web_status_repro.mjs` : banner esbuild
`createRequire(import.meta.url)` (solution standard) pour que les `require`
internes de react-dom/server (CJS) fonctionnent en bundle ESM — l'échec
« Dynamic require of stream » disparaît. Le répertoire et le bundle sont
CONSERVÉS sous TMPDIR (preuve relisible, plus de cleanup récursif) ; toutes les
assertions SSR existantes sont conservées et les cas URL du point 3 ajoutés.

## Commandes proposées (à exécuter par le principal — NON exécutées ici)

    # 1) Suite backend ciblée (mêmes fichiers que run209-web-tests-first)
    cd /home/tetrax/workspace/wallia/backend && python -m pytest tests/test_web.py tests/test_web_chat.py -q -ra

    # 2) Frontend : types + build (même commande que run209-web-front-build-first)
    cd /home/tetrax/workspace/wallia/frontend && npm run build

    # 3) SSR (point 5 + parité URL) — conserve son répertoire de preuve
    cd /home/tetrax/workspace/wallia/tests/e2e && node web_status_repro.mjs

    # 4) Repro navigateur du batching (point 4) — utilise le navigateur déjà installé
    cd /home/tetrax/workspace/wallia/tests/e2e && node web_stream_repro.mjs

## Limites et points de relecture

- **Aucun résultat de test dans ce document** : tout est à exécuter par le
  principal ; les preuves run209-web-* antérieures ne sont pas des preuves de
  ce lot.
- Le test du vrai chemin spawn importe `app.web` dans l'enfant : si le
  sandbox du conteneur de test bloque `spawn`, le test échouera explicitement
  (message « chemin spawn anormalement lent » ou « service indisponible
  (démarrage du chercheur) ») — le dire, ne pas l'atténuer.
- La fixture `fork` peut produire un `DeprecationWarning` Python 3.12 (fork
  dans un process multithreadé) : c'est la voie autorisée par le lot pour
  hériter les doubles ; aucune config de filtrage n'a été trouvée dans le
  dépôt. Les tests du vrai chemin spawn couvrent le contexte de production.
- `web_stream_repro.mjs` échoue explicitement si aucun navigateur Playwright
  n'est trouvé (jamais un faux PASS) ; il suppose le bundle esbuild
  (`frontend/node_modules` déjà présent) et `--dump-dom` du binaire.
- La note « docs/resume-files-only.md » et les trois preuves `run209-web-*`
  sont les seules sources lues dans `runtime/`, conformément au brief.
