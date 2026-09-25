#!/usr/bin/env bash
# Wallia — hook de renouvellement TLS (À ADAPTER AUX CONVENTIONS DU VPS).
#
# Ce hook est prévu pour être appelé par l'outil de gestion des certificats
# (certbot / paire gérée) APRÈS renouvellement. Il vérifie la paire active puis
# recharge Nginx. Il n'est pas installé automatiquement par l'implémenteur.
set -euo pipefail

CERT_NAME="${WALLIA_TLS_NAME:-wallia.valdev.me}"
CERT_DIR="${WALLIA_TLS_DIR:-/etc/letsencrypt/live/${CERT_NAME}}"

if [[ ! -f "$CERT_DIR/fullchain.pem" || ! -f "$CERT_DIR/privkey.pem" ]]; then
  echo "[wallia-tls] paire absente pour $CERT_NAME : rien à faire" >&2
  exit 1
fi

# La clé privée ne doit jamais être lisible par tous.
mode="$(stat -c '%a' "$CERT_DIR/privkey.pem")"
if [[ "$mode" != "600" && "$mode" != "640" ]]; then
  echo "[wallia-tls] permissions inhabituelles sur privkey.pem ($mode)" >&2
fi

openssl x509 -in "$CERT_DIR/fullchain.pem" -noout -enddate | sed 's/^/[wallia-tls] validité : /'
nginx -t
systemctl reload nginx
echo "[wallia-tls] Nginx rechargé après renouvellement de $CERT_NAME"
