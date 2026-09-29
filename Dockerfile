# syntax=docker/dockerfile:1.7
# Wallia — image unique API/worker (frontend intégré), CPU uniquement.
# Modèles pinnés (E5, reranker, Docling layout Heron/tableformer), un seul
# format de poids, manifestes SHA256 vérifiés ; aucun bind de développement.

# --- Frontend (React/TS/Vite) ---
FROM node:22-bookworm-slim AS frontend
WORKDIR /src/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

# --- Python runtime ---
FROM python:3.12-slim-bookworm AS runtime

ARG WALLIA_GIT_SHA=dev
ARG WALLIA_BUILD_DATE=unknown

LABEL org.opencontainers.image.title="wallia" \
      org.opencontainers.image.description="Wallia — prototype de support technique (non officiel)" \
      org.opencontainers.image.source="https://github.com/Tetrax/wallia" \
      org.opencontainers.image.revision="${WALLIA_GIT_SHA}" \
      org.opencontainers.image.created="${WALLIA_BUILD_DATE}"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/tmp \
    WALLIA_GIT_SHA=${WALLIA_GIT_SHA} \
    HF_HUB_OFFLINE=1 \
    TOKENIZERS_PARALLELISM=false \
    OMP_NUM_THREADS=2 \
    MKL_NUM_THREADS=2

RUN apt-get update \
 && apt-get install -y --no-install-recommends libgomp1 libglib2.0-0 libgl1 \
 && rm -rf /var/lib/apt/lists/* \
 && groupadd -g 1002 wallia \
 && useradd -u 1002 -g 1002 -M -d /tmp -s /usr/sbin/nologin wallia

# Dépendances Python : lock versionné RÉEL (pip freeze de l'environnement de
# référence). torch CPU d'abord, depuis l'index CPU officiel (aucune variante
# CUDA possible), puis le lock complet — aucune résolution flottante.
COPY backend/requirements-torch-cpu.txt backend/requirements.lock.txt /tmp/
RUN pip install -r /tmp/requirements-torch-cpu.txt \
 && pip install -r /tmp/requirements.lock.txt \
 && python -c "import torch; assert torch.__version__.endswith('+cpu'), torch.__version__"

# Modèles CPU pinnés (E5 + reranker + Docling layout/tableformer) à révisions
# immuables, fichiers explicitement listés, un seul format de poids
# (safetensors) ; manifestes SHA256 écrits PUIS vérifiés. HF_HUB_OFFLINE=1
# (posé pour l'exécution) n'est levé que pendant ce téléchargement explicite.
COPY scripts/fetch_models.py /tmp/fetch_models.py
RUN HF_HUB_OFFLINE=0 python /tmp/fetch_models.py \
      --model-dir /opt/models/e5-small \
      --reranker-dir /opt/models/reranker \
      --docling-dir /opt/docling-models \
 && python /tmp/fetch_models.py --verify \
      --model-dir /opt/models/e5-small \
      --reranker-dir /opt/models/reranker \
      --docling-dir /opt/docling-models \
 && rm -f /tmp/fetch_models.py /tmp/requirements-torch-cpu.txt /tmp/requirements.lock.txt

WORKDIR /app
COPY backend/ /app/
COPY resources/ /app/resources/
COPY fixtures/ /app/fixtures/
COPY --from=frontend /src/frontend/dist /app/static
# Poids et code restent root-owned (immuables au runtime) ; seuls les droits de
# LECTURE/traversée sont ouverts au non-root : aucun `chown -R` massif des
# poids, et les fichiers copiés en 0600 restent lisibles par l'application.
RUN chmod -R a+rX /app /opt/models /opt/docling-models \
 && mkdir -p /data /secrets

ENV WALLIA_FRONTEND_DIR=/app/static \
    WALLIA_MODEL_DIR=/opt/models/e5-small \
    WALLIA_RERANKER_MODEL_DIR=/opt/models/reranker \
    WALLIA_DOCLING_MODELS=/opt/docling-models \
    WALLIA_DATA_DIR=/data \
    WALLIA_SECRETS_DIR=/secrets

USER 1002:1002
EXPOSE 8000

HEALTHCHECK --interval=10s --timeout=5s --start-period=20s --retries=6 \
  CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4)" || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log", "--proxy-headers"]
