#!/usr/bin/env bash
# Wallia — rafraîchit les données de recette du harnais isolé (lot3).
#
# 1. export LECTURE SEULE du corpus réel (Db vivante) → runtime/tests-isolated/live-corpus.json
# 2. vecteurs E5 réels des questions d'acceptance → runtime/tests-isolated/acceptance-vectors.json
# 3. vecteurs E5 réels du jeu de calibration → runtime/tests-isolated/calibration-vectors.json
# 4. analyse de calibration lot3b → runtime/evidence/lot3b-calibration.json
#
# Aucun secret affiché ; aucun modèle chargé côté hôte (endpoint interne de l'API) ;
# aucune écriture dans la base vivante. Réexécutable par Astra.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/../lib.sh"

cd "$WALLIA_DIR"
mkdir -p runtime/tests-isolated runtime/evidence

python3 scripts/tests-isolated/export-live-corpus.py --out runtime/tests-isolated/live-corpus.json
python3 scripts/tests-isolated/fetch-embeddings.py --acceptance --out runtime/tests-isolated/acceptance-vectors.json
python3 scripts/tests-isolated/fetch-embeddings.py --calibration fixtures/calibration/calibration.json --out runtime/tests-isolated/calibration-vectors.json
python3 scripts/tests-isolated/run-calibration.py \
  --calibration fixtures/calibration/calibration.json \
  --vectors runtime/tests-isolated/calibration-vectors.json \
  --out runtime/evidence/lot3b-calibration.json

log "données de recette isolées rafraîchies (analyse : lot3b-calibration.json)."
