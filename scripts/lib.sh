#!/usr/bin/env bash
# Socle commun des scripts Wallia. Aucun secret n'est affiché.
set -euo pipefail

WALLIA_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_PROJECT="${WALLIA_COMPOSE_PROJECT:-wallia}"
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

worktree_dirty() {
  ! git -C "$WALLIA_DIR" diff --quiet || ! git -C "$WALLIA_DIR" diff --cached --quiet \
    || [[ -n "$(git -C "$WALLIA_DIR" ls-files --others --exclude-standard)" ]]
}

head_pushed() {
  local branch upstream
  branch="$(git -C "$WALLIA_DIR" rev-parse --abbrev-ref HEAD 2>/dev/null || echo "")"
  [[ -z "$branch" || "$branch" == "HEAD" ]] && return 1
  upstream="$(git -C "$WALLIA_DIR" rev-parse --abbrev-ref --symbolic-full-name "@{upstream}" 2>/dev/null || echo "")"
  [[ -z "$upstream" ]] && return 1
  [[ "$(git -C "$WALLIA_DIR" rev-parse HEAD)" == "$(git -C "$WALLIA_DIR" rev-parse "$upstream")" ]]
}

assert_clean_and_pushed() {
  worktree_dirty && die "arbre de travail sale : livraison refusée (commit ou stash d'abord)"
  head_pushed || die "HEAD non poussé sur la branche distante : livraison refusée (push d'abord)"
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
