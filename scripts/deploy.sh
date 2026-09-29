#!/usr/bin/env bash
# Déploiement Wallia (à exécuter par le principal après revue).
#
#   scripts/deploy.sh --env-file runtime/secrets/app.production.env [--skip-build]
#
# Invariants de livraison :
# - SHA = HEAD local ; arbre propre ET HEAD réellement présent sur origin
#   (vérification en ligne). AUCUNE sélection de SHA arbitraire : le retour
#   vers un état ancien passe uniquement par scripts/rollback.sh ;
# - l'environnement Compose EFFECTIF (rendu, variables héritées incluses) est
#   contrôlé strictement par scripts/delivery_env_check.py — la valeur des
#   variables n'est jamais imprimée ;
# - image `wallia:<sha-complet>`, label OCI concordant ; son ID immuable
#   (`sha256:…`) est résolu UNE fois, un snapshot Compose RENDU référençant
#   cet ID est produit AVANT up/migrate, et CE snapshot est utilisé pour les
#   mutations (jamais le tag mutable pendant que l'ID n'est enregistré
#   qu'après). L'image effectivement portée par api/worker est vérifiée après
#   démarrage ;
# - état précédent = snapshot de Compose RENDU + fichier d'environnement +
#   image ID, écrit atomiquement (0700/0600). Première installation sans
#   runtime existant ⇒ previous=null ; runtime existant sans état courant
#   VALIDE ⇒ refus AVANT toute mutation (un snapshot fidèle de la
#   configuration réellement active doit être préparé/vérifié manuellement,
#   voir docs/operations.md §12 — jamais un état fabriqué depuis un conteneur
#   arbitraire ni le nouvel env recopié comme ancien) ;
# - démarrage UNIQUEMENT db/api/worker du projet wallia, santé « healthy »
#   exigée des trois services, aucun remove-orphans, aucune écriture /etc.
set -euo pipefail

source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
require_cmd docker
require_cmd python3

ENV_FILE_ARG=""
SKIP_BUILD=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --env-file) ENV_FILE_ARG="${2:-}"; shift 2 ;;
    --skip-build) SKIP_BUILD=1; shift ;;
    --bootstrap-previous-env)
      die "option --bootstrap-previous-env RETIRÉE : la capture automatique d'un état précédent depuis les seuls conteneurs n'est PAS un rollback fidèle (anciens mounts/env perdus, conteneur arbitraire). Préparer et VÉRIFIER manuellement un snapshot fidèle de la configuration réellement active (image ID effectif, Compose rendu, env) puis l'installer comme runtime/deploy-state/current.json avant cette migration initiale (docs/operations.md §12). Aucune mutation effectuée."
      ;;
    *) die "argument inconnu: $1 (le déploiement utilise toujours HEAD ; pour revenir à un snapshot ancien : scripts/rollback.sh)" ;;
  esac
done

[[ -n "$ENV_FILE_ARG" && -f "$ENV_FILE_ARG" ]] || die "fournir --env-file <fichier d'environnement de production>"
ENV_FILE_ARG="$(cd "$(dirname "$ENV_FILE_ARG")" && pwd)/$(basename "$ENV_FILE_ARG")"

log "mode livraison : vérification arbre propre + HEAD réellement sur origin (en ligne)"
assert_clean_and_pushed
SHA="$(git_sha_full)"
IMAGE="wallia:$SHA"
export WALLIA_IMAGE="$IMAGE"

STATE_DIR="$WALLIA_DIR/runtime/deploy-state"
mkdir -p "$STATE_DIR"
chmod 700 "$STATE_DIR"
STATE_HELPER="$WALLIA_DIR/scripts/delivery_state.py"
ENV_HELPER="$WALLIA_DIR/scripts/delivery_env_check.py"
CURRENT_STATE="$STATE_DIR/current.json"
PREVIOUS_STATE="$STATE_DIR/previous.json"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"

COMPOSE_PROD=(docker compose -p "$COMPOSE_PROJECT" -f "$WALLIA_DIR/docker-compose.yml" --env-file "$ENV_FILE_ARG")

