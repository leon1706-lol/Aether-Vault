# infrastructure

How to run Aether-Vault's Docker stack: what runs, how to start it, what to configure, and how to verify each piece. Architecture contracts live in [architecture.md](architecture.md); this file stays operational.

Stack components (v1.2.2 engine topology — ONE image, ONE container for the product):

- `aether-vault-engine` — ONE container running ALL subservices: FastAPI CAS registry on :8000 AND the Next.js dashboard on :3000, dispatched/supervised by `docker/engine-entrypoint.sh` (`AV_ENGINE_ROLE=all|server|webui`; default `all`). Image: built from the root Dockerfile (dev compose) or `ghcr.io/leon1706-lol/aether-vault-engine:latest` (release compose). The historical `aether-vault-server`/`-webui` images remain published as ALIASES of this same engine image for one transition cycle (removed next release); legacy per-service containers keep working via entrypoint role auto-detect.
- `db` — PostgreSQL 15 persistence (image `postgres:15-alpine`, container `aether-vault-db`)
- `redis` — RedisBloom existence cache (image `redis/redis-stack-server:latest`, container `aether-vault-redis`)

**Naming convention**: every container is prefixed `aether-vault-*`. This machine also runs the sibling project's containers (`aether-quant-*`) — never share volumes, networks, or compose projects between them, and never "clean up" docker resources by name wildcard across the two stacks.

## Data Flow

One lap through the running stack, so every later command has context:

1. The client hashes and stages files locally into `.av/objects/`, then commits locally.
2. Commits succeed even fully offline — they queue in `.av/pending_push`.
3. On push, objects are batch-checked then uploaded BEFORE commit rows; server tree rows reference object hashes.
4. Postgres persists Merkle trees, project-scoped refs, and metrics; RedisBloom caches object existence; shards land on the `vault_data` volume.
5. Dashboards, `av clone`, and `av pull` read all of it back through `/api/*`.

## Starting the stack

Start Postgres + Redis first; the engine brings registry AND dashboard up together:

```bash
docker compose up -d db redis aether-vault-engine
```

This dev compose deliberately maps `5432` and `6379` to the host so `tests/test_server.py` can connect directly from localhost. The RELEASE compose at `python/av_cli/docker/docker-compose.release.yml` removes those mappings on purpose — do not port them back.

```bash
curl http://localhost:8000/api/health   # registry leg
curl -sf http://localhost:3000/ >/dev/null && echo webui-ok   # dashboard leg
```

**Verified directly:** the engine healthcheck checks BOTH legs in one container — python-urllib against :8000 and node fetch against :3000 (both runtimes ship in-engine), `start_period: 40s`.

End users on Local mode never run any of this by hand — `av init` detects whether the backend is missing, unbuilt, or stopped and starts it automatically.

**Ops measurement task (not a feature):** benchmark #5 (cold clone / first pull) shipped with v1.1.1 but its measured row still reads "capture pending" in the README comparison table — capture it by running `av benchmark --markdown` against this live stack during the next Docker session and pasting the result into [BENCHMARKS.md](BENCHMARKS.md). CI now automates most other formerly-deferred verifications (server-tests/webui-e2e run the live-path suites on every push), so this manual capture is the only remaining one-off.

## Starting the Web UI

Prefer the CLI entry point; it checks before it rebuilds:

```bash
av webui              # opens http://localhost:3000; skips rebuild if already healthy
av webui --rebuild    # force a fresh image after editing webui/ source
```

The fast path matters: a healthy running container goes straight to opening the browser instead of re-running `docker compose` every time. Manual development against hot reload works too:

```bash
cd webui && npm install && npm run dev
```

If Protected mode is active, `av webui` appends a one-time `?av_token=` parameter that `TokenGate` consumes and strips — opening `localhost:3000` by hand shows the token prompt once instead.

## Environment Variables

Set in `docker-compose.yml` / `.env` for the stack; each has a concrete consequence when wrong:

