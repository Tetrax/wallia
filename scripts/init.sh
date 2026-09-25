#!/usr/bin/env bash
# Bootstrap local Wallia : dossiers runtime, secrets propres, base migrée,
# premier administrateur (aucun écran d'installation publique).
# N'affiche jamais de secret ; le mot de passe initial est écrit uniquement
# dans runtime/initial-access.txt (0600).
set -euo pipefail

source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
require_cmd docker
require_cmd openssl

ADMIN_EMAIL="${WALLIA_ADMIN_EMAIL:-admin@wallia.local}"
RUNTIME="$WALLIA_DIR/runtime"
SECRETS="$RUNTIME/secrets"
API_PORT="$(env_value WALLIA_API_PORT 13745)"

log "préparation des dossiers runtime"
mkdir -p "$RUNTIME"/{secrets,data/{uploads,documents,ingestion},evidence/container,backups}
# runtime/evidence appartient à l'opérateur (preuves) ; seul evidence/container
# est écrit par les conteneurs (uid 1002).

gen() { openssl rand -hex "$1"; }

write_secret() { # write_secret chemin contenu
  local path="$1" value="$2"
  umask 077
  printf '%s\n' "$value" > "$path"
  chmod 600 "$path"
}

if [[ ! -f "$SECRETS/db_password" ]]; then
  log "génération du mot de passe PostgreSQL (fichier dédié, non affiché)"
  write_secret "$SECRETS/db_password" "$(gen 24)"
else
  log "mot de passe PostgreSQL déjà présent (conservé)"
fi
if [[ ! -f "$SECRETS/session_secret" ]]; then
  log "génération du secret de session"
  write_secret "$SECRETS/session_secret" "$(gen 32)"
fi
if [[ ! -f "$SECRETS/worker_token" ]]; then
  log "génération du jeton interne worker"
  write_secret "$SECRETS/worker_token" "$(gen 32)"
fi

if [[ ! -f "$ENV_FILE" ]]; then
  log "écriture de runtime/secrets/app.env (configuration locale, sans secret)"
  cat > "$ENV_FILE" <<EOF
WALLIA_ENV=local
WALLIA_COOKIE_SECURE=0
WALLIA_ALLOWED_ORIGINS=http://localhost:${API_PORT},http://127.0.0.1:${API_PORT}
WALLIA_TRUSTED_PROXY_CIDRS=172.31.245.0/24,127.0.0.1/32,::1/128
WALLIA_PROVIDER_ALLOWED_DOMAINS=api.deepseek.com
WALLIA_PROVIDER_ENDPOINT=https://api.deepseek.com/v1
WALLIA_PROVIDER_MODEL=deepseek-flash
WALLIA_VISION_ENABLED=0
WALLIA_WEB_ENABLED=0
WALLIA_IMAGE=wallia:local
WALLIA_API_PORT=${API_PORT}
EOF
  chmod 600 "$ENV_FILE"
fi

# Les conteneurs tournent en uid 1002 : ils doivent lire/écrire ces dossiers.
log "attribution des dossiers runtime au service (uid 1002)"
for dir in "$SECRETS" "$RUNTIME/data"; do
  if have_sudo; then
    sudo -n chown -R 1002:1002 "$dir" 2>/dev/null || true
  fi
  chmod 750 "$dir" 2>/dev/null || true
done
# Preuves produites par les conteneurs : lisibles par l'opérateur (jamais de secret dedans).
if have_sudo; then
  sudo -n chown -R 1002:1002 "$RUNTIME/evidence/container" 2>/dev/null || true
fi
chmod 755 "$RUNTIME/evidence" "$RUNTIME/evidence/container" 2>/dev/null || true

# Le fichier d'accès initial doit rester lisible par l'opérateur tetrax : on le
# retire des dossiers du service.
INITIAL_ACCESS="$RUNTIME/initial-access.txt"

log "démarrage de la base (pgvector/pg17)"
"${COMPOSE[@]}" up -d db --wait

log "application des migrations"
"${COMPOSE[@]}" run --rm api python -m app.migrate

ADMIN_PASSWORD_FILE="$SECRETS/.admin_password_bootstrap"
if [[ ! -f "$INITIAL_ACCESS" ]]; then
  log "création du premier administrateur (aucun affichage)"
  write_secret "$ADMIN_PASSWORD_FILE" "$(gen 16)"
  create_output="$("${COMPOSE[@]}" run --rm api python -m app.cli create-admin --email "$ADMIN_EMAIL" --password-file /secrets/.admin_password_bootstrap 2>&1)" || true
  printf '%s\n' "$create_output" | grep -v -i "password" || true
  if printf '%s' "$create_output" | grep -q '"status": *"created"'; then
    umask 077
    cat > "$INITIAL_ACCESS" <<EOF
# Wallia — accès initial (confidentiel, ne pas diffuser)
url: http://127.0.0.1:${API_PORT}
email: ${ADMIN_EMAIL}
password: $(cat "$ADMIN_PASSWORD_FILE")
EOF
    chmod 600 "$INITIAL_ACCESS"
    if have_sudo; then
      sudo -n chown tetrax:tetrax "$INITIAL_ACCESS" 2>/dev/null && log "initial-access.txt attribué à tetrax (0600)" \
        || warn "attribution à tetrax impossible automatiquement (le principal ajustera l'ownership)"
    fi
    rm -f "$ADMIN_PASSWORD_FILE"
    log "accès initial écrit : $INITIAL_ACCESS (0600, non affiché)"
  else
    warn "création de l'administrateur non confirmée (déjà existant ?) — voir la sortie ci-dessus"
    rm -f "$ADMIN_PASSWORD_FILE"
  fi
else
  log "accès initial déjà présent : $INITIAL_ACCESS (conservé tel quel)"
fi

log "bootstrap terminé."
log "Le fournisseur de génération n'est PAS configuré à ce stade : l'application"
log "fonctionne en mode démonstration honnête jusqu'à la fourniture de la clé."
log "Clé à déposer par le principal dans : runtime/secrets/provider_api_key (0600)"
