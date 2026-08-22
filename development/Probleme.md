# Problems & Findings — Aether-Vault Code Audit

As of: 2026-06-23

Complete inventory of all problems found during the audit (edge cases, memory leaks, logic
errors, performance bottlenecks, security risks), each **rated 1–10** by severity.

- **Status `✅ fixed`** = fixed in code (severity ≥ 5 and pure code security vulnerabilities).
- **Status `🔸 open`** = deliberately not changed (deployment config / too extensive / severity
  1–4), documented here for a later fix.

---

## ✅ Fixed (Severity ≥ 5 + code security vulnerabilities)

### [10] Parallel hash ≠ canonical SHA-256 → content-addressing & remote upload broken
- **File:** `src/core.cpp` (binding of `hash_file`).
- **Problem:** `aether_core.hash_file` pointed at `hash_file_parallel`, which for files ≥ 16 MB
  returns a *tree hash* (SHA-256 over the concatenation of chunk hashes) instead of the real
  file SHA-256. The Python fallback and the server-side verification (`storage.store_object`)
  use the real SHA-256, though.
- **Impact:** The same file got different hashes depending on core availability/size;
  whole-file uploads of large non-`.safetensors` artifacts (`.pt`, `.parquet`, `.csv`, `.h5`)
  failed with HTTP 400 → core functionality (remote sync/checkout) broken.
- **Fix:** Bound `hash_file` to `hash_file_sequential` (canonical SHA-256); tree hash remains
  available as `hash_file_tree`; added an invariant comment.

### [7] Path traversal / LFI via `ref_name`
- **Files:** `python/av_server/server.py` (ref endpoints), `python/av_server/storage.py`.
- **Problem:** `GET/PUT /api/refs/{ref_name:path}` passed `ref_name` unchecked to the
  filesystem fallbacks (`refs_dir / ref_name`); `../../…` allowed reading/writing outside the
  data directory.
- **Fix:** Central `validate_ref_name()` (whitelist, no `..`/absolute/backslash) in
  `update_ref`/`get_ref`; additionally a defensive `_safe_ref_path()` in `CASStorage`
  (resolved path must stay below `refs_dir`).

### [6] `os.walk` traverses the entire CAS object store + faulty substring filter
- **File:** `python/av_cli/main.py` (`add`, `status`).
- **Problem:** `if ".av" in root` is (a) a substring test (folders like `data.average` were
  incorrectly skipped) and (b) doesn't prune → `os.walk` descended into `.av/objects` (tens of
  thousands of shards).
- **Fix:** Shared helper `iter_working_files()` with in-place pruning (`dirnames[:] = …`) and
  path-component checking.

### [6] `av add` fully re-hashes every file, even unchanged ones
- **File:** `python/av_cli/main.py` (`add`).
- **Problem:** Every file was fully read/hashed regardless of whether it had changed.
- **Fix:** Before hashing, run `compare_meta_safe` against the index; on a match (size + mtime)
  the file is skipped.

### [5] Memory spike: layer extraction reads entire layers into RAM
- **File:** `python/av_cli/main.py` (`add`, safetensors path).
- **Problem:** `dst_f.write(src_f.read(l_size))` loaded an entire layer (up to GB-sized) into
  memory.
- **Fix:** Chunked copying in 8 MB blocks.

### [5] C++ `split_and_hash_safetensors`: missing validation → OOM/DoS
- **File:** `src/core.cpp`.
- **Problem:** Unvalidated 8-byte `header_size` → arbitrarily large allocation; `end - start`
  could underflow, offsets could exceed EOF.
- **Fix:** Bounds checks (`header_size` must fit within the file, `end >= start`,
  `base_offset + end <= file_size`).

### [5] Everything appears as "modified"/"staged" after `checkout`
- **File:** `python/av_cli/main.py` (`checkout`).
- **Problem:** Index entries were written with `mtime_ns=0` and marked as `staged` by being
  re-inserted into a cleared index.
- **Fix:** Capture the real `size`/`mtime_ns` after materializing and set `staged=False` →
  clean working tree after checkout.

### [5] `checkout` overwrites/deletes the working copy without a dirty check → data loss
- **File:** `python/av_cli/main.py` (`checkout`).
- **Problem:** Tracked files were unconditionally overwritten/deleted.
- **Fix:** A dirty check (modified/deleted/staged files) now aborts the operation; new
  `--force`/`-f` flag to deliberately discard changes.

### [5] DB column `size` as a 32-bit `Integer` → overflow above 2 GB
- **File:** `python/av_server/models.py` (`DBObject.size`, `DBTree.size`).
- **Problem:** Postgres `INTEGER` (max ~2.1 GB) for a tool that versions multi-GB files.
- **Fix:** `BigInteger`. **Note:** only takes effect on a **fresh DB** — the server uses
  `Base.metadata.create_all` without migrations. For an existing DB, a manual
  `ALTER TABLE objects ALTER COLUMN size TYPE BIGINT;` (same for `trees`) or Alembic is needed.

### [5] `/api/stats` does a full filesystem walk on every dashboard refresh
- **File:** `python/av_server/server.py` (`get_stats`).
- **Problem:** Every call (the Web UI polls roughly every 15s) walked + `stat()`'d every shard.
- **Fix:** DB aggregates (`count`/`sum`); filesystem walk is now only a fallback when the DB is
  empty.

### [5] Server ignores the commit's author timestamp → wrong sort order
- **File:** `python/av_server/server.py` (`push_commit`).
- **Problem:** `DBCommit.timestamp` defaulted to insert time; combined with the pending-push
  queue, this sorted commits incorrectly on the dashboard.
- **Fix:** `commit_data["timestamp"]` (ISO 8601) is now parsed and set; falls back to
  `utcnow()`.

---

## ✅ Fixed — Deployment security (2026-06-28)

### [7] No authentication + open attack surface
- **Files:** `python/av_server/server.py`, `docker-compose.yml`, `python/av_cli/docker/docker-compose.release.yml`, `python/av_cli/client.py`, `python/av_cli/main.py` (new `av auth` group + `av init` prompt), `webui/src/components/TokenGate.tsx`.
- **Problem:** No auth/authz whatsoever on any endpoint; the **destructive** `POST
  /api/admin/gc` was unauthenticated (any reachable client could wipe storage); Postgres
  `5432`/Redis `6379` were mapped externally in Compose with hardcoded default credentials
  (`av_user`/`av_password`) that are public the moment this repo is — meaning the port mapping
  was the only thing standing between "public password" and full DB access for anyone who
  could reach the host.
- **Fix:** an optional shared-secret token ("Protected" mode, off by default — "Anonymous"
  stays byte-for-byte identical to before for solo/local use). A `require_token` middleware
  gates every route except `GET /api/health` and the FastAPI docs routes when a token is
  configured; `av auth set-token`/`clear`/`status` manage it, `av init` offers it as a
  Anonymous/Protected choice (with a "generate new" vs. "join an existing registry" sub-choice)
  at setup time, and the CLI/webui both prompt for the token interactively on a 401 rather than
  failing with a generic error. The externally-mapped DB/Redis ports were removed from
  `docker-compose.release.yml` (the file real end users actually deploy) — kept in the **dev**
  `docker-compose.yml` specifically because `tests/test_server.py` connects to them directly
  from the host (verified by checking; removing it there would have silently degraded that
  test file to skip-mode instead of a loud failure).
- **Explicitly NOT fixed, by design — still open:** CORS remains `allow_origins=["*"]`. This
  round's scope was specifically the auth gap, not CORS hardening; noted here so it doesn't
  read as fully closed.
- **A real bug found via manual debugging while building this fix:** `commit()`'s push-to-
  remote logic assumed any push failure would surface as a `False`/`None` return (matching the
  existing "queue it for `av push` later" fallback) — but `VaultClient`'s methods now *raise*
  `AuthenticationError` on a 401 instead, since `server_available()`'s own health check is
  deliberately exempt from the auth gate and so can't be used to infer "my token is valid."
  That exception propagated straight out of `commit()`, skipping the queue-for-retry fallback
  entirely — a commit made against a Protected registry with a stale/wrong token was created
  locally but **silently never queued**, unlike every other kind of push failure. Reproduced
  for real against the live Docker stack (not just unit tests): committed with a deliberately
  wrong token, confirmed the commit was missing from both the server and `.av/pending_push`.
  Fixed by catching `AuthenticationError` in `commit()`'s push block and in
  `flush_pending_push()` (which now also preserves the rest of the queue before re-raising, so
  a bad token hit partway through retrying several queued commits doesn't drop the untried
  ones) and queueing exactly like any other push failure.
- **Verified:** `tests/test_server.py` (14 new cases — header parsing edge cases, health/docs
  exemption, reads+writes both gated), `tests/test_client.py` (token header + every method
  raising `AuthenticationError` on 401), `tests/test_cli.py` (`av auth`, `av init`'s
  Protected/join-existing flow, the commit-queueing regression), `tests/test_docker_runtime.py`
  (`.env` read/write round-trip including awkward characters, the webui URL token handoff),
  and a full manual pass against the live Docker stack: rebuilt the server image, confirmed
  Anonymous is unchanged, confirmed `av auth set-token` restarts the server and Protected mode
  rejects/accepts correctly, confirmed the CLI's own 401 retry message, and confirmed the
  commit-queueing fix recovers a "lost" commit via `av push` once the correct token is restored.

---

