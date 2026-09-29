#!/usr/bin/env bash
# Wallia — runtime UI de recette ISOLÉ (lot4). Projet compose dédié `wallia-e2e`.
#
# Prépare les secrets DE TEST dans leur propre racine (`runtime/e2e-isolated/`,
# jamais `runtime/secrets`), sème la base de test (migrations + compte de
# recette + corpus démo), puis démarre l'API en loopback 127.0.0.1:13746 avec
# le front actuel (`frontend/dist`) et le fournisseur double local.
#
#   scripts/e2e-isolated.sh          # prépare + démarre (idempotent)
#   scripts/e2e-isolated.sh seed     # rejoue uniquement le seed
#
# Politique de conservation (lot3b) : AUCUN down/stop/restart/rm ici ; le
# runtime dédié reste en place pour la recette et les replays. La base est
# celle du harnais lot3 (projet `wallia-tests`, réseau `wallia_tests_net`).
set -euo pipefail

source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
require_cmd docker
require_cmd python3
require_cmd curl

ROOT="$WALLIA_DIR/runtime/e2e-isolated"
SECRETS="$ROOT/secrets"
EVIDENCE="$WALLIA_DIR/runtime/evidence"
TESTS_ROOT="$WALLIA_DIR/runtime/tests-isolated"
COMPOSE=(docker compose -p "wallia-e2e" -f "$WALLIA_DIR/docker-compose.e2e-isolated.yml" --env-file /dev/null)

mkdir -p "$SECRETS" "$ROOT/data" "$EVIDENCE"
chmod 700 "$ROOT" "$SECRETS"

# Mot de passe de la base de TEST : celui du harnais lot3 (même base dédiée),
# copié dans la racine d'isolation e2e — jamais un secret de livraison.
if [[ ! -s "$TESTS_ROOT/db_password" ]]; then
  python3 -c 'import secrets; print(secrets.token_hex(24))' > "$TESTS_ROOT/db_password"
  chmod 600 "$TESTS_ROOT/db_password"
fi
cp -f "$TESTS_ROOT/db_password" "$SECRETS/db_password"
chmod 600 "$SECRETS/db_password"

# Secrets de session/worker du runtime de recette : générés une seule fois dans
# leur propre racine (idempotent ; jamais rejoué sur les secrets de livraison).
if [[ ! -s "$SECRETS/session_secret" ]]; then
  python3 -c 'import secrets; print(secrets.token_hex(32))' > "$SECRETS/session_secret"
  chmod 600 "$SECRETS/session_secret"
fi
if [[ ! -s "$SECRETS/worker_token" ]]; then
  python3 -c 'import secrets; print(secrets.token_hex(32))' > "$SECRETS/worker_token"
  chmod 600 "$SECRETS/worker_token"
fi

# Compte de recette (données synthétiques, aucun credential réel).
if [[ ! -s "$ROOT/credentials.json" ]]; then
  python3 - "$ROOT/credentials.json" <<'PY'
import json, secrets, sys
from pathlib import Path

path = Path(sys.argv[1])
payload = {"email": "recette-lot4@wallia.test", "password": secrets.token_urlsafe(18)}
path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
PY
  chmod 600 "$ROOT/credentials.json"
fi

# Base de test du harnais lot3 : démarrée si absente (jamais arrêtée ici).
export WALLIA_TESTS_DB_PASSWORD="$(cat "$TESTS_ROOT/db_password")"
docker compose -p "wallia-tests" -f "$WALLIA_DIR/docker-compose.tests-isolated.yml" --env-file /dev/null \
  up -d --wait --wait-timeout 120 db >/dev/null

if [[ "${1:-all}" == "seed" ]]; then
  "${COMPOSE[@]}" run --rm seed
  exit 0
fi

log "seed : migrations + compte de recette + corpus démo (base de test)"
"${COMPOSE[@]}" run --rm seed

log "démarrage api + faux fournisseur (loopback 127.0.0.1:13746 uniquement)"
"${COMPOSE[@]}" up -d --wait --wait-timeout 180 api fake-upstream

for _ in $(seq 1 60); do
  if curl -fsS "http://127.0.0.1:13746/healthz" >/dev/null 2>&1; then break; fi
  sleep 1
done
curl -fsS "http://127.0.0.1:13746/healthz" | python3 -c 'import json,sys; print("healthz:", json.dumps(json.load(sys.stdin), ensure_ascii=False))'

log "runtime de recette prêt : http://127.0.0.1:13746 (loopback, projet wallia-e2e conservé sans cleanup)"