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

## 🔸 Open — Deployment security (deliberately documented only)

> Left unchanged in code/config by request. Should be fixed urgently before production use.

### [7] No authentication + open attack surface
- **Files:** `python/av_server/server.py`, `docker-compose.yml`.
- **Points:**
  - No auth/authz whatsoever on any endpoint.
  - CORS `allow_origins=["*"]` (`server.py`, `add_middleware`).
  - The **destructive** `POST /api/admin/gc` is unauthenticated — any reachable client can wipe
    storage.
  - Postgres port `5432` is mapped externally in Compose; default credentials
    `av_user/av_password` are hardcoded.
  - Redis port `6379` is also open, with no password.
- **Recommendation:** API token/reverse proxy with auth, restrict CORS to known origins, secure
  the admin/GC endpoint, don't bind DB/Redis ports externally, source secrets from an
  environment/secret store instead of defaults.

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

### 🔸 Open — checkpoint list resolves N commits via N parallel requests
- **File:** `webui/src/components/WeightDiffPanel.tsx`.
- **Problem:** `GET /api/commits` (the list endpoint) returns **no** tree/layer data (metadata
  only); to populate the checkpoint list with `rel_path`/layer info, `GET /api/commits/{hash}`
  has to be called individually for each candidate commit. To avoid exactly repeating the N+1
  request pattern that was already fixed once in this project for `fetchCommitsForBranches`,
  these requests run **in parallel** (`Promise.all`, not serially) and are hard-capped at
  `CHECKPOINT_FETCH_LIMIT = 30`.
- **Why not fixed:** A real solution would require a new server endpoint (e.g.
  `GET /api/commits?include_layers=true`), which the scope decision for this feature explicitly
  meant to avoid (no server/API changes). Unproblematic for repos with only a few dozen commits;
  a very long history with many checkpoints would need an aggregate endpoint.

### 🔸 Open — `Index.save()` is not atomic
- **File:** `python/av_cli/index.py` (`Index.save`).
- **Problem:** Writes `.av/index` directly (`open(..., 'w')`), without the temp-file +
  `fsync` + `os.replace` pattern (`atomic_write_text`/`atomic_write_json`) already established
  in `main.py`. A crash mid-write can leave a truncated/empty index file.
- **Why not fixed:** Out of scope for this feature; consistent with the category of non-atomic
  writes already listed elsewhere in this document under "Minor Items" — noted for a later fix.

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