## ✅ Fixed — formerly "Architecture" (severity 4–5)

### [5] GC is mark-and-sweep without locking (race condition)
- **File:** `python/av_server/server.py` (`run_garbage_collection`, `purge_orphans`).
- **Problem:** A parallel upload whose commit hadn't been recorded yet could be deleted (the
  live object was classified as orphaned).
- **Fix:** Grace period (`GC_GRACE_SECONDS`, 1h). Objects whose DB row `created_at` or shard
  file `mtime` is younger than the GC start window are never deleted — protecting the window
  between object upload and commit push without a global lock.

### [4] N+1 DB queries during tree traversal
- **File:** `python/av_server/server.py` (`resolve_tree`, `_collect_alive_in_memory`).
- **Problem:** A separate DB query per tree node → slow for deep/wide trees.
- **Fix:** `get_commit` now traverses level-by-level with **one** batched query per depth level
  (dedup-safe via path prefixes). The GC mark phase loads **all** `DBTree` rows in **one** query
  and traverses purely in memory (`_collect_alive_in_memory`).
- **Additionally:** GC deletions now run batched (`_GC_DELETE_BATCH`) to avoid exceeding
  asyncpg's bind-parameter limit (formerly a separate severity-3 item).

### [4] Cross-language mtime inconsistency
- **File:** `python/av_cli/main.py` (`get_file_meta_safe`/`compare_meta_safe`).
- **Problem:** C++ `fs::last_write_time` (implementation-defined epoch, e.g. 1601) vs. Python
  `st_mtime_ns` (Unix epoch). Mixed paths caused spurious "modified" results.
- **Fix:** Metadata (size/mtime) now flows **exclusively** through Python's `os.stat` (a single
  Unix epoch, exactly self-consistent). The C++ core is now used purely for hashing; the unused
  C++ metadata path was removed from the CLI.

---

## ✅ Fixed — formerly "Minor Items" (severity 1–4)

| Severity | File | Problem | Fix |
|---|---|---|---|
| 4 | `python/av_cli/pointer.py` (`is_pointer_file`) | Read binary files in text mode via `readline()`; for a file with no early newline, this could read potentially huge amounts of data. | **Fixed:** Now only reads the fixed magic bytes (`_POINTER_MAGIC`) in binary mode. |
| 4 | `python/av_cli/main.py` (`commit`) | Commit JSON and ref were not written atomically (crash window). | **Fixed:** `atomic_write_text`/`atomic_write_json` (temp file + `fsync` + `os.replace`); the commit object is written before the ref. |
| 4 | `webui/src/lib/api.ts` (`fetchCommitsForBranches`) | Loaded commits serially via the parent chain (waterfall, N round trips). | **Fixed:** New `fetchCommits()` fetches the most recent commits in **one** `/api/commits` request; dashboard fetches run in parallel via `Promise.all`. |
| 3 | `python/av_server/server.py` (`upload_object`) | Parallel uploads of the same hash → `IntegrityError`/HTTP 500. | **Fixed:** `IntegrityError` is caught → idempotent HTTP 409. |
| 3 | `python/av_server/server.py` (`push_commit`) | Trusted unbounded client `metrics`/`tree` (DoS potential). | **Fixed:** Limits (`MAX_TREE_ENTRIES`, `MAX_METRICS`, `MAX_TAGS`, `MAX_TAG_LEN`, `MAX_MESSAGE_LEN`) → HTTP 422 when exceeded. |
| 3 | `python/av_server/models.py` (`DBCommit.parent_hash` FK) | FK violation when the parent commit wasn't on the server → 500. | **Fixed:** FK on `parent_hash` removed (allows shallow/out-of-order pushes; column still indexed); additionally `IntegrityError`→409 in `push_commit`. |
| 3 | `python/av_server/server.py` (`run_garbage_collection`) | `dead_hashes.in_(list)` could exceed asyncpg's parameter limit. | **Fixed:** Deletions now run in batches (`_GC_DELETE_BATCH`). |
| 2 | `python/av_server/models.py`, `server.py` | Deprecations: `datetime.utcnow()`, `@app.on_event("startup")`. | **Fixed:** `utcnow_naive()` (tz-aware → naive UTC) used everywhere; FastAPI `lifespan` handler instead of `on_event`. |
| 2 | `python/av_cli/main.py` (`save_pending_push`, `update_registry`, `save_config`) | Non-atomic JSON writes. | **Fixed:** via `atomic_write_json`/`atomic_write_text`. |
| 2 | `src/core.cpp` (`hash_file_parallel`) | Thread-pool overhead for files just over 2x the chunk size. | **Fixed:** Parallelization now only kicks in above `PARALLEL_MIN_CHUNKS` (8 chunks ≈ 64 MB). |
| 1 | `python/av_cli/client.py` (`VaultClient.session`) | `requests.Session` was never closed (not a real leak). | **Fixed:** `close()` + context manager (`__enter__`/`__exit__`) + defensive `__del__`. |

---

## ✅ Fixed — Weight Diffing (Visual) feature implementation (2026-06-24)

New feature: a fully client-side visual diff UI in the Web UI (sidebar tab "Weight Diff").
Checkpoints can be dragged (or clicked) from a list into two comparison slots; a heatmap grid
and a Recharts bar chart show which `.safetensors` layers changed between two commits — without
any new server endpoints, since `GET /api/commits/{hash}` already returns the per-layer hashes.
During end-to-end testing with real synthetic checkpoints (two `.safetensors` versions, 5
tensors, 1 deliberately changed), the following **pre-existing** bugs were uncovered, which
would have made the feature (and partly also the already-shipped CLI function
`av handoff --diff-weights`) completely unusable:

### [10] Commits are pushed before their objects → server sync for artifacts completely unusable
- **Files:** `python/av_cli/main.py` (`commit`, `flush_pending_push`),
  `python/av_server/server.py` (`push_commit`), `python/av_server/models.py` (`DBTree`).
