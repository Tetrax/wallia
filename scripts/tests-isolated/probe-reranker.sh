#!/usr/bin/env bash
# Wallia — LOT3C : probe EXPÉRIMENTAL de reranking sémantique CPU (cross-encoder).
#
# OBJECTIF : télécharger UN modèle de classement de passages (fichiers figés,
# révision immuable) puis exécuter la calibration + l'acceptance + les mesures
# de performance dans UN conteneur borné non-root, SANS réseau d'inférence,
# SANS socket Docker, SANS secrets, SANS data live, SANS toucher à la pile
# Wallia vivante (API/worker/DB laissés strictement intacts).
#
# Ce script :
#   1. copie en LECTURE SEULE (staging) les textes exportés nécessaires ;
#   2. télécharge les 7 fichiers autorisés du dépôt public `cross-encoder/
#      mmarco-mMiniLMv2-L12-H384-v1` à la révision 1427fd65… (aucun fichier
#      bin/ONNX/OpenVINO/train_script) ; vérifie la taille de chaque fichier et
#      le SHA256 des poids (5daeca2481…), écrit un manifeste de provenance ;
#   3. exécute `probe-reranker-eval.py` dans `wallia:local` avec des bornes
#      explicites (2 CPU, 1600 Mio, non-root 1002:1002, réseau none) ;
#   4. consigne l'état de l'API vivante AVANT/APRÈS (StartedAt/RestartCount/
#      santé) et le statut réel du conteneur (rc, OOMKilled).
#
# POLITIQUE lot3b conservée : AUCUN down/stop/restart/kill/rm, aucun cleanup.
# Le conteneur de probe est laissé à l'arrêt pour inspection (non destructif).
#
# Sorties : runtime/tests-isolated/reranker-probe-* (données + preuves) ;
# copies dans runtime/evidence/lot3c-*. Aucun secret n'est lu ni affiché.
set -euo pipefail
umask 022

WALLIA_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
ISO="$WALLIA_DIR/runtime/tests-isolated"
MODEL_DIR="$ISO/reranker-probe-model"
INPUT_DIR="$ISO/reranker-probe-input"
OUT_DIR="$ISO/reranker-probe-out"
EVIDENCE="$WALLIA_DIR/runtime/evidence"
IMAGE="${WALLIA_IMAGE:-wallia:local}"

REPO="cross-encoder/mmarco-mMiniLMv2-L12-H384-v1"
REV="1427fd652930e4ba29e8149678df786c240d8825"
BASE_URL="https://huggingface.co/${REPO}/resolve/${REV}"
WEIGHTS_SHA256="5daeca2481a76b5976a2bdc32f0a78532b6716da4f8cd3ff59460ef8d2f359b4"
LICENSE="Apache-2.0 (déclarée par la fiche du dépôt ; aucun fichier de code du dépôt n'est exécuté)"

log() { printf '[probe-reranker] %s\n' "$*"; }
stamp_utc() { date -u +%Y-%m-%dT%H:%M:%SZ; }
api_state() {
  docker inspect wallia-api-1 --format \
    '{{.Name}} StartedAt={{.State.StartedAt}} RestartCount={{.RestartCount}} health={{if .State.Health}}{{.State.Health.Status}}{{else}}n/a{{end}} OOM={{.State.OOMKilled}}'
}

mkdir -p "$MODEL_DIR" "$INPUT_DIR" "$OUT_DIR" "$EVIDENCE"
chmod 755 "$MODEL_DIR" "$INPUT_DIR"
chmod 777 "$OUT_DIR"

RUN_STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
CONTAINER_NAME="wallia-reranker-probe-$(date -u +%H%M%S)"

# ---------------------------------------------------------------------------
# 0) État de l'API vivante AVANT (lecture seule, aucune modification)
# ---------------------------------------------------------------------------
{
  echo "== état API vivante AVANT probe (lecture seule) =="
  api_state
  echo "pushes aucun ; aucun lifecycle appelé sur la pile wallia"
} | tee "$EVIDENCE/lot3c-reranker-api-before.txt"

