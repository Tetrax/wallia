# syntax=docker/dockerfile:1.7
# Wallia — image unique API/worker (frontend intégré), CPU uniquement.

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
ARG EMBEDDING_MODEL_REVISION=614241f622f53c4eeff9890bdc4f31cfecc418b3

LABEL org.opencontainers.image.title="wallia" \
      org.opencontainers.image.description="Wallia — prototype de support technique (non officiel)" \
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

COPY backend/requirements-torch-cpu.txt /tmp/req-torch.txt
RUN pip install -r /tmp/req-torch.txt

COPY backend/requirements.txt backend/requirements-dev.txt /tmp/
RUN pip install -r /tmp/requirements-dev.txt

# Modèles CPU pinnés (embeddings E5 + Docling), aucune variante CUDA.
# HF_HUB_OFFLINE=1 (posé plus haut pour l'exécution) est levé le temps du téléchargement.
COPY scripts/fetch_models.py /tmp/fetch_models.py
RUN EMBEDDING_MODEL_REVISION=${EMBEDDING_MODEL_REVISION} HF_HUB_OFFLINE=0 python /tmp/fetch_models.py \
      --model-dir /opt/models/e5-small --docling-dir /opt/docling-models \
 && rm -f /tmp/fetch_models.py /tmp/req-torch.txt /tmp/requirements-dev.txt /tmp/requirements.txt

WORKDIR /app
COPY backend/ /app/
COPY resources/ /app/resources/
COPY fixtures/ /app/fixtures/
COPY --from=frontend /src/frontend/dist /app/static
RUN chown -R wallia:wallia /app /opt/models /opt/docling-models \
 && mkdir -p /data /secrets

ENV WALLIA_FRONTEND_DIR=/app/static \
    WALLIA_MODEL_DIR=/opt/models/e5-small \
    WALLIA_DOCLING_MODELS=/opt/docling-models \
    WALLIA_DATA_DIR=/data \
    WALLIA_SECRETS_DIR=/secrets

USER 1002:1002
EXPOSE 8000

HEALTHCHECK --interval=10s --timeout=5s --start-period=20s --retries=6 \
  CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4)" || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log", "--proxy-headers"]
