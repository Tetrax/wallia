#!/usr/bin/env bash
# Démarrage local de la pile Wallia (non publiée sur Internet : écoute 127.0.0.1).
set -euo pipefail

source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
require_cmd docker

PORT="$(env_value WALLIA_API_PORT 13745)"

log "démarrage db + api + worker (projet $COMPOSE_PROJECT)"
"${COMPOSE[@]}" up -d --remove-orphans

log "attente de la santé de l'API"
for _ in $(seq 1 60); do
  if curl -fsS "http://127.0.0.1:${PORT}/healthz" >/dev/null 2>&1; then
    log "API prête : http://127.0.0.1:${PORT}"
    log "état : ${COMPOSE[*]} ps"
    "${COMPOSE[@]}" ps
    exit 0
  fi
  sleep 2
done

warn "API non prête après 120 s — journaux :"
"${COMPOSE[@]}" logs --tail 40 api || true
die "démarrage incomplet"