# ---------------------------------------------------------------------------
# 1) Staging des entrées (copies, textes INTACTS ; l'original n'est jamais modifié)
# ---------------------------------------------------------------------------
cp -f "$WALLIA_DIR/fixtures/calibration/calibration.json" "$INPUT_DIR/calibration.json"
cp -f "$ISO/live-corpus.json" "$INPUT_DIR/live-corpus.json"
cp -f "$ISO/acceptance-vectors.json" "$INPUT_DIR/acceptance-vectors.json"
chmod 644 "$INPUT_DIR"/*.json
{
  sha256sum "$WALLIA_DIR/fixtures/calibration/calibration.json" "$INPUT_DIR/calibration.json"
  sha256sum "$ISO/live-corpus.json" "$INPUT_DIR/live-corpus.json"
  sha256sum "$ISO/acceptance-vectors.json" "$INPUT_DIR/acceptance-vectors.json"
} | tee "$OUT_DIR/staging-sha256.txt"
log "staging : 3 fichiers copiés (textes intacts, SHA enregistrés)"

# ---------------------------------------------------------------------------
# 2) Téléchargement du modèle (réseau UNIQUEMENT ici) — 7 fichiers autorisés
# ---------------------------------------------------------------------------
FILES=(README.md config.json special_tokens_map.json tokenizer.json tokenizer_config.json sentencepiece.bpe.model model.safetensors)
declare -A SIZE=(
  [README.md]=2278
  [config.json]=891
  [special_tokens_map.json]=239
  [tokenizer.json]=17082660
  [tokenizer_config.json]=435
  [sentencepiece.bpe.model]=5069051
  [model.safetensors]=470592698
)
for f in "${FILES[@]}"; do
  dest="$MODEL_DIR/$f"
  want="${SIZE[$f]}"
  if [[ -s "$dest" ]] && [[ "$(stat -c %s "$dest")" == "$want" ]]; then
    log "déjà présent (taille conforme) : $f"
    continue
  fi
  log "téléchargement : $f ($want octets attendus)"
  curl -fsSL --retry 3 --retry-delay 3 -o "$dest.part" "$BASE_URL/$f"
  mv "$dest.part" "$dest"
done
chmod 644 "$MODEL_DIR"/*
[[ -f "$MODEL_DIR/model.safetensors" ]] || { log "ÉCHEC : poids absents"; exit 2; }

WEIGHTS_GOT="$(sha256sum "$MODEL_DIR/model.safetensors" | cut -d' ' -f1)"
if [[ "$WEIGHTS_GOT" != "$WEIGHTS_SHA256" ]]; then
  log "ÉCHEC SHA256 des poids : attendu=$WEIGHTS_SHA256 obtenu=$WEIGHTS_GOT"
  exit 2
fi
log "SHA256 des poids vérifié : $WEIGHTS_GOT"

# Manifeste de provenance (fichiers/tailles/SHA/provenance/licence)
: > "$OUT_DIR/.manifest-entries.ndjson"
for f in "${FILES[@]}"; do
  jq -cn --arg name "$f" --argjson size "$(stat -c %s "$MODEL_DIR/$f")" \
        --arg sha "$(sha256sum "$MODEL_DIR/$f" | cut -d' ' -f1)" \
        --arg url "$BASE_URL/$f" \
        '{name:$name, size:$size, sha256:$sha, url:$url}' >> "$OUT_DIR/.manifest-entries.ndjson"
done
jq -s \
  --arg kind "reranker-probe-model-manifest" \
  --arg repo "$REPO" --arg rev "$REV" --arg license "$LICENSE" \
  --arg weights_sha256_expected "$WEIGHTS_SHA256" \
  --arg weights_sha256_verified "$WEIGHTS_GOT" \
  --arg downloaded_at "$(stamp_utc)" \
  --arg curl "$(curl --version | head -1)" \
  --argjson excluded '["pytorch_model.bin","onnx/*","openvino/*","train_script.py",".gitattributes"]' \
  --arg note "Dépôt public, aucun credential utilisé. Révision immuable pour tous les fichiers. Aucun code du dépôt n'est exécuté (pas de trust_remote_code) ; seuls config JSON, tokenizer et tenseurs safetensors sont lus." \
  '{kind:$kind, repo:$repo, revision:$rev, license:$license, downloaded_at:$downloaded_at, http_client:$curl, weights_sha256_expected:$weights_sha256_expected, weights_sha256_verified:$weights_sha256_verified, excluded_files:$excluded, note:$note, files:.}' \
  "$OUT_DIR/.manifest-entries.ndjson" > "$MODEL_DIR/MANIFEST.json"
chmod 644 "$MODEL_DIR/MANIFEST.json"
log "manifeste écrit : $MODEL_DIR/MANIFEST.json"

DISK_BYTES="$(du -sb "$MODEL_DIR" | cut -f1)"
{
  echo "== taille disque modèle =="
  du -sb "$MODEL_DIR"
  echo "== image =="
  docker image inspect "$IMAGE" --format 'id={{.Id}} created={{.Created}} revision={{index .Config.Labels "org.opencontainers.image.revision"}}'
} | tee "$EVIDENCE/lot3c-reranker-model-disk.txt"

# ---------------------------------------------------------------------------
# 3) Exécution bornée (inférence offline, réseau none, non-root)
# ---------------------------------------------------------------------------
log "conteneur : $CONTAINER_NAME (network none, 2 CPU, 1600 Mio, uid 1002)"
set +e
docker run --name "$CONTAINER_NAME" \
  --network none \
  --memory 1600m --memory-swap 1600m --cpus 2 \
  --user 1002:1002 --cap-drop ALL --security-opt no-new-privileges:true \
  --pids-limit 256 --read-only --tmpfs /tmp:size=1024m \
  -e WALLIA_RERANKER_PROBE_CONTAINER=1 \
  -e HF_HOME=/tmp/hf -e HF_HUB_OFFLINE=1 -e TRANSFORMERS_OFFLINE=1 -e HOME=/tmp \
  -e PYTHONDONTWRITEBYTECODE=1 -e PYTHONUNBUFFERED=1 -e TOKENIZERS_PARALLELISM=false \
  -e OMP_NUM_THREADS=2 -e MKL_NUM_THREADS=2 \
  -v "$WALLIA_DIR/scripts/tests-isolated/probe-reranker-eval.py:/probe/probe-reranker-eval.py:ro" \
  -v "$INPUT_DIR/calibration.json:/inputs/calibration.json:ro" \
  -v "$INPUT_DIR/live-corpus.json:/inputs/live-corpus.json:ro" \
  -v "$INPUT_DIR/acceptance-vectors.json:/inputs/acceptance-vectors.json:ro" \
  -v "$MODEL_DIR:/model:ro" \
  -v "$OUT_DIR:/out" \
  "$IMAGE" \
  python /probe/probe-reranker-eval.py \
    --model /model \
    --calibration /inputs/calibration.json \
    --corpus /inputs/live-corpus.json \
    --queries /inputs/acceptance-vectors.json \
    --out /out \
  2>&1 | tee "$EVIDENCE/lot3c-reranker-probe.log"
RC="${PIPESTATUS[0]}"
set -e
log "code de sortie réel du conteneur : $RC"

{
  echo "== conteneur de probe (post-run, laissé à l'arrêt pour inspection) =="
  docker inspect "$CONTAINER_NAME" --format \
    'name={{.Name}} image={{.Image}} rc={{.State.ExitCode}} oom={{.State.OOMKilled}} started={{.State.StartedAt}} finished={{.State.FinishedAt}} memory={{.HostConfig.Memory}} cpus={{.HostConfig.NanoCpus}} user={{.Config.User}} network={{.HostConfig.NetworkMode}} readonly={{.HostConfig.ReadonlyRootfs}}'
} | tee "$EVIDENCE/lot3c-reranker-container.txt"

# ---------------------------------------------------------------------------
# 4) État de l'API vivante APRÈS (comparaison exigée)
# ---------------------------------------------------------------------------
{
  echo "== état API vivante APRÈS probe (lecture seule) =="
  api_state
} | tee "$EVIDENCE/lot3c-reranker-api-after.txt"

# Copies d'évidence (jamais d'écrasement des preuves lot3/lot3b)
for f in "$OUT_DIR"/reranker-probe-*.json; do
  [[ -f "$f" ]] || continue
  cp -f "$f" "$EVIDENCE/lot3c-$(basename "$f")"
done
log "preuves copiées : $EVIDENCE/lot3c-reranker-probe-*.json"
log "ressources laissées en place (non destructif) : modèle=$MODEL_DIR, sorties=$OUT_DIR, conteneur=$CONTAINER_NAME (arrêté)"
log "FIN probe lot3c rc=$RC"
exit "$RC"
