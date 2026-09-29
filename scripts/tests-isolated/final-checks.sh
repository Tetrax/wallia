#!/usr/bin/env bash
# Contrôles d'environnement (lot3) — la pile vivante n'est JAMAIS modifiée :
# lecture seule (inspect, ps, comptes, empreintes de secrets).
set -euo pipefail
source "$(dirname "$0")/../lib.sh"

echo "== horodatage (UTC) =="
date -u +%Y-%m-%dT%H:%M:%SZ

echo "== conteneurs de la pile vivante =="
docker ps --format '{{.Names}}\t{{.Status}}' | grep -E 'wallia|^NAMES' || true

echo "== api : identité d'exécution (doit rester 08:57:16, restarts inchangés) =="
docker inspect wallia-api-1 --format 'StartedAt={{.State.StartedAt}} Restarts={{.RestartCount}} OOMKilled={{.State.OOMKilled}} Running={{.State.Running}}'

echo "== secrets runtime (empreintes sha256 seules, jamais les valeurs) =="
sha256sum runtime/secrets/* | sed 's# runtime/secrets/#  #'

echo "== données vivantes importées (comptes lecture seule) =="
docker exec -i wallia-db-1 sh -c "PGPASSWORD=\$(cat /run/secrets/db_password) psql -U wallia -d wallia -tA -f -" <<'SQL'
SELECT 'documents=' || count(*) FROM documents;
SELECT 'chunks=' || count(*) FROM chunks;
SQL

echo "== ressources hôte =="
free -m | sed -n '1,2p'
df -h / | tail -1
