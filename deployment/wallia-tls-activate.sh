#!/usr/bin/env bash
# Wallia — activateur TLS (opérateur root). Versionné dans le dépôt, prévu
# pour être installé root-owned sous /usr/local/sbin/wallia-tls-activate.
#
# Trois phases EXPLICITES :
#   wallia-tls-activate --install-http   # phase 1 : vhost HTTP (ACME) + probe local
#   wallia-tls-activate --stage-le       # phase 2 : paire LE SERVIE (vhost intermédiaire) + empreinte vérifiée
#   wallia-tls-activate --activate       # phase 3 : paire LE → paire gérée → vhost final
#   wallia-tls-activate --self-check --cert <f> --key <f> --host <h>
#                                        # validation d'une paire (sans privilège, sans mutation)
#
# SÉCURITÉ : ce script root lit UNIQUEMENT les modèles root-owned de
# /etc/wallia/tls-templates — jamais un fichier du dépôt (modifiable par
# l'utilisateur). L'installation des modèles et du script est faite par
# l'opérateur root, voir deployment/README.md ; ce script ne s'auto-installe pas.
# Aucune racine de chemin n'est modifiable par variable d'environnement.
#
# TRANSACTION : dès la PREMIÈRE mutation d'une phase, l'état précédent EXACT
# (fichier vhost + mode, entrée sites-enabled absente/lien avec sa cible
# exacte/fichier + mode, lien `active` mémorisé) est capturé ; si quoi que ce
# soit échoue ensuite (install, nginx -t/reload, probe, empreinte servie), un
# rollback UNIQUE restaure cet état exact — pas seulement après nginx/probe.
# Le code de sortie non nul est conservé ; aucun double rollback.
#
# VERROU INFRA EXTERNE : ce script ne prend JAMAIS le verrou
# /home/tetrax/workspace/.locks/valdev-infra.lock. L'appel manuel se fait SOUS
# verrou :   flock /home/tetrax/workspace/.locks/valdev-infra.lock \
#                /usr/local/sbin/wallia-tls-activate --install-http
# Le hook de renouvellement est déjà appelé sous le verrou de certbot et ne
# doit pas le reprendre (aucune acquisition imbriquée, aucun deadlock).
#
# Aucune sortie ne contient de clé privée : uniquement empreintes/sujets/états.
set -euo pipefail

DOMAIN="wallia.valdev.me"
LE_DIR="/etc/letsencrypt/live/$DOMAIN"
MANAGED_ROOT="/var/lib/wallia/certificates"
VHOST_DIR="/etc/nginx/sites-available"
VHOST_ENABLED_DIR="/etc/nginx/sites-enabled"
VHOST_FILE="$VHOST_DIR/$DOMAIN"
VHOST_BACKUP_DIR="/var/lib/wallia/vhost-backups"
ACME_WEBROOT="/var/www/wallia-acme"
# Modèles de vhost root-owned (installés par l'opérateur, jamais lus du dépôt).
TEMPLATES_DIR="/etc/wallia/tls-templates"
SNI_RETRIES="${WALLIA_TLS_SNI_RETRIES:-12}"
SNI_RETRY_DELAY="${WALLIA_TLS_SNI_DELAY:-2}"
OPENSSL_TIMEOUT="${WALLIA_TLS_OPENSSL_TIMEOUT:-10}"
# Probe ACME local : retries BORNÉS (anciens workers Nginx) — jamais un seul curl.
HTTP_PROBE_RETRIES="${WALLIA_TLS_HTTP_PROBE_RETRIES:-6}"
HTTP_PROBE_DELAY="${WALLIA_TLS_HTTP_PROBE_DELAY:-1}"

log() { printf '[wallia-tls] %s\n' "$*" >&2; }
die() { printf '[wallia-tls] ERREUR: %s\n' "$*" >&2; exit 1; }

require_root() {
  [[ "$(id -u)" == "0" ]] || die "opération réservée à root (installer/exécuter en tant que root)"
}

fingerprint() { # fingerprint <certificat> -> empreinte SHA256 (jamais la clé)
  openssl x509 -in "$1" -noout -fingerprint -sha256 2>/dev/null | cut -d= -f2
}

