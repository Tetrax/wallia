#!/usr/bin/env bash
# Sauvegarde Wallia — bundle COHÉRENT (DB + fichiers), sans secrets.
#
#   scripts/backup.sh [--dry-run]
#   scripts/backup.sh --env-file <env-réel>          # runtime historique SANS état suivi
#
# Sélection du runtime (JAMAIS de fallback implicite) :
# - si `runtime/deploy-state/current.json` existe et est VALIDE, le snapshot
#   Compose rendu + le fichier d'environnement qu'il référence sont utilisés
#   (c'est la configuration réellement active suivie par deploy.sh) ;
# - sinon (runtime historique sans état), `--env-file` EXPLICITE est exigé :
#   le Compose du dépôt avec `runtime/secrets/app.env` n'est JAMAIS choisi
#   silencieusement.
#
# Invariants :
# - la découverte des services en cours (docker compose ps) est FATALE en cas
#   d'échec, AVANT toute pause/archive (une panne ne vaut pas « zéro service »
#   et ne doit jamais produire une sauvegarde à chaud) ; idem pour la
#   vérification réelle de l'arrêt ;
# - seuls les services api/worker INITIALEMENT ACTIFS sont arrêtés (la base
#   reste active) ; le trap de reprise est armé AVANT l'arrêt et reprend
#   EXACTEMENT ces services — même si l'arrêt échoue partiellement ;
# - archive, comptes de tables, références DB→fichiers et manifeste sont
#   calculés PENDANT la pause (jamais après la reprise) : cohérence garantie ;
# - le bundle est passé aux validations EXISTANTES `restore_lib.py validate`
#   (manifeste strict, SHA256 des archives, sûreté tar : liens/absolus
#   refusés) et une référence DB sans fichier rend la sauvegarde NON valide
#   (code de sortie non nul AVEC reprise des services) ;
# - la reprise exige api+worker « healthy » (healthchecks réels) ; si la
#   reprise échoue, le code de sortie est NON NUL même si la sauvegarde a
#   réussi ;
# - bundle horodaté sous runtime/backups (créé SANS -p : jamais d'écrasement,
#   même dans la même seconde), répertoire 0700, fichiers 0600 ; jamais de
#   secret ni de contenu utilisateur affiché.
#
# Sensibilité du bundle : la base sauvegardée contient les empreintes ET les
# jetons hachés de comptes/sessions ainsi que les données applicatives
# (conversations, pièces jointes…). Ce n'est PAS un export publiable ni un
# objet sans information sensible ; les clés externes (runtime/secrets,
# absentes du bundle) doivent être sauvegardées séparément. Le manifeste ne
# contient que des nombres et des chemins/empreintes de fichiers, aucun
# contenu.
set -euo pipefail

source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
require_cmd docker
require_cmd python3
require_cmd gzip

DRY_RUN=0
ENV_FILE_ARG=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) DRY_RUN=1; shift ;;
    --env-file) ENV_FILE_ARG="${2:-}"; shift 2 ;;
    *) die "argument inconnu: $1" ;;
  esac
done

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
BACKUPS="$WALLIA_DIR/runtime/backups"
BUNDLE="$BACKUPS/wallia-bundle-$STAMP"
DATA_DIR="$WALLIA_DIR/runtime/data"
STATE_HELPER="$WALLIA_DIR/scripts/delivery_state.py"
RESTORE_LIB="$WALLIA_DIR/scripts/restore_lib.py"
CURRENT_STATE="$WALLIA_DIR/runtime/deploy-state/current.json"

if [[ "$DRY_RUN" == "1" ]]; then
  log "mode dry-run : séquence prévue (aucune exécution)"
  log "  0. runtime : snapshot courant validé (current.json) sinon --env-file EXPLICITE exigé"
  log "  1. découverte FATALE des services en cours, trap de reprise armé AVANT l'arrêt"
  log "  2. stop des seuls services initialement actifs (db reste active) + vérification réelle de l'arrêt"
  log "  3. pg_dump → $BUNDLE/db.sql.gz"
  log "  4. archive $DATA_DIR (hors evidence) → $BUNDLE/data.tar.gz"
  log "  5. comptes des tables + références DB→fichiers pendant la pause (ON_ERROR_STOP)"
  log "  6. manifest.json pendant la pause (SHA256 archives + comptes + fichiers + références)"
  log "  7. validations restore_lib sur le bundle (manifeste, archives, tar) + références sans fichier = échec"
  log "  8. reprise des SEULS services initialement actifs, état « healthy » exigé (rc non nul sinon)"
  exit 0
