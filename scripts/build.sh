#!/usr/bin/env bash
# Construction de l'image Wallia (API + worker, frontend intégré, CPU).
#
#   scripts/build.sh            # mode test local (défaut) — aucune publication exigée
#   scripts/build.sh --deliver  # livraison : refuse arbre sale ou HEAD absent d'origin (en ligne)
#
# L'image de livraison est nommée par SHA COMPLET (`wallia:<sha>`), son label
# OCI `org.opencontainers.image.revision` ET la variable embarquée
# WALLIA_GIT_SHA doivent concorder avec ce SHA. Le mode test tague en plus
# `wallia:local` (harnais locaux) et n'est pas publiable : les tags de test
# (`wallia:test-<court>`, `wallia:local`) ne peuvent jamais usurper un SHA
# livré. Le build livraison reconstruit toujours l'image (jamais de réemploi
# silencieux d'un tag existant).
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

SHA="$(git_sha_full)"
[[ -n "$SHA" ]] || die "dépôt Git introuvable : impossible de nommer l'image par SHA"
BUILD_DATE="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
EVIDENCE="$WALLIA_DIR/runtime/evidence"
mkdir -p "$EVIDENCE"
LOG_FILE="$EVIDENCE/build-${SHA:0:12}-$(date -u +%Y%m%dT%H%M%SZ).log"

if [[ "$MODE" == "deliver" ]]; then
  log "mode livraison : vérification arbre propre + HEAD réellement sur origin (en ligne)"
  assert_clean_and_pushed
  TAG="wallia:$SHA"
  TAGS=(-t "$TAG")
else
  log "mode test local : publication non requise, image non publiable"
  if worktree_dirty; then
    warn "arbre de travail sale : image marquée test local uniquement"
  fi
  TAG="wallia:test-${SHA:0:12}"
  TAGS=(-t "$TAG" -t wallia:local)
fi

log "construction de $TAG (journal : $LOG_FILE)"
{
  docker build \
    --build-arg "WALLIA_GIT_SHA=$SHA" \
    --build-arg "WALLIA_BUILD_DATE=$BUILD_DATE" \
    "${TAGS[@]}" \
    "$WALLIA_DIR"
} 2>&1 | tee "$LOG_FILE"

# Le label OCI et le SHA applicatif doivent concorder : une image mal étiquetée
# ne peut pas être livrée ni déployée sous ce SHA.
LABEL_SHA="$(docker inspect -f '{{ index .Config.Labels "org.opencontainers.image.revision" }}' "$TAG")"
if [[ "$LABEL_SHA" != "$SHA" ]]; then
  die "label OCI ($LABEL_SHA) différent du SHA applicatif ($SHA)"
fi
if [[ "$MODE" == "deliver" ]]; then
  # Double vérification : la variable embarquée doit aussi porter le SHA complet.
  ENV_SHA="$(docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$TAG" | sed -n 's/^WALLIA_GIT_SHA=//p')"
  if [[ "$ENV_SHA" != "$SHA" ]]; then
    die "WALLIA_GIT_SHA embarquée ($ENV_SHA) différente du SHA livré ($SHA)"
  fi
fi
log "label OCI vérifié : $LABEL_SHA"

# Vérification réelle de l'image : manifestes des modèles + embeddings +
# reclassement + conversion Docling (2 pages avec tableau), SANS réseau ni
# secrets. Conteneur de sonde borné (mémoire/CPU/tmpfs, lecture seule, caps
# abandonnées). Un échec rend le build non livrable.
log "vérification des modèles embarqués (hors réseau, sans secrets)"
if ! docker run --network none \
      --memory 2500m --cpus 2 --read-only --tmpfs /tmp:size=768m --cap-drop ALL \
      --entrypoint python "$TAG" -m app.cli check-models 2>&1 | tee -a "$LOG_FILE"; then
  die "vérification des modèles en échec : image non conforme"
fi

log "image prête : $TAG"
log "journal de build : $LOG_FILE"
