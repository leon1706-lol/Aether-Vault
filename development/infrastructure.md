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
                Worker tick interval AND the base of the v1.2.5 exponential backoff
                (next_retry_at = now + this * 2^(attempt-1), capped by the var below —
                this is no longer a flat retry step, only the base/tick).
AV_WEBHOOK_RETRY_MAX_SECS  3600   (default)
                Ceiling on the exponential backoff above — a delivery never waits longer
                than this between attempts no matter how high the attempt count climbs.
AV_WEBHOOK_DISABLE_AFTER  0 = off   (default)
                Consecutive delivery failures before a webhook auto-disables (active=false
                + disabled_reason set, a webhook_disabled event + audit row emitted).
                `av webhooks enable <id>` re-enables and clears the counter.
AV_ENGINE_ROLE  all   (container-side default)
                Engine dispatch: all | server | webui. Legacy alias containers WITHOUT
                this var auto-detect: DATABASE_URL set → server; NEXT_PUBLIC_API_URL
                without it → webui (v1.2.5: this path now logs a deprecation warning).
AV_ENGINE_STOP_GRACE_SECS  25   (default)
                Seconds engine-entrypoint.sh waits after TERM/INT before SIGKILLing a
                still-running subservice. Both compose files set stop_grace_period: 30s
                so Docker's own 10s default doesn't SIGKILL the container first.
AV_ENGINE_RESTART_SUBSERVICE  1 = on   (default)
                role=all: a dying subservice restarts just itself instead of tearing the
                whole engine down. =0 reverts to pre-v1.2.5 behavior (any child dying
                takes the container down, relying on `restart: unless-stopped`).
AV_ENGINE_MAX_RESTARTS  5   (default)
                Sliding-window restart budget (paired with the var below) — exceeding it
                shuts the engine down loudly instead of crash-looping forever silently.
AV_ENGINE_RESTART_WINDOW_SECS  300   (default)
                Window the restart budget above is measured over.
AV_REMOTE_URL  (CLI-side) default registry for av clone; else http://localhost:8000.
AV_AUTHOR      (CLI-side) commit author string; defaults to "anonymous".
AV_RUN_ID      (CLI-side) file subsequent commits under this run.
AV_ENV_CAPTURE_VARS  (CLI-side) comma-separated env var names captured into a snapshot's
               HASHED env.env_vars (default: CUDA_VISIBLE_DEVICES,
               PYTORCH_CUDA_ALLOC_CONF, OMP_NUM_THREADS, TOKENIZERS_PARALLELISM,
               HF_HOME, TORCH_HOME). Overrides, not appends, the default list.
AV_PERF_BUDGET_MULTIPLIER  (dev-only, tests/test_perf_gate.py) unset = the built-in
               per-class multipliers (CPU 2.0x, disk 3.0x, +1.5x more on Windows disk).
               Set to a float to override BOTH classes outright (not stack with them) for
               a genuinely slow/noisy machine — one number is the whole story for that run.
AV_ANOMALY_METRIC_JUMP_RATIO  3.0   (default)
               A metric changing by this ratio or more vs. its parent commit emits a
               kind="anomaly" event (type: metric_jump) — see the Anomaly Alerts
               Contract in architecture.md.
AV_ANOMALY_MASS_REWRITE_FILES  200   (default)
               Files added/removed/changed vs. the parent commit's tree at or above this
               count emits an anomaly event (type: mass_rewrite).
AV_ANOMALY_AUTH_SPIKE_THRESHOLD  5   (default)
               Auth failures (401 or scope-denied 403) for the same identifier within the
               window below trips an anomaly event (type: auth_spike); the in-process
               counter then resets so one burst raises exactly one event.
AV_ANOMALY_AUTH_SPIKE_WINDOW_SECS  60   (default)
               Sliding window the threshold above is measured over.
AV_APP_DATABASE_URL  (empty/unset = request-serving sessions use DATABASE_URL, same as
               pre-v1.3.2)
               Optional second connection string for ORDINARY request-serving sessions
               only — migrations and the two cross-tenant background workers keep using
               DATABASE_URL unconditionally (they need DDL rights / the bypass-RLS GUC).
               Point this at the non-superuser `av_app` role (migration 0015) for RLS to
               actually filter anything — see architecture.md's Tenancy Isolation
               Contract. docker-compose.yml sets this by default for its own topology.
AV_TENANCY_ENFORCE  0 = off   (default)
               1 = the application-level tenant guard + RLS GUC application activate.
               Off means every route behaves byte-identically to pre-v1.3.2 regardless
               of tenant_id columns existing on disk.