log "contrôle STRICT de l'environnement Compose effectif (silencieux, aucune mutation)"
"${COMPOSE_PROD[@]}" config --format json 2>/dev/null | python3 "$ENV_HELPER" --image "$IMAGE" \
  || die "environnement effectif non conforme : déploiement refusé avant toute mutation"

# --- Image ----------------------------------------------------------------
if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
  [[ "$SKIP_BUILD" == "0" ]] || die "image $IMAGE absente localement (relancer sans --skip-build)"
  log "image $IMAGE absente : construction (mode livraison)"
  bash "$WALLIA_DIR/scripts/build.sh" --deliver
fi
LABEL_SHA="$(docker inspect -f '{{ index .Config.Labels "org.opencontainers.image.revision" }}' "$IMAGE")"
[[ "$LABEL_SHA" == "$SHA" ]] || die "label OCI de $IMAGE ($LABEL_SHA) différent du SHA déployé ($SHA)"
IMAGE_ID="$(docker inspect -f '{{.Id}}' "$IMAGE")"
[[ -n "$IMAGE_ID" ]] || die "image $IMAGE sans ID inspectable : déploiement refusé"
log "image de livraison : $IMAGE (id immuable $IMAGE_ID)"

# --- État précédent (snapshot Compose rendu + env + image ID) --------------
if [[ -f "$CURRENT_STATE" ]]; then
  python3 "$STATE_HELPER" validate-refs "$CURRENT_STATE" >/dev/null \
    || die "état courant ($CURRENT_STATE) incohérent : déploiement refusé avant toute mutation"
  PREV_IMAGE="$(python3 "$STATE_HELPER" read "$CURRENT_STATE" image)"
  PREV_IMAGE_ID="$(python3 "$STATE_HELPER" read "$CURRENT_STATE" image_id)"
  PREV_COMPOSE="$(python3 "$STATE_HELPER" read "$CURRENT_STATE" compose_snapshot)"
  PREV_ENV="$(python3 "$STATE_HELPER" read "$CURRENT_STATE" env_snapshot)"
  python3 "$STATE_HELPER" write "$PREVIOUS_STATE" --stamp "$STAMP" \
    --image "$PREV_IMAGE" --image-id "$PREV_IMAGE_ID" \
    --compose "$PREV_COMPOSE" --env "$PREV_ENV" \
    --note "état avant déploiement de $IMAGE" >/dev/null
  log "état précédent repris de current.json (image=$PREV_IMAGE, env=$PREV_ENV)"
else
  EXISTING_CONTAINERS="$(docker ps -a --filter 'label=com.docker.compose.project=wallia' --format '{{.Names}}' 2>/dev/null || true)"
  if [[ -n "$EXISTING_CONTAINERS" ]]; then
    die "runtime wallia existant sans état courant VALIDE ($CURRENT_STATE absent) : refus de fabriquer un état depuis les conteneurs. Préparer et VÉRIFIER manuellement un snapshot fidèle de la configuration réellement active (image ID effectif, Compose rendu, env) et l'installer comme current.json avant cette migration initiale (docs/operations.md §12). Aucune mutation effectuée."
  fi
  log "première installation (aucun runtime wallia existant) : previous=null"
  python3 "$STATE_HELPER" write "$PREVIOUS_STATE" --stamp "$STAMP" --null-previous \
    --note "première installation sans runtime wallia existant" >/dev/null
fi

# --- Snapshot immuable du nouvel état (AVANT up/migrate) -------------------
# Le snapshot est le Compose RENDU au format JSON (auto-contenu) et référence
# l'ID immuable (jamais le tag mutable) : produit et VÉRIFIÉ avant toute
# mutation, puis CE snapshot est utilisé pour les mutations.
NEW_COMPOSE="$STATE_DIR/compose-$STAMP.json"
NEW_ENV="$STATE_DIR/env-$STAMP.env"
cp -f "$ENV_FILE_ARG" "$NEW_ENV"
chmod 600 "$NEW_ENV"
WALLIA_IMAGE="$IMAGE_ID" "${COMPOSE_PROD[@]}" config --format json > "$NEW_COMPOSE" 2>/dev/null \
  || die "rendu du snapshot Compose impossible : déploiement refusé avant toute mutation"
