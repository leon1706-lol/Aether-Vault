# ============================================================================
# Aether-Vault Engine — ONE image, ONE container running ALL subservices.
#
# v1.2.2 consolidation: the previous split (`aether-vault-server` image with
# uvicorn only + a separate `aether-vault-webui` Node image) collapses into this
# single multi-stage build producing ghcr.io/leon1706-lol/aether-vault-engine.
# Both runtimes ship in the final layer: Python 3.12 runs the FastAPI registry,
# Node 20 serves the Next.js standalone dashboard; docker/engine-entrypoint.sh
# supervises both inside one container. AV_ENGINE_ROLE has NO default at the
# image level (see the ENV block below) — both compose files set it explicitly
# (=all); a container with it unset falls through to the entrypoint's own
# runtime auto-detect/default logic instead.
#
# Legacy aliasing: REMOVED as of v1.3.0 (see VERSIONING.md's "Removed in v1.3.0" entry) —
# the release/edge workflows no longer publish the historical aether-vault-server /
# aether-vault-webui repository-name aliases. This comment used to describe that
# publishing as still active (v1.3.4 correction). The entrypoint's legacy auto-detect
# (DATABASE_URL set → server-only; NEXT_PUBLIC_API_URL set without it → webui-only) is
# UNCHANGED and still works for any already-pulled legacy-shaped container — only the
# alias TAGS themselves stopped being published going forward.
#
# Stage invariant (Probleme.md #69): the runtime Python minor MUST match the
# py-builder's (a cp312 wheel is rejected by a 3.11 interpreter). Keep them in sync.
# ============================================================================

# v1.3.4 (W3c): build-time metadata, threaded through to every final stage's OCI LABELs
# below AND into the wheel's own version (via SETUPTOOLS_SCM_PRETEND_VERSION in
# py-builder) — without this, `.dockerignore` excluding `.git/` means setuptools-scm
# always falls back to "0.0.0.dev0" and EVERY published image reports that same fake
# version from `/api/health` regardless of what tag or commit it was actually built from
# (Probleme.md #69 already found the symptom; this is the actual fix, not just a
# description of it). All three default to a clearly-fake value so a build that forgets
# to pass them fails obviously rather than silently looking like a real release.
ARG AV_VERSION=0.0.0.dev0+unknown
ARG VCS_REF=unknown
ARG BUILD_DATE=unknown

# ── Stage 1: py-builder — C++17 core + wheel (unchanged logic) ──────────────
FROM python:3.12-slim-bookworm AS py-builder
ARG AV_VERSION
# setuptools-scm reads this in preference to trying (and, with no .git/ in the build
# context, always failing) to derive a version from VCS state -- see the top-of-file
# comment for why this matters. `pyproject.toml`'s `fallback_version = "0.0.0.dev0"`
# stays as the safety net for a genuinely tag-less local `pip install -e .`.
ENV SETUPTOOLS_SCM_PRETEND_VERSION=$AV_VERSION
RUN apt-get update && apt-get install -y build-essential cmake g++
COPY requirements.txt setup.py pyproject.toml /build/
COPY src /build/src
COPY python /build/python
WORKDIR /build
# v1.3.4 (W0.10): was `pip wheel . -w /wheels --no-deps` — ONLY the local package's own
# wheel, with fastapi/uvicorn/requests/click fetched separately (and unpinned to this
# build) by each final stage below. pyproject.toml's `saml` extra comment also claimed
# "the native xmlsec1/libxml2 libraries (installed in the Dockerfile's engine/server
# targets)" — verified false, neither ever appeared here, so SSO/SAML were dead in every
# shipped image. Resolving `.[sso,saml,sign]` (base deps + SSO/SAML/signing) as ONE
# dependency graph here — where build-essential + the xmlsec build headers already exist
# — avoids two independently-resolved wheel sets ever disagreeing on a shared transitive
# version, and lets both final stages install everything from local wheels with zero
# network access.
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
# v1.3.0 fix (Probleme.md): MUST be named. Docker builds the LAST stage in the file when
# no --target/BuildKit `target:` is given, and v1.3.0 appended the `server`/`webui`
# targets below this one — an unnamed stage here would silently stop being the
# untargeted-build default the moment those were added (docker-compose.yml's `build: .`
# and both release workflows' main "Build and push engine image" step all rely on that
# default; every one of them now pins `target: engine` explicitly instead, but naming the
# stage itself is the belt-and-suspenders fix that survives a future reordering too).
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
# NodeSource Node 20 on top of the python base — node-fetch-style healthchecks
# and the standalone Next server share this interpreter-free runtime layer.
# procps (pkill/ps/etc.) is NOT in python:3.12-slim by default — needed both for
# `docker exec <container> pkill ...` in e2e-engine-smoke's independent-restart CI
# check and for real operational debugging (`docker exec -it ... ps aux`).
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates gnupg procps libxml2 libxmlsec1-openssl \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && apt-get purge -y gnupg \
    && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

