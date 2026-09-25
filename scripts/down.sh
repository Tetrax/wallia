#!/usr/bin/env bash
# Arrêt de la pile Wallia. --purge supprime aussi les volumes (destructif).
set -euo pipefail

source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

if [[ "${1:-}" == "--purge" ]]; then
  warn "suppression des conteneurs ET volumes du projet $COMPOSE_PROJECT (données locales perdues)"
  read -r -p "Taper PURGE pour confirmer : " answer
  [[ "$answer" == "PURGE" ]] || die "annulé"
  "${COMPOSE[@]}" down -v --remove-orphans
  log "pile et volumes supprimés (runtime/ conservé : sauvegardes, preuves, secrets)"
else
  "${COMPOSE[@]}" down --remove-orphans
  log "pile arrêtée (volumes et runtime/ conservés)"
fi