validate_pair() { # validate_pair <cert> <key> <host> — lève une erreur explicite
  local cert="$1" key="$2" host="$3" hostcheck fp_cert fp_key notbefore nb_epoch now_epoch
  [[ -f "$cert" ]] || die "certificat introuvable: $cert"
  [[ -f "$key" ]] || die "clé introuvable: $key"
  openssl x509 -in "$cert" -noout -checkend 0 >/dev/null 2>&1 \
    || die "certificat expiré ou invalide (checkend)"
  notbefore="$(openssl x509 -in "$cert" -noout -startdate 2>/dev/null | cut -d= -f2)"
  [[ -n "$notbefore" ]] || die "certificat sans date de début lisible (notBefore)"
  # notBefore est COMPARÉ à maintenant : un certificat futur n'est pas valide,
  # même s'il « n'est pas expiré ».
  nb_epoch="$(LC_ALL=C date -u -d "$notbefore" +%s 2>/dev/null || true)"
  [[ -n "$nb_epoch" ]] || die "date notBefore illisible ($notbefore)"
  now_epoch="$(date -u +%s)"
  (( nb_epoch <= now_epoch )) \
    || die "certificat PAS ENCORE VALIDE : notBefore ($notbefore) est dans le futur"
  # Comparaison EXPLICITE de la sortie checkhost : son code de sortie seul ne
  # prouve pas toujours la correspondance du nom.
  hostcheck="$(openssl x509 -in "$cert" -noout -checkhost "$host" 2>&1 || true)"
  case "$hostcheck" in
    *"does match certificate"*) ;;
    *) die "le certificat ne correspond pas à l'hôte $host ($hostcheck)";;
  esac
  # Clé PUBLIQUE des DEUX côtés (avec -pubout sur la clé privée) : comparer le
  # DER privé au DER public rejetterait toute paire réellement valide.
  fp_cert="$(openssl x509 -in "$cert" -noout -pubkey 2>/dev/null | openssl pkey -pubin -outform der 2>/dev/null | openssl sha256)"
  fp_key="$(openssl pkey -in "$key" -pubout -outform der 2>/dev/null | openssl sha256)"
  [[ -n "$fp_cert" && "$fp_cert" == "$fp_key" ]] || die "la clé privée ne correspond pas au certificat"
  log "paire valide pour $host — empreinte $(fingerprint "$cert") (notBefore $notbefore)"
}

replace_symlink() { # replace_symlink <cible-texte-exacte> <lien> — remplacement ATOMIQUE
  local target="$1" link="$2" tmp
  tmp="$(dirname "$link")/.$(basename "$link").tmp-$$"
  rm -f "$tmp"
  ln -s "$target" "$tmp"
  mv -T "$tmp" "$link"  # rename même système de fichiers : jamais d'état intermédiaire
}

# --- Transaction exacte (rollback UNIQUE sur tout échec après mutation) -----
TX_ARMED=0
ROLLED_BACK=0
VHOST_TOUCHED=0
ENABLED_TOUCHED=0
ACTIVE_TOUCHED=0
VHOST_STATE_KIND=""; VHOST_STATE_TARGET=""; VHOST_STATE_MODE=""; VHOST_STATE_BACKUP=""
ENABLED_STATE_KIND=""; ENABLED_STATE_TARGET=""; ENABLED_STATE_MODE=""; ENABLED_STATE_BACKUP=""
ACTIVE_STATE_KIND=""; ACTIVE_STATE_TARGET=""; ACTIVE_STATE_MODE=""; ACTIVE_STATE_BACKUP=""

_state_get() { local name="$1"; printf '%s' "${!name}"; }

