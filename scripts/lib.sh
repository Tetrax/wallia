#!/usr/bin/env bash
# Socle commun des scripts Wallia. Aucun secret n'est affiché.
set -euo pipefail

WALLIA_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# Le projet Compose est FIXE : jamais une variable d'environnement ne peut
# substituer un autre projet (les conteneurs de livraison sont wallia-*).
COMPOSE_PROJECT="wallia"
ENV_FILE="$WALLIA_DIR/runtime/secrets/app.env"
COMPOSE=(docker compose -p "$COMPOSE_PROJECT" -f "$WALLIA_DIR/docker-compose.yml")
if [[ -f "$ENV_FILE" ]]; then
  COMPOSE+=(--env-file "$ENV_FILE")
fi

log() { printf '[wallia] %s\n' "$*"; }
warn() { printf '[wallia] ATTENTION: %s\n' "$*" >&2; }
die() { printf '[wallia] ERREUR: %s\n' "$*" >&2; exit 1; }

require_cmd() { command -v "$1" >/dev/null 2>&1 || die "commande manquante: $1"; }

have_sudo() { sudo -n true 2>/dev/null; }

sha_short() { git -C "$WALLIA_DIR" rev-parse --short=12 HEAD 2>/dev/null || echo "sans-git"; }

# SHA COMPLET du commit courant (jamais abrégé) : c'est l'identité de livraison
# (nom d'image, label OCI, état de déploiement).
git_sha_full() { git -C "$WALLIA_DIR" rev-parse HEAD 2>/dev/null || echo ""; }

worktree_dirty() {
  ! git -C "$WALLIA_DIR" diff --quiet || ! git -C "$WALLIA_DIR" diff --cached --quiet \
    || [[ -n "$(git -C "$WALLIA_DIR" ls-files --others --exclude-standard)" ]]
}

# Le commit HEAD est-il RÉELLEMENT disponible sur origin ? Vérification EN
# LIGNE (interroge le remote, jamais le seul tracking local) : la branche
# distante doit pointer exactement sur le HEAD local.
head_on_origin() {
  local sha branch remote_sha
  sha="$(git_sha_full)"
  [[ -n "$sha" ]] || return 1
  branch="$(git -C "$WALLIA_DIR" rev-parse --abbrev-ref HEAD 2>/dev/null || echo "")"
  [[ -z "$branch" || "$branch" == "HEAD" ]] && return 1
  remote_sha="$(git -C "$WALLIA_DIR" ls-remote origin "refs/heads/$branch" 2>/dev/null | awk '{print $1}' | head -1)"
  [[ -n "$remote_sha" && "$remote_sha" == "$sha" ]]
}

assert_clean_and_pushed() {
  worktree_dirty && die "arbre de travail sale : livraison refusée (commit ou stash d'abord)"
  head_on_origin || die "HEAD absent de origin (vérification en ligne) : livraison refusée (pousser le commit d'abord)"
}

compose_running() {
  "${COMPOSE[@]}" ps --status running --services 2>/dev/null | grep -q . || return 1
}

env_value() { # env_value NOM défaut
  local name="$1" fallback="${2:-}"
  if [[ -f "$ENV_FILE" ]]; then
    local line
    line="$(grep -E "^${name}=" "$ENV_FILE" | tail -1 || true)"
    if [[ -n "$line" ]]; then printf '%s\n' "${line#*=}"; return; fi
  fi
  printf '%s\n' "$fallback"
}

# Santé OBLIGATOIREMENT « healthy » : un conteneur sans healthcheck, `running`,
# `starting` ou `unhealthy` n'est JAMAIS accepté (contrat de livraison — un
# conteneur qui ne fait que tourner ne prouve pas que le service est sain).
#
#   wait_healthy <nom-du-tableau-compose> <service> [timeout_secondes]
#
# Le timeout par défaut est WALLIA_HEALTH_TIMEOUT (180 s) ; le pas de sondage
# WALLIA_HEALTH_POLL (3 s). Utilisable par un trap (retourne 0/1, ne `die` pas).
wait_healthy() {
  local -n compose_ref="$1"
  local service="$2"
  local timeout="${3:-${WALLIA_HEALTH_TIMEOUT:-180}}"
  local poll="${WALLIA_HEALTH_POLL:-3}"
  local deadline container state
  deadline=$((SECONDS + timeout))
  while (( SECONDS < deadline )); do
    container="$("${compose_ref[@]}" ps -q "$service" 2>/dev/null || true)"
    if [[ -n "$container" ]]; then
      state="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}aucun-healthcheck{{end}}' "$container" 2>/dev/null || echo "inconnu")"
      if [[ "$state" == "healthy" ]]; then
        return 0
      fi
    fi
    sleep "$poll"
  done
  return 1
}

# ID d'image RÉELLEMENT porté par le conteneur d'un service (vérification
# après démarrage : la santé ne prouve pas que la bonne image tourne).
#
#   verify_service_image <nom-du-tableau-compose> <service> <image-id-attendu>
#
# Retourne 0 si `docker inspect {{.Image}}` du conteneur correspond à l'ID
# attendu ; retourne 1 (sans `die`) sinon : l'appelant décide du message et de
# la conduite (rollback ou échec explicite).
verify_service_image() {
  local -n compose_ref="$1"
  local service="$2" expected="$3"
  local container got
  container="$("${compose_ref[@]}" ps -q "$service" 2>/dev/null || true)"
  if [[ -z "$container" ]]; then
    warn "service $service : aucun conteneur inspectable (ID d'image non vérifiable)"
    return 1
  fi
  got="$(docker inspect -f '{{.Image}}' "$container" 2>/dev/null || true)"
  if [[ "$got" != "$expected" ]]; then
    warn "service $service : image effective ($got) différente de l'ID attendu ($expected)"
    return 1
  fi
  return 0
}

# Flux du fichier d'accès initial (jamais affiché). Utilise sudo si l'opérateur
# qui exécute le script n'en est pas le propriétaire.
initial_access_stream() {
  local file="$WALLIA_DIR/runtime/initial-access.txt"
  [[ -f "$file" ]] || die "runtime/initial-access.txt absent : exécuter scripts/init.sh"
  if [[ -r "$file" ]]; then
    cat "$file"
  elif have_sudo; then
    sudo -n cat "$file"
  else
    die "runtime/initial-access.txt illisible (ni direct, ni via sudo) : lancer le script en tant que son propriétaire"
  fi
}
