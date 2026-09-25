#!/usr/bin/env bash
# Smoke HTTP authentifié contre la pile en cours (identifiants jamais affichés).
set -euo pipefail

source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

INITIAL_ACCESS="$WALLIA_DIR/runtime/initial-access.txt"
[[ -f "$INITIAL_ACCESS" ]] || die "runtime/initial-access.txt absent : lancer scripts/init.sh d'abord"

"${COMPOSE[@]}" ps --status running --services | grep -qx api || die "service api non démarré (scripts/dev_up.sh)"

log "smoke HTTP (lecture des identifiants par stdin, aucune trace)"
initial_access_stream | "${COMPOSE[@]}" exec -T api python tests/smoke_http.py
