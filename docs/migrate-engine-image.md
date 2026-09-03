# Migrating off the legacy `aether-vault-server` / `aether-vault-webui` images

**Status: the legacy alias tags were removed at v1.3.0.** If your compose file (or a bare
`docker pull`/`docker run`) still references `ghcr.io/leon1706-lol/aether-vault-server` or
`ghcr.io/leon1706-lol/aether-vault-webui`, new pulls under those names now 404. Anything
you already pulled before v1.3.0 keeps running unaffected — this only stops NEW pulls
under the old names. See `VERSIONING.md`'s "Removed in v1.3.0" entry for the full history.

## Background

Through v1.2.1, Aether-Vault shipped two separate images: `aether-vault-server` (the
FastAPI registry) and `aether-vault-webui` (the Next.js dashboard). v1.2.2 consolidated
both into ONE image, `aether-vault-engine`, running both subservices in one container
(`AV_ENGINE_ROLE=all`) — supervised by `docker/engine-entrypoint.sh`, which independently
restarts a subservice that dies rather than always tearing the whole container down (see
`development/infrastructure.md`'s orchestrator readiness/liveness section). From v1.2.2
through v1.2.5, the SAME image was also published under the two old names as compatibility
aliases, so a pinned two-container compose file kept working with zero edits. v1.3.0
stopped publishing those alias tags.

## Two ways to migrate

### Option A: automated (`av doctor --compose`)

```bash
# Requires PyYAML: pip install PyYAML  (or `pip install aether-vault[docker]`)
av doctor --compose ./docker-compose.yml           # dry run — prints the rewrite
av doctor --compose ./docker-compose.yml --write   # applies it in place
```

This detects your compose file's legacy `aether-vault-server` / `aether-vault-webui`
services (by image name, or by the `DATABASE_URL` / `NEXT_PUBLIC_API_URL` environment
markers `engine-entrypoint.sh`'s own auto-detect already keys off of), merges them into
one `aether-vault-engine` service with `AV_ENGINE_ROLE=all`, the union of both services'
environment variables, both ports (`8000:8000` and `3000:3000`), the server service's
`depends_on`/`volumes`, and a `stop_grace_period: 30s` (carried forward, or set if
missing — see below for why this matters). Everything else in the file (other services
like `db`/`redis`, top-level `volumes:`, etc.) is left untouched.

It fails cleanly — without touching your file — if it can't find both a legacy service in
the shape it expects; it does not attempt a general-purpose compose rewrite, so a compose
file that doesn't match the standard two-container split needs the manual edit below
instead.

### Option B: manual edit

In your compose file / pull command:

1. Replace `ghcr.io/leon1706-lol/aether-vault-server` and
   `ghcr.io/leon1706-lol/aether-vault-webui` (wherever they appear, in whatever tag) with
   `ghcr.io/leon1706-lol/aether-vault-engine`.
2. Collapse the two services into one, exposing both ports (`8000:8000` and `3000:3000`).
3. Set `AV_ENGINE_ROLE=all` explicitly — don't rely on the old services' `DATABASE_URL`
   (server) / `NEXT_PUBLIC_API_URL` (webui) environment markers to auto-detect a role;
   with both env vars now present on one merged service, auto-detect would be ambiguous.
   (Auto-detect itself isn't removed — it still works for any already-pulled legacy-shaped
   *single-role* container; it's just not what you want for the new merged service.)
4. Carry over both services' environment variables onto the merged one.
5. Set `stop_grace_period: 30s` (or higher) on the merged service. `engine-entrypoint.sh`
   forwards `SIGTERM` to both subservices and waits up to `AV_ENGINE_STOP_GRACE_SECS`
   (default 25s) for in-flight requests to finish before force-killing — Docker's own
   default stop timeout is 10s, which would `SIGKILL` before that grace window elapses and
   can truncate an in-flight upload. Both compose files this project ships set this
   explicitly for exactly that reason.

## Prefer a single-role image instead?

If you deliberately want to keep running the registry and the dashboard as separate
containers (rather than moving to the one-container `AV_ENGINE_ROLE=all` topology), v1.3.0
also publishes real slim single-role images built from the Dockerfile's `server`/`webui`
build targets — `aether-vault-engine:server-latest` / `:webui-latest` (and matching
`-edge` tags off `master`). Each installs only the runtime its role needs (no Node in the
server image, no Python in the webui image) — smaller and leaner than running `:latest`
twice with `AV_ENGINE_ROLE` split between them. Point the server one at your database
exactly as the old `aether-vault-server` image was configured (`DATABASE_URL`, etc.), and
the webui one at `NEXT_PUBLIC_API_URL=http://<server-host>:8000` exactly as the old
`aether-vault-webui` image was.

## Verifying the migration

```bash
docker compose up -d
curl http://localhost:8000/api/health   # registry liveness
curl http://localhost:8000/api/ready    # registry readiness (DB/Redis/AV_DATA_DIR)
curl http://localhost:3000/             # webui
```

If `AV_ENGINE_ROLE` ends up unset on the merged service by mistake, the container's logs
will show a `[engine] DEPRECATED: ...` line naming whichever role it inferred — that's the
signal the merge didn't fully land; go back and confirm `AV_ENGINE_ROLE=all` is actually
present in the running container's environment (`docker inspect` or `docker exec ... env`).
