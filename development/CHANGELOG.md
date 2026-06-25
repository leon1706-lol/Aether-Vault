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

> See [`Probleme.md`](Probleme.md) for the full audit log of correctness, performance and security findings (resolved and still-open).
