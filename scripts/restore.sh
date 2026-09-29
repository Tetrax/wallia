#!/usr/bin/env bash
# Restauration Wallia depuis un bundle de sauvegarde (scripts/backup.sh).
#
#   scripts/restore.sh --isolated <bundle>
#
# V1 : restauration ISOLÉE uniquement — cible NEUVE garantie, base jetable sans
# port publié, aucun accès aux données de livraison. La restauration EN PLACE
# n'est plus exécutable par ce script : procédure opérateur CONTRÔLÉE décrite
# dans docs/operations.md (sauvegarde préalable, arrêt, restauration SQL et
# fichiers, vérifications, redémarrage).
#
# Invariants :
# - manifeste strict + SHA256 des archives vérifiés AVANT tout parsing ;
# - conteneur `wallia-restore-<unique>` : réseau none, aucun port, 512 Mo/1 CPU,
#   données PostgreSQL dans un volume dédié (jamais un tmpfs) ;
# - vérifications : comptes des 8 tables, fichiers (manquants/extra/altérés),
#   références DB→fichiers, erreurs SQL fatales (ON_ERROR_STOP) ;
# - AUCUN cleanup automatique : conteneur, volume et cible SONT la preuve et
#   restent en place ; les sorties de vérification (comptes/références) sont
#   privées, écrites SOUS la cible isolée et CONSERVÉES avec elle (aucune
#   suppression automatique, même temporaire) ; les commandes de nettoyage
#   sont affichées à la fin.
set -euo pipefail

source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
require_cmd docker
require_cmd python3
require_cmd gzip

MODE=""
BUNDLE=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --isolated) MODE="isolated"; BUNDLE="${2:-}"; shift 2 ;;
    --inplace) die "restauration en place retirée du script (V1) : procédure opérateur contrôlée dans docs/operations.md §10" ;;
    --yes) die "option --yes retirée : la restauration en place se fait selon la procédure contrôlée (docs/operations.md §10)" ;;
    *) die "argument inconnu: $1" ;;
  esac
done
[[ "$MODE" == "isolated" && -n "$BUNDLE" ]] || die "usage: restore.sh --isolated <bundle>"
[[ -d "$BUNDLE" ]] || die "bundle introuvable: $BUNDLE"

RESTORE_LIB="$WALLIA_DIR/scripts/restore_lib.py"

log "validation du bundle (manifeste STRICT + SHA256 archives + sûreté tar) — avant tout parsing"
python3 "$RESTORE_LIB" validate "$BUNDLE"

# --- Cible NEUVE et unique (jamais de réutilisation silencieuse) ------------
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
UNIQUE="$(python3 -c 'import secrets; print(secrets.token_hex(4))')"
NAME="wallia-restore-$STAMP-$UNIQUE"
VOLUME="$NAME-pgdata"
TARGET="$WALLIA_DIR/runtime/restore-isolated/$STAMP-$UNIQUE"
if [[ -e "$TARGET" ]]; then
  die "cible déjà existante ($TARGET) : la restauration isolée exige une cible neuve"
fi
mkdir -p "$(dirname "$TARGET")"
chmod 700 "$(dirname "$TARGET")"
mkdir "$TARGET"
chmod 700 "$TARGET"
if docker ps -a --format '{{.Names}}' | grep -qx "$NAME"; then
  die "conteneur $NAME déjà existant (collision improbable) : réessayer"
fi

# Sorties de vérification PRIVÉES (0700/0600) écrites SOUS la cible isolée et
# CONSERVÉES avec elle : aucun cleanup automatique, même temporaire.
VERIF_DIR="$TARGET/verification"
mkdir "$VERIF_DIR"
chmod 700 "$VERIF_DIR"
COUNTS_OUT="$VERIF_DIR/table-counts.txt"
REFS_OUT="$VERIF_DIR/db-file-refs.txt"

log "cible isolée : $TARGET (aucun port publié, base jetable $NAME, volume $VOLUME)"
DB_PASSWORD="$(python3 -c 'import secrets; print(secrets.token_hex(16))')"
docker run -d --name "$NAME" --network none \
  --memory 512m --cpus 1 \
  -e POSTGRES_DB=wallia -e POSTGRES_USER=wallia -e POSTGRES_PASSWORD="$DB_PASSWORD" \
  -v "$VOLUME:/var/lib/postgresql/data" \
  pgvector/pgvector:pg17 >/dev/null

ready=0
for _ in $(seq 1 60); do
  if docker exec "$NAME" pg_isready -U wallia -d wallia >/dev/null 2>&1; then
    ready=1
    break
  fi
  sleep 1
done
if [[ "$ready" != "1" ]]; then
  die "base isolée non prête — conteneur $NAME et cible $TARGET CONSERVÉS comme preuve (aucun cleanup automatique)"
fi

log "restauration SQL (ON_ERROR_STOP=1 : toute erreur SQL est fatale)"
if ! gunzip -c "$BUNDLE/db.sql.gz" | docker exec -i "$NAME" psql -v ON_ERROR_STOP=1 -U wallia -d wallia -q; then
  die "restauration SQL en échec (erreur fatale) — conteneur $NAME et cible $TARGET CONSERVÉS comme preuve"
fi

docker exec "$NAME" psql -U wallia -d wallia -tA -c \
  "SELECT 'users=' || count(*) FROM users UNION ALL SELECT 'conversations=' || count(*) FROM conversations UNION ALL SELECT 'messages=' || count(*) FROM messages UNION ALL SELECT 'attachments=' || count(*) FROM attachments UNION ALL SELECT 'documents=' || count(*) FROM documents UNION ALL SELECT 'chunks=' || count(*) FROM chunks UNION ALL SELECT 'ingestion_jobs=' || count(*) FROM ingestion_jobs UNION ALL SELECT 'schema_migrations=' || count(*) FROM schema_migrations" \
  > "$COUNTS_OUT"
chmod 600 "$COUNTS_OUT"
python3 "$RESTORE_LIB" verify-counts "$BUNDLE" "$COUNTS_OUT"

docker exec "$NAME" psql -U wallia -d wallia -tA -c \
  "SELECT 'attachments=' || stored_relpath FROM attachments UNION ALL SELECT 'documents=' || stored_relpath FROM documents" \
  > "$REFS_OUT"
chmod 600 "$REFS_OUT"

log "extraction des fichiers (membres réguliers uniquement, cible neuve)"
python3 "$RESTORE_LIB" extract "$BUNDLE" "$TARGET/data"
python3 "$RESTORE_LIB" verify-files "$BUNDLE" "$TARGET/data"
python3 "$RESTORE_LIB" verify-refs "$BUNDLE" "$TARGET/data" "$REFS_OUT"

log "restauration isolée VÉRIFIÉE : comptes des 8 tables, fichiers et références DB→fichiers conformes"
log "sorties de vérification CONSERVÉES sous la cible : $COUNTS_OUT, $REFS_OUT"
log "conteneur, volume et cible CONSERVÉS comme preuve — nettoyage EXPLICITE par l'opérateur :"
log "  docker rm -f $NAME && docker volume rm $VOLUME"
log "  rm -rf $TARGET"
