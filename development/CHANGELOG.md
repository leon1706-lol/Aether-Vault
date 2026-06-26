# Changelog — Build Phases & Development Walkthrough

Aether-Vault was built incrementally across the following phases. This is the full
development history; see [`README.md`](../README.md) for current usage docs and
[`Probleme.md`](Probleme.md) for the full audit log of correctness, performance and security
findings (resolved and still-open).

## Phase 1 — High-Performance C++ Hashing Core
- **Custom SHA-256 Engine**: Thread-safe cryptographic hashing.
- **Parallel Tree-Hashing**: Splits files into 8MB chunks, hashes concurrently across all CPU cores.

## Phase 2 — CLI Framework & LFS Pointers
- **Staging Index Manager**: Manages the local `.av/index`.
- **LFS-Style Pointers**: Detects large files, copies them to object storage, and replaces them with `.av-pointer` files.

## Phase 3 — Content-Addressable Storage (CAS)
- **Robust CAS Manager**: Deduplicates by SHA-256 hash with atomic writes.
- **FastAPI Endpoints**: High-concurrency streaming uploads, downloads, and branch management.

## Phase 4 — Database & Cache Integration
- **PostgreSQL Schema**: Structured SQL representation of the commit DAG, branches, and metadata.
- **Redis Integration**: `redis-stack-server` for high-performance in-memory caching.

## Phase 5 — Safetensors & Merkle Trees
- **C++ Layer-Splitting**: Parses `.safetensors` JSON headers to independently hash individual model layers — saving up to **99% storage** when only classifier heads change.
- **Merkle Tree DAG**: PostgreSQL tables modelling the full directory hierarchy as a content-addressed tree.

## Phase 6 — Scalability & Garbage Collection
- **RedisBloom Filter**: O(1) hash existence checks, dramatically reducing Postgres load.
- **Mark-and-Sweep GC**: Traverses all Merkle Trees to purge orphaned data shards.

## Phase 7 — ML Experiment Tracking
- **Dynamic Metadata**: `--tag` and `--metric` flags bind arbitrary tracking data (Sharpe ratio, loss, accuracy, drawdown) directly into atomic commits.

## Phase 8 — Native Codebase Visualization
- **AST Parsing & Graph Generation**: `av graph` dynamically maps function calls, external library dependencies, and docstrings into an Obsidian-compatible Markdown vault.

## Phase 9 — Web UI Dashboard
- **Next.js Frontend**: Dark glassmorphism dashboard at `http://localhost:3000`.
- **SVG Commit Graph**: DAG visualizer with coloured branch lanes and bezier edges.
- **Recharts Metrics**: Line charts plotting all numeric ML metrics over time.
- **Live API**: `GET /api/commits`, `GET /api/dashboard/summary` — auto-refreshes every 15 seconds.
- **Docker Service**: `aether-vault-webui` added to `docker-compose.yml`, launched via `av webui`.

## Phase 10 — Commit Integrity & Offline Resilience
- **Change-Aware Staging**: `av add` only re-stages a file when its content hash actually changed, so re-running `av add .` after a commit no longer produces an empty duplicate commit.
- **Pending-Push Queue**: Commits made while the remote registry is unreachable are saved locally and queued in `.av/pending_push` instead of silently failing to reach the Web UI dashboard.
- **`av push`**: Retries syncing queued commits to the remote registry on demand; every `av commit` also auto-retries the queue when the server is back up.

## Phase 11 — Agent Context Handoff
- **`.avh` Open Format**: A JSON snapshot of branch, commit, tags, metrics, model/dataset lineage, and freeform agent instructions — designed to be read by another AI agent picking up the work.
- **`av handoff`**: Generates/updates `handoff.avh` plus a human-readable Markdown note logged chronologically into `Aether-Handoff/`, indexed by a central `Handoff-Hub.md`.
- **Per-Layer Weight Diffing**: `av handoff --diff-weights` reuses the Phase 5 safetensors layer hashes to report exactly which model layers changed since the parent commit, without re-hashing the file.
- **`av handoff log` / `show`**: Browse and inspect the chronological snapshot history directly from the terminal.