capture_state() { # capture_state <chemin> <préfixe> — état EXACT : absent/lien/fichier + mode
  local path="$1" prefix="$2" kind="absent" target="" mode="" backup="" stamp n
  if [[ -L "$path" ]]; then
    kind="symlink"
    target="$(readlink "$path")"
  elif [[ -e "$path" ]]; then
    kind="file"
    mode="$(stat -c '%a' "$path")"
    mkdir -p "$VHOST_BACKUP_DIR"
    chmod 700 "$VHOST_BACKUP_DIR"
    stamp="$(date -u +%Y%m%dT%H%M%SZ)"
    # Préfixe de RÔLE obligatoire : vhost et sites-enabled partagent le même
    # basename dans le même processus/seconde — sans lui, la seconde capture
    # écraserait la première. Une sauvegarde existante n'est JAMAIS écrasée.
    backup="$VHOST_BACKUP_DIR/${prefix}-$(basename "$path").state-$stamp-$$"
    n=1
    while [[ -e "$backup" ]]; do
      backup="$VHOST_BACKUP_DIR/${prefix}-$(basename "$path").state-$stamp-$$-$n"
      n=$((n + 1))
    done
    install -o root -g root -m 0600 "$path" "$backup"
  fi
  printf -v "${prefix}_KIND" '%s' "$kind"
  printf -v "${prefix}_TARGET" '%s' "$target"
  printf -v "${prefix}_MODE" '%s' "$mode"
  printf -v "${prefix}_BACKUP" '%s' "$backup"
}

restore_state() { # restore_state <chemin> <préfixe> — restaure EXACTEMENT (lien/cible/mode/absence)
  local path="$1" prefix="$2" kind target mode backup
  kind="$(_state_get "${prefix}_KIND")"
  target="$(_state_get "${prefix}_TARGET")"
  mode="$(_state_get "${prefix}_MODE")"
  backup="$(_state_get "${prefix}_BACKUP")"
  case "$kind" in
    symlink)
      replace_symlink "$target" "$path"
      ;;
    file)
      if [[ -n "$backup" && -f "$backup" ]]; then
        install -o root -g root -m "${mode:-0644}" "$backup" "$path"
      else
        log "ATTENTION: copie de sauvegarde absente pour $path — état précédent NON restaurable"
        return 1
      fi
      ;;
    *)
      rm -f "$path"
      ;;
  esac
}

arm_transaction() { # armé UNIQUEMENT quand l'état exact a été capturé
  TX_ARMED=1
}

rollback_all() { # idempotent : jamais deux rollbacks ; tout échec de restauration est cumulé ; rc initial conservé par l'appelant
  [[ "$TX_ARMED" == "1" && "$ROLLED_BACK" == "0" ]] || return 0
  ROLLED_BACK=1
  TX_ARMED=0
  set +e
  local restored=0 failed=0
  log "ROLLBACK de la transaction TLS : restauration EXACTE de l'état précédent"
  if [[ "$VHOST_TOUCHED" == "1" ]]; then
    if restore_state "$VHOST_FILE" VHOST_STATE; then restored=1; else failed=1; fi
  fi
  if [[ "$ENABLED_TOUCHED" == "1" ]]; then
    if restore_state "$VHOST_ENABLED_DIR/$DOMAIN" ENABLED_STATE; then restored=1; else failed=1; fi
  fi
  if [[ "$ACTIVE_TOUCHED" == "1" ]]; then
    if restore_state "$MANAGED_ROOT/active" ACTIVE_STATE; then restored=1; else failed=1; fi
  fi
  # Une restauration en échec interdit TOUT message de succès global : l'état
  # précédent n'est pas intégralement rétabli, nginx n'est PAS rechargé sur un
  # état mixte, et l'intervention manuelle est signalée. Le code d'échec
  # initial (rc) reste celui conservé par l'appelant ; jamais de second rollback.
  if [[ "$failed" == "1" ]]; then
    log "ATTENTION: restauration PARTIELLE — au moins un fichier n'a PAS été restauré ; INTERVENTION MANUELLE requise (nginx non rechargé)"
    return 1
  fi
  if [[ "$restored" == "1" ]]; then
    if nginx_test_reload; then
      log "rollback appliqué : ancien vhost/sites-enabled/active restaurés, nginx rechargé"
    else
      log "ATTENTION: rollback appliqué mais nginx -t/reload en échec — intervention requise"
    fi
  else
    log "rollback : aucune mutation à restaurer"
  fi
  return 0
}

on_exit() {
  local rc=$?
  if [[ "$rc" != "0" ]]; then
    rollback_all
  fi
  exit "$rc"
}
trap on_exit EXIT

