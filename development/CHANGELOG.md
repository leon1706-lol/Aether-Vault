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

## Phase 25 — Cross-Tool Benchmark Suite (`av benchmark`)
- **8 new benchmarks** comparing Aether-Vault against **Git LFS**, **DVC**, and **MLflow**, each
  a real subprocess/HTTP measurement (never fabricated): hashing throughput at scale,
  safetensors layer-dedup storage savings, commit+push latency, no-op status/add speed, cold
  clone/first pull, partial-checkpoint (layer-level) fetch, storage footprint over N versions,
  and concurrent multi-user push throughput. New `benchmarks/` package: `tool_runner.py`
  (tool detection, a `NOT_APPLICABLE`-vs-`NOT_INSTALLED` distinction, a 1.5x-relative-to-best-
  competitor good/ok/bad verdict rule, table/Markdown printers) and `fixtures.py` (wraps
  `av_cli.speedcheck`'s existing fixture builders rather than duplicating them).
- **`av benchmark` CLI command** (`--only`, `--vs`, `--markdown`) — dispatches into
  `benchmarks/bench_*.py` by name, same "(Development only)" / `_find_source_root()`
  convention as `av test`. Results published in [`development/BENCHMARKS.md`](BENCHMARKS.md).
- **Found and fixed a real bug while building the flagship dedup benchmark**: `add()` stored
  the whole-file blob *in addition to* split safetensors layers, unconditionally — every
  fine-tune commit re-stored the *entire* checkpoint regardless of how many layers actually
  changed, on top of the (correctly deduped) per-layer copies. Net effect: a layered artifact
  used *more* disk than not splitting at all, the opposite of the feature's purpose. The
  codebase's own `push_objects()` already had the right condition ("upload the whole-file
  object only if layers weren't successfully chunked") and `checkout` already reassembles
  from layers on demand — `add()` was the one place that hadn't caught up. Fixed to match;
  `doctor`'s orphaned-pointer detection/`--fix` recovery made layer-aware too (otherwise every
  layered artifact would have started false-positiving as "orphaned" the moment the
  whole-file copy was removed). See [`Probleme.md`](Probleme.md#8-av-add-stored-the-whole-file-blob-in-addition-to-split-layers--layer-dedup-gave-zero-real-storage-savings).
  Verified via the benchmark itself: Aether dropped from 162.5MB to 36.7MB for the same
  6-commit fine-tune sequence, turning a losing number into a winning one.
- **Also fixed**: `scripts/run_benchmark_comparison.py` had a latent `NameError`
  (`CODE_FILE_SIZE` was referenced but never re-exported from `av_cli.speedcheck` after an
  earlier DRY refactor) — never triggered until this phase's first real re-run with DVC
  installed actually reached that code path.
- **Real product gap surfaced, not a bug**: `av` has no `clone`/`pull` command — sync is
  push-only from a single working repo today. Discovered while building the cold-clone
  benchmark; `av`'s column there is `N/A` with that footnote rather than a fabricated number,
  and it's now an open Open Source Roadmap item.
- **DVC and MLflow installed** as a new `benchmarks` extra (`pyproject.toml`) for use as
  comparison targets only — not runtime dependencies. (MLflow's full package needs `pyarrow`,
  which has no prebuilt wheel for Python 3.14 yet; `mlflow-skinny` pinned to match the
  already-installed `mlflow` 3.14.0 avoids a version-mismatch warning instead.)

## Phase 26 — Benchmark-Driven Performance Pass (no-op `add`, `commit` latency)
- **No-op `add`/`status` (Benchmark #4, was 875.0 ms vs Git LFS 143.4 ms, rated BAD)**: the
  size+mtime fast path (`compare_meta_safe`) already skipped re-hashing correctly — the
  remaining cost was everything around it. Fixed three things in `python/av_cli/main.py`:
  (1) `add()` called `get_file_meta_safe()` then `compare_meta_safe()`, which re-stats the
  same path a second time — now compares directly against the already-fetched `meta` dict;
  (2) `idx.save()` ran whenever any files were scanned, even when zero entries actually
  changed — now gated on an `any_changed` flag; (3) `VaultClient` (and the `requests` import
  it pulls in) and the `aether_core` pybind11 extension were both imported unconditionally at
  module load, even for commands that never touch the network or never hash anything — both
  are now lazily imported on first actual use (`_get_aether_core()`, local `from .client
  import VaultClient` inside the five commands that need it, plus a module `__getattr__` so
  `main.VaultClient` stays resolvable for existing test monkeypatching). **Result: ~875ms →
  ~550-625ms (~30% faster)** across repeated captures. Still rated BAD — the residual gap is
  CPython interpreter + `click` import startup, which a compiled Git LFS binary doesn't pay;
  out of scope without rewriting the CLI in a compiled language.
- **`commit` latency (Benchmark #3, was 2,933.7 ms vs DVC 354.4 ms, rated BAD)**:
  `upload_commit_objects()` did a serial `HEAD`-then-`POST` per object — up to ~120 round
  trips for a 60-file commit. The server already exposed `POST /api/sync/batch-objects` for
  exactly this (existence-check many hashes in one call) but nothing in the client called it.
  Added `VaultClient.batch_check_objects()`, a `known_missing` fast path on `upload_object()`
  to skip the now-redundant per-object `HEAD`, and rewired `upload_commit_objects()` to
  batch-check once then upload only the missing objects via a small `ThreadPoolExecutor` —
  still waiting for every upload to finish before `push_commit()` is called, preserving the
  existing FK-ordering invariant. Same code path is used by `flush_pending_push()`, so the
  offline-retry queue benefits too. **Result: ~2,933.7 ms → ~1,357-2,532 ms (45-54% faster)
  depending on machine load.** Still rated BAD against DVC — DVC's `commit` never touches the
  network (`dvc push` is separate), while av intentionally uploads synchronously during
  `commit` per the FK constraint documented in `upload_commit_objects()`'s docstring; that
  architectural difference wasn't in scope for this pass.
- **New tests**: `tests/test_client.py` (new file) covers `batch_check_objects()` and the
  `known_missing` HEAD-skip; `tests/test_cli_commands.py` adds two tests asserting
  `upload_commit_objects()` batch-checks once and uploads only what's missing;
  `tests/test_cli.py` adds a test asserting a true no-op `add` never rewrites `.av/index`.
- Verified against a real `av_server` (Docker Compose: Postgres + Redis + FastAPI), not just
  mocks: ran `av init/add/commit/push` end-to-end, confirmed all uploaded objects land
  server-side via a live `batch-objects` query, and confirmed the offline pending-push queue
  still flushes correctly through the same (now parallelized) upload path.
- See [`Probleme.md`](Probleme.md#-fixed--benchmark-driven-performance-pass-no-op-add-and-commit-latency-2026-06-27) for severity/difficulty ratings and exact file:line citations; full before/after numbers in [`BENCHMARKS.md`](BENCHMARKS.md).

## Phase 27 — Pretty `av init` UX, Local/Enterprise Login, PyPI Packaging, Auto-Update
- **Pretty `av init`**: shows a `rich`-rendered banner and a `questionary` arrow-key select
  asking Local vs. Enterprise on first run in a project. New `python/av_cli/ui.py` centralizes
  the rendering helpers (banner/step/select) so `init`, `webui`, and `update` all render
  consistently instead of each hand-rolling `click.secho` color/emoji prefixes.
- **Enterprise login seam (stub)**: new `python/av_cli/enterprise.py` defines
  `EnterpriseAuthProvider` (`login`/`logout`/`current_session`/`refresh`) and the only
  implementation today, `StubEnterpriseAuthProvider`, which prints a "coming soon" message and
  falls back to Local. Real account-based auth plugs into this seam later without changing any
  call site in `main.py`/`repl.py`.
- **Local-mode Docker onboarding factored out and extended**: new
  `python/av_cli/docker_runtime.py` extracts the docker-compose logic that used to live only in
  `webui_cmd` (`check_docker_running`, `get_container_health`, `start_services`,
  `wait_for_http_ready`) and adds the one capability that was missing — `image_exists()`, which
  distinguishes "image never built/pulled" from "built but the container is stopped" from
  "already running and healthy". `ensure_local_backend_running()` is the new top-level
  orchestrator, used by both `av webui` (refactor, behavior-preserving — same
  `"Docker is not running"` message the existing test asserts on) and `av init`'s local-mode
  first-run/reconnect path.
- **Interactive REPL session**: new `python/av_cli/repl.py`. `av init` (after setup or
  reconnect) and bare `av` (in an already-initialized repo) now drop into a persistent session
  built on `prompt_toolkit.PromptSession`, where commands are still typed with the `av` prefix
  (e.g. `av status`) and dispatched into the same Click group used for one-shot invocations
  (`cli.main(..., standalone_mode=False)`), so behavior never diverges from running the same
  command outside the session. `exit`/`quit`/Ctrl+D leave; Ctrl+C cancels the current line only.
  `cli()` gained `invoke_without_command=True` so bare `av` in an initialized repo reconnects
  (no re-prompting) straight into the session instead of just printing help.
  - **Bug found in manual debugging (step 1) and fixed**: on Git Bash/mintty on Windows,
    `sys.stdin.isatty()`/`sys.stdout.isatty()` both report `True` but `prompt_toolkit`'s Win32
    backend still can't get a real console screen buffer handle, so
    `PromptSession(...)` itself raised an unhandled `NoConsoleScreenBufferError` and crashed
    bare `av` outright. Fixed by wrapping both the session construction and each `.prompt()`
    call in `run_repl()` in a broad `except Exception`, degrading to a one-line warning
    ("Interactive session isn't available in this terminal — run `av <command>` directly
    instead.") instead of crashing. See
    [`Probleme.md`](Probleme.md#-fixed--repl-session-construction-crashed-bare-av-under-git-bashmintty-on-windows-2026-06-27).
    Regression test: `tests/test_repl.py::test_repl_degrades_gracefully_when_session_cannot_be_constructed`.
- **PyPI packaging**: `pyproject.toml` switched from a hardcoded `version = "1.0.0"` to
  `dynamic = ["version"]` via `setuptools-scm` (`write_to = "python/av_cli/_version.py"`,
  gitignored, derived from git tags at build time) — eliminates the prior risk of the
  `pyproject.toml`/`__init__.py` version strings drifting out of sync. Added
  `[tool.cibuildwheel]` so releases ship prebuilt wheels (no local C++ compiler needed for most
  users; the sdist fallback still requires one, same as today, for platforms outside the built
  matrix). New `.github/workflows/release.yml`, triggered on `v*.*.*` tag push: builds wheels
  (`cibuildwheel`) + sdist, publishes to PyPI via trusted publishing (OIDC, no stored token),
  and builds/pushes the Docker image to **GHCR** (chosen over Docker Hub — uses the repo's
  built-in `GITHUB_TOKEN`, no extra secrets, no anonymous pull-rate limits).
- **Update checking**: new `python/av_cli/update_check.py`. User-level config at
  `~/.aether-vault/config.json` (distinct from the existing per-repo `.av/config`) holds
  `auto_update` (off by default — opt-in only), `update_check_enabled`, and a cached
  last-check result (12h cache window, so most invocations are a zero-network-call file read).
  New `av update` command: `--check` (report only), `--list-versions` (every published
  version, newest first, current one marked), `--enable-auto-update`/`--disable-auto-update`.
  `av init` prints a one-line "update available" banner at the end of its flow; deliberately
  **not** hooked into every routine command (`av add`, `av status`, ...) — only `av init` and
  the explicit `av update` ever make a PyPI network call.
- **Shared atomic-write helper extracted**: `atomic_write_text`/`atomic_write_json` moved from
  `main.py` into new `python/av_cli/fsutil.py` so both the per-repo config (`main.py`) and the
  new user-level config (`update_check.py`) can use them without importing each other.
- **Docs cleanup (unrelated to the feature, done alongside)**: removed the
  "Optimization pass (2026-06-27)" narrative from `README.md`'s Benchmark Comparison section
  and `development/BENCHMARKS.md` — both now show only current benchmark standing, since the
  optimization history already lives in this changelog and in `Probleme.md`.
- **New tests**: `tests/test_docker_runtime.py`, `tests/test_repl.py`, `tests/test_update_check.py`.
  Updated every existing call site that invokes `av init` as a subprocess or in-process
  (`tests/conftest.py`'s `repo` fixture, `tests/test_cli.py`, `tests/test_server.py`,
  `tests/test_plugins.py`, `python/av_cli/speedcheck.py`, three `benchmarks/bench_*.py` files)
  to pass `--mode local --yes --no-repl` so none of them block on an interactive prompt or
  the REPL. Full suite: 122 passed, 3 skipped (pre-existing, unrelated).
- **Verified manually** (step 1 of the wrap-up checklist, via the real installed `av` binary in
  a scratch dir outside this checkout, not just `CliRunner`): `av init` (default, no flags),
  re-running `av init` against an already-initialized repo (reconnect path), `av init --mode
  enterprise` (stub fallback), bare `av`, and a full `add`/`commit`/`status` cycle — this is
  what surfaced the Git Bash/mintty REPL bug above.
- **Deferred, tracked on the README roadmap**: no real `vX.Y.Z` tag has been pushed yet — the
  release workflow needs a TestPyPI dry run before trusted publishing points at the real
  `pypi.org` project, and the `aether-vault` PyPI name needs to be confirmed available/claimed.
  The Docker-onboarding three-state decision tree (`image_exists`/health/not-running) is unit
  tested but has not been exercised end-to-end against a real Docker daemon in this pass — no
  Docker install was available in the environment this phase was built in.

## Phase 28 — Docker Auto-Update: Rolling `:edge` Builds + `av update --docker`

- **Real gap found while investigating "can the Docker image auto-update"**: `docker-compose.yml`
  (used by `docker_runtime.ensure_local_backend_running`, called from `av init` and `av webui`)
  defines `aether-vault-server`/`aether-vault-webui` with `build: .` / `build: ./webui` — it
  builds from local source, it never pulled a published image at all. Combined with
  `_find_source_root()` resolving relative to the installed package's file location, **Local-mode
  onboarding only worked for an editable/source install** — a real `pip install aether-vault` end
  user has no `Dockerfile`/`docker-compose.yml` on disk for it to find. Fixed by adding a second,
  image-only compose file (`python/av_cli/docker/docker-compose.release.yml`, no `build:` keys,
  references `ghcr.io/leon1706/aether-vault-server`/`-webui:latest`), shipped as package data
  (`[tool.setuptools.package-data]` in `pyproject.toml`). New
  `docker_runtime.resolve_compose_file()` picks the right one: the dev compose file when a real
  source checkout is found (unchanged behavior for contributors), the bundled release compose file
  otherwise. `ensure_local_backend_running()` now calls this instead of hardcoding the dev path.
- **Also found**: `release.yml`'s `build-and-push-docker` job only ever built **one** image (the
  server) — the webui's Docker image had never been published to GHCR at all. Fixed: the job now
  builds and pushes both images, renamed to `ghcr.io/leon1706/aether-vault-server` and
  `...-webui` (safe to rename — no real tag has ever been pushed, nothing live to break).
- **`av update --docker`** (new flag, deliberately separate from plain `av update` — restarting a
  running backend is disruptive, same reasoning as `auto_update` being off-by-default): pulls the
  latest published image via `docker_runtime.pull_latest_image()` (compares `docker images -q`
  before/after the pull rather than parsing `docker compose pull`'s text output), reports whether
  anything changed, and on confirmation (or `--yes`) calls `restart_service()` then
  `remove_old_images()` to clean up the now-superseded image by its exact ID — never a blanket
  `docker image prune`, since a real machine can have plenty of unrelated images from other
  projects that must not be touched. No-ops with guidance ("use `git pull` + `av webui --rebuild`
  instead") when run from a dev/source checkout, since that backend isn't tied to a published
  image tag at all.
- **Bug found in manual debugging and fixed**: `check_for_docker_update()` didn't check whether
  Docker was even running before calling `docker compose pull` — against a registry image that
  doesn't exist yet (nothing's been published), this can hang for minutes (up to the 600s
  per-service timeout) instead of failing fast. Fixed by checking `check_docker_running()` first,
  matching the existing fast-fail UX everywhere else in `docker_runtime.py`.
- **New workflow `.github/workflows/docker-edge.yml`**: rolling `:edge` build on every push to
  `main` (path-filtered to Dockerfile/webui/python/compose/pyproject changes, so doc-only commits
  don't trigger a rebuild), pushing both images tagged `:edge`. `:latest` is left untouched — it
  stays exclusively tied to tagged releases via `release.yml`, so "stable" and "bleeding edge"
  never collide.
- **Verified locally** (real Docker, not mocked — this machine has Docker installed and running):
  ran `docker compose build` against the dev compose file to confirm the rebuild path still works
  unchanged, then `docker compose up -d` to restart all four containers on the freshly built
  images, confirmed all four (`server`, `webui`, `db`, `redis`) report `healthy`, and confirmed no
  aether-vault image duplicates were left behind (the old pre-rebuild image IDs were already
  superseded by BuildKit's tag-reuse; the one dangling image found on the machine afterward
  belongs to an unrelated project and was correctly left untouched).
- **New tests**: extended `tests/test_docker_runtime.py` (`resolve_compose_file`,
  `pull_latest_image`'s old-ID tracking, `remove_old_images`, `check_for_docker_update`'s
  dev-checkout/not-running/up-to-date/updated paths) and `tests/test_cli_commands.py`
  (`av update --docker`'s three outcomes, including that `remove_old_images` is called with the
  right IDs after a confirmed restart). Full suite: 133 passed, 3 skipped (pre-existing).
- **Deferred**: the actual GHCR pull/restart path against real published images can't be
  end-to-end verified until something is actually published — no tag has been pushed and
  `docker-edge.yml` hasn't run yet (this work itself hasn't been pushed to `main`). Tracked on the
  README roadmap alongside the existing "first real tagged release" item.

## Phase 29 — `av init` Polish, `.avignore`/`av file`, `av unstage`, `av stash`

- **`av init` prompt cleanup**: `select_login_mode()` now passes `instruction=""` to suppress
  questionary's default "(Use arrow keys)" hint, drops the "(recommended)" label from the Local
  choice (pre-highlighted via `default=` instead), and prints a blank line before the prompt.
- **Real logo banner**: `print_banner()` no longer renders a `rich.panel.Panel` box — it renders
  a small ANSI block-art rendering of the actual "AV" monogram (`development/logo.png`), two-tone
  (graphite `rgb(90,90,90)` / copper `rgb(230,160,40)`, exact TrueColor values, not approximated
  hex), with the title/subtitle text preserved underneath it. Design provided by the user as a
  bash/ANSI mockup; compared side by side against an earlier draft (which read as two abstract
  diagonal bars, not recognizable as "A"/"V") and rated 8/10 vs. 4/10 before implementing it.
- **`.avignore` + `av file --avignore`**: new `load_avignore_patterns()`/`_matches_avignore()` in
  `main.py`, wired into `iter_working_files()` (the single function already shared by `add`,
  `status`, `doctor --speed`, and the speedcheck probes) — one change covers every caller.
  Gitignore-*lite* (plain `fnmatch` globs, `#` comments, no negation/anchoring/`**`). New `av
  file` command (`--avignore` flag, extensible for future generated-file types as new flags;
  refuses to overwrite an existing file).
- **`av unstage`**: undoes `av add` without touching the working tree — reverts a staged entry
  back to its last-committed state (so `av status` correctly reports it "modified" again) or
  removes it entirely if it was never committed (back to "untracked"), using the already-existing
  `Index.remove_entry()`/`get_staged_entries()`.
- **`av stash`** (`push`/`list`/`pop`/`apply`/`drop`, `@cli.group(invoke_without_command=True)`
  mirroring git's own `git stash` shape): shelves staged + modified-tracked-file changes and
  reverts the working tree to HEAD, so `checkout`/`branch` can proceed without `--force`; `pop`
  restores everything exactly as it was, staged or not. Built on four small extractions from
  `add()`/`checkout()` rather than reimplementing their logic freehand — deliberately, since
  `checkout`'s safetensors restore path has had a real corruption bug fixed in it before
  (see Phase history) and duplicating that logic risked reintroducing a similar one:
  - `materialize_file()` / `remove_file_and_pointer()` — extracted from `checkout()`'s per-entry
    restore/cleanup blocks; `checkout()` now calls these instead of inlining them
    (behavior-preserving refactor, verified against the full existing `checkout`/`add` test suite).
  - `resolve_head_tree()` — new helper reading HEAD's commit tree, normalizing the legacy
    `{"code":..., "artifacts":...}` shape into the unified flat one.
  - `stage_one_file()` — extracted from `add()`'s per-file loop body (hash, LFS-threshold check,
    safetensors layer-split, pointer creation); `add()`'s loop now calls this once per file.
  - `compute_status()` — extracted from `status()`'s staged/modified/deleted/untracked
    classification, so `av stash push` computes the exact same dirty set `status()` displays.
- **Two real bugs found via manual debugging (not just unit tests) and fixed**:
  1. `av stash pop` initially restored a previously-modified-but-unstaged file's index entry
     using its *dirty* hash/stat instead of HEAD's baseline — since `status()` detects
     "modified" purely via a stat mismatch against the stored entry, this made the file look
     silently clean after popping instead of "modified" again. Fixed by looking up
     `resolve_head_tree()` again during pop for `was_staged=False` entries and storing HEAD's
     baseline (with a deliberately non-matching mtime) instead of the dirty data.
  2. Two stashes created within the same second sorted unpredictably in `av stash list` — the
     filename-based newest-first sort relied on a second-resolution timestamp prefix, so a tie
     fell back to comparing the random shortid suffix, which isn't time-ordered. Fixed by using
     microsecond resolution in the stash ID. Found by the test suite itself
     (`test_stash_list_orders_newest_first`), not inferred from reading the code.
- **New tests**: `tests/test_stash.py` (10 cases — push reverting staged/modified entries, pop's
  exact staged/unstaged restoration, apply keeping the record, drop, list ordering, "nothing to
  stash", skipped-deleted-files warning, a full safetensors layer-split push/pop round-trip);
  extended `tests/test_cli.py` with 9 cases for `.avignore`/`av file`/`av unstage`. Full suite:
  161 passed, 3 skipped (pre-existing, unrelated) — the `checkout()`/`add()`/`status()` refactors
  introduced zero behavior change there.
- **Manually verified** end-to-end with the real installed `av` binary, not just `CliRunner`:
  the full `init` → `file --avignore` → `add` (ignoring a `venv/`) → `unstage` → `stash` cycle,
  including the motivating scenario itself — `checkout` blocked by dirty state, `av stash`
  unblocking it, a clean branch switch, then `av stash pop` restoring everything exactly as it
  was. Also verified every new command works identically inside the REPL both bare and
  `av`-prefixed, and outside it with the `av` prefix required — per the existing,
  un-special-cased dispatch mechanism in `repl.py`.

## Phase 30 — WebUI Logo, Neon-Orange Theme, Four Dedicated Sidebar Panels

- **Real logo in the sidebar**: `Sidebar.tsx`'s top-left brand mark was emoji+text
  (`🌌 Aether-Vault`) — replaced with the actual `development/logo.png` monogram via `next/image`
  (`webui/public/logo.png`, 140×91 rendered), with the "ML Registry Dashboard" subtitle kept
  underneath. Same image also added as `webui/src/app/icon.png` so the browser tab favicon
  matches (Next.js App Router auto-serves a literal `app/icon.png`, zero config).
- **Theme: single neon-orange brand accent, replacing black+blue/purple**: `globals.css`'s
  `--accent-blue`/`--accent-purple` (and every selector deriving from them — nav active state,
  commit dots/hashes, spinner, tag pills, branch icons/tips, project badge, checkpoint labels,
  `--grad-brand`, `--border-accent`, `--shadow-glow`) collapsed into two shades of one hue:
  `--accent-orange` (#ff7a1a) and `--accent-orange-soft` (#ffb380). `--accent-amber` shifted from
  #f6ad55 to #ffd166 (more toward yellow) so it stays visually distinct from the new orange rather
  than colliding — the two were only 6° apart in hue before the shift, 18° after. Hardcoded hex in
  `MetricsChart.tsx`, `CommitGraph.tsx`, `LayerDriftChart.tsx`, and `BranchList.tsx`'s inline
  styles were swept too, not just the CSS variables. The `accent-blue`/`accent-purple` *class*
  names themselves (passed as literal strings from `StatsRow.tsx` and `WeightDiffPanel.tsx`) were
  renamed to `accent-orange`/`accent-orange-soft` rather than left as a permanently misleading
  "a class named blue renders orange" naming mismatch.
- **Four sidebar tabs that used to alias the Dashboard now have real, distinct panels**: before
  this phase, `page.tsx`'s `active` state only special-cased `weight-diff` and `projects` —
  `commits`, `branches`, `metrics`, and `storage` all fell into the same catch-all `else`,
  rendering the identical Dashboard teaser view. Each now has its own component:
  - **`CommitsPanel.tsx`** — offset-aware pagination over `GET /api/commits` (new
    `fetchCommitsPage()` in `lib/api.ts`, since the existing `fetchCommits()` only fetched a fixed
    window for the dashboard hook), client-side search/filter over the loaded page, a branch
    filter via a new shared reachability-walk helper (`lib/branchGraph.ts`), and click-to-expand
    rows that lazily fetch full tree detail (cached per-hash) to show an added/removed/changed
    file diff against the parent commit.
  - **`BranchesPanel.tsx`** — full tip detail, a "commits ahead of main" count via the same
    reachability walk (labeled "(of loaded history)" when the walk runs off the edge of the
    loaded window rather than presenting it as exact), branch-row expand to see its commits, and
    a working "branch from here" create action (new `createRef()` in `lib/api.ts`, since
    `PUT /api/refs/{name}` already upserts). Branch *delete* has no backend route at all — not
    added, with an explicit note in the UI that it isn't available yet rather than silently
    omitting it.
  - **`MetricsPanel.tsx`** — full-size metrics chart with per-metric show/hide toggles, a metrics
    table (commit × metric, fully derived from already-loaded data), and a single-branch
    comparison dropdown.
  - **`StoragePanel.tsx`** — store-wide CAS stats reused from `data.stats`, plus a file-type
    breakdown, largest-tracked-files list, and an approximate dedup ratio — all derived from only
    the **latest commit's** hydrated tree (not summed across commits, to avoid double-counting
    deduped content), and explicitly labeled as a latest-snapshot view, not a CAS-store-wide one.
    A true store-wide file-type breakdown, growth-over-time, and a store-wide largest-objects list
    all need new backend endpoints (no path/extension column on `DBObject`, no historical
    snapshots, no listable object table) — noted as future work, not faked.
  - `page.tsx`'s branch chain is now fully explicit (`dashboard` included) with a `null` fallback
    for an unrecognized `active` value, instead of silently rendering the Dashboard for anything
    unmatched.
- **One real bug found via manual debugging (not unit tests) and fixed**: `TopBar.tsx`'s title
  was hardcoded to the literal string `"Dashboard"` regardless of which sidebar tab was active —
  harmless before this phase (every tab *was* the Dashboard), but confusing now that Commits/
  Branches/Metrics/Storage are real distinct pages. Added a `title` prop to `TopBar` and a
  `TAB_TITLES` lookup in `page.tsx` so the header always matches the active tab.
- **New tests**: `CommitsPanel.test.tsx`, `BranchesPanel.test.tsx`, `MetricsPanel.test.tsx`,
  `StoragePanel.test.tsx` (loading/empty states, the reachability-walk ahead-count, search/branch
  filtering, lazy tree-detail fetch on expand, file-type bucketing). Full webui suite: 64 passed.
- **Manually verified** by running `npm run dev` and driving the real browser with Playwright
  (headless Chromium, no `chromium-cli` available in this environment) against an offline backend
  to exercise every loading/empty state: clicked through all 7 sidebar tabs, confirmed the logo
  and neon-orange theme render consistently, and confirmed the topbar-title fix above.

## Phase 31 — `av test` Auto-Updates README's Test-Count Badge

- **Motivation:** README.md's `tests-N%2FM passing` badge was a hand-edited literal string
  (`161%2F164`) — it silently drifted from reality every time the suite gained/lost tests, with
  nobody remembering to bump it. Asked to make it self-maintaining instead of manually edited.
- **`test_cmd` (`av test`) now streams *and* captures pytest's output**: the Python suite
  previously ran via a plain `subprocess.run(args, cwd=source_root)` that just inherited the
  terminal. It now runs via `subprocess.Popen(..., stdout=PIPE, stderr=STDOUT, text=True)`,
  echoing each line as it arrives (so the live experience is unchanged) while also collecting it
  for parsing afterward — avoiding a second, redundant pytest invocation just to get the numbers.
  `--color=yes` is forced on the pytest invocation since piping stdout makes pytest think it's
  not a real terminal and silently drop all colorization otherwise.
- **New `_update_readme_test_badge(passed, failed)`**: parses the captured output for pytest's
  own `"N passed"` / `"N failed"` / `"N error"` summary counts (after stripping ANSI escapes,
  which can otherwise sit between a number and its label and break the regex), then rewrites both
  the badge URL and its `alt` text in `README.md` via a single regex substitution, using
  `fsutil.atomic_write_text` (already used elsewhere in this codebase for crash-safe writes) so a
  failure mid-write never leaves the README half-edited. The badge turns red instead of green
  when any failures/errors are present, rather than just changing the numbers.
- **Only updates on a full, unfiltered run**: gated on `test_filter is None` — `av test -k
  <pattern>` never touches the badge, since a scoped subset's count would misrepresent the whole
  suite. `--cov`/`--speed`/`--webui` don't restrict which Python tests run, so they don't gate it.
- **New tests**: `tests/test_cli.py` — unit tests for `_update_readme_test_badge` itself (rewrites
  URL+alt text, turns red on failures, no-ops when nothing parsed) plus integration tests driving
  the real `test_cmd` against a fake source root with a fake README.md, confirming the badge
  updates from a fake pytest summary line and that `-k` leaves it untouched. The existing
  `test_test_command_*` tests all had to switch from faking `subprocess.run` to faking
  `subprocess.Popen` for the pytest call specifically (npm/av-CLI calls inside the same command
  are still real `subprocess.run` calls, faked the same way as before) — a `FakePytestPopen`
  helper (`_fake_pytest_popen`) mimics just enough of the real interface (`.stdout` as an
  iterable of lines, `.wait()`, `.returncode`) for `test_cmd`'s streaming loop.
- **Manually verified**: ran the real `av test` end-to-end (not just the faked unit tests) against
  this repo's actual suite — confirmed colored output still streamed live, and the badge was
  rewritten to the real result (178/178 passing) with the correct URL-encoding and alt text.

> See [`Probleme.md`](Probleme.md) for the full audit log of correctness, performance and security findings (resolved and still-open).
