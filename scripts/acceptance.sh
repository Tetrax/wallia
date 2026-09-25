#!/usr/bin/env bash
# Acceptance Wallia — scénarios exécutables par l'opérateur (ou le principal).
#
#   scripts/acceptance.sh all        # tout (tests, frontend, corpus, recherche, streaming, e2e)
#   scripts/acceptance.sh tests      # pytest backend (DB pgvector réelle)
#   scripts/acceptance.sh frontend   # npm ci + tsc + vite build
#   scripts/acceptance.sh corpus     # génération des PDF fictifs + ingestion réelle
#   scripts/acceptance.sh rag        # mesures de recherche (embeddings réels)
#   scripts/acceptance.sh streaming  # streaming/stop/erreurs via faux fournisseur
#   scripts/acceptance.sh e2e        # Playwright desktop + mobile (captures)
set -euo pipefail

source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
require_cmd docker

STAGE="${1:-all}"
PORT="$(env_value WALLIA_API_PORT 13745)"
EVIDENCE="$WALLIA_DIR/runtime/evidence"
INITIAL_ACCESS="$WALLIA_DIR/runtime/initial-access.txt"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
MASTER_LOG="$EVIDENCE/acceptance-$STAMP.log"
mkdir -p "$EVIDENCE"

# La suite de tests embarquée démarre un serveur HTTP supplémentaire DANS le
# conteneur api : on relève sa limite mémoire le temps de l'acceptance.
export WALLIA_API_MEM_LIMIT="${WALLIA_API_MEM_LIMIT:-2500m}"

[[ -f "$INITIAL_ACCESS" ]] || die "runtime/initial-access.txt absent : exécuter scripts/init.sh puis scripts/dev_up.sh"
"${COMPOSE[@]}" ps --status running --services | grep -qx api || die "pile non démarrée (scripts/dev_up.sh)"

RESULTS=()
run_stage() { # run_stage nom fonction
  local name="$1" fn="$2"
  log "=== acceptance : $name ==="
  if "$fn" 2>&1 | tee -a "$MASTER_LOG"; then
    RESULTS+=("OK    $name")
  else
    RESULTS+=("ÉCHEC $name")
  fi
}

stage_tests() {
  "${COMPOSE[@]}" up -d api worker
  "${COMPOSE[@]}" exec -T api python -m pytest -q tests
}

stage_frontend() {
  (cd "$WALLIA_DIR/frontend" && npm ci --no-audit --no-fund && npm run build)
}

stage_corpus() {
  "${COMPOSE[@]}" exec -T api python fixtures/generate_corpus.py --out /data/fixtures/corpus
  initial_access_stream | "${COMPOSE[@]}" exec -T -e WALLIA_FIXTURES=/data/fixtures/corpus api \
    python -m tests.acceptance.ingest_fixtures
}

stage_rag() {
  initial_access_stream | "${COMPOSE[@]}" exec -T api python -m tests.acceptance.rag_measure
}

stage_streaming() {
  log "autorisation temporaire du domaine fake-upstream pour l'API"
  WALLIA_PROVIDER_ALLOWED_DOMAINS="api.deepseek.com,fake-upstream" "${COMPOSE[@]}" up -d api worker
  "${COMPOSE[@]}" --profile testtools up -d fake-upstream
  sleep 2
  set +e
  initial_access_stream | "${COMPOSE[@]}" exec -T api python -m tests.acceptance.stream_tests
  local code=$?
  set -e
  "${COMPOSE[@]}" --profile testtools stop fake-upstream
  log "restauration de la liste de domaines autorisés"
  "${COMPOSE[@]}" up -d api worker
  return $code
}

stage_e2e() {
  # Cache navigateurs local éventuel (ex. ~/.cache/ms-playwright/chromium-1243) :
  # on l'utilise s'il existe, sinon Playwright télécharge selon sa configuration.
  local cache="${PLAYWRIGHT_BROWSERS_PATH:-$HOME/.cache/ms-playwright}"
  if compgen -G "$cache/chromium-*" >/dev/null; then
    export PLAYWRIGHT_BROWSERS_PATH="$cache"
    export PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1
    log "cache navigateurs Playwright : $cache"
  else
    log "aucun cache navigateurs local détecté : téléchargement Playwright standard"
  fi
  (cd "$WALLIA_DIR/tests/e2e" && npm install --no-audit --no-fund)
  (cd "$WALLIA_DIR/tests/e2e" && \
    WALLIA_E2E_BASE_URL="http://127.0.0.1:${PORT}" \
    WALLIA_E2E_CREDENTIALS="$INITIAL_ACCESS" \
    WALLIA_EVIDENCE_DIR="$EVIDENCE" \
    npx playwright test --reporter=list)
}

case "$STAGE" in
  tests) run_stage tests stage_tests ;;
  frontend) run_stage frontend stage_frontend ;;
  corpus) run_stage corpus stage_corpus ;;
  rag) run_stage rag stage_rag ;;
  streaming) run_stage streaming stage_streaming ;;
  e2e) run_stage e2e stage_e2e ;;
  all)
    run_stage frontend stage_frontend
    run_stage tests stage_tests
    run_stage corpus stage_corpus
    run_stage rag stage_rag
    run_stage streaming stage_streaming
    run_stage e2e stage_e2e
    ;;
  *) die "étape inconnue: $STAGE" ;;
esac

log "récapitulatif :"
printf '  %s\n' "${RESULTS[@]}"
log "journal consolidé : $MASTER_LOG"
if printf '%s\n' "${RESULTS[@]}" | grep -q "ÉCHEC"; then
  die "au moins une étape a échoué (voir $MASTER_LOG)"
fi
log "acceptance terminée avec succès"
