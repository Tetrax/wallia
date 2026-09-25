#!/usr/bin/env bash
# Restauration Wallia.
#
#   scripts/restore.sh --isolated DUMP.sql.gz   # teste la restaurabilité (conteneur jetable)
#   scripts/restore.sh --inplace DUMP.sql.gz --yes   # restauration destructive en place
set -euo pipefail

source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
require_cmd docker

MODE=""
DUMP=""
CONFIRM=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --isolated) MODE="isolated"; DUMP="${2:-}"; shift 2 ;;
    --inplace) MODE="inplace"; DUMP="${2:-}"; shift 2 ;;
    --yes) CONFIRM=1; shift ;;
    *) die "argument inconnu: $1" ;;
  esac
done
[[ -n "$MODE" && -n "$DUMP" ]] || die "usage: restore.sh --isolated|--inplace DUMP.sql.gz [--yes]"
[[ -f "$DUMP" ]] || die "dump introuvable: $DUMP"

verify_counts() { # verify_counts "commande docker exec de base"
  log "vérification des tables restaurées"
  docker exec -i "$1" psql -U wallia -d wallia -tA -c \
    "SELECT 'schema_migrations=' || count(*) FROM schema_migrations; SELECT 'users=' || count(*) FROM users; SELECT 'documents=' || count(*) FROM documents; SELECT 'chunks=' || count(*) FROM chunks;"
}

if [[ "$MODE" == "isolated" ]]; then
  NAME="wallia-restore-$(date -u +%Y%m%dT%H%M%SZ)"
  log "restauration isolée dans un conteneur jetable ($NAME)"
  docker run -d --name "$NAME" \
    -e POSTGRES_DB=wallia -e POSTGRES_USER=wallia -e POSTGRES_PASSWORD="$(openssl rand -hex 16)" \
    --tmpfs /var/lib/postgresql/data:size=1g \
    pgvector/pgvector:pg17 >/dev/null
  cleanup() { docker rm -f "$NAME" >/dev/null 2>&1 || true; }
  trap cleanup EXIT
  for _ in $(seq 1 60); do
    docker exec "$NAME" pg_isready -U wallia -d wallia >/dev/null 2>&1 && break
    sleep 1
  done
  docker exec -i "$NAME" psql -U wallia -d wallia -q < <(gunzip -c "$DUMP")
  verify_counts "$NAME"
  log "restauration isolée vérifiée — le conteneur jetable est supprimé à la sortie"
  exit 0
fi

warn "restauration EN PLACE : la base courante sera écrasée"
[[ "$CONFIRM" == "1" ]] || die "ajouter --yes pour confirmer la restauration destructive"
log "arrêt de api et worker pendant la restauration"
"${COMPOSE[@]}" stop api worker
"${COMPOSE[@]}" exec -T db psql -U wallia -d wallia -q -c "DROP SCHEMA public CASCADE; CREATE SCHEMA public;"
"${COMPOSE[@]}" exec -T db psql -U wallia -d wallia -q < <(gunzip -c "$DUMP")
"${COMPOSE[@]}" run --rm api python -m app.migrate
"${COMPOSE[@]}" start api worker
log "restauration en place terminée ; vérifier avec scripts/smoke.sh"