## Phase 12 — Hardening & Robustness
- **Race-Free Garbage Collection**: `av gc` now honours a grace period — object shards (and their DB rows) created during the upload→commit window are never reaped, so a GC running concurrently with a push can no longer delete a live object whose commit is still in flight.
- **Batched Merkle-Tree Resolution**: Commit-tree reconstruction (`GET /api/commits/{hash}`) and the GC mark phase no longer issue one DB query per tree node (N+1). Tree resolution runs level-by-level with a single batched query per depth (dedup-safe via path prefixes); GC loads all tree rows once and walks them in memory. Bulk deletes are chunked to stay within driver bind-parameter limits.
- **Unified File-Metadata Source**: Size/mtime change-detection is handled exclusively through Python's `os.stat` (a single Unix-epoch source). This removes a cross-language hazard where the C++ core's `std::filesystem::last_write_time` (implementation-defined epoch) and Python's `st_mtime_ns` could disagree and make unchanged files appear "modified"; the C++ core is now used purely for hashing.
- **Crash-Safe Local Writes**: Commit objects, refs/HEAD, the pending-push queue, the metadata registry and config are written atomically (temp file + `fsync` + `os.replace`), so an interrupted `av commit` can never leave a ref pointing at a half-written or missing commit.
- **Idempotent Registry API**: Concurrent uploads of the same object hash, or concurrent pushes of the same commit, now resolve to a clean `409` instead of a `500` (`IntegrityError` is caught and treated as success). `push_commit` also enforces payload limits (tree size, metric/tag counts, message length) to reject abusive input on the unauthenticated endpoint.
- **Shallow / Out-of-Order Pushes**: A commit whose parent isn't on the server yet (offline pending-push, partial clone) no longer triggers a foreign-key `500`; DAG integrity is anchored by content-addressed hashes.
- **Single-Request Commit Loading**: The Web UI fetches recent commits in one `/api/commits` call (newest-first, with parent links) instead of walking the parent chain one request at a time, and runs all dashboard fetches in parallel.
- **Smaller polish**: pointer detection reads only the fixed magic prefix in binary mode (safe on multi-GB inputs); the parallel hasher only spins up a thread pool when there is enough work to amortize it; `VaultClient` is now closable / a context manager; deprecated `datetime.utcnow()` and `@app.on_event` replaced with timezone-correct helpers and a FastAPI `lifespan`.

