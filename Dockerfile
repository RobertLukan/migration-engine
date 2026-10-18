# Build on a machine with internet: dependencies are baked into the image.
# The resulting image runs without pulling anything at runtime (air-gapped lab).

FROM python:3.12-slim-bookworm

ARG MIGRATION_ENGINE_VERSION=2.2.1
ARG MIGRATION_ENGINE_GIT_REVISION=unknown

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    MIGRATION_ENGINE_CONFIG=/app/config.docker.yaml \
    MIGRATION_ENGINE_VERSION=${MIGRATION_ENGINE_VERSION} \
    MIGRATION_ENGINE_GIT_REVISION=${MIGRATION_ENGINE_GIT_REVISION}

WORKDIR /app

# Patch OS packages at build time; rebuild periodically so this layer picks up new fixes.
RUN apt-get update \
    && apt-get upgrade -y --no-install-recommends \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py worker.py ui.py states.py pve_safety.py config_validate.py ./
COPY static/ ./static/
COPY config.docker.example.yaml /app/config.docker.yaml

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=5)"
