#!/usr/bin/env bash
# Déploiement Wallia (à exécuter par le principal après revue).
#
#   scripts/deploy.sh --env-file runtime/secrets/app.production.env [--skip-build]
#
# Refuse un arbre sale ou un HEAD non poussé (mode livraison). N'écrit RIEN dans
# /etc : Nginx et TLS restent appliqués par le principal selon les conventions
# du VPS (voir deployment/README.md et deployment/nginx-wallia.conf).
set -euo pipefail

source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
require_cmd docker

ENV_FILE_ARG=""
SKIP_BUILD=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --env-file) ENV_FILE_ARG="${2:-}"; shift 2 ;;
    --skip-build) SKIP_BUILD=1; shift ;;
    *) die "argument inconnu: $1" ;;
  esac
done

[[ -n "$ENV_FILE_ARG" && -f "$ENV_FILE_ARG" ]] || die "fournir --env-file <fichier d'environnement de production>"

log "mode livraison : vérification arbre propre + HEAD poussé"
assert_clean_and_pushed

if [[ "$SKIP_BUILD" == "0" ]]; then
  bash "$WALLIA_DIR/scripts/build.sh" --deliver
fi

COMPOSE_PROD=(docker compose -p "$COMPOSE_PROJECT" -f "$WALLIA_DIR/docker-compose.yml" --env-file "$ENV_FILE_ARG")

log "démarrage des services (db + api + worker) avec $ENV_FILE_ARG"
"${COMPOSE_PROD[@]}" up -d --remove-orphans

log "attente de la santé locale"
PORT="$(grep -E '^WALLIA_API_PORT=' "$ENV_FILE_ARG" | tail -1 | cut -d= -f2 || true)"
PORT="${PORT:-13745}"
for _ in $(seq 1 60); do
  if curl -fsS "http://127.0.0.1:${PORT}/healthz" >/dev/null 2>&1; then
    log "API locale prête sur 127.0.0.1:${PORT}"
    break
  fi
  sleep 2
done

log "étapes réservées au principal (non automatisées ici) :"
log "  1. appliquer deployment/nginx-wallia.conf (adapter domaine/certificats) sous les conventions VPS ;"
log "  2. vérifier la paire TLS gérée + hook de renouvellement (deployment/tls-hook.sh adapté) ;"
log "  3. basculer WALLIA_ENV=production, WALLIA_COOKIE_SECURE=1, WALLIA_ALLOWED_ORIGINS=https://<domaine> ;"
log "  4. smoke HTTPS sans -k (curl --resolve) puis scripts/acceptance.sh all."
log "déploiement applicatif local terminé (aucune modification système effectuée)."