chmod 600 "$NEW_COMPOSE"
COMPOSE_DEPLOY=(docker compose -p "$COMPOSE_PROJECT" --project-directory "$WALLIA_DIR" -f "$NEW_COMPOSE")
if ! python3 - "$NEW_COMPOSE" "$IMAGE_ID" <<'PY'
import json
import sys
from pathlib import Path

path, expected = Path(sys.argv[1]), sys.argv[2]
try:
    config = json.loads(path.read_text(encoding="utf-8"))
except ValueError as exc:
    raise SystemExit(f"snapshot Compose illisible ({path}): {exc}")
services = config.get("services") if isinstance(config, dict) else None
if not isinstance(services, dict):
    raise SystemExit(f"snapshot Compose sans section services ({path})")
bad = []
for name in ("api", "worker"):
    entry = services.get(name)
    image = entry.get("image") if isinstance(entry, dict) else None
    if image != expected:
        bad.append(f"{name}={image!r}")
if bad:
    raise SystemExit(
        f"snapshot Compose ({path}) ne référence PAS l'ID immuable {expected} "
        f"pour api/worker ({', '.join(bad)})"
    )
print("[wallia] snapshot Compose vérifié : api/worker référencent l'ID immuable")
PY
then
  die "snapshot du nouvel état non conforme : déploiement refusé avant toute mutation"
fi

# --- Déploiement ----------------------------------------------------------
if docker ps --format '{{.Names}}' | grep -q '^wallia-fake-upstream-1$'; then
  warn "le faux fournisseur de tests tourne encore (wallia-fake-upstream-1) : outil de recette, à arrêter explicitement hors de ce script"
fi

log "démarrage de la base (seul service requis par les migrations)"
"${COMPOSE_DEPLOY[@]}" up -d --wait --wait-timeout 120 db

log "application des migrations avec l'image $IMAGE"
"${COMPOSE_DEPLOY[@]}" run -T api python -m app.migrate

log "démarrage des services applicatifs (db, api, worker uniquement)"
"${COMPOSE_DEPLOY[@]}" up -d db api worker

log "attente de la santé des trois services (état « healthy » exigé, jamais « running »)"
for service in db api worker; do
  if ! wait_healthy COMPOSE_DEPLOY "$service"; then
    die "santé du service '$service' non atteinte (healthy requis) — déploiement en échec (rollback : scripts/rollback.sh)"
  fi
  log "service $service : healthy"
done

log "vérification de l'image réellement portée par api et worker"
for service in api worker; do
  verify_service_image COMPOSE_DEPLOY "$service" "$IMAGE_ID" \
    || die "image effective de '$service' différente du snapshot immuable ($IMAGE_ID) — déploiement en échec (rollback : scripts/rollback.sh)"
  log "service $service : image $IMAGE_ID confirmée"
done

# --- Nouvel état courant --------------------------------------------------
python3 "$STATE_HELPER" write "$CURRENT_STATE" --stamp "$STAMP" \
  --image "$IMAGE" --image-id "$IMAGE_ID" --sha "$SHA" \
  --compose "$NEW_COMPOSE" --env "$NEW_ENV" \
  --note "déploiement de $IMAGE" >/dev/null
log "état courant enregistré (snapshot Compose rendu + env + image ID)"

log "déploiement applicatif terminé : image $IMAGE, services db/api/worker healthy"
log "étapes réservées au principal (non automatisées ici) :"
log "  1. vhost/TLS : deployment/wallia-tls-activate.sh sous verrou infra (voir deployment/README.md) ;"
log "  2. smoke HTTPS sans -k puis recette native sur la nouvelle image ;"
log "  3. en cas de problème : scripts/rollback.sh (snapshot précédent enregistré)."