```text
DATABASE_URL   postgresql+asyncpg://av_user:av_password@db:5432/aether_vault
               Wrong host/db name → server fails at init_db during startup, not lazily.
REDIS_URL      redis://redis:6379/0
               Unreachable → existence checks fall back slower; bloom filter init fails.
AV_DATA_DIR    /data   (default)
               Container-oriented default backed by the vault_data volume.
AV_API_TOKEN   (empty/unset = Anonymous mode)
               Set → every route except the auth-exempt paths requires Bearer auth.
AV_AUTH_USERS  (empty/unset = no per-user tokens)
               JSON map {"username": "token", ...}; a Bearer match here authenticates too.
               Invalid JSON fails server STARTUP loudly (never silently Anonymous).
AV_CORS_ORIGINS http://localhost:3000   (default; comma-separated; "*" reopens all)
               Anything not listed gets no Access-Control-Allow-Origin on responses.
AV_RATE_LIMIT_GC  10/minute   (default)
                Hard cap on the destructive GC endpoint, Anonymous mode included.
AV_RATE_LIMIT_DEFAULT  (empty = data plane unlimited)
                Opt-in cap for every other /api route; bulk uploads burst by design.
AV_GC_GRACE_SECONDS  3600   (default)
               Shrink for drills/E2E; 0 makes freshly uploaded orphans sweepable.
AV_EVENT_RETENTION_DAYS  30   (default)
               Event-stream retention; swept during GC and via DELETE /api/events.
AV_RATE_LIMIT_EVENTS  (empty = unlimited)
               Opt-in cap for the agent event/webhook surface (long-poll loops).
AV_COMMIT_UPLOAD  1   (default)
                =0 → every commit defers upload (queue drains via av push).
AV_AUDIT_LOG  1   (default)
                =0 disables the audit trail inserts.
AV_AUDIT_RETENTION_DAYS  90   (default)
                Audit-trail retention; swept during GC + DELETE /api/admin/audit?before_days=N.
AV_WEBHOOK_MAX_ATTEMPTS  5   (default)
                Webhook delivery attempts before a row dead-letters (status='dead').
AV_WEBHOOK_RETRY_INTERVAL_SECS  30   (default)
                Worker interval AND retry backoff step for failed webhook deliveries.
AV_ENGINE_ROLE  all   (container-side default)
                Engine dispatch: all | server | webui. Legacy alias containers WITHOUT
                this var auto-detect: DATABASE_URL set → server; NEXT_PUBLIC_API_URL
                without it → webui.
AV_REMOTE_URL  (CLI-side) default registry for av clone; else http://localhost:8000.
AV_AUTHOR      (CLI-side) commit author string; defaults to "anonymous".
AV_RUN_ID      (CLI-side) file subsequent commits under this run.
```

**Caution:** `AV_DATA_DIR`'s `/data` default is container-oriented. Bare-metal uvicorn MUST point it at a writable directory, or every object upload fails with PermissionError while `/api/health` stays green — the most misleading failure mode in the project. This exact failure broke CI `webui-e2e` once: uploads 500ed, seed pushes queued offline, the dashboard rendered empty, Playwright failed on element-not-found. Documented in [CHANGELOG.md](CHANGELOG.md); the fix lives as explicit env vars on both uvicorn-starting CI jobs.

## Protected Mode

Protected mode gates every route behind a credential. Manage it entirely from the CLI:

```bash
av auth set-token            # generate + apply + restart the server with it active
av auth set-token <token>    # join a registry someone else already protected
av auth clear                # back to Anonymous everywhere
av auth status               # masked report, never prints the token
```

Per-user tokens (v1.1.8) live beside the owner's shared secret as `AV_AUTH_USERS` — a JSON
map written to the same `.env` by the same plumbing:

```bash
av auth add-user alice            # generate alice's token, print it once, restart
av auth add-user bob <token>      # specific token instead of a generated one
av auth list-users                # masked overview
av auth remove-user bob           # revoke; last removal drops the line entirely
```