install_vhost() { # install_vhost <nom-du-modèle root-owned>
  local template="$1" source
  source="$TEMPLATES_DIR/$template"
  # Validation de la source AVANT toute mutation.
  [[ -f "$source" ]] || die "modèle root-owned introuvable: $source (voir deployment/README.md — installation des modèles)"
  # Capture de l'état EXACT avant armement : le rollback ne touche que ce qui a
  # été capturé, et rien n'est armé si la capture échoue.
  capture_state "$VHOST_FILE" VHOST_STATE
  capture_state "$VHOST_ENABLED_DIR/$DOMAIN" ENABLED_STATE
  arm_transaction
  VHOST_TOUCHED=1
  ENABLED_TOUCHED=1
  install -o root -g root -m 0644 "$source" "$VHOST_FILE"
  replace_symlink "$VHOST_FILE" "$VHOST_ENABLED_DIR/$DOMAIN"
  log "vhost installé : $VHOST_FILE (modèle $template)"
}

nginx_test_reload() {
  nginx -t || return 1
  systemctl reload nginx || return 1
}

verify_served_fingerprint() { # verify_served_fingerprint <empreinte attendue>
  local expected="$1" served attempt
  for attempt in $(seq 1 "$SNI_RETRIES"); do
    # Sonde BORNÉE : timeout autour d'openssl s_client (jamais de blocage).
    served="$(echo | timeout "$OPENSSL_TIMEOUT" openssl s_client -connect 127.0.0.1:443 -servername "$DOMAIN" 2>/dev/null \
      | openssl x509 -noout -fingerprint -sha256 2>/dev/null | cut -d= -f2 || true)"
    if [[ -n "$served" && "$served" == "$expected" ]]; then
      log "certificat réellement servi (SNI $DOMAIN) : $served"
      return 0
    fi
    sleep "$SNI_RETRY_DELAY"
  done
  log "empreinte servie (${served:-aucune}) différente de l'attendue ($expected) après $SNI_RETRIES tentatives"
  return 1
}

cmd_self_check() { # --self-check --cert <f> --key <f> --host <h>
  local cert="" key="" host=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --cert) cert="${2:-}"; shift 2 ;;
      --key) key="${2:-}"; shift 2 ;;
      --host) host="${2:-}"; shift 2 ;;
      *) die "argument inconnu (self-check): $1" ;;
    esac
  done
  [[ -n "$cert" && -n "$key" && -n "$host" ]] || die "self-check: --cert, --key et --host requis"
  validate_pair "$cert" "$key" "$host" >/dev/null
  printf '{"ok": true, "host": "%s", "fingerprint_sha256": "%s"}\n' "$host" "$(fingerprint "$cert")"
}

cmd_install_http() { # phase 1 : vhost HTTP ACME + probe local
  require_root
  mkdir -p "$ACME_WEBROOT/.well-known/acme-challenge"
  chmod 755 "$ACME_WEBROOT" "$ACME_WEBROOT/.well-known" "$ACME_WEBROOT/.well-known/acme-challenge"
  install_vhost "nginx-wallia-http-initial.conf"
  nginx_test_reload || die "nginx -t/reload en échec pour le vhost initial — rollback de la transaction TLS en cours"
  # Probe ACME local : fichier RÉEL écrit dans le vrai répertoire
  # .well-known/acme-challenge (nom alphanumérique, servi par la location ACME),
  # vérifié avec des retries BORNÉS (anciens workers Nginx), jamais un seul curl.
  local probe_name probe_file body attempt
  probe_name="wallia-probe-$(openssl rand -hex 8)"
  probe_file="$ACME_WEBROOT/.well-known/acme-challenge/$probe_name"
  printf 'wallia-acme-probe' > "$probe_file"
  body=""
  for attempt in $(seq 1 "$HTTP_PROBE_RETRIES"); do
    body="$(curl -fsS --max-time 10 -H "Host: $DOMAIN" \
      "http://127.0.0.1/.well-known/acme-challenge/$probe_name" 2>/dev/null || true)"
    if [[ "$body" == "wallia-acme-probe" ]]; then
      break
    fi
    sleep "$HTTP_PROBE_DELAY"
  done
  rm -f "$probe_file"
  [[ "$body" == "wallia-acme-probe" ]] \
    || die "probe ACME local en échec après $HTTP_PROBE_RETRIES tentative(s) : le webroot .well-known/acme-challenge n'est pas servi — rollback de la transaction TLS en cours"
  log "vhost HTTP initial actif, probe ACME local OK"
  log "étape suivante (sous verrou infra) : certbot certonly --webroot -w $ACME_WEBROOT -d $DOMAIN --cert-name $DOMAIN"
}