fi

# --- Sélection du runtime (état courant validé sinon --env-file explicite) --
if [[ -f "$CURRENT_STATE" ]]; then
  python3 "$STATE_HELPER" validate-refs "$CURRENT_STATE" >/dev/null \
    || die "état courant ($CURRENT_STATE) incohérent : sauvegarde refusée AVANT toute pause/archive"
  SNAP_COMPOSE="$(python3 "$STATE_HELPER" read "$CURRENT_STATE" compose_snapshot)"
  SNAP_ENV="$(python3 "$STATE_HELPER" read "$CURRENT_STATE" env_snapshot)"
  BACKUP_COMPOSE=(docker compose -p "$COMPOSE_PROJECT" --project-directory "$WALLIA_DIR" -f "$SNAP_COMPOSE")
  log "runtime courant : snapshot Compose + env validés (current.json) — app.env du dépôt ignoré"
else
  [[ -n "$ENV_FILE_ARG" ]] \
    || die "aucun état courant suivi ($CURRENT_STATE absent) : runtime historique — fournir --env-file <fichier d'environnement RÉEL> (aucun fallback implicite vers runtime/secrets/app.env)"
  [[ -f "$ENV_FILE_ARG" ]] || die "--env-file introuvable: $ENV_FILE_ARG"
  ENV_FILE_ARG="$(cd "$(dirname "$ENV_FILE_ARG")" && pwd)/$(basename "$ENV_FILE_ARG")"
  BACKUP_COMPOSE=(docker compose -p "$COMPOSE_PROJECT" --project-directory "$WALLIA_DIR" -f "$WALLIA_DIR/docker-compose.yml" --env-file "$ENV_FILE_ARG")
  log "runtime historique (sans état suivi) : Compose du dépôt + --env-file explicite"
fi

# --- Découverte FATALE des services en cours (avant toute pause) ------------
ps_out=""
if ! ps_out="$("${BACKUP_COMPOSE[@]}" ps --status running --services 2>/dev/null)"; then
  die "découverte des services en échec (docker compose ps) : sauvegarde interrompue AVANT toute pause/archive — une panne n'est jamais « zéro service »"
fi
INITIALLY_RUNNING=()
while IFS= read -r service; do
  if [[ "$service" =~ ^(api|worker)$ ]]; then
    INITIALLY_RUNNING+=("$service")
  fi
done <<< "$ps_out"

# --- Bundle : création SANS -p (aucun écrasement, même seconde) -------------
mkdir -p "$BACKUPS"
chmod 700 "$BACKUPS"
if [[ -e "$BUNDLE" ]]; then
  die "bundle déjà existant ($BUNDLE) : relancer la sauvegarde (aucun écrasement, même dans la même seconde)"
fi
mkdir "$BUNDLE" || die "création du bundle impossible ($BUNDLE)"
chmod 700 "$BUNDLE"

RESUME_REQUIRED=0
RESUME_ATTEMPTED=0

