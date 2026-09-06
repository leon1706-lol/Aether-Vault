# ============================================================================
# Aether-Vault Engine — ONE image, ONE container running ALL subservices.
#
# Both runtimes ship in the final layer: Python 3.12 runs the FastAPI registry, Node 20
# serves the Next.js standalone dashboard; docker/engine-entrypoint.sh supervises both.
# AV_ENGINE_ROLE has no default at the image level -- both compose files set it
# explicitly (=all); an unset container falls through to the entrypoint's own
# auto-detect/default logic instead.
#
# Legacy aliasing (the historical aether-vault-server/-webui repository-name tags) was
# removed as of v1.3.0; the entrypoint's legacy auto-detect still works for any
# already-pulled legacy-shaped container.
#
# Stage invariant: the runtime Python minor must match the py-builder's (a cp312 wheel
# is rejected by a 3.11 interpreter). Keep them in sync.
# ============================================================================

# Build-time metadata, threaded through to every final stage's OCI LABELs and into the
# wheel's own version (via SETUPTOOLS_SCM_PRETEND_VERSION), since `.dockerignore`
# excluding `.git/` would otherwise make setuptools-scm always fall back to
# "0.0.0.dev0". All three default to a clearly-fake value so a build that forgets to
# pass them fails obviously rather than silently looking like a real release.
ARG AV_VERSION=0.0.0.dev0+unknown
ARG VCS_REF=unknown
ARG BUILD_DATE=unknown

# ── Stage 1: py-builder — C++17 core + wheel (unchanged logic) ──────────────
FROM python:3.12-slim-bookworm AS py-builder
ARG AV_VERSION
# setuptools-scm reads this in preference to deriving a version from VCS state, which
# always fails with no .git/ in the build context.
ENV SETUPTOOLS_SCM_PRETEND_VERSION=$AV_VERSION
RUN apt-get update && apt-get install -y build-essential cmake g++
COPY requirements.txt setup.py pyproject.toml /build/
COPY src /build/src
COPY python /build/python
WORKDIR /build
# Resolves `.[sso,saml,sign]` (base deps + SSO/SAML/signing) as one dependency graph
# here, where build-essential + the xmlsec build headers already exist, so both final
# stages can install everything from local wheels with zero network access.
RUN apt-get install -y --no-install-recommends libxml2-dev libxmlsec1-dev pkg-config \
    && pip install pybind11 \
    && pip wheel ".[sso,saml,sign]" -w /wheels

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
# Must be named: Docker builds the last stage in the file when no --target is given,
# and the `server`/`webui` targets are appended below this one -- naming it survives
# that ordering regardless of whether callers also pin `target: engine` explicitly.
FROM python:3.12-slim-bookworm AS engine
# ARGs declared before the first FROM (top of file) do NOT automatically carry into any
# stage -- each stage that wants one must re-declare it (Docker's own scoping rule).
ARG AV_VERSION
ARG VCS_REF
ARG BUILD_DATE
LABEL org.opencontainers.image.title="aether-vault-engine" \
      org.opencontainers.image.description="Aether-Vault registry (FastAPI) + webui (Next.js) in one container" \
      org.opencontainers.image.version="$AV_VERSION" \
      org.opencontainers.image.revision="$VCS_REF" \
      org.opencontainers.image.created="$BUILD_DATE" \
      org.opencontainers.image.source="https://github.com/leon1706-lol/Aether-Vault" \
      org.opencontainers.image.licenses="PolyForm-Noncommercial-1.0.0"