COPY --from=py-builder /wheels /wheels
# v1.3.4 (W0.10): the separate `pip install fastapi uvicorn requests click` line is gone —
# py-builder's `.[sso,saml,sign]` wheel build above already resolved and built wheels for
# every base dependency, so this now installs the ENTIRE closure from local wheels with no
# network access, instead of re-resolving a subset of it fresh against PyPI every build.
RUN pip install --no-cache-dir /wheels/*.whl \
    && mkdir -p /data && chmod 777 /data

# Next standalone bundle: server.js at /webui/server.js, static assets beside it.
COPY --from=web-builder /build/webui/.next/standalone /webui
COPY --from=web-builder /build/webui/.next/static /webui/.next/static
COPY --from=web-builder /build/webui/public /webui/public

COPY docker/engine-entrypoint.sh /engine-entrypoint.sh
RUN chmod +x /engine-entrypoint.sh

# v1.2.5.4: AV_ENGINE_ROLE is deliberately NOT defaulted here. Both docker-compose.yml
# and python/av_cli/docker/docker-compose.release.yml already set it explicitly
# (AV_ENGINE_ROLE=all), so the consolidated topology is unaffected either way — but a
# Dockerfile-level default here would make engine-entrypoint.sh's own runtime env var
# ALWAYS non-empty, which silently disables its legacy auto-detect entirely (infers
# "server" from DATABASE_URL / "webui" from NEXT_PUBLIC_API_URL when AV_ENGINE_ROLE is
# unset) for every container from this image — exactly the scenario a pre-1.2.2 pinned
# compose file relies on, since it never sets AV_ENGINE_ROLE at all. Found live: an
# `engine-legacy`-style container (DATABASE_URL only, no AV_ENGINE_ROLE) started the
# webui too, because it silently inherited "all" from here instead of the entrypoint
# ever seeing an empty value to detect from. See Probleme.md.
ENV AV_DATA_DIR=/data \
    WEBUI_PORT=3000 \
    HOSTNAME=0.0.0.0 \
    NODE_ENV=production
EXPOSE 8000 3000
# v1.3.4 (W3c): this image had NO healthcheck of its own before this -- the dual
# python-urllib(/api/ready) + node-fetch(:3000/) probe here is the SAME one
# docker-compose.yml's own `healthcheck:` stanza already runs; baking it into the image
# means a bare `docker run` (no compose file supplying its own healthcheck) still gets a
# real health signal instead of none at all. `/api/ready` (not `/api/health`) deliberately
# — a container whose DB/Redis/data-dir isn't actually usable yet should show unhealthy,
# not just "the process is alive".
HEALTHCHECK --interval=10s --timeout=10s --start-period=40s --retries=5 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/api/ready').read()" \
      && node -e "fetch('http://localhost:3000/').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))"
ENTRYPOINT ["/engine-entrypoint.sh"]

# ============================================================================
# v1.3.0 (todo.md item 19): slim, single-role targets alongside the "all" image
# above — `docker build --target server` / `--target webui`. Each installs only
# the runtime its role actually uses (no Node in the server image, no Python in
# the webui image), reusing the exact same py-builder/web-builder artifacts and
# the exact same engine-entrypoint.sh (its role dispatch already never touches
# the other runtime — start_server()/start_webui() are fully independent, and
# were already exercised this way by legacy per-service alias containers). The
# default (untargeted) `docker build .` still produces the "all" image above,
# unchanged — these are opt-in additional targets, not a replacement.
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
# v1.3.4 (W0.10): see the engine stage's identical comment above — installs the full
# `.[sso,saml,sign]` closure from local wheels, no separate network-fetched line needed.
RUN pip install --no-cache-dir /wheels/*.whl \
    && mkdir -p /data && chmod 777 /data
COPY docker/engine-entrypoint.sh /engine-entrypoint.sh
RUN chmod +x /engine-entrypoint.sh
# Defaulted here (unlike the "all" image above, which deliberately leaves it
# unset so legacy alias auto-detect keeps working) because this image CANNOT
# run any other role — there's no Node runtime to fall back to, so failing
# obviously via AV_ENGINE_ROLE=server beats a silent role-detection surprise.
ENV AV_DATA_DIR=/data \
    AV_ENGINE_ROLE=server
EXPOSE 8000
# v1.3.4 (W3c): same reasoning as the engine stage's HEALTHCHECK above, python-only leg.
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
    && rm -rf /var/lib/apt/lists/*
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
# v1.3.4 (W3c): same reasoning as the engine stage's HEALTHCHECK above, node-only leg.
HEALTHCHECK --interval=10s --timeout=10s --start-period=40s --retries=5 \
  CMD node -e "fetch('http://localhost:3000/').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))"
ENTRYPOINT ["/engine-entrypoint.sh"]