cmd_stage_le() { # phase 2 : paire LE SERVIE (vhost intermédiaire) avant la paire gérée
  require_root
  [[ -f "$LE_DIR/fullchain.pem" && -f "$LE_DIR/privkey.pem" ]] \
    || die "paire Let's Encrypt absente pour $DOMAIN ($LE_DIR)"
  log "phase 2 : paire LE validée puis SERVIE (vhost intermédiaire, avant toute bascule managed)"
  validate_pair "$LE_DIR/fullchain.pem" "$LE_DIR/privkey.pem" "$DOMAIN"
  install_vhost "nginx-wallia-le-bootstrap.conf"
  nginx_test_reload || die "nginx -t/reload en échec pour le vhost LE intermédiaire — rollback de la transaction TLS en cours"
  local expected
  expected="$(fingerprint "$LE_DIR/fullchain.pem")"
  verify_served_fingerprint "$expected" \
    || die "certificat LE non réellement servi — rollback de la transaction TLS en cours"
  log "phase 2 terminée : LE réellement servi ($expected). Étape suivante : --activate (paire gérée)"
}

cmd_activate() { # phase 3 : paire LE → paire gérée → vhost final
  require_root
  [[ -f "$LE_DIR/fullchain.pem" && -f "$LE_DIR/privkey.pem" ]] \
    || die "paire Let's Encrypt absente pour $DOMAIN ($LE_DIR)"

  log "validation de la paire LE (host/clé/validité/notBefore) — avant toute copie"
  validate_pair "$LE_DIR/fullchain.pem" "$LE_DIR/privkey.pem" "$DOMAIN"

  # Première mutation de la phase : génération immuable — transaction armée
  # AVANT (le moindre échec ultérieur restaure old vhost/enabled/active).
  arm_transaction
  mkdir -p "$MANAGED_ROOT"
  chmod 700 "$MANAGED_ROOT"
  local serial generation expected
  serial="$(openssl x509 -in "$LE_DIR/fullchain.pem" -noout -serial | cut -d= -f2)"
  generation="$MANAGED_ROOT/gen-$(date -u +%Y%m%dT%H%M%SZ)-${serial:0:12}"
  mkdir -p "$generation"
  chmod 700 "$generation"
  install -o root -g root -m 0644 "$LE_DIR/fullchain.pem" "$generation/fullchain.pem"
  install -o root -g root -m 0600 "$LE_DIR/privkey.pem" "$generation/privkey.pem"
  # Validation de la copie gérée AVANT bascule.
  validate_pair "$generation/fullchain.pem" "$generation/privkey.pem" "$DOMAIN"

  # Bascule ATOMIQUE du lien relatif active (rename), avec mémorisation de
  # l'état précédent EXACT (lien existant avec sa cible textuelle, ou absence).
  capture_state "$MANAGED_ROOT/active" ACTIVE_STATE
  ACTIVE_TOUCHED=1
  replace_symlink "$(basename "$generation")" "$MANAGED_ROOT/active"
  log "lien actif relatif : active -> $(readlink "$MANAGED_ROOT/active")"

  install_vhost "nginx-wallia-final.conf"
  nginx_test_reload || die "nginx -t/reload en échec pour le vhost final — rollback de la transaction TLS en cours"

  expected="$(fingerprint "$MANAGED_ROOT/active/fullchain.pem")"
  verify_served_fingerprint "$expected" \
    || die "certificat réellement servi non conforme — rollback de la transaction TLS en cours"
  log "activation terminée : paire gérée active ($expected), vhost final actif"
}

MODE="${1:-}"
shift || true
case "$MODE" in
  --install-http) cmd_install_http ;;
  --stage-le) cmd_stage_le ;;
  --activate) cmd_activate ;;
  --self-check) cmd_self_check "$@" ;;
  *) die "usage: wallia-tls-activate --install-http | --stage-le | --activate | --self-check --cert <f> --key <f> --host <h>" ;;
esac