## Phase 13 — Visual Weight Diffing
- **"Weight Diff" Web UI tab**: a sidebar tab (lifted into the existing single-page dashboard, no new route) lets you drag two checkpoints from a list into two comparison slots and see a colored per-layer heatmap, summary stats (changed/total/% changed), and a Recharts bar chart of which layers changed across model depth. Entirely client-side — it reuses the per-layer hashes `GET /api/commits/{hash}` already returns, so no new server endpoints were needed.
- **Fixed while building it — commits referencing layer-split `.safetensors` artifacts could never sync to the server.** Two compounding bugs: (1) `av commit`/`av push` uploaded a commit *before* its objects, and the server's tree rows had a hard foreign key to the objects table, so the insert always failed; the offline-queue retry path additionally never uploaded objects at all; (2) the server's generic `except IntegrityError` mapped *any* integrity violation to a "commit already exists" 409 — which the client (by design) treats as idempotent success — so the failure was completely silent: `av push` reported success while the commit and ref never reached the database. Fixed by uploading objects before the commit (in both the live and queued-retry paths), dropping the now-provably-wrong foreign key (a layer-split file's whole-file blob is never uploaded by design), and having the server re-check by hash before deciding a 409 is genuine.
- **Fixed:** `av add` computed per-layer safetensors hashes but never actually persisted them to `.av/index` (an internal `auto_save` wrote the index before the layers were attached to the in-memory entry) — so every `av commit` silently shipped an empty `layers: []`, degrading `av handoff --diff-weights` (and now the Web UI) into a whole-file comparison for every checkpoint, undetected until this feature exercised it end-to-end.
- **Fixed:** `atomic_write_text`'s temp filename (PID + full UUID4 hex) could push a commit's path past Windows' 260-character `MAX_PATH` limit, making the write — and the whole commit — fail outright on deeply nested working directories.
- See [`Probleme.md`](Probleme.md) for full details, severity ratings, and a couple of smaller items left open.

## Phase 14 — Per-Project Registry Separation + Real-World Fixes
- **Per-project identity on the shared registry**: every `av init` repo previously pointed at the exact same `http://localhost:8000` with no way to tell commits from different local folders apart — so a Web UI started from one repo would show commits pushed by an unrelated one. `av init` now generates a stable `project_id` (UUID) + `project_name` (folder name, renameable via `av config --name`), included in the hashed commit payload and namespacing every branch ref as `"<project_id>/<branch>"` (so two projects can each have a `main` without colliding). Repos initialized before this change are backfilled automatically and stably on first use.
- **`av config --remote-url`**: point a repo at a different registry entirely; `av config` with no arguments now prints the current LFS threshold, remote URL, and project identity.
- **New "Projects" Web UI tab**: lists every project that has pushed to the registry (commit count, last push), with an "Open" button that scopes the Dashboard, Branch List, and Weight Diff tab to just that project (persisted across reloads); a badge in the top bar shows the active filter with a one-click clear.
- **`GET /api/projects`** (new) and an optional `?project_id=` filter on `GET /api/commits`/`GET /api/refs`. Object storage stays deduplicated *across* projects on purpose — only commit/ref metadata is scoped.
- **Fixed real usability bugs reported from a separate test install**: the Layer Drift chart's tooltip text was unreadable (black on dark background) and its X-axis label was clipped with no Y-axis explanation; `av webui` rebuilt/re-evaluated the Docker image on every single invocation even when nothing changed (now skips straight to the browser if already healthy, ~15s instead of 2+ minutes; `--rebuild` forces a fresh build when needed).
- See [`Probleme.md`](Probleme.md) for the full edge-case pass (legacy configs, project-name collisions, branch-name collisions across projects, GC/stats behavior with multiple projects) and what was deliberately left unscoped.

## Phase 15 — Framework Plugins (PyTorch Lightning & HuggingFace Transformers)
- **`av_plugins` package**: `AetherVaultCallback` (Lightning) and `AetherVaultTrainerCallback` (Transformers) hook into each framework's native checkpoint-save callback and drive the existing `av` CLI in-process (`cli.main(..., standalone_mode=False)`) rather than duplicating add/commit/push logic — every existing guarantee (LFS pointers, safetensors layer splitting, offline pending-push queueing, per-project ref namespacing) is reused as-is.
- Both frameworks are optional extras (`pip install aether-vault[lightning]` / `[transformers]`) — the core package stays framework-agnostic.
- Plain PyTorch/TensorFlow were deliberately left out of scope: neither exposes a native checkpoint-save hook comparable to Lightning's `Callback` or HF's `TrainerCallback`, so supporting them would mean a manual "call this after `torch.save()`" API — a different, lower-value feature.

## Phase 16 — Dataset Auto-Logging + Symmetric Import Commands

- **Dataset auto-logging**: `AetherVaultCallback` (Lightning) and `AetherVaultTrainerCallback` (Transformers) gained a `dataset_paths` constructor argument, committed once at `on_train_start`/`on_train_begin` and tagged `dataset` — closing a gap against the roadmap's "Framework Plugins" item, which called for auto-logging datasets used, not just checkpoints and metrics. Auto-*detection* of a dataset's on-disk path isn't feasible (generic `Dataset`/`DataLoader` objects don't reliably expose one), so this is opt-in rather than automatic, same as the existing `checkpoint_paths` override.
- **MLflow compatibility layer**: new `python/av_plugins/mlflow.py` with `import_run(run_id, ...)`, closing the roadmap's "optional MLflow compatibility layer (import existing MLflow runs)" item. Downloads a run's artifacts into `<repo_root>/mlflow_imports/<run_id>/` (MLflow's own artifact store is typically outside the Aether-Vault repo, and `av add` requires staged paths to live under the repo root), then commits them tagged `mlflow-import` with the run's metrics and string params attached.
- **Symmetric import commands across all three plugins**: `import_checkpoint()` added to both `lightning.py` and `transformers.py`, mirroring `mlflow.py`'s `import_run()` — backfills a checkpoint that already exists on disk from before a callback was wired in. All three are exposed identically as CLI commands: `av import-lightning <path>`, `av import-transformers <path>`, `av import-mlflow <run_id>`.
- **Found and fixed during manual end-to-end testing (not mocks):** `MlflowClient.download_artifacts()` raises its own internal `MlflowException` (instead of returning an empty directory) when a run has zero artifacts — `import_run` now checks `list_artifacts()` first and raises Aether-Vault's own clear error instead of letting MLflow's internal exception leak through. See [`Probleme.md`](Probleme.md).
- **Verified manually** (real throwaway `av init` repos, a real installed MLflow with a sqlite-backed tracking store — file-store backend is deprecated/blocked by default as of MLflow 3.x): double-importing the same unchanged checkpoint is a no-op; importing while unrelated files are staged commits those too (existing, intentional `av commit`-everything-staged behavior, not unique to imports — documented in the README rather than changed); a missing checkpoint path fails with a clear, actionable message.
- **Also found and fixed:** the new MLflow tests themselves left a stray `mlruns/` folder in the real repo root — a sqlite tracking URI only relocates run *metadata*, not MLflow's default `./mlruns`-relative-to-cwd artifact storage. Fixed with `monkeypatch.chdir(tmp_path)` in both tests. See [`Probleme.md`](Probleme.md).

## Phase 17 — Minimum Viable Test Suite + Diagnostics
- **45-test pytest suite**: New `tests/test_cli.py` (CLI commands via `click.testing.CliRunner`:
  `init`, `add`, `status`, `commit`, `checkout`, `doctor`, `test`), `tests/test_core.py` (the
  `aether_core` pybind11 bindings: `hash_file`, `compare_metadata`, `split_and_hash_safetensors`,
  skipped cleanly via `pytest.importorskip` if the native core isn't built), and
  `tests/test_registry.py` (registry/config load-save round-trips), on top of the existing
  `test_vault.py`/`test_plugins.py`. Shared `tests/conftest.py` `repo` fixture bootstraps a real
  `.av` repo via `av init` rather than hand-rolled directories.
- **`av doctor`**: New read-only diagnostic command — checks native core availability, remote
  server reachability, index/pointer consistency, the pending-push queue, and leftover
  interrupted-write temp files. No auto-repair (`--fix`) yet; see the Open Source Roadmap.
- **`av test`**: New dev-only command that runs the project's own pytest suite via
  `python -m pytest` from the installed package's source root; gives a clear error instead of a
  crash on a non-editable (wheel) install.
- **CI**: New `.github/workflows/tests.yml` (GitHub Actions, `windows-latest`) runs the full
  suite (with `pip install -e .[dev]`, which builds the C++ core) on every push/PR.
- **Found and fixed while building this suite** (manual end-to-end debugging, not just unit
  tests — see `Probleme.md`): `av checkout` never restored `code`-type files (only
  `artifact`-type), and `av add` never wrote a CAS object for code/sub-threshold files in the
  first place — so rolling back code to an older commit was silently a no-op despite reporting
  success. Fixed by writing every tracked file (not just LFS artifacts) into `.av/objects/` on
  `add`, restoring any changed file type on `checkout`, and uploading code objects to the remote
  in `upload_commit_objects()`.

## Phase 18 — `av doctor --fix` Auto-Repair Mode
- **`--fix`**: closes the `av doctor --fix` roadmap item — repairs what `av doctor` already
  knows how to detect: re-links orphaned/stale `.av-pointer` entries back to their CAS object
  (downloading it from the remote first if it's only available there), removes `*.tmp.*`
  leftovers from interrupted atomic writes, and clears pending-push queue entries whose commit
  no longer exists locally (genuinely unrecoverable) while retrying whatever legitimately
  remains via the existing `flush_pending_push()`. Anything it can't safely recover (object
  missing both locally and on an unreachable/lacking remote) stays a `[WARN]`, never fabricated
  or silently dropped.
- **`--fix --dry-run`**: previews exactly what `--fix` would do — using only non-mutating checks
  (`VaultClient.object_exists()`'s `HEAD`-only request instead of an actual download, local
  existence checks instead of writes/deletes) — and prints `[WOULD FIX]` instead of `[FIXED]`,
  with a "(dry run — nothing was changed)" summary suffix. `--dry-run` without `--fix` is a
  no-op, identical to plain `av doctor`.
- **Manually verified end-to-end** (not just unit tests) in a scratch repo: hand-constructed all
  four broken `.av/` states (orphaned pointer entry with no remote, stale pointer file with an
  intact object, a `*.tmp.*` leftover, and a pending-push entry referencing a missing commit),
  confirmed `av doctor` reports each, confirmed `--fix --dry-run` previews without touching
  anything on disk, then confirmed the real `--fix` actually repairs the recoverable ones and
  correctly leaves the two genuinely-unrecoverable ones (no local or remote copy of the object)
  as `[WARN]`. No new bugs found during this pass.

## Phase 19 — Closed the 5 Remaining Test-Coverage Roadmap Gaps
- **`av_server` test coverage** (was 0%): new `tests/test_server.py` — pure validation tests
  (`validate_ref_name` path-traversal rejection, `CASStorage._safe_ref_path` escape rejection,
  always run) plus FastAPI `TestClient`-backed HTTP-layer tests (health, upload/download
  round-trip, hash-mismatch rejection, idempotent duplicate-upload 409, `push_commit`'s payload
  limits — `MAX_TREE_ENTRIES`/`MAX_TAGS`/`MAX_TAG_LEN`/`MAX_METRICS`/`MAX_MESSAGE_LEN` — all
  422, duplicate-commit 409, ref update/get round-trip, project-scoped ref filtering, dashboard/
  projects endpoint shape, and the GC grace-period logic both protecting a fresh object and
  sweeping an aged one). Requires a reachable Postgres + Redis (`AV_TEST_DATABASE_URL`/
  `AV_TEST_REDIS_URL`, sensible localhost defaults) — skips cleanly with a clear message
  otherwise, same philosophy as `test_core.py`'s `importorskip`.
- **Integration tests against a live stack**: one dedicated "real wire" test drives a real
  `av init`/`add`/`commit` through the actual CLI against a genuinely running
  `aether-vault-server` process (not just `TestClient`), then confirms the commit landed via a
  direct `GET /api/commits/{hash}` — the first repeatable test of the real wire protocol rather
  than the in-process ASGI call. Gated on `http://localhost:8000/api/health` responding.
- **`webui/` test suite** (was none at all): added Vitest, covering the pure diff/formatting
  logic — `diffWeights.ts`'s `diffFile`/`isModelPath`/`listModelPaths`/`unionModelPaths`
  (including a regression test for the documented `__header__` pseudo-layer filtering) and
  `api.ts`'s `formatBytes`/`shortHash`. React Testing Library component tests and Playwright
  E2E are a deliberate, documented scope decision — not implemented this round (still 🔲 on the
  README roadmap).
- **Framework-plugin callbacks now actually run in CI**: root cause was `tests.yml` only ever
  installing the `[dev]` extra, never `[lightning,transformers,mlflow]` — the 2 callback tests
  (already written, already correct) silently always skipped. Fixed via a new `plugin-tests` CI
  job that installs the extras and runs `tests/test_plugins.py`.
- **Direct CLI command tests**: new `tests/test_cli_commands.py` covers `branch`, `push`, `gc`,
  `list-meta`, `config`, `graph --update`, `webui` (Docker-not-running path), and all three
  `import-lightning`/`import-transformers`/`import-mlflow` commands (via `sys.modules`
  injection, since the real plugin modules raise `ImportError` at import time without their
  optional extras installed — can't import-then-monkeypatch a module that doesn't import).
- **New CI**: `plugin-tests` and `webui-tests` (both `ubuntu-latest`) and `server-tests`
  (`ubuntu-latest` with Postgres + Redis as GitHub Actions service containers, plus a live
  `uvicorn` process for the real-wire test) — four jobs total in `tests.yml` now.
- **Bonus, not one of the 5 roadmap lines**: `av test --webui` runs the webui Vitest suite
  after the Python suite in one command, combining exit codes — closes a real workflow friction
  (two toolchains, two commands) rather than just the roadmap's literal ask.
- **Found and fixed during manual debugging**: `av test --webui` failed with a "npm not found
  on PATH" error on this Windows dev machine *despite npm being genuinely installed and on
  PATH* — `subprocess.run(["npm", "test"], ...)` doesn't reliably resolve `npm` to `npm.cmd` on
  Windows without going through `shutil.which()` first (a well-known Windows
  `subprocess`/`npm` interaction). Fixed by resolving the full path via `shutil.which("npm")`
  before invoking it, falling back to the original clear error message only when that genuinely
  returns nothing.

## Phase 20 — Framework-Plugin Tests Verified Against the Real Libraries
- The 7 `tests/test_plugins.py` tests that previously always skipped in local dev (no
  `lightning`/`transformers`/`mlflow` installed) were run for real for the first time, in an
  isolated `venv/` (kept out of the main dev environment specifically to avoid pulling `torch`
  — a multi-GB transitive dependency of both `lightning` and `transformers` — into it).
  `pip install -e .[dev,lightning,transformers,mlflow]` succeeded cleanly; all 6 previously-
  skipped callback/import tests now **pass** (not just skip), and the 3 "raises a clear
  `ImportError` when missing" tests correctly flip to **skipped** instead (their entire purpose
  is exercising the *absent*-dependency path, which no longer applies once the packages are
  genuinely installed). Full suite re-run inside the same venv: 88 passed, 20 skipped (the
  remaining skips are the 17 `test_server.py` tests needing Postgres/Redis/Docker, unrelated to
  this venv) — zero regressions from having the heavier packages importable. No bugs found.

## Phase 21 — `tests/test_server.py` Verified Against a Live Docker Stack
- The 17 `av_server` tests that previously had only ever been verified by static source review
  (no Docker available in earlier sessions) were run for real for the first time, against
  `docker compose up -d db redis aether-vault-server` plus a dedicated `aether_vault_test`
  database (created inside the same Postgres container, kept separate from the real dev
  database so the tests' per-test `TRUNCATE` cleanup can never touch real data) and Redis
  index `1` (kept separate from index `0`, the real server's default).
- **Found and fixed a genuine production bug**: `run_garbage_collection`'s physical-shard sweep
  computed its cutoff by calling `.timestamp()` directly on a naive UTC datetime, which Python
  silently interprets as *local* time — on this host (UTC+2) that made the cutoff two hours too
  early, so aged orphaned objects were never actually swept from disk (the DB-side row deletion
  was unaffected, since it compares two naive datetimes directly without an epoch conversion).
  On a host *behind* UTC, the same bug would delete objects *before* their grace period really
  expired. Fixed by attaching `tzinfo=timezone.utc` before converting to an epoch. See
  `Probleme.md` for the full writeup.
- Also fixed two test-only issues surfaced by the same run: the per-test DB cleanup crashed at
  teardown on every test (a SQLAlchemy pooled connection reused across a mismatched asyncio
  event loop — fixed by using a fresh, self-contained `asyncpg` connection instead), and
  leftover orphan shard files from earlier tests made the GC grace-period test's exact-count
  assertion flaky (fixed by clearing the on-disk storage directories between tests, not just
  the DB tables).
- Final result: all 29 `test_server.py` tests pass (17 previously-skipped + 12 always-run pure
  tests); full suite: 101 passed, 7 skipped (only the framework-plugin "raises ImportError when
  missing" tests, unrelated to Docker), 0 failed.

## Phase 22 — Combined venv + Docker: the True Test-Suite Maximum
- Ran the full suite through the plugin `venv/` (Phase 20) *together with* the live Docker
  stack (Phase 21) for the first time in one `pytest tests/` invocation — previously each had
  only ever been verified separately. Result: **105 passed, 3 skipped, 0 failed** out of 108 —
  the 3 remaining skips are permanent by design (the "raises a clear `ImportError` when missing"
  tests, which structurally can never pass once their package is actually installed).
- Found and fixed one real flakiness bug surfaced only by this heavier combined run: the
  real-wire test's reachability check was a collection-time `skipif` condition, which raced
  against the much heavier import phase (`torch`/`transformers`/`lightning`) and misread the
  server as unreachable. Moved to a lazy, in-test check instead. See `Probleme.md`.
- README test badge updated to `105/108` to reflect the real demonstrated maximum.

## Phase 23 — `webui/` Component Tests (RTL) + Playwright E2E
- **React Testing Library component tests**: extended the existing Vitest setup with a `jsdom`
  environment scoped to `src/components/**` (the existing pure-logic tests stay on `node`), plus
  `@vitejs/plugin-react` for JSX support and an explicit `afterEach(cleanup)` (Vitest doesn't
  auto-register RTL's cleanup the way Jest does). New tests for `StatsRow`, `WeightHeatmap`,
  `LayerDriftChart`, `CheckpointPicker`, `BranchList`, `CommitList`, and `MetricsChart` — 27 new
  tests, 46 total in `webui/` now. `WeightDiffPanel`/`ProjectsPanel`/`useDashboard` are
  deliberately still out of scope (they manage their own async fetch state; testing them
  meaningfully needs either a fetch-mock layer or extracting that logic into a hook first).
- **Playwright E2E**: two flows against the real `docker compose` stack — a dashboard smoke test
  and a full Weight Diff comparison (select two real seeded checkpoints, assert the rendered
  layer diff matches what was actually pushed). `webui/e2e/seed_data.py` seeds real data through
  the actual `av` CLI (`CliRunner`, same pattern as `test_cli_commit_pushes_to_a_live_server`),
  not synthetic API calls. New `webui-e2e` CI job (own service containers + a freshly-built
  webui, not the cached docker-compose image — see the bug below for why that distinction
  mattered here).
- **Found and fixed a real bug**: adding `vitest.setup.ts` broke the *production* `next build` —
  `next build` type-checks the whole project, and an `@ts-expect-error` directive that suppressed
  a real Vitest-context type error was flagged as "unused" under Next's own type resolution
  (TypeScript treats an unused suppression directive as its own error). Fixed by using a plain
  type cast instead of a suppression comment, and excluded test-only files
  (`vitest.config.ts`/`vitest.setup.ts`/`playwright.config.ts`/`e2e/`/`*.test.ts(x)`) from the
  app's `tsconfig.json` scope so this category of cross-contamination can't recur.
- **Also diagnosed (not a bug)**: the Weight Diff E2E test initially looked broken (checkpoint
  rows never appeared) when run with 2 parallel Playwright workers — turned out to be genuine
  slowness, not breakage: the panel resolves up to 30 commits' full Merkle trees via individual
  sequential requests (no batched server endpoint for this exists yet), and 2 workers competing
  for CPU/network made an already-slow ~15-20s load look like a hang. Fixed by pinning
  `workers: 1` and raising the timeout, rather than "fixing" a feature that wasn't broken.

## Phase 24 — Speed Fixes + `--speed` Diagnostics
- **Four bottleneck fixes** found by reading the hot paths directly: `av add` was calling
  `Index.save()` (a full JSON re-serialize + write) once *per staged file* instead of once per
  `add` invocation — fixed by batching with `auto_save=False` inside the loop, matching the
  pattern already used elsewhere in the same command. `handoff.py`'s `resolve_head()` read the
  same ref file twice. `av_server/storage.py`'s `get_storage_stats()` read every ref file's full
  *contents* just to count them — switched to a plain `os.walk` file count. `webui`'s
  `CommitGraph`/`MetricsChart` rebuilt their graph/metric-key data from scratch on every render
  (the dashboard polls every 15s) — wrapped in `useMemo`.
- **`av doctor --speed`**: a new, read-only timing snapshot of the *current* repo's hot paths
  (`Index.load()`, `load_config()`, a working-tree scan, local object-store stats) — for an end
  user diagnosing why their own repo feels slow.
- **`av test --speed`** (dev-only): the same hot paths timed against disposable, fixed-size
  synthetic fixtures (`python/av_cli/speedcheck.py`) so results are repeatable across machines and
  runs, plus `pytest --durations=20`. Each probe prints against a soft advisory budget — exceeding
  one only flags the row, never fails the run. Combined with `--webui`, also runs a small Vitest
  `bench()` suite (`webui/src/components/__benchmarks__/speed.bench.ts`) covering `buildGraph()`
  and `extractMetricKeys()`, and (when `av` is found on `PATH`) a third "av CLI, end-to-end"
  subsection timing real `av init`/`add`/`commit` subprocess calls.
- **Benchmark Comparison (README)**: `scripts/run_benchmark_comparison.py` times `av` against
  equivalent Git LFS and DVC operations on the same synthetic fixture (the script skips and labels
  any tool not found on `PATH` rather than guessing at numbers) and prints a Markdown table, pasted
  into a new README section with the exact command, versions, and capture date — so the comparison
  stays reproducible rather than a stale, undefendable claim.

> See [`Probleme.md`](Probleme.md) for the full audit log of correctness, performance and security findings (resolved and still-open).