resume_services() {
  [[ "$RESUME_ATTEMPTED" == "1" ]] && return 0
  RESUME_ATTEMPTED=1
  [[ ${#INITIALLY_RUNNING[@]} -gt 0 ]] || return 0
  log "reprise de api/worker (initialement actifs uniquement) : ${INITIALLY_RUNNING[*]}"
  local rc=0 service
  for service in "${INITIALLY_RUNNING[@]}"; do
    "${BACKUP_COMPOSE[@]}" start "$service" || rc=1
  done
  for service in "${INITIALLY_RUNNING[@]}"; do
    wait_healthy BACKUP_COMPOSE "$service" || { warn "santé de $service non atteinte (healthcheck « healthy » requis)"; rc=1; }
  done
  return "$rc"
}

finalize() { # trap EXIT : reprise garantie, code non nul si la reprise échoue
  local rc=$?
  set +e
  if [[ "$RESUME_REQUIRED" == "1" && "$RESUME_ATTEMPTED" == "0" ]]; then
    if ! resume_services; then
      warn "reprise de api/worker EN ÉCHEC — intervention manuelle requise"
      rc=1
    fi
  fi
  exit "$rc"
}
trap finalize EXIT

# --- Pause bornée -----------------------------------------------------------
if [[ ${#INITIALLY_RUNNING[@]} -gt 0 ]]; then
  RESUME_REQUIRED=1
  log "arrêt de api/worker uniquement (la base reste active) : ${INITIALLY_RUNNING[*]}"
  stop_rc=0
  for service in "${INITIALLY_RUNNING[@]}"; do
    "${BACKUP_COMPOSE[@]}" stop "$service" || stop_rc=1
  done
  # Vérification RÉELLE de l'arrêt (jamais supposé) : un échec de `ps` est
  # FATAL (l'état des services est inconnu, il n'est pas « arrêté »).
  still=()
  for _ in $(seq 1 5); do
    still=()
    if ! check_out="$("${BACKUP_COMPOSE[@]}" ps --status running --services 2>/dev/null)"; then
      die "vérification d'arrêt IMPOSSIBLE (docker compose ps en échec) : sauvegarde interrompue, aucune archive produite"
    fi
    for service in "${INITIALLY_RUNNING[@]}"; do
      if printf '%s\n' "$check_out" | grep -qx "$service"; then
        still+=("$service")
      fi
    done
    if [[ ${#still[@]} -eq 0 ]]; then
      break
    fi
    sleep 1
  done
  if [[ ${#still[@]} -gt 0 ]]; then
    die "arrêt de api/worker NON CONFIRMÉ (${still[*]}) : sauvegarde interrompue, aucune donnée modifiée"
  fi
  [[ "$stop_rc" == "0" ]] || warn "commande stop en erreur partielle mais services réellement arrêtés : sauvegarde poursuivie"
else
  warn "api/worker déjà arrêtés : sauvegarde des données au repos (aucune pause nécessaire)"
fi

log "dump PostgreSQL (cohérent, api/worker arrêtés)"
"${BACKUP_COMPOSE[@]}" exec -T db pg_dump -U wallia -d wallia --no-owner --clean --if-exists | gzip -9 -n > "$BUNDLE/db.sql.gz"
chmod 600 "$BUNDLE/db.sql.gz"

log "archive des données applicatives (hors evidence/cache éphémère)"
tar --sort=name --exclude './evidence' --exclude './evidence/*' -czf "$BUNDLE/data.tar.gz" -C "$DATA_DIR" .
chmod 600 "$BUNDLE/data.tar.gz"

log "comptes des tables importantes (nombres uniquement, erreur SQL fatale)"
COUNTS_FILE="$BUNDLE/.counts.json"
COUNTS_QUERY="SELECT 'users=' || count(*) FROM users UNION ALL SELECT 'conversations=' || count(*) FROM conversations UNION ALL SELECT 'messages=' || count(*) FROM messages UNION ALL SELECT 'attachments=' || count(*) FROM attachments UNION ALL SELECT 'documents=' || count(*) FROM documents UNION ALL SELECT 'chunks=' || count(*) FROM chunks UNION ALL SELECT 'ingestion_jobs=' || count(*) FROM ingestion_jobs UNION ALL SELECT 'schema_migrations=' || count(*) FROM schema_migrations"
"${BACKUP_COMPOSE[@]}" exec -T db psql -v ON_ERROR_STOP=1 -U wallia -d wallia -tA -c "$COUNTS_QUERY" > "$COUNTS_FILE"
chmod 600 "$COUNTS_FILE"

log "références DB→fichiers (pendant la pause, erreur SQL fatale)"
REFS_FILE="$BUNDLE/.db-files.json"
REFS_QUERY="SELECT 'attachments=' || stored_relpath FROM attachments UNION ALL SELECT 'documents=' || stored_relpath FROM documents"
"${BACKUP_COMPOSE[@]}" exec -T db psql -v ON_ERROR_STOP=1 -U wallia -d wallia -tA -c "$REFS_QUERY" > "$REFS_FILE"
chmod 600 "$REFS_FILE"

log "manifeste pendant la pause (archives, comptes, fichiers, références DB)"
python3 - "$BUNDLE" "$STAMP" "$DATA_DIR" "$REFS_FILE" "$COUNTS_FILE" <<'PY'
import hashlib
import json
import sys
from pathlib import Path
from pathlib import PurePosixPath

bundle = Path(sys.argv[1])
stamp = sys.argv[2]
data_dir = Path(sys.argv[3])
refs_file = Path(sys.argv[4])
counts_file = Path(sys.argv[5])

TABLES = ("users", "conversations", "messages", "attachments", "documents", "chunks", "ingestion_jobs", "schema_migrations")
REF_SOURCES = {"attachments": "uploads", "documents": "documents"}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def safe_rel(raw: str) -> str:
    candidate = PurePosixPath(raw)
    if not raw or raw.startswith("/") or any(part in ("..", "") for part in candidate.parts):
        raise SystemExit(f"référence DB invalide (chemin non sûr): {raw!r}")
    return raw


counts: dict = {}
for line in counts_file.read_text(encoding="utf-8").splitlines():
    key, _, value = line.strip().partition("=")
    if key in TABLES and value.isdigit():
        counts[key] = int(value)
if len(counts) != len(TABLES):
    raise SystemExit(f"comptes incomplets: {counts}")

files = []
paths = set()
for path in sorted(data_dir.rglob("*")):
    if not path.is_file():
        continue
    rel = path.relative_to(data_dir).as_posix()
    if rel.split("/", 1)[0] == "evidence":
        continue
    paths.add(rel)
    files.append({"path": rel, "size": path.stat().st_size, "sha256": sha256(path)})

db_files = []
db_files_missing = []
for line in refs_file.read_text(encoding="utf-8").splitlines():
    source, _, rel = line.strip().partition("=")
    if not source or not rel:
        continue
    if source not in REF_SOURCES:
        raise SystemExit(f"référence DB de source inconnue: {source!r}")
    full = f"{REF_SOURCES[source]}/{safe_rel(rel)}"
    db_files.append({"source": source, "path": full})
    if full not in paths:
        db_files_missing.append(full)
if db_files_missing:
    print(
        f"[wallia] ATTENTION: {len(db_files_missing)} référence(s) DB sans fichier dans les données "
        "(sauvegarde NON valide : la restauration échouera tant que ce n'est pas résolu)",
        file=sys.stderr,
    )

manifest = {
    "stamp": stamp,
    "archives": {
        name: {"size": (bundle / name).stat().st_size, "sha256": sha256(bundle / name)}
        for name in ("db.sql.gz", "data.tar.gz")
    },
    "tables": counts,
    "files": files,
    "files_count": len(files),
    "db_files": db_files,
    "db_files_missing": db_files_missing,
    "note": (
        "sans clé externe (runtime/secrets exclu) mais la base contient les empreintes ET les jetons hachés "
        "de comptes/sessions ainsi que les données applicatives — bundle à protéger comme les données de "
        "production, PAS un export publiable ; comptes = nombres, jamais des contenus"
    ),
}
(bundle / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
print(json.dumps({"bundle": str(bundle), "archives": manifest["archives"], "tables": manifest["tables"], "files_count": len(files), "db_files": len(db_files)}, ensure_ascii=False, indent=1))
PY
rm -f "$COUNTS_FILE" "$REFS_FILE"
chmod 600 "$BUNDLE/manifest.json"

# --- Validations restore_lib sur le bundle (AVANT succès) -------------------
log "validations restore_lib sur le bundle (manifeste strict, SHA256, sûreté tar)"
if ! python3 "$RESTORE_LIB" validate "$BUNDLE" >/dev/null; then
  die "bundle non conforme aux validations restore_lib (voir message ci-dessus) : sauvegarde NON valide — reprise des services"
fi
if ! python3 - "$BUNDLE" <<'PY'
import json
import sys
from pathlib import Path

manifest = json.loads((Path(sys.argv[1]) / "manifest.json").read_text(encoding="utf-8"))
missing = manifest.get("db_files_missing") or []
if missing:
    raise SystemExit(
        f"{len(missing)} référence(s) DB sans fichier dans les données (ex.: {missing[:3]}) — "
        "sauvegarde NON valide"
    )
PY
then
  die "référence(s) DB sans fichier : sauvegarde NON valide — reprise des services"
fi

# --- Reprise (santé obligatoire) --------------------------------------------
log "reprise de api/worker"
if resume_services; then
  RESUME_REQUIRED=0
else
  die "reprise de api/worker en échec (état « healthy » non atteint) — intervention manuelle requise"
fi

log "sauvegarde terminée : $BUNDLE (clés externes NON incluses ; bundle à protéger, non publiable)"
