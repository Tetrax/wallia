#!/usr/bin/env bash
# Retour arrière Wallia — rejoue le SNAPSHOT PRÉCÉDENT enregistré par
# scripts/deploy.sh (runtime/deploy-state/previous.json) : Compose RENDU +
# fichier d'environnement + image ID immuable.
#
#   scripts/rollback.sh [--state-file <previous.json>]
#
# Invariants :
# - échoue AVANT toute mutation si aucun état précédent valide n'est défini
#   (previous=null explicite ou fichier absent ⇒ message clair, aucun appel
#   Docker de mutation) ;
# - le snapshot rendu doit RÉELLEMENT référencer l'image ID attendu pour
#   api/worker (sinon refus AVANT up/run : jamais lancer une autre image ou un
#   tag mutable parce que l'ID existe localement) ;
# - n'utilise QUE le snapshot précédent (jamais le Compose/env courants) ;
# - santé « healthy » obligatoire pour db/api/worker ; l'image effectivement
#   portée par api/worker est vérifiée après démarrage ; aucun remove-orphans ;
# - après succès, `current.json` est ACTUALISÉ vers le snapshot restauré
#   (env/mounts conservés tels que rendus) et l'état précédent consommé est
#   retiré (le déploiement suivant recapturera).
#
# Migrations : AVANT-only (aucune migration descendante n'existe) — si la
# version déployée a appliqué des migrations incompatibles, restaurer d'abord
# la sauvegarde (scripts/backup.sh / scripts/restore.sh --isolated) ; procédure
# détaillée dans docs/operations.md.
set -euo pipefail

source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
require_cmd docker
require_cmd python3

STATE_FILE="$WALLIA_DIR/runtime/deploy-state/previous.json"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --state-file) STATE_FILE="${2:-}"; shift 2 ;;
    *) die "argument inconnu: $1" ;;
  esac
done

[[ -f "$STATE_FILE" ]] || die "état précédent introuvable ($STATE_FILE) : aucun déploiement suivi à annuler"
STATE_DIR="$(cd "$(dirname "$STATE_FILE")" && pwd)"
CURRENT_STATE="$STATE_DIR/current.json"
STATE_HELPER="$WALLIA_DIR/scripts/delivery_state.py"

# Refus AVANT toute mutation tant que l'état précédent n'est pas défini/valide.
python3 "$STATE_HELPER" validate-refs "$STATE_FILE" || die "état précédent inutilisable : rollback refusé avant toute mutation"
PREV_IMAGE="$(python3 "$STATE_HELPER" read "$STATE_FILE" image)"
PREV_IMAGE_ID="$(python3 "$STATE_HELPER" read "$STATE_FILE" image_id)"
PREV_COMPOSE="$(python3 "$STATE_HELPER" read "$STATE_FILE" compose_snapshot)"
PREV_ENV="$(python3 "$STATE_HELPER" read "$STATE_FILE" env_snapshot)"
[[ -n "$PREV_IMAGE" ]] || die "previous=null (première installation) : aucun snapshot ancien à restaurer"
docker image inspect "$PREV_IMAGE_ID" >/dev/null 2>&1 \
  || die "image précédente $PREV_IMAGE_ID absente localement : rollback impossible sans elle"

log "retour vers le snapshot précédent : $PREV_IMAGE (id $PREV_IMAGE_ID, env $PREV_ENV)"
COMPOSE_PREV=(docker compose -p "$COMPOSE_PROJECT" --project-directory "$WALLIA_DIR" -f "$PREV_COMPOSE")

# Le snapshot RENDU doit référencer l'ID attendu : sinon le rollback lancerait
# une autre image (ou un tag mutable) que celle enregistrée — refus AVANT
# up/run, aucun conteneur touché.
if ! python3 - "$PREV_COMPOSE" "$PREV_IMAGE_ID" <<'PY'
import json
import sys
from pathlib import Path

path, expected = Path(sys.argv[1]), sys.argv[2]
try:
    config = json.loads(path.read_text(encoding="utf-8"))
except ValueError as exc:
    raise SystemExit(f"snapshot précédent illisible ({path}): {exc}")
services = config.get("services") if isinstance(config, dict) else None
if not isinstance(services, dict):
    raise SystemExit(f"snapshot précédent sans section services ({path})")
bad = []
for name in ("api", "worker"):
    entry = services.get(name)
    image = entry.get("image") if isinstance(entry, dict) else None
    if image != expected:
        bad.append(f"{name}={image!r}")
if bad:
    raise SystemExit(
        f"snapshot précédent ({path}) ne référence PAS l'ID immuable {expected} "
        f"pour api/worker ({', '.join(bad)})"
    )
print("[wallia] snapshot précédent vérifié : api/worker référencent l'ID immuable")
PY
then
  die "snapshot précédent non conforme : rollback refusé AVANT toute mutation (aucune image ni tag mutable ne sera lancé à sa place)"
fi

"${COMPOSE_PREV[@]}" up -d --wait --wait-timeout 120 db
"${COMPOSE_PREV[@]}" run -T api python -m app.migrate
"${COMPOSE_PREV[@]}" up -d db api worker

for service in db api worker; do
  if ! wait_healthy COMPOSE_PREV "$service"; then
    die "rollback : santé du service '$service' non atteinte (healthy requis)"
  fi
  log "service $service : healthy"
done

log "vérification de l'image réellement portée par api et worker"
for service in api worker; do
  verify_service_image COMPOSE_PREV "$service" "$PREV_IMAGE_ID" \
    || die "rollback : image effective de '$service' différente du snapshot ($PREV_IMAGE_ID)"
  log "service $service : image $PREV_IMAGE_ID confirmée"
done

# L'état réel est désormais le snapshot restauré : current.json est actualisé
# (env/mounts conservés tels que rendus par le snapshot), et l'état précédent
# consommé est retiré (aucune fausse piste pour un nouveau rollback).
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
python3 "$STATE_HELPER" write "$CURRENT_STATE" --stamp "$STAMP" \
  --image "$PREV_IMAGE" --image-id "$PREV_IMAGE_ID" \
  --compose "$PREV_COMPOSE" --env "$PREV_ENV" \
  --note "rollback exécuté le $STAMP vers $PREV_IMAGE" >/dev/null
rm -f "$STATE_FILE"
log "rollback terminé : image $PREV_IMAGE active (current.json actualisé)"
