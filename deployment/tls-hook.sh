#!/usr/bin/env bash
# Wallia — hook de renouvellement TLS (délégué à l'activateur root).
#
# Appelé par certbot APRÈS un renouvellement, SOUS le verrou infra déjà
# détenu par certbot : ce hook ne reprend JAMAIS le verrou (aucune acquisition
# imbriquée, aucun deadlock de renouvellement).
#
# Politique : NO-OP pour toute lignée autre que wallia.valdev.me — ce hook
# peut être installé globalement sans toucher aux autres domaines du VPS.
# L'activateur est un chemin FIXE (root-owned, installé par l'opérateur) :
# aucun override d'environnement pour un script exécuté par root.
set -euo pipefail

WALLIA_DOMAIN="wallia.valdev.me"
ACTIVATOR="/usr/local/sbin/wallia-tls-activate"
lineage="${RENEWED_LINEAGE:-}"

if [[ "$lineage" != "/etc/letsencrypt/live/$WALLIA_DOMAIN" ]]; then
  echo "[wallia-tls] lignée « ${lineage:-absente} » différente de $WALLIA_DOMAIN : aucune action"
  exit 0
fi

if [[ ! -x "$ACTIVATOR" ]]; then
  echo "[wallia-tls] activateur absent ou non exécutable : $ACTIVATOR" >&2
  exit 1
fi

exec "$ACTIVATOR" --activate
