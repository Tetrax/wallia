#!/usr/bin/env bash
# Sauvegarde locale : dump PostgreSQL + données applicatives (hors secrets).
set -euo pipefail

source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
require_cmd docker
require_cmd gzip

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
BACKUPS="$WALLIA_DIR/runtime/backups"
mkdir -p "$BACKUPS"
DB_DUMP="$BACKUPS/wallia-db-$STAMP.sql.gz"
DATA_TAR="$BACKUPS/wallia-data-$STAMP.tar.gz"

log "dump PostgreSQL → $DB_DUMP"
"${COMPOSE[@]}" exec -T db pg_dump -U wallia -d wallia --no-owner --clean --if-exists | gzip -9 > "$DB_DUMP"

log "archive des données applicatives → $DATA_TAR"
tar -czf "$DATA_TAR" -C "$WALLIA_DIR/runtime/data" --exclude 'evidence' .

log "empreintes SHA-256"
sha256sum "$DB_DUMP" "$DATA_TAR" | tee "$BACKUPS/SHA256-$STAMP.txt"

log "sauvegarde terminée (les secrets ne sont PAS inclus)"
