#!/usr/bin/env bash
# Wallia — runner de tests ISOLÉ (lot3, politique de conservation lot3b).
#
# Exécute la suite (ou une commande donnée) dans l'image `wallia:local`, sur une
# base PostgreSQL/pgvector DÉDIÉE (projet compose `wallia-tests`, aucun port
# hôte), avec limites CPU/RAM/tmpfs explicites. Ne monte jamais runtime/secrets
# ni runtime/data ni credentials Hermes ; le seul secret est le mot de passe de
# la base de test, généré dans `runtime/tests-isolated/` (sa propre racine).
#
#   scripts/tests-isolated.sh                                   # suite complète
#   scripts/tests-isolated.sh python tests/acceptance/rag_fr_en_isolated.py
#   scripts/tests-isolated.sh -- pytest -q -p no:cacheprovider tests/test_retrieval.py
#
# Politique lot3b : le runner démarre la DB dédiée si absente et exécute le
# conteneur de test éphémère (fin naturelle), puis NE NETTOIE RIEN — jamais de
# down/stop/restart/rm ici : la DB et le réseau dédiés restent en place pour
# les replays (opération de lifecycle refusée sans approbation interactive).
#
# Sortie : récapitulatif + journal dans runtime/evidence/tests-isolated-<stamp>.log
# (rien n'est affiché d'un secret).
set -euo pipefail

source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
require_cmd docker

PROJECT="wallia-tests"
ISO_FILE="$WALLIA_DIR/docker-compose.tests-isolated.yml"
ISO_ROOT="$WALLIA_DIR/runtime/tests-isolated"
EVIDENCE="$WALLIA_DIR/runtime/evidence"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
LOG="$EVIDENCE/tests-isolated-$STAMP.log"
mkdir -p "$ISO_ROOT/evidence" "$EVIDENCE"
chmod 700 "$ISO_ROOT"

# Secret de TEST généré dans sa propre racine (jamais runtime/secrets).
if [[ ! -s "$ISO_ROOT/db_password" ]]; then
  python3 -c 'import secrets; print(secrets.token_hex(24))' > "$ISO_ROOT/db_password"
  chmod 600 "$ISO_ROOT/db_password"
fi
WALLIA_TESTS_DB_PASSWORD="$(cat "$ISO_ROOT/db_password")"
export WALLIA_TESTS_DB_PASSWORD

COMPOSE=(docker compose -p "$PROJECT" -f "$ISO_FILE" --env-file /dev/null)

if [[ $# -gt 0 && "$1" == "--" ]]; then shift; fi
if [[ $# -gt 0 ]]; then
  CMD=("$@")
else
  CMD=(python -m pytest -q -p no:cacheprovider tests)
fi

log "harnais isolé : projet=$PROJECT fichier=$ISO_FILE"
log "commande runner : ${CMD[*]}"
"${COMPOSE[@]}" up -d --wait --wait-timeout 120 db

code=0
set +e
"${COMPOSE[@]}" run --no-deps -T runner "${CMD[@]}" 2>&1 | tee -a "$LOG"
code="${PIPESTATUS[0]}"
set -e

# Aucun arrêt/nettoyage automatique (politique lot3b) : la base et le réseau
# isolés restent en place pour les replays ; le conteneur de test éphémère
# s'arrête à sa fin naturelle. Ne jamais ajouter down/stop/restart/rm ici.
log "harnais conservé : db dédiée + réseau du projet $PROJECT laissés en place (aucun cleanup)"

log "code de sortie réel : $code"
log "journal : $LOG"
exit "$code"
