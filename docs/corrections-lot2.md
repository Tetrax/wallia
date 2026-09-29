# Corrections lot 2 — rapport

> Note du principal après clôture : rapport enfant PROVISOIRE, sections finales incomplètes. Le dernier retrieval régresse (3 tests échouent), FR→EN n'est pas validé et Playwright n'a pas été exécuté. Le contrôle final et le blocage vérifié sont dans `docs/review-lot2.md`, autoritatif pour la reprise ; ne pas transformer les descriptions de correction ci-dessous en verdict PASS.

Reprise du travail EXISTANT (diff lot 1 non commité sur `feat/prototype@11265a5`),
correction applicative des points de `docs/review-lot1-followup.md`, puis recette
locale réelle. **Ce lot ne livre rien** : commit local uniquement, aucun push,
aucun merge.

## 1. Corrections par point de la revue

### Point 1 — Streaming : annulation réelle, zéro octet amont
- `backend/app/routers/chat.py` : `SseStreamingResponse` (sous-classe ASGI) qui
  écoute réellement `receive()` — la déconnexion cliente est détectée par
  `http.disconnect` (mécanisme prouvé par sonde ASGI sur uvicorn 0.54 /
  starlette 1.7), et déclenche `ProviderCancellation.cancel("disconnect")` +
  `generator.close()`.
- `backend/app/llm.py` : `ProviderCancellation.attach()` ferme le **client httpx**
  (et la réponse) — un amont silencieux est fermé même avant tout en-tête.
- Le faux fournisseur `backend/tests/fake_upstream.py` : mode `silent`
  **réellement zéro octet** (aucun keepalive) ; la fermeture est constatée par un
  veilleur `receive()` (`_await_disconnect`), jamais par un envoi côté serveur.
  Mode `delta_then_silent` ajouté (deltas réels puis silence total).
- Tests : fermeture juste après `meta`, amont sans en-têtes, amont
  en-têtes + zéro octet, delta puis silence borné par le délai total, stop ET
  déconnexion combinés, EOF sans `[DONE]`, `[DONE]` après corps vide.

### Point 2 — Erreurs fournisseur stables
- Endpoint avec port invalide → refus de configuration stable (422), jamais 500.
- Événement `error` du flux → catégorie stable, **jamais** le texte brut.
- Veilleur de délai TOTAL du flux (silence borné et annulable, avant et après
  en-têtes) ; fermeture explicite du client httpx.

### Point 3 — Réservation atomique et reprise
- `chat.py` : réservation de génération sous **verrou consultatif transactionnel**
  global + fenêtre de récupération ; message utilisateur et réponse (placeholder)
  persistés AVANT le streaming ; partiels persistés dans tous les chemins
  (`GeneratorExit`, déconnexion, stop, erreur).
- Reprise au démarrage : messages laissés `streaming` par un arrêt brutal →
  `interrupted` (rejouable), fenêtre 0 au démarrage.

### Point 4 — Jobs : un seul job en cours, publication sous bail
- `jobs.py` : réclamation sous verrou transactionnel (aucun second job en cours
  tant qu'un bail est valide ; un bail expiré ne bloque pas la reprise),
  `renew_lease`/`set_progress` gardés par la propriété d'un bail ENCORE valide
  (un bail expiré n'est jamais ressuscité).
- `worker.py` : **fence de propriété atomique** — `locked_by`, statut et bail
  revérifiés DANS la transaction de publication, sous verrou de ligne ; un worker
  déchu ne peut pas publier. Verrou de session : un seul worker traite la file.
- Reprise des baux expirés : replanification s'il reste des tentatives, sinon
  échec + document cohérent (génération publiée conservée `ready`, sinon `failed`).

### Point 5 — Fuites de secrets
- `db.py` : `hide_parameters=True` sur le moteur (les paramètres liés n'apparaissent
  jamais dans un rendu d'erreur).
- Logs d'erreur sans traceback (`log.error(..., exc.__class__.__name__)`) ;
  erreurs API stables, jamais de corps amont.

### Point 6 — Bornes d'entrée réellement terminables
- `main.py` : `BodySizeLimitMiddleware` — borne ASGI cumulée du corps AVANT
  parsing/spool multipart ; 413 immédiat sur `Content-Length` au-delà.
- `parse_runner.py` (nouveau) : l'analyse PDF/image tourne en **sous-processus**
  borné, tué en cas de dépassement (jamais dans la boucle web). Test qui prouve la
  terminaison réelle (`_execute_parser` → kill, processus inexistant ensuite) et
  qu'une requête saine suit un refus 413.

### Point 7 — Chunks : aucune sortie hors fenêtre
- `chunking.py` : en-tête de tableau surdimensionné borné (répété seulement s'il
  laisse la place, sinon préservé intégralement découpé), contrôle final de TOUTE
  sortie (un passage ne dépasse jamais la fenêtre du tokenizer), sans perte.

### Point 8 — Prompts : budget global et métadonnées échappées
- `prompts.py` : budget GLOBAL du contexte hors système (historique + données +
  dernier message RÉSERVÉ) ; métadonnées (titre, produit, versions, nom de
  fichier) sérialisées en JSON échappé — une valeur ne peut jamais fermer une
  balise d'encadrement.

### Point 9 — Déclarations explicites
- `declarations.py` : fenêtre de négation arrêtée au DÉBUT de la déclaration
  (« version » inclus) ; marqueurs d'incertitude (`peut-être`, `probablement`,
  `sans doute`, `je pense`, `hypothèse`) traités comme non déclaratifs ;
  alternatives ambiguës (deux valeurs distinctes) → aucune version.

### Point 10 — Frontend : isolation async
- `App.tsx` : génération de session + jeton de requête — `refreshConversations`,
  `openConversation`, `finalizeStream` ne peuvent plus appliquer une réponse
  tardive après logout ou navigation A→B ; la finalisation du flux ne recharge
  QUE la conversation streamée ; panneaux vidés au changement de cas.
- `Panels.tsx` : `onSaved(conversationId, state)` — une sauvegarde de A n'est
  jamais appliquée à l'écran de B ; source indisponible (document supprimé) :
  lien d'origine désactivé, excerpt conservé.
- `serializers.py` / `conversations.py` : disponibilité des citations résolue en
  une requête (jamais de renumérotation des sources).

### Point 11 — Cohérence documentaire
- `documents.py` : `demo == (scope == "demo")` imposé à l'égalité (création et
  modification), déduplication par empreinte sérialisée, réindex sous verrou.

### Point 12 — RAG FR→EN réel
- `retrieval.py` : la branche plein texte construisait un AND de tous les mots de
  la question (`plainto_tsquery`) → 0 candidat pour une question en langage
  naturel, la « rescousse lexicale » prévue par la barrière ne fonctionnait
  jamais. Corrigé : requête OR des lexèmes RÉELS (`to_tsvector` → `to_tsquery`),
  seuil figé inchangé.
- `backend/tests/acceptance/rag_fr_en.py` (nouveau) : question FR → passage
  Quick Start EN page 2 « seven days » (scores enregistrés), question hors corpus
  → aucune source. Preuves JSON dans le répertoire de preuves runtime.

## 2. Commandes et résultats

À compléter à la fin du lot (résultats réels des runs).

## 3. Limites connues

À compléter.