- **Problem (two compounding bugs):**
  1. **Wrong order:** `commit` called `client.push_commit(commit_data)` **before** the
     referenced objects/layer shards were uploaded (`flush_pending_push` never uploaded them at
     all — the upload code only existed inline in the live-commit path). However, the server
     stores each tree entry's `object_hash` as a **foreign key** into `objects.hash`
     ([models.py:41](python/av_server/models.py#L41) before the fix). The insert into `trees`
     therefore practically **always** failed with `ForeignKeyViolationError`.
  2. **Misattributed error handling:** `push_commit` caught *every* `IntegrityError` and
     blanket-returned `409 "Commit already exists"`
     ([server.py:307](python/av_server/server.py#L307) before the fix) — regardless of whether
     the commit truly already existed or (as here) a completely different constraint was
     violated (tree→object FK, later also ref→commit FK). The client deliberately treats 409 as
     idempotent success (designed for concurrent pushes of the same hash) — and was thereby
     fooled into masking a total failure: `av commit`/`av push` consistently reported success,
     but **commits and refs never made it into the database**
     (`SELECT * FROM commits` → 0 rows, despite "✓ Pushed 2 commit(s)" on the console).
  3. **Deeper root cause:** Even with the correct order, the insert still fails for **every**
     layer-split `.safetensors` file: when layer-splitting, the whole file (`object_hash`) is
     deliberately **never** uploaded as its own object (only the layer shards, to avoid
     duplicate storage) — but the FK on `objects.hash` requires exactly that.
  4. Additionally, the return value of `client.update_ref(...)` was never checked anywhere — a
     failed ref update was treated as "done" instead of being re-queued into the pending queue.
- **Impact:** Every commit containing a `.safetensors` file above the LFS threshold (i.e.
  exactly this tool's core use case) could **never sync successfully** — neither live nor via
  the offline pending queue. The entire "Weight Diffing" feature (both CLI **and** the new Web
  UI) ran on empty, because no second version of a model ever actually reached the server.
- **Fix:**
  - Shared helper `upload_commit_objects()` (instead of duplicated inline code), called
    **before** `push_commit()` — both in the live-commit path and in `flush_pending_push()`
    (previously: objects were never uploaded during queue replay).
  - `DBTree.object_hash` loses its `ForeignKey("objects.hash")` (analogous to the already
    previously-removed `parent_hash` FK) — the hash remains intact as a content identity,
    without forcing a physical object row that never exists for layer-split files.
  - `push_commit` now, after an `IntegrityError`, re-checks **by hash** whether the commit
    actually exists before returning 409; otherwise 500 with a genuine error message (no more
    false "success" reported to the client).
  - `flush_pending_push`/`commit` now check the result of `update_ref()` and keep the commit in
    the pending queue if the ref update fails.
- **Verified (end-to-end against a real Docker stack):** Created four real commits with
  layer-split synthetic checkpoints; before the fix, 0 of 2 commits landed in the DB
  (`SELECT hash FROM commits` empty) despite a success message. After the fix: both the
  live push AND the offline-queue push correctly land in `commits`/`refs`;
  `GET /api/commits/{hash}` returns the expected per-layer hashes; a Node script that exactly
  replicates the browser diff logic confirms the correct layer diff between two real server
  commits.
- **Note:** Existing dev databases (created via `create_all` before this fix, no migrations)
  physically retain the old FK constraint in their schema until it's manually removed
  (`ALTER TABLE trees DROP CONSTRAINT trees_object_hash_fkey;`) or the DB is recreated — an
  already-documented migration caveat, see the `BigInteger`/`parent_hash` entry.

### [9] `av add` never persists per-layer hashes to disk
- **File:** `python/av_cli/main.py` (`add`), `python/av_cli/index.py` (`Index.add_entry`).
- **Problem:** `idx.add_entry(...)` internally already calls `self.save()` (parameter
  `auto_save=True`) **before** the caller in `add` executes the line
  `idx.entries[rel_path]["layers"] = layers`. The layer list therefore only ends up in the
  in-memory dict of the already-finished `add` process — the `.av/index` file on disk never
  gets `"layers"`. Since `av commit` loads the index fresh from disk in a **new** process,
  `tree[rel_path]["layers"]` was always `[]` in every commit object, regardless of the console
  output "Staged [ARTIFACT] … (LFS, 6 layers)". Consequence: both `av handoff --diff-weights`
  and the new Web UI fell back to a whole-file hash comparison for **every** `.safetensors`
  file (`status: changed`, but no individual changed layer reported) — the entire "per-layer
  weight diffing" feature from README Phase 11 had been ineffective since its introduction.
- **Fix:** After setting `idx.entries[rel_path]["layers"]`, `idx.save()` is now called
  explicitly so the layer data is actually persisted.
- **Verified:** Created two synthetic `.safetensors` commits (layer `layer2.weight` changed);
  `av handoff --diff-weights` now correctly reports `changed layers: layer2.weight`, whereas
  before it only reported `status: changed` with no detail.

### [4] `atomic_write_text`'s temp filename can exceed Windows' `MAX_PATH`
- **File:** `python/av_cli/main.py` (`atomic_write_text`).
- **Problem:** The temp suffix `f".tmp.{os.getpid()}.{uuid.uuid4().hex}"` (PID + a full
  32-character UUID4 hex) combined with a 64-character commit-hash filename and a deeply nested
  repo path (e.g. CI runner temp directories, OneDrive sync folders) can easily push the total
  path length past Windows' 260-character `MAX_PATH`. Rather than "just" being long, the
  `open()` call then fails with `FileNotFoundError` — the entire `av commit` aborts, even though
  the actual goal (atomic, crash-safe writes) should be the opposite of "operation fails".
  Reproducibly encountered while testing this feature (repo located under a deep temp path).
- **Fix:** Reduced the temp suffix to a short 8-character random hex (removed the PID, which was
  redundant anyway alongside the UUID for collision avoidance).

### [3] Synthetic `__header__` pseudo-layer pollutes the diff view
- **File:** `webui/src/lib/diffWeights.ts` (new).
- **Problem:** `aether_core.split_and_hash_safetensors` returns an extra entry `__header__`
  alongside the real tensors (a hash over the safetensors JSON header, for reconstruction
  integrity). `av_cli/main.py` carries this through unfiltered into
  `idx.entries[rel_path]["layers"]`. In the previous CLI text output (`--diff-weights`) this
  barely stood out; in a **visual** layer-by-layer view, however, it would be a confusing,
  non-tensor entry in the heatmap and drift chart.
- **Fix:** `diffFile()` filters out layer entries named `__header__` before they flow into the
  UI diff structure (purely client-side, no change to the core/server/index format).

### ✅ Fixed — checkpoint list resolved N commits via N parallel requests (2026-06-28)
- **Files:** `python/av_server/server.py` (`list_commits`, new module-level `resolve_tree`),
  `webui/src/lib/api.ts` (`fetchCommitsWithLayers`), `webui/src/components/WeightDiffPanel.tsx`.
- **Problem:** `GET /api/commits` (the list endpoint) returned **no** tree/layer data (metadata
  only); populating the checkpoint list with `rel_path`/layer info required calling
  `GET /api/commits/{hash}` individually for each candidate commit. These ran in parallel
  (`Promise.all`) and were hard-capped at `CHECKPOINT_FETCH_LIMIT = 30` to bound the request
  count, but it was still N requests for N commits.
- **Fix:** `get_commit`'s tree-resolution helper was factored out to a module-level
  `resolve_tree(db, root_hash)` so both endpoints share it. `GET /api/commits` gained an
  `include_layers: bool = false` query param; when true, each returned commit's tree is
  resolved and attached, matching `get_commit`'s existing shape — one request instead of N.
  Resolution runs **sequentially** per commit, not via `asyncio.gather` — a single
  `AsyncSession`/asyncpg connection can't safely run concurrent queries, so concurrent
  resolution would have been a real (if subtle) correctness bug, caught and avoided before it
  ever ran. `WeightDiffPanel.tsx` now calls `fetchCommitsWithLayers` once; `CHECKPOINT_FETCH_LIMIT`
  raised from 30 to 100 now that it bounds one request's response size, not a request count.
- **Verified:** `tests/test_server.py` (`include_layers=true` returns trees matching
  `get_commit`'s output for the same commits), `webui/src/components/__tests__/WeightDiffPanel.test.tsx`
  (updated to mock the single aggregate call).

### ✅ Fixed — `Index.save()` is not atomic (2026-06-28)
- **File:** `python/av_cli/index.py` (`Index.save`).
- **Problem:** Wrote `.av/index` directly (`open(..., 'w')`), without the temp-file +
  `fsync` + `os.replace` pattern (`atomic_write_text`/`atomic_write_json`) already established
  in `main.py`. A crash mid-write could leave a truncated/empty index file.
- **Fix:** mechanical swap to the existing `atomic_write_json` helper (`.fsutil`) — no
  behavior change beyond atomicity.
- **Verified:** `tests/test_vault.py::test_index_operations` and the rest of the existing
  `Index` coverage pass unchanged.

---

## ✅ Fixed — Web UI feedback after a real-world practical test (2026-06-24)

The user ran the app from their own folder (`Aether-Vault-Test`) and reported four real
problems. Fixed in order of difficulty, easiest first:

### [2] Tooltip text in the Layer Drift chart is black on a dark background
- **File:** `webui/src/components/LayerDriftChart.tsx`.
- **Problem:** Recharts' `<Tooltip>` colors name/value pairs black by default; the configured
  `contentStyle.color` only affects the label, not the items.
- **Fix:** Added `itemStyle={{ color: "#e2e8f0" }}` and `labelStyle={{ color: "#718096" }}`.

### [3] Layer Drift chart: Y-axis without meaning, X-axis label clipped
- **File:** `webui/src/components/LayerDriftChart.tsx`.
- **Problem:** `margin.bottom: 0` + label position `insideBottom` caused "Layer depth →" to be
  clipped at the bottom edge; the Y-axis showed only raw `0`/`1` ticks with no explanation, even
  though the chart itself shows 4 status colors (changed/unchanged/added/removed).
- **Fix:** `margin.bottom` set to 20, label position changed to `"bottom"` with a positive
  offset; `tickFormatter` translates 0/1 into "unchanged"/"changed"; added a color legend with
  all 4 status labels below the chart (`.status-legend` in `globals.css`).

### [5] `av webui` rebuilds/reloads the Docker image on every invocation, even when already running
- **File:** `python/av_cli/main.py` (`webui_cmd`).
- **Problem:** Unconditionally called `docker compose up -d --build` — even when the container
  was already running and healthy, costing a full build step plus health-check wait every time
  (in the user's real-world log: 24s build + >100s waiting).
- **Fix:** Before starting, checks via `docker inspect --format='{{.State.Health.Status}}'`
  whether the container is already running and healthy; if so, opens the browser directly (no
  `docker compose` needed). New `--rebuild` option still forces a fresh build after source code
  changes.
  **Verified:** A second consecutive run took ~15s instead of the previous >2 minutes.

### [8] Weight Diff page shows unrelated commits — no project concept, one shared server
- **Files:** `python/av_cli/main.py` (`init`, `load_config`, `config`, `commit`,
  `flush_pending_push`), `python/av_server/models.py` (`DBCommit`), `python/av_server/server.py`
  (`push_commit`, `list_commits`, `list_refs`, new: `list_projects`), `webui/src/lib/api.ts`,
  `webui/src/components/ProjectsPanel.tsx` (new), `webui/src/components/BranchList.tsx`,
  `webui/src/app/page.tsx`, `webui/src/components/Sidebar.tsx`, `webui/src/components/TopBar.tsx`.
- **Root cause:** Every `av init` repo points at the same `http://localhost:8000` by default,
  and `av webui` resolves `docker-compose.yml` relative to the **installed package**
  (`Path(__file__).parents[2]`), not the current folder — all local repos share the same
  container/DB, with no way for commits to be attributed to a repo (`DBCommit`/`DBRef` had no
  project concept).
- **Fix:** Real project separation on the still-shared server:
  - `av init` generates a `project_id` (UUID4) + `project_name` (folder name), persisted in
    `.av/config`. Repos from **before** this fix are automatically backfilled once on the next
    `load_config()` call and saved immediately (no repeated regeneration on every call).
  - New options `av config --remote-url URL` / `--name NAME`; `av config` with no arguments
    shows the current configuration including the project ID.
  - `project_id`/`project_name` flow into the hashed commit payload (two projects can never
    collide on the same commit hash); `DBCommit` gains both columns.
  - Branch refs are namespaced client-side as `"<project_id>/<branch>"` (no schema change to
    `DBRef` needed — the existing `{ref_name:path}` endpoint already allows slashes and already
    validates them via `validate_ref_name`).
  - New endpoint `GET /api/projects` (project list with commit count + last push);
    `GET /api/commits`/`GET /api/refs` optionally accept `?project_id=`.
  - New Web UI tab **"Projects"** (`ProjectsPanel.tsx`): lists all projects, "Open" sets the
    active project filter (persisted in `localStorage`) and switches back to the dashboard; the
    TopBar shows the active project as a badge with a "✕" clear button; the dashboard, branch
    list, and Weight Diff checkpoint list all respect the filter.
  - **Found+fixed during implementation:** without adjustment, `BranchList.tsx` would have shown
    the raw `"<project_id>/<branch>"` names unchanged; now only the branch part is shown (with
    the project name as a prefix when multiple projects are visible simultaneously, to keep
    identically-named branches distinguishable).
  - **Schema migration:** `DBCommit` gains two `NOT NULL` columns; since this project doesn't use
    migrations (`create_all` only creates missing tables), the existing dev schema was upgraded
    **in place via `ALTER TABLE`** (existing commits set to `project_id='legacy'`) instead of
    wiping the DB — no data loss.
- **Verified (end-to-end, four real test repos):**
  - Two fresh projects (`proj_a`, `proj_b`) → `GET /api/projects` correctly lists both with
    commit count/last push; `GET /api/commits?project_id=…` and `GET /api/refs?project_id=…`
    filter correctly; both independently have a `main` branch without overwriting each other
    (`"<id-a>/main"` and `"<id-b>/main"` coexist).
  - A repo with `.av/config` **without** `project_id` (simulating an "old" state) → backfill
    kicks in on the first command, then stays stable across multiple calls (no new UUID per
    call).
  - Two projects with an **identical** `project_name` but different `project_id` → both appear
    separately in `GET /api/projects` (distinguishable by `project_id`).
  - The offline-queue path (`av push` after a server restart) correctly uses the already
    namespaced ref name — lands in the correct project branch.
  - `av branch`/`av status`/`av list-meta` (all local) remain functional unchanged.
  - `av gc`/`GET /api/stats`/`GET /api/dashboard/summary` continue to run error-free across
    **all** projects (deliberately global/cross-project — object deduplication is meant to stay
    cross-project).
- **Deliberately left unchanged (documented, not a bug):**
  - `GET /api/commits/{hash}` remains accessible independent of project (a universal
    content-address lookup) — a file with a known hash is reachable from any project. This is
    intentional (same philosophy as the cross-project object deduplication) and not a security
    issue, since hashes aren't guessable.
  - `GET /api/stats` and `GET /api/dashboard/summary` remain unscoped by project (showing the
    global object store / all refs unfiltered) — a consistent scope decision, not a follow-on
    bug; the same `?project_id=` filter can be added later if needed.
  - `python/av_server/storage.py`'s local filesystem fallback (only active when the DB is empty)
    has no project concept — irrelevant as long as the DB-backed route is primary.

---

## ✅ Fixed — Framework Plugins: dataset auto-logging + symmetric import commands (2026-06-25)

Added dataset auto-logging (`dataset_paths` on both training callbacks) and a matching
"import" entry point across all three plugins (Lightning, Transformers, MLflow) for backfilling
artifacts that already exist on disk/in MLflow from before a callback was wired in. Found via a
real manual debugging pass (actual installed MLflow against a sqlite-backed tracking store, not
mocks) rather than unit tests alone.

### [4] `MlflowClient.download_artifacts()` raises instead of returning empty for a zero-artifact run
- **File:** `python/av_plugins/mlflow.py` (`import_run`).
- **Problem:** The intended flow was "download artifacts, then check if the resulting directory
  is empty, then raise a clear error." In practice, calling
  `client.download_artifacts(run_id, ".", dst_path=...)` on a run with **zero** logged artifacts
  doesn't return an empty directory at all — MLflow's `RunsArtifactRepository` itself raises an
  internal `mlflow.exceptions.MlflowException` ("Failed to download artifacts from path '.',
  please ensure that the path is correct."), which would have leaked straight through `import_run`
  to the caller as a confusing, MLflow-internals-specific error instead of Aether-Vault's own
  clear message.
- **How found:** Only surfaced when testing against a real MLflow installation
  (`pip install mlflow`, sqlite-backed tracking store) with a run that logs a metric but no
  artifacts — the equivalent mocked/stub-based test would not have caught this, since it would
  need to know to replicate MLflow's specific failure mode rather than just "return empty".
- **Fix:** Check `client.list_artifacts(run_id)` *before* attempting any download; raise
  Aether-Vault's `AetherVaultException("MLflow run {run_id} has no artifacts to import.")`
  immediately if it's empty, so `download_artifacts` is never called in the zero-artifact case.
- **Verified:** `tests/test_plugins.py::test_mlflow_import_run_raises_when_no_artifacts` now
  passes against a real MLflow run with a metric but no artifacts (previously failed with the
  raw `MlflowException` traceback before this fix).

### 🔸 Not a bug — imports commit everything currently staged, not just the imported path
- **Files:** `python/av_plugins/lightning.py`, `python/av_plugins/transformers.py`,
  `python/av_plugins/mlflow.py` (all three `import_*`/callback functions).
- **Observation during manual testing:** staging an unrelated file (`av add notes.py`) and then
  calling `import_checkpoint()` produces a commit containing **both** the unrelated file and the
  imported checkpoint.
- **Why not changed:** This is the existing, intentional behavior of `av commit` everywhere else
  in the tool (it commits the full staging area, mirroring `git commit`'s model) — the plugins
  reuse `commit` as-is rather than duplicating its logic (see the in-process CLI-invocation
  design note in `Aether-vault-Obsidian-Vault/ARCHITECTURE.md`). Changing it would mean the
  plugins' commits behave differently from every other `av commit` in the tool, which would be
  more surprising, not less. Documented as a usage caveat in `README.md` instead of "fixed."

### [2] Test fixtures let MLflow write a stray `mlruns/` folder into the real repo root
- **File:** `tests/test_plugins.py` (`test_mlflow_import_run`, `test_mlflow_import_run_raises_when_no_artifacts`).
- **Problem:** Both tests pointed MLflow's **tracking** URI at a sqlite DB inside `tmp_path`,
  but a sqlite tracking URI only relocates run *metadata* — MLflow still defaults **artifact**
  storage to `./mlruns` relative to the process's current working directory. Since pytest's cwd
  is the real repository root, running these tests left a real `mlruns/` directory (with actual
  run/artifact files) sitting in the repo working tree — caught when the next Obsidian vault
  regeneration picked up an unexpected `mlruns.md` folder index that had no business existing.
- **Fix:** Both tests now use `monkeypatch.chdir(tmp_path)` before setting the tracking URI, so
  MLflow's default relative artifact path lands inside the test's own temp directory instead of
  the real repo.
- **Verified:** Re-ran the full suite from the repo root; confirmed no `mlruns/` directory is
  created there afterward (`git status` clean of it).

---

## ✅ Fixed — Minimum-viable test suite + diagnostics (2026-06-25)

Added a pytest suite covering the CLI commands (`init`/`add`/`status`/`commit`/`checkout`), the
`aether_core` C++ bindings, and registry/config load-save, plus two new commands (`av doctor`,
`av test`). Found via real end-to-end manual testing (not just unit tests) while writing the
`checkout` tests below.

### [8] `av checkout` never restores `code`-type files — only `artifact`-type
- **Files:** `python/av_cli/main.py` (`add`, `checkout`, `upload_commit_objects`).
- **Problem:** `add()` only ever copied a file's bytes into `.av/objects/<hash>` when it was
  classified `artifact` **and** exceeded the LFS size threshold; every `code`-type file (and any
  sub-threshold artifact) had its hash recorded in the index/commit tree but its actual bytes
  were never written anywhere outside the live working-tree file. `checkout()`'s restore loop
  (the unified flat-tree format from PR #8) then only materialized entries where
  `file_type == "artifact"` — so checking out an older commit silently left every `code` file at
  whatever content the working tree already had, while still printing
  `"Checked out '<hash>'"` and reporting a clean `av status` afterward. `upload_commit_objects()`
  had the same `type != "artifact"` skip, so even a successful remote push never carried code
  bytes either.
- **Impact:** For a tool whose entire pitch is versioning "the Holy Trinity" (code + models +
  datasets) together, the **code** pillar could never actually be rolled back — `av checkout
  <old-commit>` silently no-op'd on every `.py`/`.json`/`.md`/etc. file. Manual repro: commit
  `train.py` with `print('v1')`, overwrite + commit `print('v2')`, `av checkout <v1-hash>` →
  `train.py` still read `print('v2')`, with no error of any kind.
- **Fix:** `add()` now writes a CAS object for **every** tracked file regardless of type/size
  (not just LFS-thresholded artifacts); `checkout()`'s restore step and
  `upload_commit_objects()` no longer gate on `file_type == "artifact"` — both apply uniformly to
  every tree entry. The artifact-specific layer-reassembly path is unaffected (code never has
  `layers`, so it always falls through to the plain copy/download branch).
- **Verified:** Manual repro above re-run after the fix — `train.py` correctly reads `print('v1')`
  after `av checkout <v1-hash>`; `tests/test_cli.py::test_checkout_restores_previous_commit` and
  `test_checkout_refuses_with_uncommitted_changes_without_force` (both use a tracked `code`-type
  file) pass.
- **Note:** This doubles on-disk storage for tracked code files (working-tree copy + CAS copy) —
  the same tradeoff git itself makes for every tracked blob, and necessary for checkout to have
  anything to restore from.

---

## ✅ Fixed — Closing the 5 remaining test-coverage roadmap gaps (2026-06-25)

Added `tests/test_server.py` (av_server FastAPI tests + one live-wire integration test),
`tests/test_cli_commands.py` (direct CLI command tests), a Vitest suite for `webui/`'s pure
diff/formatting logic, CI jobs for all of the above plus the framework-plugin extras, and an
`av test --webui` convenience flag. Found via manual end-to-end debugging while exercising the
new `--webui` flag for real (not mocked) on this Windows dev machine.

### [4] `av test --webui` reports "npm not found on PATH" even when npm is genuinely installed
- **File:** `python/av_cli/main.py` (`test_cmd`).
- **Problem:** The initial implementation called `subprocess.run(["npm", "test"], cwd=webui_dir)`
  directly. On Windows, the real `npm` executable is a `npm.cmd` shim; passing the bare string
  `"npm"` to `subprocess.run` without `shell=True` frequently fails to locate/execute it via
  `CreateProcess`, even though `npm` is genuinely installed and resolvable from an interactive
  shell (`npm --version` worked fine in the same environment). This raised `FileNotFoundError`,
  which the code caught and reported as the user-facing "npm not found on PATH — install
  Node.js..." message — a *correct-looking* error for the *wrong* reason, since npm was in fact
  installed.
- **How found:** Only surfaced by actually running `av test --webui` for real after writing it
  (not just the monkeypatched unit tests, which mock `subprocess.run` itself and therefore never
  exercise the real Windows path-resolution behavior) — exactly the kind of platform-specific gap
  the manual-debugging step exists to catch.
- **Fix:** Resolve the executable's full path first via `shutil.which("npm")` (which does the
  PATHEXT-aware lookup correctly, the same way an interactive shell does) and pass that resolved
  path to `subprocess.run` instead of the bare string. The "not found" error message is now only
  shown when `shutil.which` genuinely returns `None`.
- **Verified:** `av test --webui -k test_validate_ref_name_accepts_normal_names` run for real
  (not mocked) on this machine — failed with the npm-not-found message before the fix, ran both
  suites successfully (pytest, then the real `npm test` → Vitest) after it.

---

## ✅ Fixed — `tests/test_server.py` run for real against a live Docker stack (2026-06-26)

`docker compose up -d db redis aether-vault-server` was started and the 17 `test_server.py`
tests (previously only verified by static source review) were run against it for the first
time. Found and fixed one genuine production bug plus two test-infrastructure issues.

### [7] GC's physical-shard sweep silently never deletes anything on a host ahead of UTC
- **File:** `python/av_server/server.py` (`run_garbage_collection`).
- **Problem:** `grace_ts = gc_cutoff.timestamp()`, where `gc_cutoff` is a **naive** datetime that
  represents UTC (per `utcnow_naive()`'s own docstring). Calling `.timestamp()` directly on a
  naive datetime makes Python treat it as **local** time when converting to a Unix epoch — on
  this host (UTC+2), that silently shifted `grace_ts` two hours earlier than the real cutoff.
  Since `obj_path.stat().st_mtime` is a real, correctly-UTC-based epoch, the comparison
  `st_mtime >= grace_ts` then almost never evaluates as "old enough to delete": a file would
  need to be more than `GC_GRACE_SECONDS + |local UTC offset|` old before the physical sweep
  would ever touch it — on a host *behind* UTC, the bug runs the other way and would delete
  objects **before** their real grace window expires, defeating the entire purpose of the grace
  period (protecting objects mid-upload from a concurrent GC).
- **How found:** `test_gc_respects_grace_period_then_sweeps_when_aged` — after zeroing
  `GC_GRACE_SECONDS`, the test asserted the now-orphaned object's shard file was actually
  removed from disk; it consistently returned `deleted_objects: 0` and the file remained on
  disk, even though the DB-side deletion (a naive-to-naive datetime comparison, unaffected by
  this bug) correctly removed the corresponding row. The DB/filesystem inconsistency was the
  tell — only the epoch-converting comparison was wrong.
- **Fix:** `grace_ts = gc_cutoff.replace(tzinfo=timezone.utc).timestamp()` — attaching the
  correct `tzinfo` before converting to epoch makes `.timestamp()` compute the right value
  regardless of the host's local timezone.
- **Verified:** Re-ran the test after the fix — the aged object's shard file is now actually
  removed from disk, and `deleted_objects: 1` is returned as expected.

### [3] Test-only: `tests/test_server.py`'s per-test DB cleanup crashed at teardown
- **File:** `tests/test_server.py` (`_truncate_all`, `db` fixture).
- **Problem:** The cleanup helper ran `async with engine.begin() as conn: ...` using the
  module's pooled SQLAlchemy async engine, invoked via a fresh `asyncio.run()` call in the
  fixture's teardown. The engine's pooled connection is bound to whichever event loop first used
  it (`TestClient`'s own internal lifespan loop); reusing that pool from a *different* loop
  (the one `asyncio.run()` spins up for the teardown call) raised
  `RuntimeError: ... got Future ... attached to a different loop` on every single test.
- **Fix:** Open a brand-new `asyncpg.connect()` directly (bypassing the SQLAlchemy pool
  entirely) for the truncate, scoped wholly to the teardown call's own event loop.
- **Verified:** Re-ran the suite after the fix — no more teardown errors.

### [2] Test-only: leftover orphan shard files from earlier tests polluted the GC grace test
- **File:** `tests/test_server.py` (`db` fixture).
- **Problem:** Per-test cleanup truncated the DB tables but never cleared
  `CASStorage`'s on-disk `objects/`/`commits/`/`refs/` directories, which are shared across the
  whole test session. Earlier tests' uploaded objects (now orphaned once their DB rows were
  truncated) accumulated on disk; once the GC timezone bug above was fixed, the grace-period
  test's `deleted_objects == 1` assertion became flaky — it correctly swept *all* eligible
  orphans on disk, not just the one this specific test created (observed once as `3`).
- **Fix:** `_clear_storage_dirs()` added to the `db` fixture's teardown, deleting file contents
  (not the directories themselves) from all three storage subdirectories after every test.
- **Verified:** Full `test_server.py` run is now stable at 29 passed, 0 failed.

### [2] Test-only: the real-wire test's reachability check raced with collection-time load
- **File:** `tests/test_server.py` (`test_cli_commit_pushes_to_a_live_server`).
- **Problem:** `_real_server_reachable()` was wired up as a `@pytest.mark.skipif(...)`
  condition, which pytest evaluates exactly once, at module-collection time — the very start of
  the whole run, before any other test executes. When the full suite was first run through the
  plugin `venv/` together with the live Docker stack (a much heavier collection phase, importing
  `torch`/`transformers`/`lightning`), the 1.5s `httpx` reachability check raced against that
  load spike and read the server as unreachable even though it was confirmed healthy and fast
  (51ms) moments later via a direct `curl`.
- **Fix:** Moved the check into the test body itself (`pytest.skip(...)` called lazily, only
  when this specific test actually runs, after collection and ~100 other tests have already
  settled) instead of a collection-time `skipif` decorator.
- **Verified:** Re-ran the full combined venv+Docker suite twice after the fix — stable at
  105 passed, 3 skipped (the 3 permanent-by-design "raises ImportError when missing" tests).

---

## ✅ Fixed — `webui/` test infrastructure files broke the production build (2026-06-26)

Found while adding React Testing Library component tests and a Playwright E2E suite for
`webui/` (the one remaining roadmap line).

### [6] `vitest.setup.ts` broke `next build` via an "unused `@ts-expect-error`" type error
- **File:** `webui/vitest.setup.ts`, `webui/tsconfig.json`.
- **Problem:** `next build` type-checks the *entire* TypeScript project, including files that
  are never part of the shipped app — `vitest.setup.ts` was picked up by `tsconfig.json`'s
  broad `"**/*.ts"` include. That file stubs `ResizeObserver` for jsdom with an
  `@ts-expect-error` comment suppressing a type error that exists under Vitest's type
  resolution but not under Next's (the DOM lib types it pulls in already cover the assignment) —
  TypeScript itself treats an `@ts-expect-error` with nothing to suppress as an error
  ("Unused '@ts-expect-error' directive"), so the production Docker image build for
  `aether-vault-webui` failed outright (`docker compose build aether-vault-webui` → exit 1).
- **How found:** The Weight Diff E2E test was timing out with the dashboard stuck on
  "Connecting…"/"Loading checkpoints…" forever, even though a manual `fetch()` to the API from
  inside the same browser context succeeded — the running `aether-vault-webui` container was
  46 hours old (built before several of this session's fixes); rebuilding it to get current
  source is what actually surfaced the type-check failure instead of silently shipping stale
  code.
- **Fix:** Replaced the `@ts-expect-error` comment with a plain type cast (never errors either
  way, so it can't go stale), and added `vitest.config.ts`/`vitest.setup.ts`/
  `playwright.config.ts`/`e2e/`/`src/**/*.test.ts(x)` to `tsconfig.json`'s `exclude` so test-only
  files are never part of the app's production type-check scope again.
- **Verified:** `docker compose build aether-vault-webui` succeeds; the rebuilt container's
  Weight Diff tab now loads real data and both new Playwright specs pass against it.

### [8] `av add` stored the whole-file blob *in addition to* split layers — layer-dedup gave zero real storage savings
- **File:** `python/av_cli/main.py` (`add`, `doctor`).
- **Problem:** When `aether_core.split_and_hash_safetensors` succeeded, `add()` correctly
  stored each layer separately under `.av/objects/` — but then *unconditionally* also copied
  the entire original file to `.av/objects/<whole_file_hash>` (lines 469–473, pre-fix). Every
  fine-tune commit that only touched the classifier head still re-stored the *full* checkpoint
  every time, on top of the (genuinely deduped) per-layer copies. Net effect: a layered
  artifact ended up using *more* disk than not splitting at all, completely negating the
  feature's purpose. The codebase's own `push_objects()` already had the correct condition
  ("upload the whole-file object only if layers weren't successfully chunked," line 244) and
  `checkout` already reassembles the whole file from layers on demand when the blob is
  absent — `add()` was the one place that didn't follow that established pattern.
- **How found:** Building benchmark #2 of the new `av benchmark` suite ("safetensors
  layer-dedup storage savings" vs DVC/Git LFS/MLflow) — the real measured numbers came back
  *worse* for Aether (162.5MB) than all three whole-file-only competitors (125.8MB each) after
  6 simulated fine-tune commits, the opposite of the intended/advertised behavior.
- **Fix:** `add()` now only writes the whole-file blob when `layers` is empty, matching
  `push_objects()`'s existing condition exactly. `doctor`'s orphaned-pointer detection and
  `--fix` recovery were also made layer-aware (an entry with `layers` is now checked/repaired
  per-layer, not by checking for an intentionally-absent whole-file blob — without this,
  every layered artifact would have started failing `av doctor` as a false-positive "orphaned
  pointer" the moment the whole-file copy was removed).
- **Verified:** New tests in `tests/test_cli.py`
  (`test_add_safetensors_skips_whole_file_copy_when_layers_split`,
  `test_checkout_reassembles_safetensors_from_layers`,
  `test_doctor_does_not_flag_layered_artifact_as_orphaned`,
  `test_doctor_detects_orphaned_layered_artifact_with_missing_layer`) — full suite green.
  Re-ran `benchmarks/bench_safetensors_dedup.py` after the fix: Aether dropped from 162.5MB to
  36.7MB for the same 6-commit sequence, now genuinely beating all three competitors (125.8MB
  each) instead of losing to them.

## ✅ Fixed — Benchmark-driven performance pass: no-op `add` and `commit` latency (2026-06-27)

`development/BENCHMARKS.md` rated two benchmarks BAD against other tools. Both were traced to
exact root causes (not rewrites) and improved without changing the CLI's external behavior or
the server's API contract. Severity = impact on the benchmark gap; difficulty = effort/risk to
implement, both rated 1–10.

### [7] No-op `add`/`status` was 6.1x slower than Git LFS — redundant stat, unconditional index save, eager imports
- **File:** `python/av_cli/main.py` (`add`, module-level imports).
- **Problem:** Benchmark #4 showed `av add .` on 60 unchanged files taking 875ms vs Git LFS's
  143.4ms. The existing fast path (`compare_meta_safe`, skips re-hashing when size+mtime match)
  worked correctly — the cost was everything around it:
  1. **(Severity 4, Difficulty 1)** `add()` fetched `meta` via `get_file_meta_safe()` then
     immediately called `compare_meta_safe()`, which calls `get_file_meta_safe()` again on the
     same path — a redundant second `stat()` syscall per file.
  2. **(Severity 5, Difficulty 2)** `idx.save()` ran whenever `files_to_process` was non-empty,
     regardless of whether any entry actually changed — a true no-op still did a full JSON
     serialize-and-write of the index.
  3. **(Severity 7, Difficulty 2)** `from .client import VaultClient` at module scope pulled in
     `requests`/`urllib3`/`certifi` on *every* `av` invocation, including purely local commands
     (`add`, `init`, `status`, `branch`) that never touch the network.
  4. **(Severity 2, Difficulty 2, stretch)** `import aether_core` (the pybind11 C extension) at
     module scope cost ~90ms even when the fast path means no hashing happens at all.
- **Fix:** Inlined the meta comparison in `add()` instead of re-calling `compare_meta_safe()`;
  added an `any_changed` flag so `idx.save()` only runs when an entry actually changed; moved
  `VaultClient` to local imports inside the five commands that use it (`commit`, `checkout`,
  `push`, `gc`, `doctor`), with a module `__getattr__` so `main.VaultClient` stays resolvable
  for existing test monkeypatching; made `aether_core` import lazy via `_get_aether_core()`,
  called on first actual hash/split.
- **Verified:** `pytest tests/` green (111 passed incl. new tests, 20 skipped, same baseline);
  manually confirmed `.av/index`'s mtime is untouched across a true no-op `add .` in a scratch
  repo. Re-ran `benchmarks/bench_noop_status_speed.py`: 875.0ms → 552.5–624.0ms across repeated
  captures (~30% faster). Still rated BAD — the residual gap is CPython interpreter + `click`
  import startup cost, which a compiled Git LFS binary doesn't pay; closing that fully would
  mean rewriting the CLI in a compiled language, out of scope for this pass.

### [9] `commit` was 8.3x slower than DVC — serial per-object HEAD+POST instead of the existing batch-check endpoint
- **Files:** `python/av_cli/main.py` (`upload_commit_objects`), `python/av_cli/client.py`.
- **Problem (Severity 9, Difficulty 5):** Benchmark #3 showed `commit` on a 60-file fixture
  taking 2,933.7ms vs DVC's 354.4ms. `upload_commit_objects()` looped over every tracked
  file/layer hash and called `client.upload_object()`, which issued a `HEAD` request to check
  existence, then a `POST` to upload if missing — entirely serially. For 60 objects that's up
  to ~120 sequential network round trips. Separately, `python/av_server/server.py` already
  exposed `POST /api/sync/batch-objects` (checks many hashes in one call, backed by the
  RedisBloom filter) — nothing in the client called it; it was dead capability.
- **Fix:** Added `VaultClient.batch_check_objects(hashes)` (one POST to the existing endpoint)
  and a `known_missing` parameter on `upload_object()` to skip the now-redundant per-object
  `HEAD` when the caller already knows the hash is missing. Rewired `upload_commit_objects()`
  to collect every referenced hash once, batch-check it in a single call, then upload only the
  missing objects concurrently via a `ThreadPoolExecutor` (8 workers — these are network-bound
  HTTP calls, not CPU work). Still blocks until every upload completes before returning, so the
  existing invariant ("objects must land before `push_commit()`," documented in the function's
  own docstring re: the server's FK constraint) is unchanged. `flush_pending_push()` calls the
  same function, so the offline-retry queue gets the same speedup for free.
- **Verified:** `pytest tests/` green, including new `tests/test_client.py`
  (`batch_check_objects` request shape, empty-input short-circuit, non-200 handling,
  `known_missing` HEAD-skip) and two new tests in `tests/test_cli_commands.py` asserting
  `upload_commit_objects()` batch-checks once and uploads only the hashes the batch-check
  reported missing. Also verified against a real `av_server` (Docker Compose: Postgres +
  Redis + FastAPI, not mocked): ran `av init/add/commit/push` end-to-end in a scratch repo,
  confirmed all uploaded objects are queryable via a live `batch-objects` call, and confirmed
  the offline pending-push path (server stopped mid-session, commit queued, server restarted,
  `av push` flushed it) still works through the same parallelized code. Re-ran
  `benchmarks/bench_commit_push_latency.py`: 2,933.7ms → 1,357–2,532ms across captures
  (45–54% faster depending on machine load). Still rated BAD against DVC — DVC's `commit`
  never touches the network (`dvc push` is a separate step), while av intentionally uploads
  objects synchronously during `commit` to satisfy the server's FK ordering constraint; that
  architectural difference is out of scope for this pass (see README's Open Source Roadmap).

## ✅ Fixed — REPL session construction crashed bare `av` under Git Bash/mintty on Windows (2026-06-27)

### [7] Bare `av` (and `av init`) crashed with an unhandled `NoConsoleScreenBufferError` outside a real Windows console
- **Files:** `python/av_cli/repl.py` (`run_repl`).
- **Problem (Severity 7, Difficulty 1):** Found during step 1 of the Phase 27 wrap-up
  (manual debugging against the real installed `av` binary, not `CliRunner`) — bare `av` and
  `av init` (default flow) both crashed outright when run from Git Bash/mintty on Windows.
  `sys.stdin.isatty()`/`sys.stdout.isatty()` both report `True` in that terminal, so
  `ui.is_interactive()` correctly decided to skip the Local/Enterprise prompt only where
  expected — but `run_repl()`'s call to `prompt_toolkit.PromptSession(...)` still raised
  `prompt_toolkit.output.win32.NoConsoleScreenBufferError` ("Found xterm-256color, while
  expecting a Windows console") unconditionally, because mintty's pty emulation has no real
  Win32 console screen buffer behind it even though it reports as a tty. Nothing caught the
  exception, so it propagated all the way out and crashed the whole `av` invocation with a
  raw Python traceback — a real first-impression bug for exactly the platform (Windows + Git
  Bash) this CLI's own dev environment runs on.
- **Fix:** Wrapped the `PromptSession(...)` construction, and each loop iteration's
  `session.prompt(...)` call, in a broad `except Exception` in `run_repl()`. On failure, prints
  one warning line ("Interactive session isn't available in this terminal — run `av <command>`
  directly instead.") and returns/breaks instead of crashing — the rest of `av init`/bare `av`
  (repo bootstrap, Docker reconnect) already completed before this point, so the user still
  gets a fully working repo, just without the interactive session in that specific terminal.
- **Verified:** added `tests/test_repl.py::test_repl_degrades_gracefully_when_session_cannot_be_constructed`
  (monkeypatches `PromptSession` to raise, asserts `run_repl()` doesn't raise and prints the
  warning). Manually re-ran the exact repro from a real Git Bash shell — bare `av`, `av init`
  (fresh repo), and re-running `av init` against an already-initialized repo (reconnect path)
  all now complete cleanly with the warning instead of a traceback.

## ✅ Fixed — `av update --docker` could hang for minutes against a non-running/unreachable Docker daemon (2026-06-27)

### [6] `check_for_docker_update()` attempted `docker compose pull` without first checking Docker was running
- **Files:** `python/av_cli/docker_runtime.py` (`check_for_docker_update`).
- **Problem (Severity 6, Difficulty 1):** Found during manual debugging while building the
  Docker auto-update feature — calling `docker_runtime.check_for_docker_update()` against a
  registry image that doesn't exist yet (nothing published to GHCR) caused the process to sit
  unresponsively for over a minute (up to the 600s-per-service timeout on `pull_latest_image()`,
  twice, since there are two release images) instead of failing fast. Root cause: every other
  Docker-facing entry point in this module (`ensure_local_backend_running()`) checks
  `check_docker_running()` first and fails fast with a clear message
  ("Docker is not running...") — `check_for_docker_update()` was the one path that skipped this
  check and went straight to `docker compose pull`, which has no comparable fast-fail behavior of
  its own against an unresponsive/absent daemon or a registry image that 404s.
- **Fix:** Added the same `check_docker_running()` guard used elsewhere in this module, before
  attempting any pull — returns a `DockerUpdateResult(checked=False, message="Docker is not
  running...")` immediately, matching the existing UX convention instead of introducing a new
  failure mode.
- **Verified:** added `tests/test_docker_runtime.py::test_check_for_docker_update_fails_fast_when_docker_not_running`
  (monkeypatches `check_docker_running` to report not-running, asserts `pull_latest_image` is
  never called). Found live by actually running the unguarded version against this machine's real
  Docker installation pointed at the (not-yet-published) GHCR images and observing the hang
  firsthand, then killing the process — not just inferred from reading the code.

## ✅ Fixed — `av stash pop`/`av stash list` had two real bugs, found via manual debugging and the test suite (2026-06-28)

### [6] `av stash pop` restored a modified-but-unstaged file's index entry with the dirty hash/stat instead of HEAD's baseline, making it look falsely clean
- **Files:** `python/av_cli/main.py` (`_stash_apply_or_pop`).
- **Problem (Severity 6, Difficulty 2):** `status()` detects a "modified" tracked file purely by
  a stat mismatch between the on-disk file and the size/mtime stored in its index entry — there's
  no separate "dirty" flag. `_stash_apply_or_pop()`'s first version restored every entry
  (regardless of `was_staged`) using the stash record's own hash and the just-written file's real
  stat — which, for a `was_staged=False` entry, makes the stored stat match the restored (dirty)
  file exactly. Found via manual debugging (this session's established practice of driving new
  features with the real `av` binary, not just unit tests): after `av stash` then `av stash pop`,
  a file that had been modified-but-unstaged before the stash silently vanished from `av status`
  entirely instead of showing up under "Changes not staged for commit" again.
- **Fix:** `_stash_apply_or_pop()` now branches on `was_staged`. For `True`, it keeps the original
  behavior (dirty hash + real stat + `staged=True`, so it shows as "to be committed" — `status()`
  trusts the `staged` flag before ever checking the stat). For `False`, it looks up
  `resolve_head_tree()` again and stores *HEAD's* hash/size with `mtime_ns=0` (deliberately
  non-matching) — exactly mirroring how `_stash_push()` represents an unstaged modification in
  the first place, so the stat-mismatch check correctly reports "modified" again after pop.
- **Verified:** `tests/test_stash.py::test_stash_pop_restores_staged_and_modified_state_correctly`
  asserts both the staged and modified-unstaged entries are reported correctly by `av status`
  after a push/pop round-trip, not just that the file contents are right.

### [4] Two stashes created within the same second sorted unpredictably in `av stash list`
- **Files:** `python/av_cli/main.py` (`_stash_push`'s stash ID generation).
- **Problem (Severity 4, Difficulty 1):** Stash filenames are `<timestamp>-<shortid>.json`, and
  `_list_stash_files()` sorts them newest-first by reverse filename order — relying on the
  timestamp prefix to dominate the comparison. The timestamp used second-level resolution
  (`%Y%m%dT%H%M%SZ`); two stashes created within the same second (an entirely realistic case —
  e.g. a script, or just two fast manual `av stash` calls) share an identical prefix, so the sort
  falls back to comparing the random 6-hex-character shortid, which has no relationship to
  creation order at all. Found by the test suite itself
  (`tests/test_stash.py::test_stash_list_orders_newest_first`), which failed on the very first
  run — not inferred from reading the code first.
- **Fix:** Switched the stash ID's timestamp component to microsecond resolution
  (`%Y%m%dT%H%M%S%f`), which two sequential CLI invocations (each involving real file I/O) will
  not collide on in practice.
- **Verified:** re-ran the previously-failing test 5 times in a row to rule out remaining
  flakiness (all passed) in addition to the full suite.

## ✅ Fixed — WebUI logo/theme/panels manual debugging pass (2026-06-28)

### [3] Top bar title stayed hardcoded to "Dashboard" on every sidebar tab
- **Files:** `webui/src/components/TopBar.tsx`, `webui/src/app/page.tsx`.
- **Problem (Severity 3, Difficulty 1):** `TopBar.tsx` rendered `<span className="top-bar-title">Dashboard</span>`
  as a literal string, with no prop driving it. Harmless before this session — every sidebar tab
  rendered the same Dashboard view, so the label was always correct by coincidence. Once Commits,
  Branches, Metrics, and Storage became real, distinct panels (Phase 30), the header stayed stuck
  on "Dashboard" while the sidebar's active-tab highlight correctly moved. Found via manual
  debugging — driving the running `npm run dev` server with a headless Playwright browser and
  screenshotting each tab — not from reading the diff.
- **Fix:** Added an optional `title` prop to `TopBar` (defaulting to `"Dashboard"` to keep the
  component's existing standalone behavior), and a `TAB_TITLES` lookup in `page.tsx` mapping each
  `active` id to its display name, passed in as `title={TAB_TITLES[active] ?? active}`.
- **Verified:** re-ran the Playwright screenshot pass after the fix — the header now reads
  "Commits", "Branches", "Metrics", "Storage", "Weight Diff", "Projects", or "Dashboard" to match
  whichever sidebar tab is active.

## ✅ Fixed — Two test-fragility bugs found while adding benchmark regression tracking (2026-06-28)

### [3] `test_doctor_fix_cannot_recover_truly_missing_object` silently depended on no `av_server` being reachable
- **Files:** `tests/test_cli.py`.
- **Problem (Severity 3, Difficulty 1):** the test's only justification for expecting the
  missing object to stay unrecoverable was a comment — "No server running in this test
  environment" — not an explicit mock. That was true by environmental coincidence until this
  session's real Docker stack (db/redis/server/webui) was left running to capture fresh
  benchmark numbers; with a real `av_server` reachable on `localhost:8000`, `av doctor --fix`'s
  recovery path could genuinely reach it, and the object was no longer truly unrecoverable —
  breaking the test's `[WARN]`/"could not recover" assertions. Found via manual debugging (the
  full pytest run after the benchmark work), not by reading the diff.
- **Fix:** explicitly monkeypatch `VaultClient.server_available` to return `False`, mirroring
  the adjacent `test_doctor_fix_downloads_missing_object_from_server` test's existing pattern
  (which forces it `True` to test the opposite path) — neither test should depend on whatever a
  real server happens to be doing on the machine running the suite.
- **Verified:** re-ran with the real Docker stack still up — test now passes regardless.

### [2] A new test's `monkeypatch.setattr("benchmarks.tool_runner.render_doc_header", ...)` broke under an adjacent `importlib.import_module` patch in the same test
- **Files:** `tests/test_cli.py` (`test_benchmark_command_markdown_writes_file`).
- **Problem (Severity 2, Difficulty 2):** the test patches `main_module.importlib.import_module`
  to a fake (`importlib` is a shared global module object, so this patches the *real*
  `importlib.import_module` for the whole process, not just `main_module`'s reference to it).
  pytest's own `monkeypatch.setattr(<string>, ...)` form internally calls the real
  `importlib.import_module` to resolve the dotted path — which was now the test's own fake,
  returning a `_FakeBenchModule` instead of the real `benchmarks.tool_runner` module, and the
  second `setattr` call crashed with `AttributeError: '_FakeBenchModule' object has no
  attribute 'tool_runner'`.
- **Fix:** import the real module via a plain `import benchmarks.tool_runner as tool_runner_module`
  statement first (plain `import` statements use the import system's `__import__` machinery
  directly, not `importlib.import_module`, so they're unaffected by the patch) and call
  `monkeypatch.setattr(tool_runner_module, "render_doc_header", ...)` against that object
  instead of the string-target form.
- **Verified:** `pytest tests/test_cli.py -k benchmark` and the full suite (249 passed, 3
  skipped) both green after the fix.


## ✅ Fixed — Short-hash checkout round (2026-08-21)

### [4] `av checkout` rejected the short hashes `av commit` itself prints
- **Files:** `python/av_cli/main.py` (`checkout`, short-hash print at line 1285),
  `python/av_cli/handoff.py` (`load_commit`), new shared helper in
  `python/av_cli/fsutil.py` (`find_commit_file`) + new `AmbiguousCommitHash` exception in
  `python/av_cli/exceptions.py`.
- **Problem:** `av commit` prints `[a54a0b2] <message>` (7-char prefix), but `checkout`
  only resolved either an exact branch name or the full 64-char hash — no prefix matching
  existed anywhere. Copying the hash av had just printed and running
  `av checkout a54a0b2` failed with `Error: Commit 'a54a0b2' not found.` Same gap in
  `av handoff --since <hash>`.
- **How found:** manual debugging session against the real installed `av` binary in a scratch
  repo (per `Aether-vault-Obsidian-Vault/Essential-Tasks.md` step 1) — committed twice, copied
  the printed short hash into checkout, hit the error. Not caught by any unit test because all
  existing checkout tests pass full hashes read from `.av/refs/heads/main`.
- **Impact:** every user-facing flow that involves checking out a specific commit from av's own
  console output (the most common copy-paste source) was broken on first use; users would have
  to inspect `.av/refs/heads/` or guess that only the full hash works.
- **Fix:** shared `fsutil.find_commit_file()` — exact match first, then a unique hex-prefix
  match over `.av/commits/`; raises the new `AmbiguousCommitHash` (a ClickException subclass,
  so both one-shot and REPL flows render it as a red `Error: ...` line) when a prefix matches
  several commits, `FileNotFoundError` when nothing matches. `checkout()` resolves through it
  and rewrites its internal `commit_hash` to the resolved full hash before writing HEAD's
  detached entry; `handoff.load_commit()` uses the same helper so `--since` accepts prefixes
  too. Minimum prefix length is 4 characters, mirroring git's own abbreviation floor.
- **Verified:** real scratch-repo run — `av checkout a54a0b2` checks out the right commit,
  restores correct file content, writes the full hash into detached HEAD; ambiguous prefix
  rejected with a clear message; `av handoff --update --diff-weights --since d91bad3` resolves.
  New tests: CLI-level short-hash checkout + ambiguous rejection (`tests/test_cli.py`),
  resolver unit cases + `load_commit` prefix acceptance (`tests/test_vault.py`). Full suite:
  see Phase 35 in `CHANGELOG.md`.


## ✅ Fixed — Packaging & release-hygiene round (2026-08-21)

### [4] sdist shipped a 64.5 MB Docker-image tar — 64.7 MB source release
- **Files:** `aether-vault-server.tar` (untracked from git), new `MANIFEST.in`, `.gitignore`.
- **Problem:** `aether-vault-server.tar` (a 64.5 MB `docker save` export) was git-tracked.
  setuptools-scm seeds the sdist file list from all git-tracked files, so every source release
  embedded the entire server image: the published `0.1.0`/`0.1.1` sdists were **64.7 MB** for a
  package whose wheels are ~200–430 KB. It also made every `git clone` ~65 MB heavier and was
  silently exempt from `.gitignore` because it had been committed before being listed there
  (actually: it wasn't ignored at all until now).
- **How found:** audited the real PyPI JSON metadata (`pypi.org/pypi/aether-vault/json`) during
  the business-readiness review — sdist size 64,707,757 bytes vs. wheel sizes two orders of
  magnitude smaller; then built a local sdist and found the tar sitting in its file list.
- **Impact:** source installs took ~85x longer to download than necessary; PyPI has a 100 MB
  per-file limit that a slightly larger image export would have blown through, breaking the
  release pipeline mid-publish.
- **Fix:** `git rm --cached aether-vault-server.tar` (local copy kept on disk), added it to
  `.gitignore`, and added `MANIFEST.in` with `exclude aether-vault-server.tar` +
  pyc/pycache hygiene excludes as defense-in-depth for any future tracked artifact.
- **Verified:** rebuilt the sdist after the change — 761 KB total, no `.tar` member inside,
  LICENSE/MANIFEST.in present, `twine check` PASSED.

### [3] Published PyPI pages were empty — no summary, description, license, or URLs
- **Files:** `pyproject.toml` (`[project]`, `[project.urls]`).
- **Problem:** `[project]` carried only name/dynamic-version/dependencies. The published
  `0.1.0`/`0.1.1` releases therefore rendered barebones PyPI pages: `summary: null`, empty long
  description, zero classifiers, no repository link, no license — for anyone landing on PyPI,
  the project looked abandoned or automated-spam.
- **Fix:** full PEP 621 metadata: one-line description, `readme = "README.md"` (full README now
  renders as the PyPI page body), PolyForm Noncommercial license text, author
  ("Leon Schwarzkopf (Aether Quant)"), 7 keywords, 15 classifiers (Beta, audiences, OSes,
  Python 3.10–3.12, C++, version-control/AI topics), and Homepage/Repository/Issues/Changelog
  URLs.
- **Verified:** `twine check dist/*.tar.gz` PASSED; PKG-INFO inspected directly — Summary,
  License, all classifiers, all Project-URLs, Keywords, and the README long-description are
  present in the built distribution. The next tag push publishes this metadata; existing
  0.1.x pages update only when a yank/new upload happens.

### [2] No LICENSE file anywhere in the repo or the published packages
- **Files:** `LICENSE` (new), `README.md` (new License section).
- **Problem:** neither the repo nor either PyPI release carried a license — default copyright
  law applies, meaning technically nobody (including PyPI redistributors) was licensed to use
  or redistribute the software at all. Also invisible on the PyPI page (`license: null`).
- **Fix:** adopted the PolyForm Noncommercial License 1.0.0 (same license as the author's other
  projects) with Required Notice `Copyright Leon Schwarzkopf (Aether Quant)`. Noncommercial use
  (personal, research, education, nonprofits, government) is free; commercial use requires a
  separate license — aligning the free tier with the planned open-core/commercial-split model.
  setuptools auto-includes LICENSE in distributions by filename convention (confirmed present
  in the rebuilt sdist). README gained a short License section linking to it.


## ✅ Fixed — CI-caught test defects (2026-08-22)

### [4] `tests/test_merge.py` failed collection on Python ≤3.12 — annotation referenced an import defined 9 lines later
- **Files:** `tests/test_merge.py`.
- **Problem:** `def _commit_file(repo: Path, ...)` (line 184) used `Path` in a parameter
  annotation, but `from pathlib import Path` sat at line 193, *below* it. On Python ≤3.12
  function annotations evaluate **eagerly at def time** → `NameError: name 'Path' is not
  defined`, aborting the ENTIRE suite at collection (`1 error during collection`). The
  `test` job (windows, py3.10) died this way.
- **Why invisible locally:** the dev machine runs Python 3.14, where PEP 649 defers
  annotation evaluation — the same file collected fine. A version-dependent failure mode,
  not a logic bug.
- **Fix:** moved `from pathlib import Path` into the top import block; deleted the
  mid-file import.
- **Systemic fix:** new `scripts/check_eager_annotations.py` — AST scan that flags any
  module-level annotation referencing a name whose import/definition appears later in the
  file (builtins exempted, `from __future__ import annotations` files skipped). Proven both
  ways: 0 problems on the fixed tree, exit 1 with exact lines on the stashed pre-fix
  version. Run before pushing when editing tests from a ≥3.13 machine.

### [3] Live E2E crashed after succeeding — `json` used without import in `tests/test_server.py`
- **Files:** `tests/test_server.py`.
- **Problem:** `test_live_two_repo_clone_pull_flow` called `json.loads(...)` but the module
  never imported `json`. The test got all the way through init/push/clone against the real
  Docker stack and THEN crashed — so the collaboration flow itself worked; only the
  assertions were unreachable. (The adjacent `os.urandom` call was fine: `os` was already
  imported.)
- **Fix:** added `import json` to the module's import block. 47/48 other server tests
  passed on CI, confirming the Phase 39–42 server changes work live.

### [2] `dashboard.spec.ts` asserted a hero heading that no longer exists in the UI
- **Files:** `webui/e2e/dashboard.spec.ts`.
- **Problem:** the spec's boot assertion waited for `getByRole("heading", { name:
  "🌌 Aether-Vault" })` — no element with that role/name exists anywhere in the current UI
  (the brand is a sidebar `<Image>` logo + "ML Registry Dashboard" text). Every previous
  E2E failure had died at this exact line and was misread as "empty seeded data"; once the
  `AV_DATA_DIR` fix let seeding succeed, weight-diff PASSED while dashboard still timed out
  here — proving the selector, not the data, was wrong.
- **Fix:** replaced the stale assertion with two that reflect the real DOM and keep the
  intent (app shell mounted): sidebar brand text "ML Registry Dashboard" + the
  `#nav-dashboard` nav item. Both verified present in `Sidebar.tsx`; spec compiles via
  `tsc --noEmit`.

**How found:** GitHub Actions runs of the v1.1.1 cycle push — logs read directly via
`gh run view --log-failed` rather than reproduced blind. All three are test-infrastructure
defects; zero product-code changes were required.
