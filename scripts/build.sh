#!/usr/bin/env bash
# Construction de l'image Wallia (API + worker, frontend intégré, CPU).
#
#   scripts/build.sh            # mode test local (défaut) — aucune publication exigée
#   scripts/build.sh --deliver  # livraison : refuse arbre sale ou HEAD non poussé
set -euo pipefail

source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
require_cmd docker

MODE="test"
for arg in "$@"; do
  case "$arg" in
    --deliver) MODE="deliver" ;;
    --test) MODE="test" ;;
    *) die "argument inconnu: $arg (utiliser --test ou --deliver)" ;;
  esac
done

SHA="$(git -C "$WALLIA_DIR" rev-parse HEAD 2>/dev/null || echo "sans-git")"
SHORT="$(sha_short)"
BUILD_DATE="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

if [[ "$MODE" == "deliver" ]]; then
  log "mode livraison : vérification de l'arbre et du push"
  assert_clean_and_pushed
  TAG="wallia:$SHORT"
else
  log "mode test local : publication non requise"
  if worktree_dirty; then
    warn "arbre de travail sale : image marquée test local uniquement"
  fi
  TAG="wallia:test-$SHORT"
fi

EVIDENCE="$WALLIA_DIR/runtime/evidence"
mkdir -p "$EVIDENCE"
LOG_FILE="$EVIDENCE/build-$SHORT-$(date -u +%Y%m%dT%H%M%SZ).log"

log "construction de $TAG (journal : $LOG_FILE)"
{
  docker build \
    --build-arg "WALLIA_GIT_SHA=$SHA" \
    --build-arg "WALLIA_BUILD_DATE=$BUILD_DATE" \
    -t "$TAG" \
    -t "wallia:local" \
    "$WALLIA_DIR"
} 2>&1 | tee "$LOG_FILE"

log "vérification des modèles embarqués (CPU, révision épinglée)"
if have_sudo && [[ -d "$WALLIA_DIR/runtime/secrets" ]]; then
  docker run --rm --entrypoint python -v "$WALLIA_DIR/runtime/secrets:/secrets:ro" "$TAG" -m app.cli check-models 2>&1 | tee -a "$LOG_FILE"
else
  warn "vérification des modèles ignorée (secrets indisponibles sans sudo)"
fi

log "image prête : $TAG (aussi étiquetée wallia:local)"
log "journal de build : $LOG_FILE"