AV_RATE_LIMIT_BACKEND  memory   (default)
               memory = today's in-process WindowRateLimiter (correct at N=1 replica,
               silently wrong under N>1 — see the HA Contract). redis = one shared,
               atomically-incremented limit across every replica; fails OPEN on a Redis
               error, same posture as the Bloom filter.
AV_AUTH_SPIKE_BACKEND  memory   (default)
               Same memory/redis choice as AV_RATE_LIMIT_BACKEND, for the auth-spike
               anomaly counter specifically (AV_ANOMALY_AUTH_SPIKE_THRESHOLD above).
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

Phase map: A clone→diverge→conflicting merge→`--theirs`→two-parent push · B offline queue drain across a real restart · C pre-Alembic volume heal + stamp on a real boot · D protected mode with per-user tokens (join/attribution/wrong-token/revocation) · E zero-grace GC sweep · F SDK-driven run/commit lifecycle · G event stream cursor/kind filter · H promotion policy · J signed commits (ed25519 roundtrip, tamper detection, unsigned-ok) · K audit trail filters. Notes for local runs: on Windows use Git Bash (the script converts its temp paths via cygpath), keep psql on PATH, and pass options before the connection URI if calling psql yourself — MSYS-style getopt ignores `-c` after a positional URI.

**Chaos drills (v1.3.0, todo.md item 28), Phases L/M/N:** gated behind `AV_E2E_CHAOS=1`
(default off — the phases above run unaffected without it) so they're opt-in for a local
run and isolated into their own `chaos-drills` CI job rather than folded into `e2e-suite`:
```bash
AV_E2E_CHAOS=1 AV_TEST_DATABASE_URL=postgresql+asyncpg://... AV_TEST_REDIS_URL=redis://... \
E2E_PSQL_URL=postgresql://... bash scripts/e2e_scenario.sh
```
L a real (not simulated) Redis outage: `/api/ready` correctly 503s while `/api/health`
and the write path both keep working (redis_cache.py degrades its dedup-shortcut
optimization gracefully rather than failing the push — see that module's own docstring),
then recovers cleanly once Redis is reachable again · M an unwritable `AV_DATA_DIR`
(the portable, CI-safe equivalent of a full disk — same observable failure: the storage
layer's write call fails): the upload fails honestly, nothing partial lands, the commit
queues locally, and it drains once storage is writable again (skips itself with a clear
message on a filesystem/user that doesn't honor `chmod 555` as unwritable) · N the server
process is `SIGKILL`ed mid-push (no graceful shutdown): `.av/pending_push` survives
intact (proving the atomic temp-file+fsync+`os.replace` write pattern under a real crash,
not just a clean stop — Phase B already covers the clean-stop case) and a later `av push`
fully drains it.

## Orchestrator readiness & liveness (v1.3.0, todo.md item 19)

The engine exposes two distinct health routes — conflating them (using one for both
probes) is the single most common orchestrator misconfiguration for this kind of
two-dependency service:

- **`GET /api/health`** — liveness. Answers "is the process alive and able to serve HTTP
  at all" with zero external dependencies (no DB/Redis check) — auth-exempt even in
  Protected mode, so it never deadlocks behind a token gate. A liveness probe should use
  this: if it fails, the process itself is wedged and the orchestrator's correct move is
  to kill and restart the container. It must NOT fail just because Postgres or Redis is
  temporarily down — that's a readiness concern, not a liveness one, and flapping restarts
  of a perfectly-alive process that's waiting on a dependency makes an outage worse, not
  better.
- **`GET /api/ready`** — readiness. Checks DB connectivity, Redis connectivity, and
  `AV_DATA_DIR` writability; returns 503 if any fail. A readiness probe should use this:
  when it fails, the orchestrator's correct move is to stop routing traffic to this pod
  (take it out of the Service's endpoint list) WITHOUT killing it — the process may well
  recover on its own once the dependency comes back, and killing it achieves nothing but
  a pointless restart. Also auth-exempt.

Both routes exist on every target this Dockerfile produces (`all`, `server`) — the `webui`
target has neither (a Node-only container has no DB/Redis to check); point its probes at
the Next.js standalone server's own root path instead, or omit readiness for it entirely
(a webui-only container is either serving requests or it's dead — there's no partial
"waiting on a dependency" state for it the way there is for the registry).

Concrete Kubernetes probe stanzas for the `all`/`server` targets:

```yaml
livenessProbe:
  httpGet:
    path: /api/health
    port: 8000
  initialDelaySeconds: 5
  periodSeconds: 10
  failureThreshold: 3
readinessProbe:
  httpGet:
    path: /api/ready
    port: 8000
  initialDelaySeconds: 5
  periodSeconds: 5
  failureThreshold: 2
```

Drain on rolling update / scale-down: send `SIGTERM` and wait for the container to exit
on its own before `SIGKILL` — set `terminationGracePeriodSeconds` to at least
`AV_ENGINE_STOP_GRACE_SECS` (default 25) plus a small margin, matching this project's own
compose files (`stop_grace_period: 30s`). `engine-entrypoint.sh` forwards `SIGTERM` to
every child and waits up to that grace window for in-flight requests to finish before
force-killing — a shorter `terminationGracePeriodSeconds` than the grace window defeats
that drain and can truncate an in-flight upload. See "Drain under load" in the CI job map
below for the automated proof of this.

## CI Job Map

Every product surface and the workflow that guards it (Tests workflow unless noted):

| Surface | Job(s) |
|---|---|
| Full stack-free suite, py3.10 + 3.14 (Windows build) | `test` matrix |
| In-between Pythons 3.11–3.13 | `nightly.yml` (`compat`) |
| Server live stack (Postgres+Redis): TestClient + real-wire | `server-tests` |
| Same, native Windows services | `server-tests-windows` |
| Product flows via real CLI: merge collaboration, offline drain, legacy upgrade, per-user auth, GC | `e2e-suite` (`scripts/e2e_scenario.sh`) |
| Chaos drills: real Redis outage, unwritable storage, server killed mid-push | `chaos-drills` (`scripts/e2e_scenario.sh`, `AV_E2E_CHAOS=1`) |
| Engine image smoke (Phase I): role dispatch + dual healthchecks from ONE container; v1.2.5: `/api/ready` degrading independently of `/api/health`, killing one subservice restarts only it; v1.3.0: a real SIGTERM drain under concurrent load (20 in-flight uploads) all complete cleanly | `e2e-engine-smoke` |
| Plugins incl. real Lightning training loop + signed-commit gate (`[sign]` extra) | `plugin-tests` |
| WebUI lint/typecheck/Vitest | `webui-tests` |
| WebUI browser E2E: dashboard, weight-diff, token gate | `webui-e2e` |
| sdist+wheel build & twine check | `package-build` |
| Wheel install smoke (Linux venv) | `smoke-wheel-linux` |
| Sdist compile-install smoke (Windows venv, MSVC path) | `smoke-sdist-windows` |
| `:edge` images on master pushes | `docker-edge.yml` |
| Wheels cp310–314 ×3 OS, PyPI, GitHub Release, GHCR | `release.yml` (tags) |
| HA drill (v1.3.2): real 2-replica compose topology, killed replica mid-load, webhook double-delivery + rate-limit proofs | `ha-drill` (`scripts/ha_drill.sh`) |
| Helm chart schema verification (v1.3.2): `helm template \| kubeconform -strict` across 4 value permutations — NOT a real cluster deploy | `helm-lint` |
| Security scanning (v1.3.2): `pip-audit`, `bandit`, `semgrep`, `trivy` (built image), `npm audit` — PR + weekly cron | `security.yml` |

Known residuals (deliberate): no Docker-daemon-dependent `av update --docker` flow test, no macOS install smoke. Dependabot was removed (Phase 55, owner decision — config deleted, all open PRs closed); dependency freshness review is manual now.

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

## CUDA Base Matrix (`av env replay --dockerfile --cuda TAG`)

`--cuda TAG` switches the generated Dockerfile's builder base from `python:slim` to
`nvidia/cuda:<TAG>-runtime-ubuntu22.04`. These are the tags this project has actually
built and smoke-tested a resulting image from (`av_cli/cmd_env.py::_VALIDATED_CUDA_TAGS`)
— a tag outside this set still generates a Dockerfile (nvidia/cuda publishes far more
tags than any one project validates; this is a warning, never a hard failure), just with
no guarantee the combination builds cleanly:

| Tag | CUDA runtime | Notes |
|---|---|---|
| `12.1.0` | CUDA 12.1.0 | oldest validated — PyTorch 2.1–2.3 era |
| `12.1.1` | CUDA 12.1.1 | patch bump of the above |
| `12.4.1` | CUDA 12.4.1 | PyTorch 2.3–2.5 era |
| `12.6.2` | CUDA 12.6.2 | PyTorch 2.5+ era |
| `12.8.0` | CUDA 12.8.0 | current, Blackwell-generation driver baseline |

Update `_VALIDATED_CUDA_TAGS` (and this table) after actually building and smoke-testing
a new tag — this list is a validation record, not an aspirational target.

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