# procps (pkill/ps/etc.) is NOT in python:3.12-slim by default — needed both for
# `docker exec <container> pkill ...` in e2e-engine-smoke's CI check and for real
# operational debugging.
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates gnupg procps libxml2 libxmlsec1-openssl \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && apt-get purge -y gnupg \
    && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/* \
    # npm ships bundled with the nodejs package but is never invoked at runtime here,
    # and its own vendored dependencies carried real HIGH/CRITICAL CVEs unrelated to
    # this project's own code. Best-effort (`|| true`): worst case a no-op, never a build break.
    && rm -rf /usr/lib/node_modules/npm /usr/lib/node_modules/corepack \
              /usr/bin/npm /usr/bin/npx /usr/bin/corepack 2>/dev/null || true

COPY --from=py-builder /wheels /wheels
# Installs the entire dependency closure from local wheels with no network access,
# since py-builder's `.[sso,saml,sign]` wheel build already resolved everything.
RUN pip install --no-cache-dir /wheels/*.whl \
    && mkdir -p /data && chmod 777 /data

# Next standalone bundle: server.js at /webui/server.js, static assets beside it.
COPY --from=web-builder /build/webui/.next/standalone /webui
COPY --from=web-builder /build/webui/.next/static /webui/.next/static
COPY --from=web-builder /build/webui/public /webui/public

COPY docker/engine-entrypoint.sh /engine-entrypoint.sh
RUN chmod +x /engine-entrypoint.sh

# AV_ENGINE_ROLE is deliberately NOT defaulted here: a Dockerfile-level default would
# make the entrypoint's runtime env var always non-empty, silently disabling its legacy
# auto-detect (DATABASE_URL -> server / NEXT_PUBLIC_API_URL -> webui) for every
# pre-1.2.2 pinned compose file that never sets AV_ENGINE_ROLE at all.
ENV AV_DATA_DIR=/data \
    WEBUI_PORT=3000 \
    HOSTNAME=0.0.0.0 \
    NODE_ENV=production
EXPOSE 8000 3000
# Baking the same dual python-urllib(/api/ready) + node-fetch(:3000/) probe
# docker-compose.yml runs into the image means a bare `docker run` still gets a real
# health signal. `/api/ready`, not `/api/health`: a container whose DB/Redis/data-dir
# isn't usable yet should show unhealthy, not just "the process is alive".
HEALTHCHECK --interval=10s --timeout=10s --start-period=40s --retries=5 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/api/ready').read()" \
      && node -e "fetch('http://localhost:3000/').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))"
ENTRYPOINT ["/engine-entrypoint.sh"]

# ============================================================================
# Slim, single-role targets alongside the "all" image above — `docker build --target
# server` / `--target webui`. Each installs only the runtime its role actually uses,
# reusing the same py-builder/web-builder artifacts and the same entrypoint. The
# default (untargeted) `docker build .` still produces the "all" image, unchanged.
# ============================================================================

# ── Target: server — Python/FastAPI registry only, no Node ──────────────────
FROM python:3.12-slim-bookworm AS server
ARG AV_VERSION
ARG VCS_REF
ARG BUILD_DATE
LABEL org.opencontainers.image.title="aether-vault-engine-server" \
      org.opencontainers.image.description="Aether-Vault registry (FastAPI) only, no webui" \
      org.opencontainers.image.version="$AV_VERSION" \
      org.opencontainers.image.revision="$VCS_REF" \
      org.opencontainers.image.created="$BUILD_DATE" \
      org.opencontainers.image.source="https://github.com/leon1706-lol/Aether-Vault" \
      org.opencontainers.image.licenses="PolyForm-Noncommercial-1.0.0"
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates procps libxml2 libxmlsec1-openssl \
    && rm -rf /var/lib/apt/lists/*
COPY --from=py-builder /wheels /wheels
# Same reasoning as the engine stage above — installs the full `.[sso,saml,sign]`
# closure from local wheels, no separate network-fetched line needed.
RUN pip install --no-cache-dir /wheels/*.whl \
    && mkdir -p /data && chmod 777 /data
COPY docker/engine-entrypoint.sh /engine-entrypoint.sh
RUN chmod +x /engine-entrypoint.sh
# Defaulted here (unlike the "all" image, which leaves it unset for legacy auto-detect)
# because this image cannot run any other role -- failing obviously beats a silent
# role-detection surprise.
ENV AV_DATA_DIR=/data \
    AV_ENGINE_ROLE=server
EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=10s --start-period=40s --retries=5 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/api/ready').read()"
ENTRYPOINT ["/engine-entrypoint.sh"]

# ── Target: webui — Next.js standalone dashboard only, no Python ────────────
FROM node:20-bookworm-slim AS webui
ARG AV_VERSION
ARG VCS_REF
ARG BUILD_DATE
LABEL org.opencontainers.image.title="aether-vault-engine-webui" \
      org.opencontainers.image.description="Aether-Vault Next.js dashboard only, no registry" \
      org.opencontainers.image.version="$AV_VERSION" \
      org.opencontainers.image.revision="$VCS_REF" \
      org.opencontainers.image.created="$BUILD_DATE" \
      org.opencontainers.image.source="https://github.com/leon1706-lol/Aether-Vault" \
      org.opencontainers.image.licenses="PolyForm-Noncommercial-1.0.0"
RUN apt-get update && apt-get install -y --no-install-recommends curl procps \
    && rm -rf /var/lib/apt/lists/* \
    # Same npm-removal reasoning as the engine stage above, at this base image's own
    # /usr/local path convention. Best-effort, never a build break.
    && rm -rf /usr/local/lib/node_modules/npm /usr/local/lib/node_modules/corepack \
              /usr/local/bin/npm /usr/local/bin/npx /usr/local/bin/corepack 2>/dev/null || true
COPY --from=web-builder /build/webui/.next/standalone /webui
COPY --from=web-builder /build/webui/.next/static /webui/.next/static
COPY --from=web-builder /build/webui/public /webui/public
COPY docker/engine-entrypoint.sh /engine-entrypoint.sh
RUN chmod +x /engine-entrypoint.sh
ENV WEBUI_PORT=3000 \
    HOSTNAME=0.0.0.0 \
    NODE_ENV=production \
    AV_ENGINE_ROLE=webui
EXPOSE 3000
HEALTHCHECK --interval=10s --timeout=10s --start-period=40s --retries=5 \
  CMD node -e "fetch('http://localhost:3000/').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))"
ENTRYPOINT ["/engine-entrypoint.sh"]