A Bearer token matching EITHER source authenticates; the resolved identity (`owner` for the
shared secret) stamps commits pushed with the default `anonymous` author — explicit
`AV_AUTHOR` values are never overwritten. Both maps are read once at process start; the CLI
restarts `aether-vault-server` after every change so a fresh process picks them up.

Setting a token writes `.env` next to the compose file and restarts `aether-vault-server` so the new process picks it up. Restart behavior is safe by construction: `/api/health` stays exempt, so the restarting CLI's own readiness wait never deadlocks behind the gate it just enabled.

Forgot the token? There is no reset flow — re-running `av auth set-token <new-token>` IS the recovery path (per-user tokens rotate via remove-user + add-user).

## Running the Test Suite

Full suite from a source checkout:

```bash
pip install -e .[dev]
pytest tests/
```

Without Docker, live-server tests skip cleanly — `tests/test_server.py` probes TCP reachability of Postgres/Redis (via `AV_TEST_DATABASE_URL` / `AV_TEST_REDIS_URL`) rather than using `importorskip`, so you get a clean skip, not an import error. The CI `server-tests` job provisions both as service containers and runs them for real; the `server-tests-windows` twin installs native PostgreSQL 15 + Memurai via Chocolatey (service containers don't exist on Windows runners).

The `av test` wrapper adds convenience variants:

```bash
av test --cov          # coverage report
av test --speed        # synthetic hot-path benchmark + slowest-test report
av test --webui        # also run the webui Vitest suite (npm test)
```

Web UI units and E2E are separate tiers — E2E needs the live stack plus a built app:

```bash
cd webui && npm test                    # Vitest units, no services needed
docker compose up -d db redis aether-vault-server
python webui/e2e/seed_data.py           # pushes 2 real commits via the actual av CLI
cd webui && npm run build && npm run start
npx playwright test dashboard.spec.ts weight-diff.spec.ts   # anonymous-mode specs
# then, to exercise Protected mode in the browser, restart the server with
# AV_API_TOKEN/AV_AUTH_USERS set and run: npx playwright test token-gate.spec.ts
```

**Standing rule:** never seed E2E data against a registry holding real work — `seed_data.py` creates its own throwaway repos but still talks to whatever registry is listening on :8000.

### End-to-End Scenario Suite (`scripts/e2e_scenario.sh`)

Product-level flows driven by the real CLI against a real server process — the layer unit and TestClient tests cannot reach. Runs in CI as the `e2e-suite` job; locally it works against any live Postgres:

```bash
AV_TEST_DATABASE_URL=postgresql+asyncpg://... AV_TEST_REDIS_URL=redis://... \
E2E_PSQL_URL=postgresql://... bash scripts/e2e_scenario.sh
```

Phase map: A clone→diverge→conflicting merge→`--theirs`→two-parent push · B offline queue drain across a real restart · C pre-Alembic volume heal + stamp on a real boot · D protected mode with per-user tokens (join/attribution/wrong-token/revocation) · E zero-grace GC sweep. Notes for local runs: on Windows use Git Bash (the script converts its temp paths via cygpath), keep psql on PATH, and pass options before the connection URI if calling psql yourself — MSYS-style getopt ignores `-c` after a positional URI.

## CI Job Map

Every product surface and the workflow that guards it (Tests workflow unless noted):

| Surface | Job(s) |
|---|---|
| Full stack-free suite, py3.10 + 3.14 (Windows build) | `test` matrix |
| In-between Pythons 3.11–3.13 | `nightly.yml` (`compat`) |
| Server live stack (Postgres+Redis): TestClient + real-wire | `server-tests` |
| Same, native Windows services | `server-tests-windows` |
| Product flows via real CLI: merge collaboration, offline drain, legacy upgrade, per-user auth, GC | `e2e-suite` (`scripts/e2e_scenario.sh`) |
| Engine image smoke (Phase I): role dispatch + dual healthchecks from ONE container | `e2e-engine-smoke` |
| Plugins incl. real Lightning training loop + signed-commit gate (`[sign]` extra) | `plugin-tests` |
| WebUI lint/typecheck/Vitest | `webui-tests` |
| WebUI browser E2E: dashboard, weight-diff, token gate | `webui-e2e` |
| sdist+wheel build & twine check | `package-build` |
| Wheel install smoke (Linux venv) | `smoke-wheel-linux` |
| Sdist compile-install smoke (Windows venv, MSVC path) | `smoke-sdist-windows` |
| `:edge` images on master pushes | `docker-edge.yml` |
| Wheels cp310–314 ×3 OS, PyPI, GitHub Release, GHCR | `release.yml` (tags) |
| Dependency freshness | Dependabot (pip/npm/actions, weekly) |

Known residuals (deliberate): no Docker-daemon-dependent `av update --docker` flow test, no macOS install smoke, Dependabot PRs need human merges.

## Database Migrations

The schema is owned by Alembic (`python/av_server/migrations/`); `create_all` is gone. Server startup runs the chain programmatically (`python/av_server/database.py::init_db`) — no alembic.ini, no manual step:

1. Fresh database → migrations `0001_baseline` + `0002_runs_events_webhooks_audit` + `0003_webhook_deliveries_audit_signature` create every table exactly as `models.py` defines them (including `commits.extra_parents`, `trees.chunks`, the v1.2.0 runs/events/webhooks/audit tables, and 0003's `webhook_deliveries` + `commits.signature`/`env_snapshot_id` + `audit_log.status_code`), then record the head in `alembic_version`.
2. Unrecorded schema (a pre-Alembic create_all volume, or any database whose version rows were lost while the tables stayed) → startup creates any MISSING models.py tables, heals known column drift in place (`_LEGACY_COLUMNS`) and stamps the chain applied. Zero-touch; only future revisions ever execute on it. Replaying into existing tables would crash startup with DuplicateTableError — that's what adoption detection exists to prevent (see [Probleme.md](Probleme.md) #70/#73).

Authoring a new migration:

```sql
-- 1. change python/av_server/models.py
-- 2. generate + review:
--    alembic revision --autogenerate -m "add X"   (run from a checkout with DATABASE_URL set)
-- 3. restart the server — init_db() upgrades to head on boot
```

**Caution:** never edit an already-applied migration in place; append a new revision instead. Volumes that predate the v1.1.x FK removals may still carry `trees_object_hash_fkey`-style constraints — drop those manually once if inserts fail on such a volume (documented per-phase in [CHANGELOG.md](CHANGELOG.md)).

**Invariant:** `_apply_schema` MUST run the chain inside `engine.begin()` (committing), never `engine.connect()`. Postgres DDL is transactional — a plain connect() rolls the entire freshly-built schema back at context exit while startup logs stay green (Probleme.md #70, four CI cycles undetected). SQLite can't catch regressions here: its driver auto-commits DDL.

## Inspecting PostgreSQL

Drop into psql inside the db container:

```bash
docker exec -it aether-vault-db psql -U av_user -d aether_vault
```

Useful reads — newest commits with their project attribution, and total CAS object count:

```sql
SELECT hash, message, project_id FROM commits ORDER BY timestamp DESC LIMIT 10;
SELECT count(*) FROM objects;
```

Tree structure hides in two places: `trees` rows carry per-layer and per-chunk manifests in JSON columns (`layers`, `chunks`), and merge commits split parents across `parent_hash` plus the `extra_parents` JSON column — see the Merge Contract in [architecture.md](architecture.md).

**Caution:** `TRUNCATE objects;` orphans every pushed tree silently — refs keep pointing at hashes whose shards may be gone. To reset the registry, tear down the whole stack with its volumes instead (`docker compose down -v`), which drops trees, refs, and objects together.

## Connecting Between Containers

Inside the compose network, services reach each other by service name:

- `db:5432` — Postgres (this exact hostname appears in `DATABASE_URL`)
- `redis://redis:6379/0` — RedisBloom cache
- `http://aether-vault-engine:8000` — registry API inside the compose network (v1.2.2; the webui builds against `http://localhost:8000` for browser-side calls instead, and both subservices share the engine container anyway)

From the host machine, everything rides localhost — but two of these exist only because the dev compose maps them:

- `localhost:5432` — Postgres, DEV-ONLY mapping for the host-side test suite
- `localhost:6379` — Redis, DEV-ONLY for the same reason
- `localhost:8000` — registry API (engine container)
- `localhost:3000` — webui (same engine container)

**Standing rule:** the release compose intentionally drops the 5432/6379 mappings. Anything that depends on them must declare itself dev-only, like `tests/test_server.py` does via its reachability skip.

## Garbage Collection

GC is manual by design — trigger it when orphaned storage matters:

```bash
av gc        # POST /api/admin/gc under the hood
```

Mark-and-sweep deletes objects no commit's Merkle tree references, respecting the one-hour grace period (`GC_GRACE_SECONDS`) that protects uploads still racing toward their commit row. Run it after large force-branch deletions, aborted pushes, or whenever the Storage tab's object count drifts from what live history justifies. GC also rebuilds the RedisBloom filter from surviving hashes, so stale positive readings clear in the same pass.

## Releases

Cutting a release is a tag push; everything else is pipeline:

1. Land work on `master` with green CI (five test jobs).
2. Curate highlights from [CHANGELOG.md](CHANGELOG.md) since the previous tag into release notes.
3. Tag and push:

```bash
git tag vX.Y.Z && git push origin vX.Y.Z
```

4. `release.yml` then builds wheels (cp310–cp314 × Windows/Linux/macOS via cibuildwheel) plus sdist.
5. PyPI publishes via trusted publishing (OIDC, environment `pypi`) — no stored tokens.
6. A GitHub Release appears with auto-generated notes and all artifacts attached.
7. GHCR receives `:latest` + version-tagged **engine images** (`aether-vault-engine`), plus the legacy `aether-vault-server`/`-webui` names as aliases of the SAME image for the one transition cycle (deprecated — removed next release).

Users pick releases up themselves:

```bash
av update                 # check PyPI, optionally upgrade
av update --docker        # pull latest image + restart the local backend
```

**Verified directly:** all pinned actions are the Node-24 majors (`checkout@v5`, `setup-python@v6`, `setup-node@v6`, `upload-artifact@v7`, `download-artifact@v7`) — bumped deliberately off Node-20 deprecation warnings; do not downgrade pins.

**Caution:** anyone adding a new uvicorn-starting CI job must export a writable `AV_DATA_DIR` explicitly — the `/data` default is volume-backed in containers only. The webui-e2e incident in [CHANGELOG.md](CHANGELOG.md) is the reference failure. Versioning semantics for what deserves MAJOR vs MINOR live in [`../VERSIONING.md`](../VERSIONING.md).

## Local Development Without Docker

Install from source — this compiles the C++ core via pybind11, so build tools + CMake are required:

```bash
pip install -e .[dev]
```

Most of the suite runs stack-free — CLI behaviors, core bindings, sync against the fake registry, merge algorithms:

```bash
pytest tests/test_cli.py tests/test_core.py tests/test_sync.py tests/test_merge.py
```

Running the server bare-metal works, with the one non-negotiable env var:

```bash
AV_DATA_DIR=$(mktemp -d) DATABASE_URL=postgresql+asyncpg://... REDIS_URL=redis://...
python -m uvicorn av_server.server:app --host 0.0.0.0 --port 8000
```

**Caution:** full server/E2E coverage needs the compose stack. Bare-metal uvicorn against disposable local Postgres approximates it, but the real-wire tests, two-repo E2E flows, and Playwright suites all assume the dockerized topology — treat stack-free runs as a fast inner loop, not a substitute.
