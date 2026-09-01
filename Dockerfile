# ============================================================================
# Aether-Vault Engine — ONE image, ONE container running ALL subservices.
#
# v1.2.2 consolidation: the previous split (`aether-vault-server` image with
# uvicorn only + a separate `aether-vault-webui` Node image) collapses into this
# single multi-stage build producing ghcr.io/leon1706-lol/aether-vault-engine.
# Both runtimes ship in the final layer: Python 3.12 runs the FastAPI registry,
# Node 20 serves the Next.js standalone dashboard; docker/engine-entrypoint.sh
# supervises both inside one container (AV_ENGINE_ROLE=all default).
#
# Legacy aliasing (one transition cycle, removed next release): the release and
# edge workflows ALSO push this exact image under the historical
# aether-vault-server / aether-vault-webui repository names, so existing
# installs whose pinned compose files pull those tags keep working unchanged —
# the entrypoint auto-detects which role a legacy per-service container wants
# from its environment (DATABASE_URL set → server-only; NEXT_PUBLIC_API_URL set
# without it → webui-only).
#
# Stage invariant (Probleme.md #69): the runtime Python minor MUST match the
# py-builder's (a cp312 wheel is rejected by a 3.11 interpreter). Keep them in sync.
# ============================================================================

# ── Stage 1: py-builder — C++17 core + wheel (unchanged logic) ──────────────
FROM python:3.12-slim-bookworm AS py-builder
RUN apt-get update && apt-get install -y build-essential cmake g++
COPY requirements.txt setup.py pyproject.toml /build/
COPY src /build/src
COPY python /build/python
WORKDIR /build
RUN pip install pybind11 && pip wheel . -w /wheels --no-deps

# ── Stage 2: web-builder — Next.js standalone output ────────────────────────
FROM node:20-bookworm-slim AS web-builder
WORKDIR /build/webui
COPY webui/package.json webui/package-lock.json ./
RUN npm ci
COPY webui ./
ENV NEXT_TELEMETRY_DISABLED=1 \
    NEXT_PUBLIC_API_URL=http://localhost:8000
RUN npm run build

# ── Runtime: BOTH runtimes, one image ───────────────────────────────────────
FROM python:3.12-slim-bookworm
# NodeSource Node 20 on top of the python base — node-fetch-style healthchecks
# and the standalone Next server share this interpreter-free runtime layer.
# procps (pkill/ps/etc.) is NOT in python:3.12-slim by default — needed both for
# `docker exec <container> pkill ...` in e2e-engine-smoke's independent-restart CI
# check and for real operational debugging (`docker exec -it ... ps aux`).
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates gnupg procps \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && apt-get purge -y gnupg \
    && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

COPY --from=py-builder /wheels /wheels
RUN pip install --no-cache-dir /wheels/*.whl \
    && pip install --no-cache-dir fastapi uvicorn requests click \
    && mkdir -p /data && chmod 777 /data

# Next standalone bundle: server.js at /webui/server.js, static assets beside it.
COPY --from=web-builder /build/webui/.next/standalone /webui
COPY --from=web-builder /build/webui/.next/static /webui/.next/static
COPY --from=web-builder /build/webui/public /webui/public

COPY docker/engine-entrypoint.sh /engine-entrypoint.sh
RUN chmod +x /engine-entrypoint.sh

ENV AV_DATA_DIR=/data \
    AV_ENGINE_ROLE=all \
    WEBUI_PORT=3000 \
    HOSTNAME=0.0.0.0 \
    NODE_ENV=production
EXPOSE 8000 3000
ENTRYPOINT ["/engine-entrypoint.sh"]
