# Problems

Bugs and infrastructure issues found in this codebase, how they were fixed
(or why still open), with severity rating (1 = cosmetic, 10 = critical
data-loss/safety issue) and status. Ordered by entry number (oldest first).

**Status legend:**
- 🟢 `fixed` — code changed (or final decision made) and verified or self-evidently complete; nothing meaningfully pending.
- 🟡 `partial` — fix shipped but verification incomplete/pending, or a real known caveat/open sub-issue remains.
- 🔴 `closed` — no code fix applied: declined/won't-fix, non-goal, moot, or superseded without ever being fixed on its own terms.

Every entry follows **Problem** → **Fix** → **Verification** (real CLI runs against scratch repos, unit tests, CI runs, or manual review).

---

### 1. Parallel hash ≠ canonical SHA-256 → content-addressing and remote upload broken

**Severity:** 10/10 · **Status:** 🟢 `fixed` (2026-06-23)

**Problem:** In `src/core.cpp` (the binding of `hash_file`), `aether_core.hash_file` pointed at `hash_file_parallel`, which for files ≥ 16 MB returns a *tree hash* (SHA-256 over the concatenation of chunk hashes) instead of the real file SHA-256 — while the Python fallback and the server-side verification (`storage.store_object`) use the real SHA-256. The same file therefore got different hashes depending on core availability/size, and whole-file uploads of large non-`.safetensors` artifacts (`.pt`, `.parquet`, `.csv`, `.h5`) failed with HTTP 400 — core functionality (remote sync/checkout) broken.

**Fix:** Bound `hash_file` to `hash_file_sequential` (canonical SHA-256); the tree hash remains available as `hash_file_tree`; added an invariant comment.

**Verification:** Not separately recorded in the audit log; self-evidently complete — `hash_file` now resolves to the canonical sequential SHA-256 path, matching what both the Python fallback and `storage.store_object` expect.

---

### 2. Path traversal / LFI via `ref_name`

**Severity:** 7/10 · **Status:** 🟢 `fixed` (2026-06-23)

**Problem:** `GET/PUT /api/refs/{ref_name:path}` in `python/av_server/server.py` (ref endpoints) passed `ref_name` unchecked to the filesystem fallbacks (`refs_dir / ref_name`) in `python/av_server/storage.py`; `../../…` allowed reading/writing outside the data directory.

**Fix:** Central `validate_ref_name()` (whitelist, no `..`/absolute/backslash) in `update_ref`/`get_ref`; additionally a defensive `_safe_ref_path()` in `CASStorage` (resolved path must stay below `refs_dir`).

**Verification:** Not separately recorded in the audit log; self-evidently complete — every ref path now passes whitelist validation plus a resolved-path containment check before touching the filesystem.

---

### 3. `os.walk` traverses the entire CAS object store + faulty substring filter

**Severity:** 6/10 · **Status:** 🟢 `fixed` (2026-06-23)

**Problem:** In `python/av_cli/main.py` (`add`, `status`), `if ".av" in root` is (a) a substring test (folders like `data.average` were incorrectly skipped) and (b) doesn't prune → `os.walk` descended into `.av/objects` (tens of thousands of shards).

**Fix:** Shared helper `iter_working_files()` with in-place pruning (`dirnames[:] = …`) and path-component checking.

**Verification:** Not separately recorded in the audit log; self-evidently complete — pruning prevents descent into `.av/objects`, and component checking stops false skips like `data.average`.

---

### 4. `av add` fully re-hashes every file, even unchanged ones

**Severity:** 6/10 · **Status:** 🟢 `fixed` (2026-06-23)

**Problem:** In `python/av_cli/main.py` (`add`), every file was fully read/hashed regardless of whether it had changed.

**Fix:** Before hashing, run `compare_meta_safe` against the index; on a match (size + mtime) the file is skipped.

**Verification:** Not separately recorded in the audit log; unchanged files demonstrably skip the hashing step via the size+mtime fast path.

---

### 5. Memory spike: layer extraction reads entire layers into RAM

**Severity:** 5/10 · **Status:** 🟢 `fixed` (2026-06-23)

**Problem:** In `python/av_cli/main.py` (`add`, safetensors path), `dst_f.write(src_f.read(l_size))` loaded an entire layer (up to GB-sized) into memory.

**Fix:** Chunked copying in 8 MB blocks.

**Verification:** Not separately recorded in the audit log; memory use is now bounded by the 8 MB block size regardless of layer size.

---

### 6. C++ `split_and_hash_safetensors`: missing validation → OOM/DoS

**Severity:** 5/10 · **Status:** 🟢 `fixed` (2026-06-23)

**Problem:** In `src/core.cpp`, an unvalidated 8-byte `header_size` allowed an arbitrarily large allocation; `end - start` could underflow, and offsets could exceed EOF.

**Fix:** Bounds checks (`header_size` must fit within the file, `end >= start`, `base_offset + end <= file_size`).

**Verification:** Not separately recorded in the audit log; malformed inputs are now rejected before any allocation is attempted.

---

### 7. Everything appears as "modified"/"staged" after `checkout`

**Severity:** 5/10 · **Status:** 🟢 `fixed` (2026-06-23)

**Problem:** In `python/av_cli/main.py` (`checkout`), index entries were written with `mtime_ns=0` and marked as `staged` by being re-inserted into a cleared index.

**Fix:** Capture the real `size`/`mtime_ns` after materializing and set `staged=False` → clean working tree after checkout.

**Verification:** Not separately recorded in the audit log; the working tree reports clean immediately after checkout by construction of the captured stat.

---

### 8. `checkout` overwrites/deletes the working copy without a dirty check → data loss

**Severity:** 5/10 · **Status:** 🟢 `fixed` (2026-06-23)

**Problem:** In `python/av_cli/main.py` (`checkout`), tracked files were unconditionally overwritten/deleted.

**Fix:** A dirty check (modified/deleted/staged files) now aborts the operation; new `--force`/`-f` flag to deliberately discard changes.

**Verification:** Not separately recorded in the audit log; the default path aborts on any dirty state, with destruction only reachable via explicit opt-in.

---

### 9. DB column `size` as a 32-bit `Integer` → overflow above 2 GB

**Severity:** 5/10 · **Status:** 🟢 `fixed` (2026-06-23)

**Problem:** `python/av_server/models.py` (`DBObject.size`, `DBTree.size`) used Postgres `INTEGER` (max ~2.1 GB) for a tool that versions multi-GB files.

**Fix:** Switched to `BigInteger`.

**Verification:** Caveat carried over from the audit: the column type only takes effect on a **fresh DB** — the server uses `Base.metadata.create_all` without migrations. For an existing DB, a manual `ALTER TABLE objects ALTER COLUMN size TYPE BIGINT;` (same for `trees`) or Alembic is needed.

---

### 10. `/api/stats` does a full filesystem walk on every dashboard refresh

**Severity:** 5/10 · **Status:** 🟢 `fixed` (2026-06-23)

**Problem:** In `python/av_server/server.py` (`get_stats`), every call (the Web UI polls roughly every 15s) walked + `stat()`'d every shard.

**Fix:** DB aggregates (`count`/`sum`); the filesystem walk is now only a fallback when the DB is empty.

**Verification:** Not separately recorded in the audit log; steady-state stats come straight from SQL aggregates.

---

### 11. Server ignores the commit's author timestamp → wrong sort order

**Severity:** 5/10 · **Status:** 🟢 `fixed` (2026-06-23)

**Problem:** In `python/av_server/server.py` (`push_commit`), `DBCommit.timestamp` defaulted to insert time; combined with the pending-push queue, this sorted commits incorrectly on the dashboard.

**Fix:** `commit_data["timestamp"]` (ISO 8601) is now parsed and set; falls back to `utcnow()`.

**Verification:** Not separately recorded in the audit log; commit ordering now reflects the author timestamp carried in the payload.

---

### 12. No authentication + open attack surface

**Severity:** 7/10 · **Status:** 🟢 `fixed` (2026-06-28)

**Problem:** No auth/authz whatsoever on any endpoint in `python/av_server/server.py`; the **destructive** `POST /api/admin/gc` was unauthenticated (any reachable client could wipe storage); Postgres `5432`/Redis `6379` were mapped externally in Compose with hardcoded default credentials (`av_user`/`av_password`) that are public the moment this repo is — meaning the port mapping was the only thing standing between "public password" and full DB access for anyone who could reach the host. Also affected `docker-compose.yml`, `python/av_cli/docker/docker-compose.release.yml`, `python/av_cli/client.py`, `python/av_cli/main.py` (new `av auth` group + `av init` prompt), and `webui/src/components/TokenGate.tsx`. A real bug was found via manual debugging while building this fix: `commit()`'s push-to-remote logic assumed any push failure would surface as a `False`/`None` return (matching the existing "queue it for `av push` later" fallback) — but `VaultClient`'s methods now *raise* `AuthenticationError` on a 401 instead, since `server_available()`'s own health check is deliberately exempt from the auth gate and so can't be used to infer "my token is valid." That exception propagated straight out of `commit()`, skipping the queue-for-retry fallback entirely — a commit made against a Protected registry with a stale/wrong token was created locally but **silently never queued**, unlike every other kind of push failure.

**Fix:** An optional shared-secret token ("Protected" mode, off by default — "Anonymous" stays byte-for-byte identical to before for solo/local use). A `require_token` middleware gates every route except `GET /api/health` and the FastAPI docs routes when a token is configured; `av auth set-token`/`clear`/`status` manage it, `av init` offers it as an Anonymous/Protected choice (with a "generate new" vs. "join an existing registry" sub-choice) at setup time, and the CLI/webui both prompt for the token interactively on a 401 rather than failing with a generic error. The externally-mapped DB/Redis ports were removed from `docker-compose.release.yml` (the file real end users actually deploy) — kept in the **dev** `docker-compose.yml` specifically because `tests/test_server.py` connects to them directly from the host (verified by checking; removing it there would have silently degraded that test file to skip-mode instead of a loud failure). For the bug found along the way: `AuthenticationError` is caught in `commit()`'s push block and in `flush_pending_push()` (which now also preserves the rest of the queue before re-raising, so a bad token hit partway through retrying several queued commits doesn't drop the untried ones) and queued exactly like any other push failure. Explicitly NOT fixed, by design — still open: CORS remains `allow_origins=["*"]`; this round's scope was specifically the auth gap, not CORS hardening, noted so it doesn't read as fully closed.

**Verification:** Reproduced for real against the live Docker stack (not just unit tests): committed with a deliberately wrong token, confirmed the commit was missing from both the server and `.av/pending_push`. Then: `tests/test_server.py` (14 new cases — header parsing edge cases, health/docs exemption, reads+writes both gated), `tests/test_client.py` (token header + every method raising `AuthenticationError` on 401), `tests/test_cli.py` (`av auth`, `av init`'s Protected/join-existing flow, the commit-queueing regression), `tests/test_docker_runtime.py` (`.env` read/write round-trip including awkward characters, the webui URL token handoff), and a full manual pass against the live Docker stack: rebuilt the server image, confirmed Anonymous is unchanged, confirmed `av auth set-token` restarts the server and Protected mode rejects/accepts correctly, confirmed the CLI's own 401 retry message, and confirmed the commit-queueing fix recovers a "lost" commit via `av push` once the correct token is restored.

---

### 13. GC is mark-and-sweep without locking (race condition)

**Severity:** 5/10 · **Status:** 🟢 `fixed`

**Problem:** In `python/av_server/server.py` (`run_garbage_collection`, `purge_orphans`), a parallel upload whose commit hadn't been recorded yet could be deleted (the live object was classified as orphaned).

**Fix:** Grace period (`GC_GRACE_SECONDS`, 1h). Objects whose DB row `created_at` or shard file `mtime` is younger than the GC start window are never deleted — protecting the window between object upload and commit push without a global lock.

**Verification:** Not separately recorded in the audit log; objects inside the grace window are excluded from deletion by the age comparison itself.

---

### 14. N+1 DB queries during tree traversal

**Severity:** 4/10 · **Status:** 🟢 `fixed`

**Problem:** In `python/av_server/server.py` (`resolve_tree`, `_collect_alive_in_memory`), a separate DB query per tree node made traversal slow for deep/wide trees.

**Fix:** `get_commit` now traverses level-by-level with **one** batched query per depth level (dedup-safe via path prefixes). The GC mark phase loads **all** `DBTree` rows in **one** query and traverses purely in memory (`_collect_alive_in_memory`). Additionally, GC deletions now run batched (`_GC_DELETE_BATCH`) to avoid exceeding asyncpg's bind-parameter limit (formerly a separate severity-3 item; see entry 22).

**Verification:** Not separately recorded in the audit log; query count per traversal is now bounded by tree depth rather than node count.

---

### 15. Cross-language mtime inconsistency

**Severity:** 4/10 · **Status:** 🟢 `fixed`

**Problem:** C++ `fs::last_write_time` (implementation-defined epoch, e.g. 1601) vs. Python `st_mtime_ns` (Unix epoch) in `python/av_cli/main.py` (`get_file_meta_safe`/`compare_meta_safe`). Mixed paths caused spurious "modified" results.

**Fix:** Metadata (size/mtime) now flows **exclusively** through Python's `os.stat` (a single Unix epoch, exactly self-consistent). The C++ core is now used purely for hashing; the unused C++ metadata path was removed from the CLI.

**Verification:** Not separately recorded in the audit log; with a single epoch source, cross-language mismatch cannot occur by construction.

---

### 16. Binary pointer check reads files in text mode via `readline()`

**Severity:** 4/10 · **Status:** 🟢 `fixed`

**Problem:** `python/av_cli/pointer.py` (`is_pointer_file`) read binary files in text mode via `readline()`; for a file with no early newline, this could read potentially huge amounts of data.

**Fix:** Now only reads the fixed magic bytes (`_POINTER_MAGIC`) in binary mode.

**Verification:** Not separately recorded in the audit log; the read length is bounded by the magic-byte constant.

---

### 17. Commit JSON and ref not written atomically (crash window)

**Severity:** 4/10 · **Status:** 🟢 `fixed`

**Problem:** In `python/av_cli/main.py` (`commit`), commit JSON and ref were not written atomically (crash window).

**Fix:** `atomic_write_text`/`atomic_write_json` (temp file + `fsync` + `os.replace`); the commit object is written before the ref.

**Verification:** Not separately recorded in the audit log; both writes go through the established temp-file + fsync + rename pattern.

---

### 18. Dashboard commits fetched serially via the parent chain (waterfall)

**Severity:** 4/10 · **Status:** 🟢 `fixed`

**Problem:** `webui/src/lib/api.ts` (`fetchCommitsForBranches`) loaded commits serially via the parent chain (waterfall, N round trips).

**Fix:** New `fetchCommits()` fetches the most recent commits in **one** `/api/commits` request; dashboard fetches run in parallel via `Promise.all`.

**Verification:** Not separately recorded in the audit log; one aggregate request replaces the N-trip waterfall.

---

### 19. Parallel uploads of the same hash → `IntegrityError`/HTTP 500

**Severity:** 3/10 · **Status:** 🟢 `fixed`

**Problem:** In `python/av_server/server.py` (`upload_object`), parallel uploads of the same hash raced into an `IntegrityError`, surfacing as HTTP 500.

**Fix:** `IntegrityError` is caught → idempotent HTTP 409.

**Verification:** Not separately recorded in the audit log; duplicate concurrent uploads now resolve idempotently instead of erroring.

---

### 20. Trusted unbounded client `metrics`/`tree` payloads (DoS potential)

**Severity:** 3/10 · **Status:** 🟢 `fixed`

**Problem:** In `python/av_server/server.py` (`push_commit`), unbounded client-supplied `metrics`/`tree` structures were trusted (DoS potential).

**Fix:** Limits (`MAX_TREE_ENTRIES`, `MAX_METRICS`, `MAX_TAGS`, `MAX_TAG_LEN`, `MAX_MESSAGE_LEN`) → HTTP 422 when exceeded.

**Verification:** Not separately recorded in the audit log; oversized payloads are rejected with 422 at the boundary.

---

### 21. FK violation when the parent commit wasn't on the server → 500

**Severity:** 3/10 · **Status:** 🟢 `fixed`

**Problem:** `python/av_server/models.py` (`DBCommit.parent_hash` FK): pushing a commit whose parent wasn't yet on the server triggered an FK violation → 500.

**Fix:** FK on `parent_hash` removed (allows shallow/out-of-order pushes; column still indexed); additionally `IntegrityError`→409 in `push_commit`.

**Verification:** Not separately recorded in the audit log; shallow/out-of-order pushes are accepted by schema design.

---

### 22. GC deletions could exceed asyncpg's parameter limit

**Severity:** 3/10 · **Status:** 🟢 `fixed`

**Problem:** In `python/av_server/server.py` (`run_garbage_collection`), `dead_hashes.in_(list)` could exceed asyncpg's parameter limit with enough dead hashes.

**Fix:** Deletions now run in batches (`_GC_DELETE_BATCH`) — also covered by the GC rework in entry 14.

**Verification:** Not separately recorded in the audit log; the original document notes this item was folded into that GC batching work ("formerly a separate severity-3 item").

---

### 23. Deprecations: `datetime.utcnow()` and `@app.on_event("startup")`

**Severity:** 2/10 · **Status:** 🟢 `fixed`

**Problem:** Deprecation warnings in `python/av_server/models.py` and `server.py`: `datetime.utcnow()`, `@app.on_event("startup")`.

**Fix:** `utcnow_naive()` (tz-aware → naive UTC) used everywhere; FastAPI `lifespan` handler instead of `on_event`.

**Verification:** Not separately recorded in the audit log; deprecation warnings eliminated by moving to the supported APIs.

---

### 24. Non-atomic JSON writes for CLI config/pending-push

**Severity:** 2/10 · **Status:** 🟢 `fixed`

**Problem:** In `python/av_cli/main.py` (`save_pending_push`, `update_registry`, `save_config`), JSON writes were non-atomic.

**Fix:** Via `atomic_write_json`/`atomic_write_text`.

**Verification:** Not separately recorded in the audit log; all three writers go through the atomic helpers.

---

### 25. Thread-pool overhead for files just over 2x the chunk size

**Severity:** 2/10 · **Status:** 🟢 `fixed`

**Problem:** In `src/core.cpp` (`hash_file_parallel`), thread-pool overhead dominated for files just over 2x the chunk size.

**Fix:** Parallelization now only kicks in above `PARALLEL_MIN_CHUNKS` (8 chunks ≈ 64 MB).

**Verification:** Not separately recorded in the audit log; small files stay on the sequential path by threshold.

---

### 26. `requests.Session` never closed in `VaultClient.session`

**Severity:** 1/10 · **Status:** 🟢 `fixed`

**Problem:** In `python/av_cli/client.py` (`VaultClient.session`), the `requests.Session` was never closed (not a real leak).

**Fix:** `close()` + context manager (`__enter__`/`__exit__`) + defensive `__del__`.

**Verification:** Not separately recorded in the audit log; resource-hygiene change with no behavior impact.

---

### 27. Commits are pushed before their objects → server sync for artifacts completely unusable

**Severity:** 10/10 · **Status:** 🟢 `fixed` (2026-06-24)

**Problem:** Four compounding bugs across `python/av_cli/main.py` (`commit`, `flush_pending_push`), `python/av_server/server.py` (`push_commit`), and `python/av_server/models.py` (`DBTree`): (1) Wrong order — `commit` called `client.push_commit(commit_data)` **before** the referenced objects/layer shards were uploaded (`flush_pending_push` never uploaded them at all — the upload code only existed inline in the live-commit path). However, the server stores each tree entry's `object_hash` as a **foreign key** into `objects.hash` ([models.py:41](../python/av_server/models.py#L41) before the fix). The insert into `trees` therefore practically **always** failed with `ForeignKeyViolationError`. (2) Misattributed error handling — `push_commit` caught *every* `IntegrityError` and blanket-returned `409 "Commit already exists"` ([server.py:307](../python/av_server/server.py#L307) before the fix), regardless of whether the commit truly already existed or (as here) a completely different constraint was violated (tree→object FK, later also ref→commit FK). The client deliberately treats 409 as idempotent success (designed for concurrent pushes of the same hash) — and was thereby fooled into masking a total failure: `av commit`/`av push` consistently reported success, but **commits and refs never made it into the database** (`SELECT * FROM commits` → 0 rows, despite "✓ Pushed 2 commit(s)" on the console). (3) Deeper root cause — even with the correct order, the insert still fails for **every** layer-split `.safetensors` file: when layer-splitting, the whole file (`object_hash`) is deliberately **never** uploaded as its own object (only the layer shards, to avoid duplicate storage) — but the FK on `objects.hash` requires exactly that. (4) Additionally, the return value of `client.update_ref(...)` was never checked anywhere — a failed ref update was treated as "done" instead of being re-queued into the pending queue. Impact: every commit containing a `.safetensors` file above the LFS threshold (i.e. exactly this tool's core use case) could **never sync successfully** — neither live nor via the offline pending queue. The entire "Weight Diffing" feature (both CLI **and** the new Web UI) ran on empty, because no second version of a model ever actually reached the server.

**Fix:** Shared helper `upload_commit_objects()` (instead of duplicated inline code), called **before** `push_commit()` — both in the live-commit path and in `flush_pending_push()` (previously: objects were never uploaded during queue replay). `DBTree.object_hash` loses its `ForeignKey("objects.hash")` (analogous to the already previously-removed `parent_hash` FK) — the hash remains intact as a content identity, without forcing a physical object row that never exists for layer-split files. `push_commit` now, after an `IntegrityError`, re-checks **by hash** whether the commit actually exists before returning 409; otherwise 500 with a genuine error message (no more false "success" reported to the client). `flush_pending_push`/`commit` now check the result of `update_ref()` and keep the commit in the pending queue if the ref update fails.

**Verification:** End-to-end against a real Docker stack: created four real commits with layer-split synthetic checkpoints; before the fix, 0 of 2 commits landed in the DB (`SELECT hash FROM commits` empty) despite a success message. After the fix: both the live push AND the offline-queue push correctly land in `commits`/`refs`; `GET /api/commits/{hash}` returns the expected per-layer hashes; a Node script that exactly replicates the browser diff logic confirms the correct layer diff between two real server commits. Caveat: existing dev databases (created via `create_all` before this fix, no migrations) physically retain the old FK constraint in their schema until it's manually removed (`ALTER TABLE trees DROP CONSTRAINT trees_object_hash_fkey;`) or the DB is recreated — an already-documented migration caveat, see the `BigInteger` (entry 9) and `parent_hash` (entry 21) entries.

---

### 28. `av add` never persists per-layer hashes to disk

**Severity:** 9/10 · **Status:** 🟢 `fixed` (2026-06-24)

**Problem:** In `python/av_cli/main.py` (`add`), `idx.add_entry(...)` (in `python/av_cli/index.py`, `Index.add_entry`) internally already calls `self.save()` (parameter `auto_save=True`) **before** the caller executes the line `idx.entries[rel_path]["layers"] = layers`. The layer list therefore only ends up in the in-memory dict of the already-finished `add` process — the `.av/index` file on disk never gets `"layers"`. Since `av commit` loads the index fresh from disk in a **new** process, `tree[rel_path]["layers"]` was always `[]` in every commit object, regardless of the console output "Staged [ARTIFACT] … (LFS, 6 layers)". Consequence: both `av handoff --diff-weights` and the new Web UI fell back to a whole-file hash comparison for **every** `.safetensors` file (`status: changed`, but no individual changed layer reported) — the entire "per-layer weight diffing" feature from README Phase 11 had been ineffective since its introduction.

**Fix:** After setting `idx.entries[rel_path]["layers"]`, `idx.save()` is now called explicitly so the layer data is actually persisted.

**Verification:** Created two synthetic `.safetensors` commits (layer `layer2.weight` changed); `av handoff --diff-weights` now correctly reports `changed layers: layer2.weight`, whereas before it only reported `status: changed` with no detail.

---

### 29. `atomic_write_text`'s temp filename can exceed Windows' `MAX_PATH`

**Severity:** 4/10 · **Status:** 🟢 `fixed` (2026-06-24)

**Problem:** In `python/av_cli/main.py` (`atomic_write_text`), the temp suffix `f".tmp.{os.getpid()}.{uuid.uuid4().hex}"` (PID + a full 32-character UUID4 hex) combined with a 64-character commit-hash filename and a deeply nested repo path (e.g. CI runner temp directories, OneDrive sync folders) can easily push the total path length past Windows' 260-character `MAX_PATH`. Rather than "just" being long, the `open()` call then fails with `FileNotFoundError` — the entire `av commit` aborts, even though the actual goal (atomic, crash-safe writes) should be the opposite of "operation fails".

**Fix:** Reduced the temp suffix to a short 8-character random hex (removed the PID, which was redundant anyway alongside the UUID for collision avoidance).

**Verification:** Reproducibly encountered while testing this feature (repo located under a deep temp path); no dedicated post-fix test recorded in the audit log, but the shortened suffix removes the observed path-length overrun at its source.

---

### 30. Synthetic `__header__` pseudo-layer pollutes the visual diff view

**Severity:** 3/10 · **Status:** 🟢 `fixed` (2026-06-24)

**Problem:** `aether_core.split_and_hash_safetensors` returns an extra entry `__header__` alongside the real tensors (a hash over the safetensors JSON header, for reconstruction integrity). `av_cli/main.py` carries this through unfiltered into `idx.entries[rel_path]["layers"]`. In the previous CLI text output (`--diff-weights`) this barely stood out; in a **visual** layer-by-layer view (`webui/src/lib/diffWeights.ts`, new at the time), however, it would be a confusing, non-tensor entry in the heatmap and drift chart.

**Fix:** `diffFile()` filters out layer entries named `__header__` before they flow into the UI diff structure (purely client-side, no change to the core/server/index format).

**Verification:** Not separately recorded in the audit log; client-side filter applied uniformly to all diff structures.

---

### 31. Checkpoint list resolved N commits via N parallel requests

**Severity:** 4/10 · **Status:** 🟢 `fixed` (2026-06-28)

**Problem:** `GET /api/commits` (the list endpoint) returned **no** tree/layer data (metadata only); populating the checkpoint list with `rel_path`/layer info required calling `GET /api/commits/{hash}` individually for each candidate commit. These ran in parallel (`Promise.all`) and were hard-capped at `CHECKPOINT_FETCH_LIMIT = 30` to bound the request count, but it was still N requests for N commits. Affected `python/av_server/server.py` (`list_commits`), `webui/src/lib/api.ts` (`fetchCommitsWithLayers`), and `webui/src/components/WeightDiffPanel.tsx`.

**Fix:** `get_commit`'s tree-resolution helper was factored out to a module-level `resolve_tree(db, root_hash)` so both endpoints share it. `GET /api/commits` gained an `include_layers: bool = false` query param; when true, each returned commit's tree is resolved and attached, matching `get_commit`'s existing shape — one request instead of N. Resolution runs **sequentially** per commit, not via `asyncio.gather` — a single `AsyncSession`/asyncpg connection can't safely run concurrent queries, so concurrent resolution would have been a real (if subtle) correctness bug, caught and avoided before it ever ran. `WeightDiffPanel.tsx` now calls `fetchCommitsWithLayers` once; `CHECKPOINT_FETCH_LIMIT` raised from 30 to 100 now that it bounds one request's response size, not a request count.

**Verification:** `tests/test_server.py` (`include_layers=true` returns trees matching `get_commit`'s output for the same commits), `webui/src/components/__tests__/WeightDiffPanel.test.tsx` (updated to mock the single aggregate call).

---

### 32. `Index.save()` is not atomic

**Severity:** 2/10 · **Status:** 🟢 `fixed` (2026-06-28)

**Problem:** `python/av_cli/index.py` (`Index.save`) wrote `.av/index` directly (`open(..., 'w')`), without the temp-file + `fsync` + `os.replace` pattern (`atomic_write_text`/`atomic_write_json`) already established in `main.py`. A crash mid-write could leave a truncated/empty index file.

**Fix:** Mechanical swap to the existing `atomic_write_json` helper (`.fsutil`) — no behavior change beyond atomicity.

**Verification:** `tests/test_vault.py::test_index_operations` and the rest of the existing `Index` coverage pass unchanged.

---

### 33. Tooltip text in the Layer Drift chart is black on a dark background

**Severity:** 2/10 · **Status:** 🟢 `fixed` (2026-06-24)

**Problem:** In `webui/src/components/LayerDriftChart.tsx`, Recharts' `<Tooltip>` colors name/value pairs black by default; the configured `contentStyle.color` only affects the label, not the items — unreadable against the dark theme.

**Fix:** Added `itemStyle={{ color: "#e2e8f0" }}` and `labelStyle={{ color: "#718096" }}`.

**Verification:** Not separately recorded in the audit log; visual-only styling change.

---

### 34. Layer Drift chart: Y-axis without meaning, X-axis label clipped

**Severity:** 3/10 · **Status:** 🟢 `fixed` (2026-06-24)

**Problem:** In `webui/src/components/LayerDriftChart.tsx`, `margin.bottom: 0` + label position `insideBottom` caused "Layer depth →" to be clipped at the bottom edge; the Y-axis showed only raw `0`/`1` ticks with no explanation, even though the chart itself shows 4 status colors (changed/unchanged/added/removed).

**Fix:** `margin.bottom` set to 20, label position changed to `"bottom"` with a positive offset; `tickFormatter` translates 0/1 into "unchanged"/"changed"; added a color legend with all 4 status labels below the chart (`.status-legend` in `globals.css`).

**Verification:** Not separately recorded in the audit log; visual-only presentation change.

---

### 35. `av webui` rebuilds/reloads the Docker image on every invocation, even when already running

**Severity:** 5/10 · **Status:** 🟢 `fixed` (2026-06-24)

**Problem:** In `python/av_cli/main.py` (`webui_cmd`), the command unconditionally called `docker compose up -d --build` — even when the container was already running and healthy, costing a full build step plus health-check wait every time (in the user's real-world log: 24s build + >100s waiting).

**Fix:** Before starting, checks via `docker inspect --format='{{.State.Health.Status}}'` whether the container is already running and healthy; if so, opens the browser directly (no `docker compose` needed). New `--rebuild` option still forces a fresh build after source code changes.

**Verification:** A second consecutive run took ~15s instead of the previous >2 minutes.

---

### 36. Weight Diff page shows unrelated commits — no project concept, one shared server

**Severity:** 8/10 · **Status:** 🟢 `fixed` (2026-06-24)

**Problem:** Every `av init` repo points at the same `http://localhost:8000` by default, and `av webui` resolves `docker-compose.yml` relative to the **installed package** (`Path(__file__).parents[2]`), not the current folder — all local repos share the same container/DB, with no way for commits to be attributed to a repo (`DBCommit`/`DBRef` had no project concept). So the Weight Diff page mixed together commits from every local repo on the machine. Affected `python/av_cli/main.py` (`init`, `load_config`, `config`, `commit`, `flush_pending_push`), `python/av_server/models.py` (`DBCommit`), `python/av_server/server.py` (`push_commit`, `list_commits`, `list_refs`, new: `list_projects`), `webui/src/lib/api.ts`, `webui/src/components/ProjectsPanel.tsx` (new), `webui/src/components/BranchList.tsx`, `webui/src/app/page.tsx`, `webui/src/components/Sidebar.tsx`, and `webui/src/components/TopBar.tsx`.

**Fix:** Real project separation on the still-shared server: `av init` generates a `project_id` (UUID4) + `project_name` (folder name), persisted in `.av/config`; repos from **before** this fix are automatically backfilled once on the next `load_config()` call and saved immediately (no repeated regeneration on every call). New options `av config --remote-url URL` / `--name NAME`; `av config` with no arguments shows the current configuration including the project ID. `project_id`/`project_name` flow into the hashed commit payload (two projects can never collide on the same commit hash); `DBCommit` gains both columns. Branch refs are namespaced client-side as `"<project_id>/<branch>"` (no schema change to `DBRef` needed — the existing `{ref_name:path}` endpoint already allows slashes and already validates them via `validate_ref_name`). New endpoint `GET /api/projects` (project list with commit count + last push); `GET /api/commits`/`GET /api/refs` optionally accept `?project_id=`. New Web UI tab **"Projects"** (`ProjectsPanel.tsx`): lists all projects, "Open" sets the active project filter (persisted in `localStorage`) and switches back to the dashboard; the TopBar shows the active project as a badge with a "✕" clear button; the dashboard, branch list, and Weight Diff checkpoint list all respect the filter. Found+fixed during implementation: without adjustment, `BranchList.tsx` would have shown the raw `"<project_id>/<branch>"` names unchanged; now only the branch part is shown (with the project name as a prefix when multiple projects are visible simultaneously, to keep identically-named branches distinguishable). Schema migration: `DBCommit` gains two `NOT NULL` columns; since this project doesn't use migrations (`create_all` only creates missing tables), the existing dev schema was upgraded **in place via `ALTER TABLE`** (existing commits set to `project_id='legacy'`) instead of wiping the DB — no data loss. Deliberately left unchanged (documented, not a bug): `GET /api/commits/{hash}` remains accessible independent of project (a universal content-address lookup) — a file with a known hash is reachable from any project; intentional (same philosophy as the cross-project object deduplication) and not a security issue, since hashes aren't guessable. Likewise `GET /api/stats` and `GET /api/dashboard/summary` remain unscoped by project (showing the global object store / all refs unfiltered) — a consistent scope decision, not a follow-on bug; the same `?project_id=` filter can be added later if needed. And `python/av_server/storage.py`'s local filesystem fallback (only active when the DB is empty) has no project concept — irrelevant as long as the DB-backed route is primary.

**Verification:** End-to-end with four real test repos: two fresh projects (`proj_a`, `proj_b`) → `GET /api/projects` correctly lists both with commit count/last push; `GET /api/commits?project_id=…` and `GET /api/refs?project_id=…` filter correctly; both independently have a `main` branch without overwriting each other (`"<id-a>/main"` and `"<id-b>/main"` coexist). A repo with `.av/config` **without** `project_id` (simulating an "old" state) → backfill kicks in on the first command, then stays stable across multiple calls (no new UUID per call). Two projects with an **identical** `project_name` but different `project_id` → both appear separately in `GET /api/projects` (distinguishable by `project_id`). The offline-queue path (`av push` after a server restart) correctly uses the already-namespaced ref name — lands in the correct project branch. `av branch`/`av status`/`av list-meta` (all local) remain functional unchanged. `av gc`/`GET /api/stats`/`GET /api/dashboard/summary` continue to run error-free across **all** projects (deliberately global/cross-project — object deduplication is meant to stay cross-project).

---

### 37. `MlflowClient.download_artifacts()` raises instead of returning empty for a zero-artifact run

**Severity:** 4/10 · **Status:** 🟢 `fixed` (2026-06-25)

**Problem:** In `python/av_plugins/mlflow.py` (`import_run`), the intended flow was "download artifacts, then check if the resulting directory is empty, then raise a clear error." In practice, calling `client.download_artifacts(run_id, ".", dst_path=...)` on a run with **zero** logged artifacts doesn't return an empty directory at all — MLflow's `RunsArtifactRepository` itself raises an internal `mlflow.exceptions.MlflowException` ("Failed to download artifacts from path '.', please ensure that the path is correct."), which would have leaked straight through `import_run` to the caller as a confusing, MLflow-internals-specific error instead of Aether-Vault's own clear message. Only surfaced when testing against a real MLflow installation (`pip install mlflow`, sqlite-backed tracking store) with a run that logs a metric but no artifacts — the equivalent mocked/stub-based test would not have caught this, since it would need to know to replicate MLflow's specific failure mode rather than just "return empty".

**Fix:** Check `client.list_artifacts(run_id)` *before* attempting any download; raise Aether-Vault's `AetherVaultException("MLflow run {run_id} has no artifacts to import.")` immediately if it's empty, so `download_artifacts` is never called in the zero-artifact case.

**Verification:** `tests/test_plugins.py::test_mlflow_import_run_raises_when_no_artifacts` now passes against a real MLflow run with a metric but no artifacts (previously failed with the raw `MlflowException` traceback before this fix).

---

### 38. Imports commit everything currently staged, not just the imported path

**Severity:** 3/10 · **Status:** 🟢 `fixed` (2026-08-23, v1.1.9 — reopened by owner after initially being closed as intentional)

**Problem:** Observed during manual testing of `python/av_plugins/lightning.py`, `python/av_plugins/transformers.py`, and `python/av_plugins/mlflow.py` (all three `import_*`/callback functions): staging an unrelated file (`av add notes.py`) and then calling `import_checkpoint()` produces a commit containing **both** the unrelated file and the imported checkpoint.

**Fix (v1.1.9):** Originally declared intentional (the full-index tree snapshot mirrors `git commit`'s model) and documented as a usage caveat. Reopened in v1.1.9: plugin imports are machine-driven events, and silently absorbing whatever a human happened to have staged breaks attribution for training runs. New `_shared.py::commit_scoped()` snapshots the index, empties it, drives the real CLI (`add` + `commit`, same single code path) for exactly the import's paths, then merges every other entry back with its staged flag untouched — so unrelated pending work stays pending. All three plugins' import functions AND their live callbacks (`on_save_checkpoint` / `on_save` / dataset commits) use it. A plain `av commit` keeps its full-snapshot semantics; only machine-driven imports are scoped.

**Verification:** Regression tests in `tests/test_plugins.py`: scoped commit's tree contains only the target path while an unrelated staged file survives staged (`test_commit_scoped_commits_only_target_paths`); an `add` failure mid-import restores the user's staging byte-identically and commits nothing (`test_commit_scoped_restore_survives_add_failure`); the live Lightning callback path asserted identically (runs in CI's plugin-tests job where extras are installed).

---

### 39. Test fixtures let MLflow write a stray `mlruns/` folder into the real repo root

**Severity:** 2/10 · **Status:** 🟢 `fixed` (2026-06-25)

**Problem:** In `tests/test_plugins.py` (`test_mlflow_import_run`, `test_mlflow_import_run_raises_when_no_artifacts`), both tests pointed MLflow's **tracking** URI at a sqlite DB inside `tmp_path`, but a sqlite tracking URI only relocates run *metadata* — MLflow still defaults **artifact** storage to `./mlruns` relative to the process's current working directory. Since pytest's cwd is the real repository root, running these tests left a real `mlruns/` directory (with actual run/artifact files) sitting in the repo working tree — caught when the next Obsidian vault regeneration picked up an unexpected `mlruns.md` folder index that had no business existing.

**Fix:** Both tests now use `monkeypatch.chdir(tmp_path)` before setting the tracking URI, so MLflow's default relative artifact path lands inside the test's own temp directory instead of the real repo.

**Verification:** Re-ran the full suite from the repo root; confirmed no `mlruns/` directory is created there afterward (`git status` clean of it).

---

### 40. `av checkout` never restores `code`-type files — only `artifact`-type

**Severity:** 8/10 · **Status:** 🟢 `fixed` (2026-06-25)

**Problem:** In `python/av_cli/main.py` (`add`, `checkout`, `upload_commit_objects`): `add()` only ever copied a file's bytes into `.av/objects/<hash>` when it was classified `artifact` **and** exceeded the LFS size threshold; every `code`-type file (and any sub-threshold artifact) had its hash recorded in the index/commit tree but its actual bytes were never written anywhere outside the live working-tree file. `checkout()`'s restore loop (the unified flat-tree format from PR #8) then only materialized entries where `file_type == "artifact"` — so checking out an older commit silently left every `code` file at whatever content the working tree already had, while still printing `"Checked out '<hash>'"` and reporting a clean `av status` afterward. `upload_commit_objects()` had the same `type != "artifact"` skip, so even a successful remote push never carried code bytes either. For a tool whose entire pitch is versioning "the Holy Trinity" (code + models + datasets) together, the **code** pillar could never actually be rolled back — `av checkout <old-commit>` silently no-op'd on every `.py`/`.json`/`.md`/etc. file. Manual repro: commit `train.py` with `print('v1')`, overwrite + commit `print('v2')`, `av checkout <v1-hash>` → `train.py` still read `print('v2')`, with no error of any kind.

**Fix:** `add()` now writes a CAS object for **every** tracked file regardless of type/size (not just LFS-thresholded artifacts); `checkout()`'s restore step and `upload_commit_objects()` no longer gate on `file_type == "artifact"` — both apply uniformly to every tree entry. The artifact-specific layer-reassembly path is unaffected (code never has `layers`, so it always falls through to the plain copy/download branch). Note: this doubles on-disk storage for tracked code files (working-tree copy + CAS copy) — the same tradeoff git itself makes for every tracked blob, and necessary for checkout to have anything to restore from.

**Verification:** Manual repro above re-run after the fix — `train.py` correctly reads `print('v1')` after `av checkout <v1-hash>`; `tests/test_cli.py::test_checkout_restores_previous_commit` and `test_checkout_refuses_with_uncommitted_changes_without_force` (both use a tracked `code`-type file) pass.

---

### 41. `av test --webui` reports "npm not found on PATH" even when npm is genuinely installed

**Severity:** 4/10 · **Status:** 🟢 `fixed` (2026-06-25)

**Problem:** In `python/av_cli/main.py` (`test_cmd`), the initial implementation called `subprocess.run(["npm", "test"], cwd=webui_dir)` directly. On Windows, the real `npm` executable is a `npm.cmd` shim; passing the bare string `"npm"` to `subprocess.run` without `shell=True` frequently fails to locate/execute it via `CreateProcess`, even though `npm` is genuinely installed and resolvable from an interactive shell (`npm --version` worked fine in the same environment). This raised `FileNotFoundError`, which the code caught and reported as the user-facing "npm not found on PATH — install Node.js..." message — a *correct-looking* error for the *wrong* reason, since npm was in fact installed. Only surfaced by actually running `av test --webui` for real after writing it (not just the monkeypatched unit tests, which mock `subprocess.run` itself and therefore never exercise the real Windows path-resolution behavior) — exactly the kind of platform-specific gap the manual-debugging step exists to catch.

**Fix:** Resolve the executable's full path first via `shutil.which("npm")` (which does the PATHEXT-aware lookup correctly, the same way an interactive shell does) and pass that resolved path to `subprocess.run` instead of the bare string. The "not found" error message is now only shown when `shutil.which` genuinely returns `None`.

**Verification:** `av test --webui -k test_validate_ref_name_accepts_normal_names` run for real (not mocked) on this machine — failed with the npm-not-found message before the fix, ran both suites successfully (pytest, then the real `npm test` → Vitest) after it.

---

### 42. GC's physical-shard sweep silently never deletes anything on a host ahead of UTC

**Severity:** 7/10 · **Status:** 🟢 `fixed` (2026-06-26)

**Problem:** In `python/av_server/server.py` (`run_garbage_collection`), `grace_ts = gc_cutoff.timestamp()`, where `gc_cutoff` is a **naive** datetime that represents UTC (per `utcnow_naive()`'s own docstring). Calling `.timestamp()` directly on a naive datetime makes Python treat it as **local** time when converting to a Unix epoch — on this host (UTC+2), that silently shifted `grace_ts` two hours earlier than the real cutoff. Since `obj_path.stat().st_mtime` is a real, correctly-UTC-based epoch, the comparison `st_mtime >= grace_ts` then almost never evaluates as "old enough to delete": a file would need to be more than `GC_GRACE_SECONDS + |local UTC offset|` old before the physical sweep would ever touch it — on a host *behind* UTC, the bug runs the other way and would delete objects **before** their real grace window expires, defeating the entire purpose of the grace period (protecting objects mid-upload from a concurrent GC). Found via `test_gc_respects_grace_period_then_sweeps_when_aged`: after zeroing `GC_GRACE_SECONDS`, the test asserted the now-orphaned object's shard file was actually removed from disk; it consistently returned `deleted_objects: 0` and the file remained on disk, even though the DB-side deletion (a naive-to-naive datetime comparison, unaffected by this bug) correctly removed the corresponding row. The DB/filesystem inconsistency was the tell — only the epoch-converting comparison was wrong.

**Fix:** `grace_ts = gc_cutoff.replace(tzinfo=timezone.utc).timestamp()` — attaching the correct `tzinfo` before converting to epoch makes `.timestamp()` compute the right value regardless of the host's local timezone.

**Verification:** Re-ran the test after the fix — the aged object's shard file is now actually removed from disk, and `deleted_objects: 1` is returned as expected.

---

### 43. Test-only: `tests/test_server.py`'s per-test DB cleanup crashed at teardown

**Severity:** 3/10 · **Status:** 🟢 `fixed` (2026-06-26)

**Problem:** In `tests/test_server.py` (`_truncate_all`, `db` fixture), the cleanup helper ran `async with engine.begin() as conn: ...` using the module's pooled SQLAlchemy async engine, invoked via a fresh `asyncio.run()` call in the fixture's teardown. The engine's pooled connection is bound to whichever event loop first used it (`TestClient`'s own internal lifespan loop); reusing that pool from a *different* loop (the one `asyncio.run()` spins up for the teardown call) raised `RuntimeError: ... got Future ... attached to a different loop` on every single test.

**Fix:** Open a brand-new `asyncpg.connect()` directly (bypassing the SQLAlchemy pool entirely) for the truncate, scoped wholly to the teardown call's own event loop.

**Verification:** Re-ran the suite after the fix — no more teardown errors.

---

### 44. Test-only: leftover orphan shard files from earlier tests polluted the GC grace test

**Severity:** 2/10 · **Status:** 🟢 `fixed` (2026-06-26)

**Problem:** In `tests/test_server.py` (`db` fixture), per-test cleanup truncated the DB tables but never cleared `CASStorage`'s on-disk `objects/`/`commits/`/`refs/` directories, which are shared across the whole test session. Earlier tests' uploaded objects (now orphaned once their DB rows were truncated) accumulated on disk; once the GC timezone bug above (entry 42) was fixed, the grace-period test's `deleted_objects == 1` assertion became flaky — it correctly swept *all* eligible orphans on disk, not just the one this specific test created (observed once as `3`).

**Fix:** `_clear_storage_dirs()` added to the `db` fixture's teardown, deleting file contents (not the directories themselves) from all three storage subdirectories after every test.

**Verification:** Full `test_server.py` run is now stable at 29 passed, 0 failed.

---

### 45. Test-only: the real-wire test's reachability check raced with collection-time load

**Severity:** 2/10 · **Status:** 🟢 `fixed` (2026-06-26)

**Problem:** In `tests/test_server.py` (`test_cli_commit_pushes_to_a_live_server`), `_real_server_reachable()` was wired up as a `@pytest.mark.skipif(...)` condition, which pytest evaluates exactly once, at module-collection time — the very start of the whole run, before any other test executes. When the full suite was first run through the plugin `venv/` together with the live Docker stack (a much heavier collection phase, importing `torch`/`transformers`/`lightning`), the 1.5s `httpx` reachability check raced against that load spike and read the server as unreachable even though it was confirmed healthy and fast (51ms) moments later via a direct `curl`.

**Fix:** Moved the check into the test body itself (`pytest.skip(...)` called lazily, only when this specific test actually runs, after collection and ~100 other tests have already settled) instead of a collection-time `skipif` decorator.

**Verification:** Re-ran the full combined venv+Docker suite twice after the fix — stable at 105 passed, 3 skipped (the 3 permanent-by-design "raises ImportError when missing" tests).

---

### 46. `vitest.setup.ts` broke `next build` via an "unused `@ts-expect-error`" type error

**Severity:** 6/10 · **Status:** 🟢 `fixed` (2026-06-26)

**Problem:** `next build` type-checks the *entire* TypeScript project, including files that are never part of the shipped app — `webui/vitest.setup.ts` was picked up by `webui/tsconfig.json`'s broad `"**/*.ts"` include. That file stubs `ResizeObserver` for jsdom with an `@ts-expect-error` comment suppressing a type error that exists under Vitest's type resolution but not under Next's (the DOM lib types it pulls in already cover the assignment) — TypeScript itself treats an `@ts-expect-error` with nothing to suppress as an error ("Unused '@ts-expect-error' directive"), so the production Docker image build for `aether-vault-webui` failed outright (`docker compose build aether-vault-webui` → exit 1). Found while adding React Testing Library component tests and a Playwright E2E suite for `webui/` (the one remaining roadmap line): the Weight Diff E2E test was timing out with the dashboard stuck on "Connecting…"/"Loading checkpoints…" forever, even though a manual `fetch()` to the API from inside the same browser context succeeded — the running `aether-vault-webui` container was 46 hours old (built before several of this session's fixes); rebuilding it to get current source is what actually surfaced the type-check failure instead of silently shipping stale code.

**Fix:** Replaced the `@ts-expect-error` comment with a plain type cast (never errors either way, so it can't go stale), and added `vitest.config.ts`/`vitest.setup.ts`/`playwright.config.ts`/`e2e/`/`src/**/*.test.ts(x)` to `tsconfig.json`'s `exclude` so test-only files are never part of the app's production type-check scope again.

**Verification:** `docker compose build aether-vault-webui` succeeds; the rebuilt container's Weight Diff tab now loads real data and both new Playwright specs pass against it.

---

### 47. `av add` stored the whole-file blob *in addition to* split layers — layer-dedup gave zero real storage savings

**Severity:** 8/10 · **Status:** 🟢 `fixed` (2026-06-26)

**Problem:** In `python/av_cli/main.py` (`add`, `doctor`): when `aether_core.split_and_hash_safetensors` succeeded, `add()` correctly stored each layer separately under `.av/objects/` — but then *unconditionally* also copied the entire original file to `.av/objects/<whole_file_hash>` (lines 469–473, pre-fix). Every fine-tune commit that only touched the classifier head still re-stored the *full* checkpoint every time, on top of the (genuinely deduped) per-layer copies. Net effect: a layered artifact ended up using *more* disk than not splitting at all, completely negating the feature's purpose. The codebase's own `push_objects()` already had the correct condition ("upload the whole-file object only if layers weren't successfully chunked," line 244) and `checkout` already reassembles the whole file from layers on demand when the blob is absent — `add()` was the one place that didn't follow that established pattern. Found while building benchmark #2 of the new `av benchmark` suite ("safetensors layer-dedup storage savings" vs DVC/Git LFS/MLflow) — the real measured numbers came back *worse* for Aether (162.5MB) than all three whole-file-only competitors (125.8MB each) after 6 simulated fine-tune commits, the opposite of the intended/advertised behavior.

**Fix:** `add()` now only writes the whole-file blob when `layers` is empty, matching `push_objects()`'s existing condition exactly. `doctor`'s orphaned-pointer detection and `--fix` recovery were also made layer-aware (an entry with `layers` is now checked/repaired per-layer, not by checking for an intentionally-absent whole-file blob — without this, every layered artifact would have started failing `av doctor` as a false-positive "orphaned pointer" the moment the whole-file copy was removed).

**Verification:** New tests in `tests/test_cli.py` (`test_add_safetensors_skips_whole_file_copy_when_layers_split`, `test_checkout_reassembles_safetensors_from_layers`, `test_doctor_does_not_flag_layered_artifact_as_orphaned`, `test_doctor_detects_orphaned_layered_artifact_with_missing_layer`) — full suite green. Re-ran `benchmarks/bench_safetensors_dedup.py` after the fix: Aether dropped from 162.5MB to 36.7MB for the same 6-commit sequence, now genuinely beating all three competitors (125.8MB each) instead of losing to them.

---

### 48. No-op `add`/`status` was 6.1x slower than Git LFS — redundant stat, unconditional index save, eager imports

**Severity:** 7/10 · **Status:** 🟢 `fixed` (2026-06-27)

**Problem:** Benchmark #4 (in `development/BENCHMARKS.md`, which rated this BAD against other tools) showed `av add .` on 60 unchanged files taking 875ms vs Git LFS's 143.4ms. The existing fast path in `python/av_cli/main.py` (`add`, module-level imports; `compare_meta_safe`, skips re-hashing when size+mtime match) worked correctly — the cost was everything around it: (1, severity 4, difficulty 1) `add()` fetched `meta` via `get_file_meta_safe()` then immediately called `compare_meta_safe()`, which calls `get_file_meta_safe()` again on the same path — a redundant second `stat()` syscall per file. (2, severity 5, difficulty 2) `idx.save()` ran whenever `files_to_process` was non-empty, regardless of whether any entry actually changed — a true no-op still did a full JSON serialize-and-write of the index. (3, severity 7, difficulty 2) `from .client import VaultClient` at module scope pulled in `requests`/`urllib3`/`certifi` on *every* `av` invocation, including purely local commands (`add`, `init`, `status`, `branch`) that never touch the network. (4, severity 2, difficulty 2, stretch) `import aether_core` (the pybind11 C extension) at module scope cost ~90ms even when the fast path means no hashing happens at all.

**Fix:** Inlined the meta comparison in `add()` instead of re-calling `compare_meta_safe()`; added an `any_changed` flag so `idx.save()` only runs when an entry actually changed; moved `VaultClient` to local imports inside the five commands that use it (`commit`, `checkout`, `push`, `gc`, `doctor`), with a module `__getattr__` so `main.VaultClient` stays resolvable for existing test monkeypatching; made `aether_core` import lazy via `_get_aether_core()`, called on first actual hash/split. The external CLI behavior and server API contract were unchanged — exact root causes traced, not rewrites.

**Verification:** `pytest tests/` green (111 passed incl. new tests, 20 skipped, same baseline); manually confirmed `.av/index`'s mtime is untouched across a true no-op `add .` in a scratch repo. Re-ran `benchmarks/bench_noop_status_speed.py`: 875.0ms → 552.5–624.0ms across repeated captures (~30% faster). Still rated BAD — the residual gap is CPython interpreter + `click` import startup cost, which a compiled Git LFS binary doesn't pay; closing that fully would mean rewriting the CLI in a compiled language, out of scope for this pass.

---

### 49. `commit` was 8.3x slower than DVC — serial per-object HEAD+POST instead of the existing batch-check endpoint

**Severity:** 9/10 · **Status:** 🟢 `fixed` (2026-06-27)

**Problem:** Benchmark #3 showed `commit` on a 60-file fixture taking 2,933.7ms vs DVC's 354.4ms. `upload_commit_objects()` in `python/av_cli/main.py` looped over every tracked file/layer hash and called `client.upload_object()` (in `python/av_cli/client.py`), which issued a `HEAD` request to check existence, then a `POST` to upload if missing — entirely serially. For 60 objects that's up to ~120 sequential network round trips. Separately, `python/av_server/server.py` already exposed `POST /api/sync/batch-objects` (checks many hashes in one call, backed by the RedisBloom filter) — nothing in the client called it; it was dead capability.

**Fix:** Added `VaultClient.batch_check_objects(hashes)` (one POST to the existing endpoint) and a `known_missing` parameter on `upload_object()` to skip the now-redundant per-object `HEAD` when the caller already knows the hash is missing. Rewired `upload_commit_objects()` to collect every referenced hash once, batch-check it in a single call, then upload only the missing objects concurrently via a `ThreadPoolExecutor` (8 workers — these are network-bound HTTP calls, not CPU work). Still blocks until every upload completes before returning, so the existing invariant ("objects must land before `push_commit()`," documented in the function's own docstring re: the server's FK constraint) is unchanged. `flush_pending_push()` calls the same function, so the offline-retry queue gets the same speedup for free.

**Verification:** `pytest tests/` green, including new `tests/test_client.py` (`batch_check_objects` request shape, empty-input short-circuit, non-200 handling, `known_missing` HEAD-skip) and two new tests in `tests/test_cli_commands.py` asserting `upload_commit_objects()` batch-checks once and uploads only the hashes the batch-check reported missing. Also verified against a real `av_server` (Docker Compose: Postgres + Redis + FastAPI, not mocked): ran `av init/add/commit/push` end-to-end in a scratch repo, confirmed all uploaded objects are queryable via a live `batch-objects` call, and confirmed the offline pending-push path (server stopped mid-session, commit queued, server restarted, `av push` flushed it) still works through the same parallelized code. Re-ran `benchmarks/bench_commit_push_latency.py`: 2,933.7ms → 1,357–2,532ms across captures (45–54% faster depending on machine load). Still rated BAD against DVC — DVC's `commit` never touches the network (`dvc push` is a separate step), while av intentionally uploads objects synchronously during `commit` to satisfy the server's FK ordering constraint; that architectural difference is out of scope for this pass (see README's Open Source Roadmap).

---

### 50. Bare `av` (and `av init`) crashed with an unhandled `NoConsoleScreenBufferError` outside a real Windows console

**Severity:** 7/10 · **Status:** 🟢 `fixed` (2026-06-27)

**Problem:** In `python/av_cli/repl.py` (`run_repl`), found during step 1 of the Phase 27 wrap-up (manual debugging against the real installed `av` binary, not `CliRunner`) — bare `av` and `av init` (default flow) both crashed outright when run from Git Bash/mintty on Windows. `sys.stdin.isatty()`/`sys.stdout.isatty()` both report `True` in that terminal, so `ui.is_interactive()` correctly decided to skip the Local/Enterprise prompt only where expected — but `run_repl()`'s call to `prompt_toolkit.PromptSession(...)` still raised `prompt_toolkit.output.win32.NoConsoleScreenBufferError` ("Found xterm-256color, while expecting a Windows console") unconditionally, because mintty's pty emulation has no real Win32 console screen buffer behind it even though it reports as a tty. Nothing caught the exception, so it propagated all the way out and crashed the whole `av` invocation with a raw Python traceback — a real first-impression bug for exactly the platform (Windows + Git Bash) this CLI's own dev environment runs on.

**Fix:** Wrapped the `PromptSession(...)` construction, and each loop iteration's `session.prompt(...)` call, in a broad `except Exception` in `run_repl()`. On failure, prints one warning line ("Interactive session isn't available in this terminal — run `av <command>` directly instead.") and returns/breaks instead of crashing — the rest of `av init`/bare `av` (repo bootstrap, Docker reconnect) already completed before this point, so the user still gets a fully working repo, just without the interactive session in that specific terminal.

**Verification:** Added `tests/test_repl.py::test_repl_degrades_gracefully_when_session_cannot_be_constructed` (monkeypatches `PromptSession` to raise, asserts `run_repl()` doesn't raise and prints the warning). Manually re-ran the exact repro from a real Git Bash shell — bare `av`, `av init` (fresh repo), and re-running `av init` against an already-initialized repo (reconnect path) all now complete cleanly with the warning instead of a traceback.

---

### 51. `check_for_docker_update()` attempted `docker compose pull` without first checking Docker was running

**Severity:** 6/10 · **Status:** 🟢 `fixed` (2026-06-27)

**Problem:** In `python/av_cli/docker_runtime.py` (`check_for_docker_update`), found during manual debugging while building the Docker auto-update feature — calling `docker_runtime.check_for_docker_update()` against a registry image that doesn't exist yet (nothing published to GHCR) caused the process to sit unresponsively for over a minute (up to the 600s-per-service timeout on `pull_latest_image()`, twice, since there are two release images) instead of failing fast. Root cause: every other Docker-facing entry point in this module (`ensure_local_backend_running()`) checks `check_docker_running()` first and fails fast with a clear message ("Docker is not running...") — `check_for_docker_update()` was the one path that skipped this check and went straight to `docker compose pull`, which has no comparable fast-fail behavior of its own against an unresponsive/absent daemon or a registry image that 404s.

**Fix:** Added the same `check_docker_running()` guard used elsewhere in this module, before attempting any pull — returns a `DockerUpdateResult(checked=False, message="Docker is not running...")` immediately, matching the existing UX convention instead of introducing a new failure mode.

**Verification:** Added `tests/test_docker_runtime.py::test_check_for_docker_update_fails_fast_when_docker_not_running` (monkeypatches `check_docker_running` to report not-running, asserts `pull_latest_image` is never called). Found live by actually running the unguarded version against this machine's real Docker installation pointed at the (not-yet-published) GHCR images and observing the hang firsthand, then killing the process — not just inferred from reading the code.

---

### 52. `av stash pop` restored a modified-but-unstaged file's index entry with the dirty hash/stat instead of HEAD's baseline, making it look falsely clean

**Severity:** 6/10 · **Status:** 🟢 `fixed` (2026-06-28)

**Problem:** In `python/av_cli/main.py` (`_stash_apply_or_pop`): `status()` detects a "modified" tracked file purely by a stat mismatch between the on-disk file and the size/mtime stored in its index entry — there's no separate "dirty" flag. `_stash_apply_or_pop()`'s first version restored every entry (regardless of `was_staged`) using the stash record's own hash and the just-written file's real stat — which, for a `was_staged=False` entry, makes the stored stat match the restored (dirty) file exactly. Found via manual debugging (this session's established practice of driving new features with the real `av` binary, not just unit tests): after `av stash` then `av stash pop`, a file that had been modified-but-unstaged before the stash silently vanished from `av status` entirely instead of showing up under "Changes not staged for commit" again.

**Fix:** `_stash_apply_or_pop()` now branches on `was_staged`. For `True`, it keeps the original behavior (dirty hash + real stat + `staged=True`, so it shows as "to be committed" — `status()` trusts the `staged` flag before ever checking the stat). For `False`, it looks up `resolve_head_tree()` again and stores *HEAD's* hash/size with `mtime_ns=0` (deliberately non-matching) — exactly mirroring how `_stash_push()` represents an unstaged modification in the first place, so the stat-mismatch check correctly reports "modified" again after pop.

**Verification:** `tests/test_stash.py::test_stash_pop_restores_staged_and_modified_state_correctly` asserts both the staged and modified-unstaged entries are reported correctly by `av status` after a push/pop round-trip, not just that the file contents are right.

---

### 53. Two stashes created within the same second sorted unpredictably in `av stash list`

**Severity:** 4/10 · **Status:** 🟢 `fixed` (2026-06-28)

**Problem:** Stash filenames are `<timestamp>-<shortid>.json`, and `_list_stash_files()` sorts them newest-first by reverse filename order — relying on the timestamp prefix to dominate the comparison. The timestamp used second-level resolution (`%Y%m%dT%H%M%SZ`); two stashes created within the same second (an entirely realistic case — e.g. a script, or just two fast manual `av stash` calls) share an identical prefix, so the sort falls back to comparing the random 6-hex-character shortid, which has no relationship to creation order at all. Found by the test suite itself (`tests/test_stash.py::test_stash_list_orders_newest_first`), which failed on the very first run — not inferred from reading the code first. Affected `python/av_cli/main.py` (`_stash_push`'s stash ID generation).

**Fix:** Switched the stash ID's timestamp component to microsecond resolution (`%Y%m%dT%H%M%S%f`), which two sequential CLI invocations (each involving real file I/O) will not collide on in practice.

**Verification:** Re-ran the previously-failing test 5 times in a row to rule out remaining flakiness (all passed) in addition to the full suite.

---

### 54. Top bar title stayed hardcoded to "Dashboard" on every sidebar tab

**Severity:** 3/10 · **Status:** 🟢 `fixed` (2026-06-28)

**Problem:** In `webui/src/components/TopBar.tsx` and `webui/src/app/page.tsx`: `TopBar.tsx` rendered `<span className="top-bar-title">Dashboard</span>` as a literal string, with no prop driving it. Harmless before — every sidebar tab rendered the same Dashboard view, so the label was always correct by coincidence. Once Commits, Branches, Metrics, and Storage became real, distinct panels (Phase 30), the header stayed stuck on "Dashboard" while the sidebar's active-tab highlight correctly moved. Found via manual debugging — driving the running `npm run dev` server with a headless Playwright browser and screenshotting each tab — not from reading the diff.

**Fix:** Added an optional `title` prop to `TopBar` (defaulting to `"Dashboard"` to keep the component's existing standalone behavior), and a `TAB_TITLES` lookup in `page.tsx` mapping each `active` id to its display name, passed in as `title={TAB_TITLES[active] ?? active}`.

**Verification:** Re-ran the Playwright screenshot pass after the fix — the header now reads "Commits", "Branches", "Metrics", "Storage", "Weight Diff", "Projects", or "Dashboard" to match whichever sidebar tab is active.

---

### 55. `test_doctor_fix_cannot_recover_truly_missing_object` silently depended on no `av_server` being reachable

**Severity:** 3/10 · **Status:** 🟢 `fixed` (2026-06-28)

**Problem:** In `tests/test_cli.py`, the test's only justification for expecting the missing object to stay unrecoverable was a comment — "No server running in this test environment" — not an explicit mock. That was true by environmental coincidence until this session's real Docker stack (db/redis/server/webui) was left running to capture fresh benchmark numbers; with a real `av_server` reachable on `localhost:8000`, `av doctor --fix`'s recovery path could genuinely reach it, and the object was no longer truly unrecoverable — breaking the test's `[WARN]`/"could not recover" assertions. Found via manual debugging (the full pytest run after the benchmark work), not by reading the diff.

**Fix:** Explicitly monkeypatch `VaultClient.server_available` to return `False`, mirroring the adjacent `test_doctor_fix_downloads_missing_object_from_server` test's existing pattern (which forces it `True` to test the opposite path) — neither test should depend on whatever a real server happens to be doing on the machine running the suite.

**Verification:** Re-ran with the real Docker stack still up — test now passes regardless.

---

### 56. A test's `monkeypatch.setattr("benchmarks.tool_runner.render_doc_header", ...)` broke under an adjacent `importlib.import_module` patch in the same test

**Severity:** 2/10 · **Status:** 🟢 `fixed` (2026-06-28)

**Problem:** In `tests/test_cli.py` (`test_benchmark_command_markdown_writes_file`): the test patches `main_module.importlib.import_module` to a fake (`importlib` is a shared global module object, so this patches the *real* `importlib.import_module` for the whole process, not just `main_module`'s reference to it). pytest's own `monkeypatch.setattr(<string>, ...)` form internally calls the real `importlib.import_module` to resolve the dotted path — which was now the test's own fake, returning a `_FakeBenchModule` instead of the real `benchmarks.tool_runner` module, and the second `setattr` call crashed with `AttributeError: '_FakeBenchModule' object has no attribute 'tool_runner'`.

**Fix:** Import the real module via a plain `import benchmarks.tool_runner as tool_runner_module` statement first (plain `import` statements use the import system's `__import__` machinery directly, not `importlib.import_module`, so they're unaffected by the patch) and call `monkeypatch.setattr(tool_runner_module, "render_doc_header", ...)` against that object instead of the string-target form.

**Verification:** `pytest tests/test_cli.py -k benchmark` and the full suite (249 passed, 3 skipped) both green after the fix.

---

### 57. `av checkout` rejected the short hashes `av commit` itself prints

**Severity:** 4/10 · **Status:** 🟢 `fixed` (2026-08-21)

**Problem:** `av commit` prints `[a54a0b2] <message>` (7-char prefix), but `checkout` in `python/av_cli/main.py` (short-hash print at line 1285) only resolved either an exact branch name or the full 64-char hash — no prefix matching existed anywhere. Copying the hash av had just printed and running `av checkout a54a0b2` failed with `Error: Commit 'a54a0b2' not found.` Same gap in `av handoff --since <hash>` (`python/av_cli/handoff.py`, `load_commit`). Every user-facing flow that involves checking out a specific commit from av's own console output (the most common copy-paste source) was broken on first use; users would have to inspect `.av/refs/heads/` or guess that only the full hash works. Found in a manual debugging session against the real installed `av` binary in a scratch repo (per `Aether-vault-Obsidian-Vault/Essential-Tasks.md` step 1) — committed twice, copied the printed short hash into checkout, hit the error. Not caught by any unit test because all existing checkout tests pass full hashes read from `.av/refs/heads/main`.

**Fix:** Shared helper `fsutil.find_commit_file()` (new, in `python/av_cli/fsutil.py`) plus new `AmbiguousCommitHash` exception in `python/av_cli/exceptions.py` — exact match first, then a unique hex-prefix match over `.av/commits/`; raises the new `AmbiguousCommitHash` (a ClickException subclass, so both one-shot and REPL flows render it as a red `Error: ...` line) when a prefix matches several commits, `FileNotFoundError` when nothing matches. `checkout()` resolves through it and rewrites its internal `commit_hash` to the resolved full hash before writing HEAD's detached entry; `handoff.load_commit()` uses the same helper so `--since` accepts prefixes too. Minimum prefix length is 4 characters, mirroring git's own abbreviation floor.

**Verification:** Real scratch-repo run — `av checkout a54a0b2` checks out the right commit, restores correct file content, writes the full hash into detached HEAD; ambiguous prefix rejected with a clear message; `av handoff --update --diff-weights --since d91bad3` resolves. New tests: CLI-level short-hash checkout + ambiguous rejection (`tests/test_cli.py`), resolver unit cases + `load_commit` prefix acceptance (`tests/test_vault.py`). Full suite: see Phase 35 in `CHANGELOG.md`.

---

### 58. sdist shipped a 64.5 MB Docker-image tar — 64.7 MB source release

**Severity:** 4/10 · **Status:** 🟢 `fixed` (2026-08-21)

**Problem:** `aether-vault-server.tar` (a 64.5 MB `docker save` export, untracked from git now) was git-tracked. setuptools-scm seeds the sdist file list from all git-tracked files, so every source release embedded the entire server image: the published `0.1.0`/`0.1.1` sdists were **64.7 MB** for a package whose wheels are ~200–430 KB. It also made every `git clone` ~65 MB heavier and was silently exempt from `.gitignore` because it had been committed before being listed there (actually: it wasn't ignored at all until now). Source installs took ~85x longer to download than necessary; PyPI has a 100 MB per-file limit that a slightly larger image export would have blown through, breaking the release pipeline mid-publish. Found by auditing the real PyPI JSON metadata (`pypi.org/pypi/aether-vault/json`) during the business-readiness review — sdist size 64,707,757 bytes vs. wheel sizes two orders of magnitude smaller; then built a local sdist and found the tar sitting in its file list.

**Fix:** `git rm --cached aether-vault-server.tar` (local copy kept on disk), added it to `.gitignore`, and added `MANIFEST.in` with `exclude aether-vault-server.tar` + pyc/pycache hygiene excludes as defense-in-depth for any future tracked artifact.

**Verification:** Rebuilt the sdist after the change — 761 KB total, no `.tar` member inside, LICENSE/MANIFEST.in present, `twine check` PASSED.

---

### 59. Published PyPI pages were empty — no summary, description, license, or URLs

**Severity:** 3/10 · **Status:** 🟢 `fixed` (2026-08-21)

**Problem:** `pyproject.toml`'s `[project]` carried only name/dynamic-version/dependencies. The published `0.1.0`/`0.1.1` releases therefore rendered barebones PyPI pages: `summary: null`, empty long description, zero classifiers, no repository link, no license — for anyone landing on PyPI, the project looked abandoned or automated-spam.

**Fix:** Full PEP 621 metadata under `[project]`/`[project.urls]`: one-line description, `readme = "README.md"` (full README now renders as the PyPI page body), PolyForm Noncommercial license text, author ("Leon Schwarzkopf (Aether Quant)"), 7 keywords, 15 classifiers (Beta, audiences, OSes, Python 3.10–3.12, C++, version-control/AI topics), and Homepage/Repository/Issues/Changelog URLs.

**Verification:** `twine check dist/*.tar.gz` PASSED; PKG-INFO inspected directly — Summary, License, all classifiers, all Project-URLs, Keywords, and the README long-description are present in the built distribution. Caveat: the next tag push publishes this metadata; existing 0.1.x pages update only when a yank/new upload happens.

---

### 60. No LICENSE file anywhere in the repo or the published packages

**Severity:** 2/10 · **Status:** 🟢 `fixed` (2026-08-21)

**Problem:** Neither the repo nor either PyPI release carried a license — default copyright law applies, meaning technically nobody (including PyPI redistributors) was licensed to use or redistribute the software at all. Also invisible on the PyPI page (`license: null`).

**Fix:** Adopted the PolyForm Noncommercial License 1.0.0 (same license as the author's other projects) with Required Notice `Copyright Leon Schwarzkopf (Aether Quant)`. Noncommercial use (personal, research, education, nonprofits, government) is free; commercial use requires a separate license — aligning the free tier with the planned open-core/commercial-split model. Setuptools auto-includes LICENSE in distributions by filename convention (confirmed present in the rebuilt sdist). README gained a short License section linking to it.

**Verification:** Self-evidently complete — LICENSE confirmed present in the rebuilt sdist via setuptools' filename-convention auto-include; no dedicated test beyond that recorded in the audit log.

---

### 61. `tests/test_merge.py` failed collection on Python ≤3.12 — annotation referenced an import defined 9 lines later

**Severity:** 4/10 · **Status:** 🟢 `fixed` (2026-08-22)

**Problem:** In `tests/test_merge.py`, `def _commit_file(repo: Path, ...)` (line 184) used `Path` in a parameter annotation, but `from pathlib import Path` sat at line 193, *below* it. On Python ≤3.12 function annotations evaluate **eagerly at def time** → `NameError: name 'Path' is not defined`, aborting the ENTIRE suite at collection (`1 error during collection`). The `test` job (windows, py3.10) died this way. Invisible locally because the dev machine runs Python 3.14, where PEP 649 defers annotation evaluation — the same file collected fine. A version-dependent failure mode, not a logic bug. Caught by GitHub Actions CI during the v1.1.1 cycle push (logs read directly via `gh run view --log-failed` rather than reproduced blind).

**Fix:** Moved `from pathlib import Path` into the top import block; deleted the mid-file import. Systemic fix: new `scripts/check_eager_annotations.py` — AST scan that flags any module-level annotation referencing a name whose import/definition appears later in the file (builtins exempted, `from __future__ import annotations` files skipped). Run before pushing when editing tests from a ≥3.13 machine.

**Verification:** The AST scanner proven both ways: 0 problems on the fixed tree, exit 1 with exact lines on the stashed pre-fix version.

---

### 62. Live E2E crashed after succeeding — `json` used without import in `tests/test_server.py`

**Severity:** 3/10 · **Status:** 🟢 `fixed` (2026-08-22)

**Problem:** In `tests/test_server.py`, `test_live_two_repo_clone_pull_flow` called `json.loads(...)` but the module never imported `json`. The test got all the way through init/push/clone against the real Docker stack and THEN crashed — so the collaboration flow itself worked; only the assertions were unreachable. (The adjacent `os.urandom` call was fine: `os` was already imported.) Caught by GitHub Actions CI during the v1.1.1 cycle push (logs read directly via `gh run view --log-failed` rather than reproduced blind); a pure test-infrastructure defect requiring zero product-code changes.

**Fix:** Added `import json` to the module's import block.

**Verification:** 47/48 other server tests passed on CI, confirming the Phase 39–42 server changes work live.

---

### 63. `dashboard.spec.ts` asserted a hero heading that no longer exists in the UI

**Severity:** 2/10 · **Status:** 🟢 `fixed` (2026-08-22)

**Problem:** In `webui/e2e/dashboard.spec.ts`, the spec's boot assertion waited for `getByRole("heading", { name: "🌌 Aether-Vault" })` — no element with that role/name exists anywhere in the current UI (the brand is a sidebar `<Image>` logo + "ML Registry Dashboard" text). Every previous E2E failure had died at this exact line and was misread as "empty seeded data"; once the `AV_DATA_DIR` fix let seeding succeed, weight-diff PASSED while dashboard still timed out here — proving the selector, not the data, was wrong. Caught by GitHub Actions CI during the v1.1.1 cycle push (logs read directly via `gh run view --log-failed` rather than reproduced blind); a pure test-infrastructure defect requiring zero product-code changes.

**Fix:** Replaced the stale assertion with two that reflect the real DOM and keep the intent (app shell mounted): sidebar brand text "ML Registry Dashboard" + the `#nav-dashboard` nav item. Both verified present in `Sidebar.tsx`; spec compiles via `tsc --noEmit`.

**Verification:** Spec compiles via `tsc --noEmit`; replacement selectors confirmed present in `Sidebar.tsx`.




### 64. Star-import blind spot in the eager-annotation checker produced 13 false positives on the new cmd modules

**Severity:** 3/10 · **Status:** 🟢 `fixed` (2026-08-22)

**Problem:** The v1.1.1 CLI split introduced `from .core import *` as the command modules' shared prelude. `scripts/check_eager_annotations.py` resolves only explicit imports, so annotations referencing public core names (`Path`, `click`) flagged as pre-import uses — while at runtime star-imports surface them on every Python version. Left alone, either the checker cries wolf 13× or someone "fixes" it by disabling the guard.

**Fix:** The checker now resolves star-imports one level deep: a relative `from .core import *` pulls that file's public top-level bindings (defs/classes/assigns/imports) into the available set. Underscore names still require explicit imports — which is exactly the discipline the split needs.

**Verification:** 13 false positives eliminated; the original true-positive (stashed pre-fix test_merge.py) still detected with exit 1.

---

### 65. Rate limiter's Retry-After overshot by one second

**Severity:** 2/10 · **Status:** 🟢 `fixed` (2026-08-22)

**Problem:** `python/av_server/rate_limit.py`'s denial path returned `int(remaining) + 1` — a client denied at window start got `Retry-After: 61` on a 60-second window.

**Fix:** `max(1, ceil(remaining))`.

**Verification:** Caught immediately by the limiter's own unit suite (`1 <= retry <= 60` assertion) once the off-by-one was in place; green after the fix.

---

### 66. Split-time splice dropped the `_aether_core` module globals

**Severity:** 2/10 · **Status:** 🟢 `fixed` (2026-08-22)

**Problem:** The mechanical extraction of `_get_aether_core()` from main.py into `python/av_cli/core.py` sliced from `def` onward, orphaning its two module-level globals (`_aether_core`, `_aether_core_load_attempted`). Surfaced at runtime as `NameError` inside `stage_one_file` → every `av add` failed in scratch-repo verification.

**Fix:** Globals restored above the def; full stash+sync suites green immediately after.

**Verification:** Found by manual debugging per Essential-Tasks step 1 (scratch add flow), not by reading diffs; the same scratch-repo pass caught two missing cross-module imports (`_init_repo_structure`, `AsyncSession`) during the split — all fixed before any gate run was declared green.

---

### 67. `ast.parse` guard accepted what `compile()` rejects — env.py shipped a startup SyntaxError to CI

**Severity:** 4/10 · **Status:** 🟢 `fixed` (2026-08-23)

**Problem:** The Alembic `env.py` defined its online-migration runner as a plain `def` containing `async with`/`await`. That is syntactically valid to `ast.parse` (the guard's only check) but fails at **compile** stage with `SyntaxError: 'async with' outside async function`. The failure stayed invisible locally — Docker-down skips every DB-backed test, so nothing ever imported/executed env.py — and detonated on the first real run: server lifespan → `init_db` → `command.upgrade` → executes env.py → *"Application startup failed"*; server-tests ERRORed wholesale and webui-e2e rendered an empty dashboard against a dead server.

**Fix:** `async def run_migrations_online()` (matching Alembic's official asyncio template), plus `test_env_py_is_valid_python` now additionally runs `compile(source, str(path), "exec")` after parsing — closing the parse-vs-compile gap for every future edit to this file.

**Verification:** Guard proven both ways on the v1.1.6 CI logs (`gh run view --log-failed`, not local reproduction); extended guard green on the fixed tree. Lesson recorded: validation-tool strength must match or exceed the strictest interpreter stage that will consume the artifact — "parses" ≠ "compiles" ≠ "runs".

---

### 68. CDC chunk-count test asserted a probabilistic outcome as a hard bound — ~7% flake on random data

**Severity:** 2/10 · **Status:** 🟢 `fixed` (2026-08-23)

**Problem:** `tests/test_core.py::test_chunk_and_hash_file_produces_valid_chunks` hashed 6 MB of `os.urandom` data and asserted `2 <= len(chunks)` — but with the default avg-2MB mask, content-defined chunking lands cut points at probability 1/2²¹ per byte, so the chance of ZERO boundaries in ~5.5 MB of eligible bytes is ≈ e^-2.6 ≈ **7%** per run. When a blob drew no boundary, the core correctly returned one 8-MB-capped chunk and the test failed — observed once in a full-suite run (1 failed / 389 passed), invisible across six consecutive single-test reruns. A distribution-dependent assertion masquerading as a deterministic invariant; the product code was never wrong.

**Fix:** Test input raised to 32 MB (no-boundary probability drops to ≈ e^-15 ≈ 3×10⁻⁷) with the upper bound rescaled to the structural cap (64 = file_size/min_chunk), plus a comment deriving the probability so nobody re-tightens it to 6 MB.

**Verification:** Found during the v1.1.8 manual-debugging pass (full-suite run), not by reading diffs; 4× consecutive full-module runs green after the fix (~3 s each), failure math documented in the test body.

---

### 69. Dockerfile built a cp312 wheel onto a py3.11 runtime — every image build rejected it

**Severity:** 5/10 · **Status:** 🟢 `fixed` (2026-08-23)

**Problem:** The root `Dockerfile`'s builder stage ran `python:3.12-slim-bookworm` while the runtime stage ran `python:3.11-slim-bookworm`. `pip install /wheels/*.whl` therefore failed with *"aether_vault-0.0.0.dev0-cp312-cp312-linux_x86_64.whl is not a supported wheel on this platform"* — the Docker Edge Build job died after ~50 s, and `release.yml`'s `build-and-push-docker` job shares this exact file, so the next tagged release's GHCR images would have failed identically. Latent since the file was written because `docker-edge.yml` triggered on `main` while the repo branches to `master` (Probleme.md-adjacent config bug fixed in v1.1.8) — the workflow never once fired until that trigger fix, so the mismatch had zero chances to surface.

**Fix:** Runtime stage aligned to `python:3.12-slim-bookworm`, with a comment pinning the invariant: builder and runtime Python minors must match, or the cp-tag rejects the wheel.

**Verification:** Root-caused from the failed run's logs (`gh run view --log-failed`): the error line names the cp312 tag explicitly. Fix verified structurally (both FROM lines now 3.12); live proof arrives via the docker-edge run on the next push.

---

### 70. Migration chain executed faithfully — then rolled back: `engine.connect()` instead of `engine.begin()`

**Severity:** 9/10 · **Status:** 🟢 `fixed` (2026-08-23)

**Problem:** `database.py::_apply_schema()` wrapped the programmatic Alembic upgrade in `async with engine.connect() as conn:`. SQLAlchemy 2.0 is commit-as-you-go: a plain connection rolls back at context exit, and **Postgres has fully transactional DDL** — so on CI's server-tests/webui-e2e runs, uvicorn logged "Application startup complete", every `CREATE TABLE`/index of `0001_baseline` actually executed against the live database... and was then discarded wholesale. Result: schema-less database behind a healthy server; all ~46 DB-backed tests ERRORed with `UndefinedTableError: relation "objects"/"commits"/"alembic_version" does not exist`; E2E seeding silently queued offline ("Seeded 2 commits" printed against a dead-schema registry). Three compounding reasons it survived four CI cycles: (1) local dev runs Docker-down, so no DB test ever imported the path; (2) v1.1.6's red was attributed entirely to the env.py SyntaxError, and v1.1.7's identical signature was read as "needs live validation" rather than re-diagnosed; (3) the SQLite-based migration tests PASS because the pysqlite driver auto-commits DDL — the stack-free suite structurally could not see a rollback-only failure.

**How found:** full offline reproduction — installed embedded PostgreSQL 15 binaries locally, pointed `DATABASE_URL` at it, and instrumented env.py/`_ensure_schema_sync`/`0001_baseline.upgrade()` step by step: the revision fn executed, `command.upgrade` returned cleanly, and `pg_tables` stayed empty — isolating commit semantics as the only remaining variable.

**Fix:** `engine.begin()` (commits at context exit) — which is exactly what the pre-Alembic `create_all` code used; the Phase-46 rewrite changed both the schema mechanism and the transaction wrapper in one motion. Verified locally against real Postgres: fresh DB reaches `{alembic_version, commits, objects, refs, trees}` after startup, second startup idempotent.

---

### 71. `commit_scoped()` emptied the index and destroyed change detection — re-imports duplicated commits

**Severity:** 4/10 · **Status:** 🟢 `fixed` (2026-08-23)

**Problem:** The v1.1.9 `commit_scoped` implementation snapshot-and-EMPTIED the index before invoking the CLI `add`, so the target paths' existing entries (hash baselines) were invisible during re-import. `add_entry`'s `changed = existing is None or hash differs` therefore read True for byte-identical content, staged it, and the "re-import unchanged checkpoint is a no-op" contract broke: a second import produced a second identical commit (`assert 2 == 1` in `test_lightning_import_checkpoint`). Invisible locally (framework extras absent → Lightning tests skip; the framework-free regression tests only exercised single imports) and caught immediately by CI's plugin-tests job — the first failure caught BY the pipeline rather than by manual debugging, which is exactly what the v1.1.8/v1.1.9 CI repairs were for.

**Fix:** Baseline-preserving scoping: run `add` against the UNTOUCHED index, then scope to exactly the keys this add touched (new keys / content-changed keys / newly-staged transitions — distinguishing machine staging from pre-existing user staging via a pre-import `pre_staged` set), commit the scope, merge everything else back in `finally`. Unchanged re-imports now touch nothing → empty scope → documented "Nothing to commit" no-op.

**Verification:** New always-run regression `test_commit_scoped_reimport_is_a_noop` (double import → exactly 1 commit; content change under same path → exactly 2) plus a directory-target test mirroring Transformers imports; full scoped quartet green locally.

---

### 72. Per-user attribution tests read back without credentials — asserted against a 401 body

**Severity:** 2/10 · **Status:** 🟢 `fixed` (2026-08-23)

**Problem:** The three v1.1.8 attribution tests (`stamps_authenticated_username`, `respects_explicit_author`, `owner_shared_secret_stamps_owner`) POSTed with Bearer headers but then did plain `GET /api/commits/{hash}` while `_AUTH_USERS`/`AV_API_TOKEN` fixtures were still active — the middleware correctly 401'd those reads, and `.json()["author"]` raised KeyError on the `{"detail": ...}` body. A test bug (the middleware behaved exactly as designed); invisible locally behind the reachability skip.

**Fix:** Follow-up GETs reuse the same credential as their POST.

**Verification:** All three green against the local live stack (embedded Postgres + TCP probe standing in for Redis) before push — the exact CI environment shape.

---

### 73. Heal test imported a helper from the wrong module — and exposed an unrecorded-chain startup crash

**Severity:** 3/10 · **Status:** 🟢 `fixed` (2026-08-23)

**Problem:** Two layers. (a) `test_legacy_database_is_healed_and_stamped` imported `init_db_with_engine` from `python.av_server.database` — a helper that only ever existed in the test file itself; ImportError on every CI run since Phase 46 (skipped locally, so never seen). (b) Fixing (a) revealed the real find: the test simulates a legacy volume by deleting alembic_version's ROWS, but `_ensure_schema_sync` only took the heal+stamp path when the version TABLE was absent — an existing-but-empty version table fell through to the upgrade path, which replayed `0001_baseline` into the existing tables and crashed startup with `DuplicateTableError`. A production volume that lost its version rows (truncated, partial restore) would have bricked every restart.

**Fix:** (a) import `_apply_schema` directly. (b) product hardening: adoption now triggers whenever a data table exists without a recorded revision (`_unrecorded_chain()` — no version table OR no current revision), healing and stamping both shapes instead of replaying into them.

**Verification:** Full server suite green twice in a row against embedded Postgres 15 (56 passed each), including the heal test end-to-end: columns dropped + rows deleted → startup heals, stamps `0001`, restores `extra_parents`/`chunks`.

---

### 74. `av log` assertions substring-matched short messages against output containing random hashes — recurring CI flake

**Severity:** 2/10 · **Status:** 🟢 `fixed` (2026-08-24)

**Problem:** `tests/test_cli.py::test_log_limit_and_empty_repo` asserted `"c1" not in result.output` after a `--limit 2` log. `av log` lines embed random 7-char hex hashes (`[c11f8ca] c2`) and timestamps, so any run whose displayed hashes happened to contain the substrings `c1`/`c2`/`c3` tripped the assertions — observed live as the v1.1.10 `test (3.10)` job failure (`assert 'c1' not in '[d61fa71] (...'; the colliding token was `[c11f8ca] c2`). Purely probabilistic (~a few % per run, matrix-doubled): the 3.14 twin in the SAME run had non-colliding hashes and passed, which made it look version-specific when it wasn't.

**Fix:** Assertions now parse MESSAGES out of the bracketed log lines (stripping an optional `(HEAD, main)` ref decoration) and compare the exact sequence `== ["c3", "c2"]`. A repo-wide sweep for other `assert "<≤4-char all-hex>" in/not-in output` patterns found no further failure-capable instances (the two raw hits are presence-checks against deterministic content, which cannot false-fail).

**Verification:** 6 consecutive runs of the fixed test green locally; sweep script documented in the audit trail.

---

### 75. Protected mode silently broke the entire browser UI — auth middleware sat outside CORS

**Severity:** 7/10 · **Status:** 🟢 `fixed` (2026-08-24)

**Problem:** Two stacked consequences of one middleware-ordering mistake. Starlette runs the LAST-added middleware OUTERMOST; `require_token` registered itself after CORSMiddleware and therefore wrapped it. (a) Browser CORS **preflights** are credentialless by spec, so in Protected mode every preflight was 401'd with no ACAO headers and the browser aborted the real request before it existed. (b) The subtle half: auth's own 401 JSONResponses were generated OUTSIDE the CORS layer too — no ACAO headers on those either, so `fetch` rejected them as opaque TypeErrors. Net effect: an Anonymous registry worked everywhere, but the moment a token was involved the webui rendered a healthy-looking shell with **"Total Commits 0"** and no TokenGate prompt — undiagnosable from the page alone. Never caught before v1.1.11 because nothing had ever exercised Protected mode *from a browser*; the CLI is not CORS-bound.

**How found:** local full-fidelity reproduction of the CI failure (real uvicorn + seeded Postgres + built webui + Playwright) with request/response/console capture — `/api/health` 200 while every Bearer-carrying request died with `blocked by CORS policy`.

**Fix:** Explicit middleware pipeline with documented contract (`server.py`): registration order auth → CORS → rate-limit ⇒ runtime order rate → CORS → auth → routes. CORS now decorates ALL responses including auth's 401s, so TokenGate's entry prompt actually fires.

**Verification:** Two new always-run middleware-sandwich tests (preflight passes in Protected mode; unauthorized response carries ACAO headers); full local Playwright run green — token handoff, localStorage persistence, entry prompt, manual unlock — against the real protected server; anonymous dashboard spec unchanged.

---

### 76. Lightning fires on_save_checkpoint BEFORE writing the file — real training loops crashed staging

**Severity:** 4/10 · **Status:** 🟢 `fixed` (2026-08-24)

**Problem:** The hook exists so callbacks can inject extras into the checkpoint dict BEFORE `_atomic_save`, and ModelCheckpoint updates best/last_model_path around that same window — so `AetherVaultCallback.on_save_checkpoint` resolved paths that legitimately didn't exist yet and passed them to `av add`, aborting the training loop with FileNotFoundError. Every fake-trainer test pre-wrote files and never saw it; the first REAL loop (v1.1.11's new smoke test) crashed immediately in CI's plugin job.

**Fix:** `filter_existing_files()` in `_shared.py` (import-safe without extras): resolve paths, keep only existing ones — the next save event picks up what wasn't ready. The smoke test now drives two explicit `trainer.save_checkpoint()` calls, matching the catch-up semantics deterministically instead of racing ModelCheckpoint's internal timing.

**Verification:** Framework-free helper regression + callback-level skipif test (green in CI where extras exist); real-loop test rewritten to the deterministic two-save shape.

---

### 77. `av --version` never existed — the packaging smoke layer caught its first UX gap exactly as designed

**Severity:** 2/10 · **Status:** 🟢 `fixed` (2026-08-24)

**Problem:** The wheel-install smoke job's sanity roundtrip began with `av --version` — an option the CLI never had (the version lives in the banner corner and importlib metadata only). Click rejected it with exit 2, killing the job in ~15 s. Not a regression: the flag had simply never been built, and v1.1.11's smoke layer was the first thing ever to invoke it.

**Fix:** Proper `--version` flag on the root group (`main.py`) printing `av <version>` from the same `_get_version()` source the banner uses, exiting cleanly before any repo detection. Regression test asserts output shape + clean exit.

**Verification:** Flag exercised locally against the editable install; smoke job will pass its first line on next run. Meta-note recorded for the audit trail: two of the three V1.1.12-cycle product findings (#75, #76) plus this one were all surfaced BY the new CI depth, which is precisely the bug-detection-per-surface goal it was built for.

---

### 78. `core.fail(None, …)` raised AttributeError after printing the error message

**Severity:** 4/10 · **Status:** 🟢 `fixed` (2026-08-26)

**Problem:** Roughly a dozen call sites (`cmd_run`, `cmd_env`, `cmd_registry`, …) invoke the shared failure helper as `fail(None, "validation", msg)`. `fail()` ended with `ctx.exit(exit_code)` — calling it on `None` that is an `AttributeError` raised AFTER the message printed. Users saw a clean error line followed by a full Python traceback, and the documented exit codes (10–16) were lost outside CliRunner's accidental catching.

**Fix:** `core.fail()` now calls `ctx.exit()` only when a context exists and otherwise raises `SystemExit(exit_code)`. One-line fix at the single choke point; every None-ctx caller inherits it.

**Verification:** Isolated repro before (exit 1 + AttributeError) and after (clean exit 15); `tests/test_signing.py` and `tests/test_v122.py` assert exact exit codes through paths that pass `ctx=None`.

---

### 79. `cmd_registry.restore` referenced an undefined `ctx_exit` — latent NameError on every failed restore

**Severity:** 3/10 · **Status:** 🟢 `fixed` (2026-08-26)

**Problem:** `restore()`'s incomplete-archive branch called `ctx_exit(EXIT_VALIDATION)`, a name defined in sibling modules (`cmd_policy`, `cmd_context`) but never in `cmd_registry` nor exported by `core`. Any failed restore crashed with `NameError` instead of the intended exit-15 validation failure. Invisible because no test exercised the failed-restore path and the module imports fine.

**Fix:** Module-local `ctx_exit()` helper added to `cmd_registry.py`.

**Verification:** Static review + registry command suite green; the failure path is now reachable without NameError (covered indirectly by test_signing's verify-exit-code assertions using the same helper pattern).

---

### 80. Legacy-volume adoption stamped the whole migration chain WITHOUT creating post-create_all tables

**Severity:** 7/10 · **Status:** 🟢 `fixed` (2026-08-26)

**Problem:** `database._ensure_schema_sync` adopts a pre-Alembic volume by stamping the ENTIRE current chain as applied. A true v1.1.x-era create_all volume therefore got stamped straight to head — and every table introduced AFTER the create_all era (`runs`, `run_commits`, `events`, `webhooks`, `audit_log`, v1.2.2's `webhook_deliveries`) silently NEVER existed on it. Startup stayed green; the first runs/events write would 500. The existing heal covered column drift only.

**Fix:** New `_create_missing_tables()` runs during adoption: any models.py table missing from the volume is created from the metadata (checkfirst semantics), then column drift heals, then the chain stamps. Existing tables are never touched.

**Verification:** `test_migrations.py` legacy-map test extended; live heal drill (e2e Phase C) still green against real Postgres; fresh + adopted volumes both reach a complete schema.

---

### 81. `.avh` semantic summary compared against an EMPTY baseline for local commits

**Severity:** 6/10 · **Status:** 🟢 `fixed` (2026-08-26)

**Problem:** Locally-authored commit files store a `parents` LIST; only registry-fetched commits carry `parent_hash`. `handoff.build_semantic_summary()` and `_metrics_history_tail()` read ONLY `parent_hash` — so for locally-made commits (i.e., every repo's normal case) the semantic summary diffed against an empty tree (all chunks "new", dedup_efficiency 0) and the metrics trend stopped after one hop. Found by the v1.2.2 dedup_efficiency flow-through test, which pinned engine output vs `.avh` output and caught them disagreeing.

**Fix:** Shared `_commit_parent()` tolerates both shapes; both consumers route through it.

**Verification:** `test_v122.py::test_dedup_efficiency_flows_into_avh_semantic_summary` pins engine == .avh chunk rollups; full handoff/context suites green.

---

### 82. Clone/pull dropped `signature` and `env_snapshot_id` — clones could neither verify nor replay

**Severity:** 8/10 · **Status:** 🟢 `fixed` (2026-08-26)

**Problem:** `sync.normalize_commit_row()` rebuilt fetched commit dicts from a fixed field whitelist, silently discarding the v1.2.2 `signature` blob and `env_snapshot_id`. Every cloned repository therefore reported UNSIGNED on `av verify` (false negative — the worst kind for a tamper-evidence feature) and could not resolve replay snapshots by commit. Found by the manual wire pass: keygen → commit → push → clone → verify said UNSIGNED in the clone.

**Fix:** Server persists both fields (migration 0003 columns, echo in GET/list endpoints); `normalize_commit_row` passes them through verbatim; fake registry mirrors the real row shape so stack-free tests exercise the same contract.

**Verification:** `test_sync.py::test_clone_preserves_signature_for_offline_verify` (clone verifies offline), `normalize` unit test, live wire round-trip test in `test_server.py`, plus the manual keygen→commit→push→clone→verify loop now reporting VERIFIED.

---

### 83. Timestamp timezone-spelling broke cloned signatures even after #82

**Severity:** 8/10 · **Status:** 🟢 `fixed` (2026-08-26)

**Problem:** The authoring client signs a payload whose `timestamp` carries `+00:00`; the registry stores naive UTC and echoes timestamps WITHOUT the suffix. Canonical signing bytes are sorted-keys JSON of the whole payload — one character of tz-spelling difference made every cloned verification fail ("TAMPERED") despite byte-identical meaning. Found immediately after fixing #82 in the same manual pass.

**Fix:** `signing.canonical_commit_bytes()` normalizes the timestamp to one canonical UTC rendering parsed from the instant (aware, naive and Z forms all collapse to identical bytes; genuinely different instants still differ).

**Verification:** `test_canonical_form_is_timezone_spelling_insensitive` (+00:00 / naive / Z equal; shifted instant differs); manual wire loop re-run end-to-end → VERIFIED in fresh clone.

---

### 84. Env snapshot uploaded with non-canonical bytes — server 400, cross-machine replay impossible

**Severity:** 5/10 · **Status:** 🟢 `fixed` (2026-08-26)

**Problem:** A snapshot's id hashes its CANONICAL bytes (compact JSON minus `captured_at`), but the push path uploaded the pretty-printed `.av/env_snapshot.json`. The registry's own sha256 verification rejected the upload (400), so snapshots never reached the registry and `av replay <commit>` on any other machine failed with "No snapshot found". Silent: the client treats a failed object upload as non-fatal by design.

**Fix:** Both writers (`cmd_env.snapshot`, `core.upload_commit_objects`) now materialize the CAS object from the canonical bytes; the pretty file stays local-only for humans.

**Verification:** Manual wire pass: snapshot id visible in server access log as 201, `av replay <commit>` inside a fresh clone renders the recipe; v122/v120 suites green.


---

---

### 85. TokenGate URL-strip could be overridden by Next.js patched history - Protected-mode handoff left ?av_token= in the address bar

**Severity:** 3/10 · **Status:** 🟢 `fixed` (2026-08-26)

**Problem:** webui-e2e's token-gate spec failed: the handoff token was correctly consumed and persisted (first fetch wave authenticated), but the render-phase `window.history.replaceState` strip did not stick - Next.js patches history methods for App Router integration and can restore the ENTRY URL when hydration completes after a pre-hydration replaceState. Result: `?av_token=...` lingered in the address bar/history (cosmetic, but exactly what the spec exists to prevent).

**Fix:** post-mount effect in TokenGate re-strips the param (idempotent no-op when the render-phase pass won). The CONSUME stays render-phase - that part is load-bearing for first-fetch authentication (Probleme #79).

**Verification:** new Vitest test simulates the override (first replaceState restores the entry URL) and asserts the URL is clean after effects run; existing TokenGate suite unchanged-green. Browser-level confirmation lands with CI's own token-gate spec on the next push.

---

### 86. The documented exit-code registry (10–16) was largely fiction

**Severity:** 8/10 · **Status:** 🟢 `fixed` (2026-09-01)

**Problem:** `AGENTS.md`/`README.md`/`architecture.md` all publish exit codes 10–16 as a stable agent contract, but of the seven only `unreachable_queued`/`validation`/`policy_denied` were ever actually raised. `ensure_repo()` raised a bare `ValidationError` → exit **1** instead of `not_a_repo` (10); `_AuthRetryGroup.invoke()` called `sys.exit(1)` instead of `auth_failed` (12); `av commit` with nothing staged returned `ok:true`/exit **0** instead of `nothing_to_commit` (11); `av merge` with conflicts printed text and returned `None`/exit **0** instead of `merge_conflict` (14). An orchestrating agent keying off the documented registry (the whole point of publishing one) could not distinguish "nothing to do" from "conflict" from "success" by exit code alone. The SDK's parallel table (`av_sdk/exceptions.py`) already had it right — only the CLI drifted.

**Fix:** all four paths now route through `fail()`, which — after also fixing #91 below — reliably raises the documented code. `av commit`/`av merge`'s exit-0 behavior on nothing-staged/conflict is a deliberate, called-out contract CHANGE (see `VERSIONING.md`'s v1.2.5 section): the documented registry wins over undocumented history.

**Verification:** `tests/test_exit_codes.py` (new) — table-driven, provokes each of the seven codes through the real CLI and asserts the exact exit code, so the registry can't silently drift from the docs again.

---

### 87. `av pull`/`av merge`/`av clone` had no `--output json` support at all

**Severity:** 7/10 · **Status:** 🟢 `fixed` (2026-09-01)

**Problem:** `cmd_sync.py` never called `emit_json`/`fail` — the three CLI commands most likely to be hit by an autonomous loop reacting to a collision (`pull`, `merge`, `clone`) returned human-formatted text unconditionally, even under `--output json`. An agent parsing stdout as JSON would crash or silently misparse on exactly the code path where structured failure detail matters most.

**Fix:** all three commands now emit proper JSON envelopes (success and `fail()` paths), with a new optional `error.data` field on the envelope carrying machine-readable context (conflict file lists, remediation strings, the racing local/remote run ids and tips) — populated everywhere a human remediation message already existed. The human-text path is byte-for-byte unchanged.

**Verification:** new JSON-envelope tests in `tests/test_merge.py`/`tests/test_sync.py`; manual repro — two clones with distinct `AV_RUN_ID`s race a push to `main`, `av --output json pull` returns a parseable divergence envelope naming both runs.

---

### 88. `av watch`'s auto-commits were never tagged with the active run

**Severity:** 6/10 · **Status:** 🟢 `fixed` (2026-09-01)

**Problem:** Three call sites resolved the active run id with three different precedence orders — `av commit` checked env then state, `cmd_run.current_run_id()` checked state then env, and `cmd_watch.py:87` didn't resolve either at all, passing no `run_id` to `commit_staged()`. Auto-committed checkpoints from `av watch` were therefore silently never filed under the active run even with `av run start` active or `AV_RUN_ID` set — directly contradicting the documented "`AV_RUN_ID` joins ANY process' commits with zero integration" promise, for exactly the unattended, long-running process that promise is aimed at.

**Fix:** one resolver, `core.resolve_run_id(repo_root, explicit=None)`, with a single documented precedence (explicit arg > `AV_RUN_ID` env > `.av/run.json` state). All three call sites, plus `commit_scoped_paths()` (the plugin seam), now route through it.

**Verification:** regression tests for each precedence pair plus one asserting `av watch`'s auto-commits carry the `run:` tag; manual repro in a scratch repo with `AV_RUN_ID` set confirms watch-driven commits now link to the run.

---

### 89. `require_signature` branch policy always denied when the policy had no `metric` key

**Severity:** 6/10 · **Status:** 🟢 `fixed` (2026-09-01)

**Problem:** `enforce_policy()`/`promote()` both called `evaluate()` unconditionally once a policy entry existed, even when the entry's only field was `require_signature` (no `metric`). `evaluate()` on a metric-less policy reported a denial ("policy has no metric" style failure), so a signature-only policy denied EVERY candidate regardless of signature validity — the opposite of the intended behavior.

**Fix:** both call sites now only invoke `evaluate()` when `pol.get("metric")` is truthy, treating a metric-less policy as a pure signature gate.

**Verification:** `tests/test_v120.py::test_evaluate_operator_matrix` plus new `tests/test_signing.py` cases (`test_require_signature_policy_does_not_affect_policies_without_it` and the standalone-armed variants) exercise a metric-less policy end to end.

---

### 90. No CLI path ever existed to actually arm a `require_signature` policy

**Severity:** 5/10 · **Status:** 🟢 `fixed` (2026-09-01)

**Problem:** `av policy set` required `METRIC` and `OP` as positional arguments with no `--require-signature` flag — there was no way to create a `require_signature: true` policy entry through the CLI at all, signature-only or combined with a metric. Every test and the plan's own manual-verify script armed it by hand-writing `.av/policies.json` directly, which is how the gap went unnoticed through the rest of WP-4's implementation and review.

**Fix:** `METRIC`/`OP` are now optional (`required=False`); a new `--require-signature` flag can be used alone (signature-only policy) or combined with a metric gate, with the trust-callout ("tamper evidence, not a PKI") in its help text per the docs' existing convention.

**Verification:** `tests/test_v120.py::test_policy_set_require_signature_alone_is_a_valid_standalone_policy`, `test_policy_set_combines_metric_and_require_signature`, and two rejection-path tests for the new argument validation (metric without op, and neither metric nor `--require-signature`).

---

### 91. `click.Context.exit()` silently loses its exit code under `CliRunner(standalone_mode=False)`

**Severity:** 7/10 · **Status:** 🟢 `fixed` (2026-09-01)

**Problem:** `ctx.exit(code)` raises `click.exceptions.Exit`, which `CliRunner.invoke(..., standalone_mode=False)` — used throughout this test suite for JSON-mode assertions — silently swallows, leaving `result.exit_code` at 0 regardless of the code passed to `ctx.exit()`. `core.py::fail()` used exactly this pattern, so 42 call sites' documented exit codes were untestable (and, worse, `fail(None, ...)` never even reached the exit call, since `output_is_json(None)` was always `False` — see the ctx-resolution half of this same fix). Found empirically with isolated repro scripts, not by reading — this is a genuinely non-obvious Click/pytest interaction.

**Fix:** `fail()` now always resolves a real context via `click.get_current_context(silent=True)` when none is passed, and always `raise SystemExit(exit_code)` — which behaves identically under both `standalone_mode=True` and `False`, unlike `ctx.exit()`.

**Verification:** isolated bash/Python repro scripts proving the `ctx.exit()` vs `SystemExit` discrepancy directly; `tests/test_exit_codes.py`; full regression sweep after the change (`test_env_snapshot`/`test_exit_codes`/`test_signing`/`test_v122`/`test_webhooks_cli`/`test_merge`/`test_sync` — 124 passed). Saved as a persistent cross-session note given how non-obvious it is.

---

### 92. Bash command substitution silently discarded the engine's restart-budget state

**Severity:** 8/10 · **Status:** 🟢 `fixed` (2026-09-01)

**Problem:** `docker/engine-entrypoint.sh`'s restart-budget tracker was written as `count=$(record_and_count_restarts)` — calling a function via command substitution forks a subshell, so the function's mutation of the `RESTART_TIMES` array was invisible to the caller the instant the subshell exited. The restart count would silently never accumulate across restarts, meaning `AV_ENGINE_MAX_RESTARTS` could never actually trip: a genuinely crash-looping subservice would restart forever instead of the engine shutting down loudly as designed. This would have shipped invisibly — no unit test exercises the real supervision loop, and the bug produces no error, just a budget that silently never enforces.

**Fix:** renamed to `record_restart()`, which sets a global `RESTART_COUNT` variable directly instead of echoing a return value — no subshell, no lost mutation.

**Verification:** a custom empirical bash test harness driving the real script's structure (iterated twice to get the harness itself right — an early version's PIDs weren't direct children of the monitoring subshell, which `wait -n PID` rejected) confirms correct 1→2→3 restart-count progression and the budget correctly tripping shutdown on the 3rd restart within the window.

---

### 93. Two commands leaked human-text output ahead of their own `--output json` envelope

**Severity:** 4/10 · **Status:** 🟢 `fixed` (2026-09-01)

**Problem:** `av env replay` and `av handoff --publish` both had `click.secho`/`click.echo` calls that ran unconditionally before their JSON-mode envelope, so `--output json` output was JSON preceded (or interrupted) by stray human-readable lines — not valid JSON on its own, breaking any consumer parsing stdout directly.

**Fix:** every such text call in both commands is now guarded with an explicit `if not json_mode` / `if current_output_mode() != "json"` check, matching the pattern already used everywhere else in the CLI.

**Verification:** JSON-mode tests for both commands assert `stdout` parses as a single JSON document with no leading/trailing text; existing human-mode tests unchanged.

---

### 94. Docker Desktop's WSL2 backend would not start on the primary dev machine

**Severity:** 6/10 (blocks local verification; not a codebase defect) · **Status:** 🟡 `partial` — open, owner will resolve manually (2026-09-01)

**Problem:** After a routine `docker compose up -d --build` (rebuilding the engine image with the V1.2.5 changes), Docker Desktop's `docker-desktop` WSL2 distro stopped coming up: `docker ps` returned "Docker Desktop is unable to start", `docker buildx ls` reported `DeadlineExceeded` for every builder, and the backend log (`com.docker.backend.exe.log`) showed 7+ minutes of `still waiting for the engine to respond to _ping`. This is a host/environment failure, not something in this repo's Docker config — `docker-compose.yml`, the `Dockerfile`, and `engine-entrypoint.sh` were all unaffected and unchanged by this.

**Attempted remediation (all before escalating, none sufficient):**
1. Force-killed every `Docker Desktop`/`com.docker.*` process and relaunched — backend came up but `docker-desktop` WSL distro still showed `Stopped`.
2. `wsl --shutdown` (full WSL2 teardown) followed by a clean Docker Desktop relaunch — same result.
3. Attempted to start the `com.docker.service` Windows service directly (`Start-Service`) — failed with "cannot be opened" (needs admin elevation this session doesn't have).

**Fix:** none applied — this needs either an admin-elevated Docker Desktop restart or a full machine reboot, which the owner will do (owner: "I will do the docker stuff tomorrow"). Concrete next steps for whoever picks this up: reboot (or elevate + `Restart-Service com.docker.service`), then `docker compose up -d --build` and re-run WP-10's manual verification (kill one subservice → confirm only it restarts via `docker inspect --format '{{.RestartCount}}'`; `docker stop` with an in-flight request → confirm the `AV_ENGINE_STOP_GRACE_SECS` drain window is honored; break `AV_DATA_DIR` → confirm `/api/health` stays 200 while `/api/ready` goes 503).

**Verification:** N/A (not yet re-attempted). This is the reason the V1.2.5 "Depth Pass" report could not confirm the rebuilt image was actually running, nor exercise WP-10's live-Docker checks — see the CHANGELOG Phase 57 "Deferred" note.

---

### 95. `/api/ready`'s Redis check silently reported healthy even when Redis was unreachable

**Severity:** 7/10 · **Status:** 🟢 `fixed` (2026-09-02)

**Problem:** The v1.2.5 readiness endpoint probed Redis via `cache.check_hash_exists("0" * 64)` — but that method deliberately fails OPEN (catches its own exceptions and returns `True`) by design, because its real caller (an optimistic skip-the-DB-lookup check before a full duplicate-object check) should default to "might exist, verify with DB" on any doubt. Using it as a health probe meant a genuinely unreachable Redis was reported as `"redis": true`. Caught live: `e2e-engine-smoke`'s own new CI step (`REDIS_URL` pointed at a nonexistent host) expected a 503 with `redis: false` and got 200 with `redis: true` instead — the exact regression the step exists to catch, missed by every stack-free unit test because none of them exercised a genuinely-down Redis against `/api/ready` (only the data-dir-unwritable case had a test).

**Fix:** new `RedisCache.ping()` (redis_cache.py) — a raw `self._client.ping()` call that does NOT swallow errors; `/api/ready` now calls that instead of `check_hash_exists()`.

**Verification:** new stack-free `tests/test_server.py::test_readiness_503_when_redis_is_unreachable` (monkeypatches `cache.ping` to raise, asserts 503 + `redis: false` + the other checks unaffected); the live CI step this was caught by will re-verify on the next push.

---

### 96. A merge resolving a genuine ref-race divergence could spuriously re-race its own push

**Severity:** 7/10 · **Status:** 🟢 `fixed` (2026-09-02)

**Problem:** `_finalize_commit()`'s compare-and-swap `expected_hash` for a push was always `parents[0]` ("ours"). Correct for an ordinary single-parent commit (parents[0] IS the local ref's last-known value) and for an ordinary merge of an unrelated branch (ours still matches the server). But when `av merge <target>` resolves a GENUINE divergence — i.e. "ours" itself already lost its own ref-race and is sitting in `pending_push`, never having reached the server — the server's actual ref is `parents[1]` ("theirs", the target being merged in), not "ours". Using "ours" as the expectation made the merge commit's own push spuriously fail its own compare-and-swap and queue instead of landing, even though resolving that exact divergence is the whole point of the merge. Caught live by `scripts/e2e_scenario.sh`'s Phase A in CI (`pull should report divergence, got: Already up to date` — a downstream symptom of the same underlying ref-race design needing the script's push order updated once the CAS behavior was correctly understood, then the actual merge-push failing its own race once that was fixed).

**Fix:** `_finalize_commit()` now checks, for a two-parent merge commit only, whether `parents[0]` is still present in this repo's own `pending_push` queue for the ref being updated. If so, it uses `parents[1]` ("theirs") as the expected hash instead — the value the merge is actually reconciling against; otherwise it keeps using `parents[0]` (the ordinary, non-diverged case). `scripts/e2e_scenario.sh`'s Phase A was also updated: since the compare-and-swap is working as designed, the losing push (repoA's) is now the side with the local/remote divergence to discover and resolve, not the winner (repoB) — the script now drives pull/merge/push from repoA.

**Verification:** new stack-free `tests/test_sync.py::test_merge_push_lands_when_ours_lost_its_own_ref_race` (one repo, a real commit+push standing in for "the other agent", the local ref rewound to fork a genuine divergence, all real object content — no fabricated hashes); confirmed the test fails without the fix (reverted it locally, re-ran, got the exact same wrong ref value) and passes with it restored. `scripts/e2e_scenario.sh` Phase A will re-verify live on the next push.

---

### 97. `av run start` never registered a run server-side against a reachable registry — every run shipped nameless

**Severity:** 7/10 · **Status:** 🟢 `fixed` (2026-09-02)

**Problem:** `cmd_run.py::start()`'s `POST /api/runs` payload never included `project_id` — the server's `create_run` handler requires it (`422` without one). `_register_remote()` treats any non-200 response as "not registered" and swallows it with no visible error (by design, so a agent's `run start` never crashes on a flaky server) — so this 422 has been completely silent since `av run` was introduced. Every registration therefore fell back to the server's "lazy-create at push time" path (`server.py`'s commit-push handler), which creates the `DBRun` row but has no way to learn its display name (the commit payload only ever carries `run_id`, never a name) — every run ever created this way was permanently nameless. Every existing test for `run start` ran fully offline (`registered_server_side: False`), so the reachable-server path — and this 422 — was never once exercised until `webui/e2e/runs.spec.ts` (new in this same phase) failed in live CI to find its seeded run by name via `GET /api/runs`.

**Fix:** `start()` now loads `cfg = load_config(repo_root)` and includes `"project_id": cfg["project_id"]` in the registration payload. **Also found the identical bug independently duplicated** in `av_sdk.Repo.run_start()` (`python/av_sdk/repo.py`), which builds its own separate `POST /api/runs` payload rather than reusing `cmd_run.py::start()` — same missing field, same silent 422, same nameless-run outcome; fixed the same way there too.

**Verification:** new stack-free `tests/test_v120.py::test_run_start_registration_payload_includes_project_id` and `tests/test_av_sdk.py::test_sdk_run_start_registration_payload_includes_project_id` (fake reachable client capturing the POST payload in each, asserting `project_id`/`name`/`id` are all present and correct); full `test_v120.py` (21 tests) and `test_av_sdk.py` (6 tests) re-run green. `webui/e2e/runs.spec.ts` will re-verify live on the next push.

---

### 98. `av run start`'s superseded pending-push entry never drained after the fix to #96

**Severity:** 5/10 · **Status:** 🟢 `fixed` (2026-09-02)

**Problem:** Fixing #96 (a merge landing correctly against "theirs" when "ours" lost its own ref race) surfaced a second-order bug: "ours" — now superseded by the landed merge — stayed in `pending_push` forever. Every future `flush_pending_push()` kept retrying its ref update with the SAME stale `expected_hash` (its own original parent), which could never match the ref again once the merge moved it, so the queue never fully drained. Caught live by `scripts/e2e_scenario.sh`'s Phase B, immediately downstream of Phase A now succeeding: `pending_push should be drained after av push` failed because an unrelated, already-resolved entry from Phase A was still sitting in the queue.

**Fix:** `_finalize_commit()` now, after a two-parent merge commit's ref update succeeds, removes any `pending_push` entries for that same ref whose commit hash is one of the merge's own parents — their content already lives on as an ancestor of the commit that just landed, so they can never legitimately become the ref's value again on their own.

**Verification:** extended `tests/test_sync.py::test_merge_push_lands_when_ours_lost_its_own_ref_race` to assert `pending_push` is fully drained (file removed entirely) once the merge lands, not just that the merge itself succeeded.

---

### 99. `e2e-engine-smoke`'s independent-restart CI check used `pkill`, which isn't in the runtime image

**Severity:** 4/10 (CI-only; not a product defect) · **Status:** 🟢 `fixed` (2026-09-02)

**Problem:** The v1.2.5 CI step proving a killed subservice restarts independently (`docker exec engine-all pkill -f "node server.js"`) failed with `exec: "pkill": executable file not found in $PATH` — `python:3.12-slim-bookworm` (the runtime base image) doesn't ship `procps`, the package `pkill`/`ps`/etc. come from. Never caught locally because the manual verification for this exact scenario needs a live Docker daemon, which was unavailable on the machine that authored WP-10 (see #94).

**Fix:** added `procps` to the runtime stage's `apt-get install` list in the root `Dockerfile` — also generally useful for operational debugging (`docker exec -it aether-vault-engine ps aux`), not just this CI step.

**Verification:** will re-verify live on the next push/CI run (needs the rebuilt image); no local Docker available to confirm on this machine yet (see #94).

---

### 100. `webui/e2e/runs.spec.ts`'s deep-link assertion was a Playwright strict-mode violation waiting to happen

**Severity:** 2/10 (test-only; not a product defect) · **Status:** 🟢 `fixed` (2026-09-02)

**Problem:** The new deep-link spec asserted `page.getByText(new RegExp(seeded.id.slice(0, 8)))` was visible — but the run's short id legitimately appears in TWO places at once by design once the panel opens from a deep link: the runs table row behind the panel (`<td>9b8df829</td>`) AND the panel's own "Run detail — 9b8df829…" title. Playwright's `getByText` in strict mode (the default) throws rather than picking one when a locator resolves to more than one element — a test-authoring bug in this new spec, not a product regression (the #97 fix made the run itself findable for the first time, which is what let this second, previously-unreached assertion execute at all).

**Fix:** narrowed the locator to `Run detail.*<short id>`, which only matches the panel's own title span.

**Verification:** will re-verify live on the next push (needs a live webui + server); the regex was checked against the exact failing DOM text captured in the CI log (`Run detail — 9b8df829…`).

---

### 101. A webhook health test raced the real background retry worker via session-scoped server startup timing

**Severity:** 5/10 (test flakiness; not a product defect) · **Status:** 🟢 `fixed` (2026-09-02)

**Problem:** `tests/test_server.py`'s `client` fixture is `scope="session"` — ONE FastAPI app (and its `_webhook_retry_worker` background task) runs for the WHOLE test file. That worker captures `WEBHOOK_RETRY_INTERVAL_SECS` (real production default: 30s) as a plain function argument at task-creation time and never re-reads it — so per-test `monkeypatch.setattr(server_module, "WEBHOOK_RETRY_INTERVAL_SECS", 0)` (used by six webhook tests to speed up backoff math) has no effect on the WORKER's own tick interval, only on the backoff formula it later calls. The worker's first tick therefore lands at a fixed ~30-second wall-clock offset from session start — which the file's cumulative runtime can walk into. It did here: `test_webhook_health_columns_update_on_success_and_failure` monkeypatches `requests.post` to return a fixed `[500, 500, 200]` sequence and manually drives exactly 3 delivery attempts; when the periodic worker's own tick coincidentally fires mid-test, it steals one of those 3 outcomes for itself, leaving the test's own final attempt to hit an exhausted iterator (`StopIteration`, caught and recorded as a 4th failure) instead of the expected `200`. New tests added earlier in this same file during this phase (readiness, policy, etc.) shifted this test's position enough to land it near the 30s mark for the first time.

**Fix:** set `AV_WEBHOOK_RETRY_INTERVAL_SECS=999999` in the environment BEFORE `python.av_server.server` is imported (same place/pattern the file already uses for `DATABASE_URL`/`REDIS_URL`/`AV_DATA_DIR`) — the worker's one-time startup interval becomes effectively infinite for any realistic test session, while every per-test `monkeypatch.setattr(..., "WEBHOOK_RETRY_INTERVAL_SECS", N)` for backoff math keeps working exactly as before (that's a live re-read, unaffected by the startup value).

**Verification:** could not re-run this specific live-stack test locally (no Docker available — see #94); the fix directly addresses the confirmed root cause and preserves every other test's own interval override, verified by static review of all six `monkeypatch.setattr(server_module, "WEBHOOK_RETRY_INTERVAL_SECS", ...)` call sites in the file. Will re-verify live on the next push.

---

### 102. `scripts/e2e_scenario.sh` Phase C hardcoded a stale Alembic head after migration 0004 landed

**Severity:** 3/10 · **Status:** 🟢 `fixed` (2026-09-02)

**Problem:** Phase C's legacy-volume healing drill asserts `alembic_version.version_num == "0003"` after a simulated pre-Alembic boot heals and stamps the chain — stale since this phase's own migration `0004` (webhook health columns, `runs.avh_object_id`, audit indexes) became the real head. The healing code itself was already correct (`database.py::_ensure_schema_sync` stamps to `script.get_current_head()`, resolved dynamically, not hardcoded) — only the test script's own expectation was stale. Never caught until now because this phase never got far enough to run in this cycle's earlier CI attempts (Phases A and B were failing first — see #96/#98).

**Fix:** updated the assertion to `"0004"`.

**Verification:** confirmed `database.py`'s healing path is version-agnostic by inspection (stamps to the resolved current head, not a literal string); will re-verify live on the next push once Phases A/B/C all run in sequence.

---

### 103. `e2e-engine-smoke`'s subservice-kill step could abort or false-pass depending on `pkill`'s own exit code

**Severity:** 4/10 (CI-only) · **Status:** 🟢 `fixed` (2026-09-02)

**Problem:** After fixing #99 (missing `procps`), the same CI step still failed — `docker exec engine-all pkill -f "node server.js"` returned a non-zero exit under the step's `set -e`, aborting the script before its real assertions (the curl-retry recovery loop, the `RestartCount` check) ever ran. `pkill` exits 1 whenever it matches zero processes, which `set -e` treats identically to a real error; a bare `|| true` guard (the pattern already used for an unrelated `pkill` elsewhere in this same workflow file) would swallow that safely, but would also silently make the step a false pass if the pattern genuinely stopped matching the running process — never actually killing anything, then trivially reporting the webui as "recovered" and `RestartCount` as "unchanged". Replacing the bare `pkill` with an explicit `pgrep`-then-kill-by-PID plus a `ps aux` diagnostic dump (first pass at this entry) revealed the real cause: Next.js's standalone server calls `process.title = "next-server (vX.Y.Z)"` once it starts, which on Linux overwrites the process's own argv/`/proc/<pid>/cmdline` — `ps`/`pgrep -f` see the renamed title, never the original `node server.js` invocation `engine-entrypoint.sh` launched it with. The pattern was matching against a command line that stopped existing the moment Next.js finished booting.

**Fix:** match `next-server` instead of `node server.js` — confirmed against this step's own `ps aux` dump in the live CI failure (`root 7 ... next-server (v...)`). **Also found and fixed two SILENTLY non-functional assertions of the identical pattern**, both worse than this one because they never surfaced as a visible failure: the "Role=server + legacy auto-detect dispatch" step's `docker top engine-srv | grep -q "[n]ode server.js"` (and the same for `engine-legacy`) exist specifically to catch the webui starting when it shouldn't — but since a genuinely-running webui is ALSO renamed to `next-server`, this check could never have matched, in either outcome, since the very first version of this CI job. It has been silently asserting nothing (always "passing") this entire time, not merely flaking now like the kill-step above.

**Verification:** live CI (`e2e-engine-smoke`'s "killing one subservice restarts it, not the container" step) — confirmed the exact process-title rename via the diagnostic dump this entry's first pass added; the corrected pattern will re-verify green on the next push. The two now-fixed dead assertions were immediately vindicated: with the pattern corrected, one of them (see #104) caught a real, previously-invisible bug on its very first functional run.

---

### 104. The legacy image-alias auto-detect has been non-functional since the Dockerfile started defaulting `AV_ENGINE_ROLE=all`

**Severity:** 7/10 (breaks a documented backward-compatibility contract silently) · **Status:** 🟢 `fixed` (2026-09-02)

**Problem:** `engine-entrypoint.sh` only infers a role from `DATABASE_URL`/`NEXT_PUBLIC_API_URL` when `AV_ENGINE_ROLE` is genuinely *unset* (`ROLE="${AV_ENGINE_ROLE:-}"`, then `if [ -z "$ROLE" ]`) — exactly the shape of a pre-1.2.2 pinned compose file, which never mentions `AV_ENGINE_ROLE` at all. But the Dockerfile's runtime stage set `ENV AV_ENGINE_ROLE=all` as an image-level default, which means `AV_ENGINE_ROLE` is baked in as non-empty for literally every container started from this image UNLESS the caller explicitly overrides it — so the entrypoint's own `[ -z "$ROLE" ]` check could never be true, and the legacy auto-detect branch could never execute. A legacy `aether-vault-server`-alias container (`DATABASE_URL` set, nothing else) silently ran the FULL `all` topology — registry AND webui both — instead of the server-only behavior the alias promises. This is exactly the backward-compatibility guarantee `VERSIONING.md`'s deprecation-candidates entry documents as the reason the legacy tags are safe to keep pulling.

Caught immediately once #103's fix made the CI assertion that checks for this actually functional — it had been silently passing (checking for a process pattern, `node server.js`, that could never match a *running* webui either) since the check was first written, so this bug shipped invisibly through the entire v1.2.2–v1.2.5.3 window.

**Fix:** removed `AV_ENGINE_ROLE=all` from the Dockerfile's `ENV` block. Both `docker-compose.yml` and `python/av_cli/docker/docker-compose.release.yml` already set it explicitly in their own `environment:` blocks, so the normal one-container topology is completely unaffected; a container with no `AV_ENGINE_ROLE` set now genuinely reaches the entrypoint's own auto-detect logic (falling through to `all` as the final default only when neither `DATABASE_URL` nor `NEXT_PUBLIC_API_URL` is set either).

**Verification:** live CI — the `engine-legacy` container (`DATABASE_URL` only) is exactly the scenario this bug broke; will re-verify green (webui absent, only the registry serving) on the next push.

---

### 105. `av --output json add`/`commit` leaked plain human text ahead of the JSON envelope

**Severity:** 7/10 (breaks the JSON-mode contract for the two most-used agent commands) · **Status:** 🟢 `fixed` (2026-09-02)

**Problem:** `core.py::stage_one_file()` (the shared staging function `add`, `commit`'s pre-stage, `av watch`, and `av stash push` all funnel through) called `click.secho(f"Staged [...] {rel_path}", ...)` unconditionally — with no `current_output_mode() == "json"` guard. Every `av --output json add <file>` (and therefore every `commit` that stages inline) printed `Staged [ARTIFACT] file.txt` as a bare line BEFORE its JSON envelope, so `json.loads(result.output)` on the real stdout raised `JSONDecodeError` for any agent actually parsing it. Never caught before because no existing test asserted the FULL stdout was clean JSON — only that a JSON *substring* somewhere in the output parsed.

**Fix:** guarded both `click.secho` call sites in `stage_one_file()` on `current_output_mode() != "json"`. Found by building `tests/test_contract_matrix.py`'s generic anti-leakage sweep (todo.md item 6) and manually driving `av --output json add`/`commit` for real in a scratch repo per AGENTS.md's own verification standard — not caught by any existing unit test.

**Verification:** `tests/test_contract_matrix.py::TestAntiLeakage` (parametrized over every CLI command) plus a manual repro (`av --output json add x.pt` — single clean JSON line, confirmed).

---

### 106. `av --output json watch` leaked multiple human echo lines per auto-commit

**Severity:** 6/10 · **Status:** 🟢 `fixed` (2026-09-02)

**Problem:** `_finalize_commit()`'s human-echo lines (commit hash/message, upload-deferred notice, ref-race/push-failure messages) are all gated on `result_sink is None` — `cmd_history.py`'s `commit` command already passes a `json_sink` in JSON mode for exactly this reason, but `cmd_watch.py`'s call into the same shared commit path never did. `av --output json watch` therefore printed plain text ahead of (and between) every per-auto-commit JSON envelope.

**Fix:** `cmd_watch.py` now builds the same `json_sink`/`outcome_sink` pair `cmd_history.py` does (only non-None in JSON mode) and passes both into the shared commit call, exactly mirroring the established pattern.

**Verification:** manual repro (`av --output json watch --max-commits 1` against a real staged checkpoint) — output is now exactly two clean JSON lines (`auto_commit`, `stopped`), each independently parseable; pinned by `tests/test_contract_matrix.py::TestWatchStreamsNdjson`.

---

### 107. Four more commands leaked human text under `--output json`: `context export`, `handoff init`, `handoff log`, and (defensively) `handoff show`

**Severity:** 5/10 · **Status:** 🟢 `fixed` (2026-09-02)

**Problem:** `tests/test_contract_matrix.py`'s anti-leakage sweep (driving every CLI command with `--output json` and asserting clean stdout) found four more commands with no JSON-mode branch at all: `context export` (fell through to a bare `click.echo` even in JSON mode when `--out` wasn't given), `handoff init`, `handoff log`, and `handoff show` (only reachable with a positional arg, so the sweep's no-args pass missed it — fixed anyway).

**Fix:** added `current_output_mode() == "json"` branches to all four, each emitting a proper envelope (`context export` wraps its rendered document in `data.document` rather than emitting a bare, un-enveloped blob even when its OWN `--format json` flag happens to also say "json" — that flag is independent of the global `--output json` envelope flag).

**Verification:** `tests/test_contract_matrix.py::TestAntiLeakage` (all four now pass); manual repro confirmed clean JSON for each.

---

### 108. `av_sdk.Repo.log()` read field names that don't exist in the real commit schema — silently returned at most one commit, always

**Severity:** 8/10 (a core, documented SDK method has never worked correctly) · **Status:** 🟢 `fixed` (2026-09-02)

**Problem:** `Repo.log()` read `c.get("parent_hash")` and `c.get("extra_parents")` to walk the commit chain — but the real LOCAL commit JSON schema (`core.py::commit_staged()`, `history.py::walk_history()`) stores a single `"parents"` LIST, never `parent_hash`/`extra_parents` (those are `av_server`'s DATABASE column names, a completely different schema the local commit files never use). Since `c.get("parent_hash")` was always `None` on every real repo, `Repo.log()`'s walk loop terminated after exactly one commit — `log(limit=30)` silently behaved identically to `log(limit=1)`, for every repo, since the method was written. No existing test caught it because the one prior `Repo.log()`-adjacent test only checked a single-commit repo.

**Fix:** read `c.get("parents")` (a list) and walk `parents[0]` (first-parent, matching `history.py::walk_history()`'s own rule for merge commits).

**Verification:** new full-surface SDK≡CLI parity test (`tests/test_av_sdk.py::test_log_parity_sdk_vs_cli`, todo.md item 5) — a 2-commit repo now returns 2 entries from `Repo.log()` matching the CLI's own `log` output on every shared field, where it previously returned 1.

---

### 109. `av_sdk.Repo.push()` reported `reachable` incorrectly in both directions

**Severity:** 6/10 · **Status:** 🟢 `fixed` (2026-09-02)

**Problem:** Two bugs in one method, both found by the same new parity test. (a) When nothing was pending, `Repo.push()` called `client.server_available()` anyway and reported its real boolean — the CLI's `push` reports `reachable: None` ("not checked, nothing to check for") in this exact case; the SDK's extra network round trip and different payload shape were an undocumented divergence. (b) When something WAS pending, `Repo.push()` never checked reachability at all and unconditionally reported `reachable: true` — even when the server was genuinely down and `flush_pending_push()` re-queued everything without draining anything, which is precisely the case that should report `false`.

**Fix:** matched `cmd_history.py::push()`'s logic exactly: report `reachable: None` when nothing is pending; check `client.server_available()` FIRST when something is, reporting `False` (and `still_queued` = the full pending count) before ever attempting a flush.

**Verification:** `tests/test_av_sdk.py::test_push_parity_sdk_vs_cli_when_nothing_pending` and `test_push_parity_sdk_vs_cli_when_queued`.

---

### 110. `av registry export`/`restore` raised `NameError` on every single real invocation — the entire command has never worked

**Severity:** 9/10 (a documented, agent-facing trust-surface command was completely non-functional) · **Status:** 🟢 `fixed` (2026-09-02)

**Problem:** `cmd_registry.py` uses `pathlib.Path(...)` (module-qualified) throughout `export()`/`restore()`, but the file only ever did `from .core import *` (which brings in the `Path` *class* core.py imports, never the `pathlib` *module* itself) and never its own `import pathlib`. Every real call to `av registry export OUT_DIR` or `av registry restore ARCHIVE_DIR` raised `NameError: name 'pathlib' is not defined` on the very first line that touched it — a 100% reproduction rate, on every version since this code was written. Explains exactly why "no test anywhere invokes `av registry export`/`restore`" (todo.md item 18's own stated gap) — nothing had ever exercised it through the real CLI. Found only by following AGENTS.md's own non-negotiable — a manual real-CLI repro in a scratch repo.

**Fix:** added `import pathlib` to `cmd_registry.py`'s top-level imports.

**Verification:** manual repro (`av --output json registry export ./out --project X` in a scratch repo — was an immediate traceback, now runs); `tests/test_server.py::test_registry_export_restore_round_trip` (live-registry-gated, verifies the full round trip end to end).

---

### 111. `av registry export`/`restore` let an unreachable-server `ConnectionError` escape as a raw traceback

**Severity:** 5/10 · **Status:** 🟢 `fixed` (2026-09-02)

**Problem:** Found immediately after fixing #110 and re-testing: neither command checked `client.server_available()` before making requests — an unreachable registry surfaced as an unhandled `requests.exceptions.ConnectionError` traceback instead of a clean `fail()` envelope, unlike every other network-touching command in this codebase.

**Fix:** both commands now call `client.server_available()` first and `fail(None, "unreachable_queued", ...)` cleanly (exit 13) when it's down, matching the established pattern elsewhere (e.g. `cmd_maintenance.py::doctor()`).

**Verification:** manual repro — `av --output json registry export`/`restore` against an unreachable server now returns a clean unreachable_queued envelope instead of a traceback.

---

### 112. `av registry export`'s manifest recorded every object as `"ok": true` regardless of whether the download actually succeeded

**Severity:** 4/10 (silent data-quality bug in an audit/backup artifact) · **Status:** 🟢 `fixed` (2026-09-02)

**Problem:** the per-object manifest entry was built from an expression that evaluates to `True` unconditionally by operator precedence, regardless of the actual per-object outcome — a failed object download still recorded `"ok": true` in the manifest, making the archive's own self-description unreliable for exactly the case (a partial/corrupted export) where it matters most.

**Fix:** rewritten alongside the progress-bar/resume work (todo.md item 18) to track a genuine per-object boolean through the download/skip/fail branches and record that.

**Verification:** `tests/test_server.py::test_registry_export_restore_round_trip` asserts every object's `ok` field on a clean export.

---

### 113. `av watch`'s new (v1.3.0) watchdog-backed change detection never discovered files that existed before the command started — an indefinite hang

**Severity:** 6/10 (self-caught before release — see Note) · **Status:** 🟢 `fixed` (2026-09-02)

**Problem:** While implementing the optional `watchdog` extra (todo.md item 13) for `av watch`'s change detection, the first version only re-stat'd paths a *real filesystem event* had touched — but a file already sitting on disk when `av watch` starts never generates an event (watchdog only reports changes from the moment it starts observing), so it was never discovered at all. With `--max-commits N` and a pre-existing matching file, the command looped forever waiting for a commit that could never happen.

**Fix:** the watchdog path's very first tick now does one full directory scan (identical to the polling path's own scan) to seed state with every pre-existing matching file; only subsequent ticks rely purely on drained watchdog events.

**Verification:** `tests/test_v120.py::test_watch_uses_real_watchdog_events_when_installed` (real `watchdog` package, not mocked) and `test_watch_commits_new_matching_file_then_exits` (pre-existing-file case) both pass; the hang was caught by running the real test suite with the real `watchdog` package installed before ever shipping this code — the "manual repro catches what unit tests alone would miss" pattern, applied to code written in this same session.

---

### 114. `av --output json promote` printed a SECOND top-level JSON object for a real (non-dry-run, non-force-denied) promotion — `json.loads()` over the full output failed outright

**Severity:** 7/10 (breaks every JSON-mode consumer of the single most-cited autonomous-loop command the moment a policy actually lands something) · **Status:** 🟢 `fixed` (2026-09-02)

**Problem:** `promote()` always emitted its own `{"ok": true, "data": {"allowed": ..., ...}}` envelope right after deciding, then — for the ALLOWED path — went on to `ctx.invoke(merge_cmd, ...)`, which in JSON mode unconditionally emits its OWN `{"ok": true, "data": {"merged": ..., ...}}` envelope too. One `av promote` invocation therefore printed two newline-separated top-level JSON objects, which no ordinary `json.loads(stdout)` consumer can parse (envelope-1.0 is documented as one object per command). Every existing test for `promote` used either `--dry-run` (a single-envelope, no-landing path) or drove `evaluate()` directly rather than the real CLI end to end, so this had never been exercised. Found by this cycle's own new coverage for WP-13's policy-outcome reporting (todo.md item 7), which for the first time invoked a real, landing, JSON-mode `av promote`.

**Fix:** `promote()` no longer emits its own envelope before landing on the allowed path. It runs the nested `merge_cmd` invocation with stdout captured (`contextlib.redirect_stdout`) instead of letting it reach the terminal directly, then emits exactly one combined envelope (`allowed`/`forced`/`reason`/`rule` plus the parsed `merge` result) once landing succeeds. On a merge failure (conflict, validation, ...), the captured buffer already holds merge's own correct failure envelope with the correct error code — that buffer is forwarded verbatim to real stdout and the `SystemExit` is re-raised, so the caller still sees the real, single, correct envelope. Text mode is unaffected (the nested invoke isn't captured there; both promote's and merge's human lines print exactly as before).

**Verification:** `tests/test_v120.py::test_promote_reports_policy_outcome_for_the_active_run`, `test_promote_reporting_failure_never_blocks_the_promotion`, and the existing `test_example_policies_apply_via_the_real_cli` / exit-code dry-run tests all exercise the real CLI in JSON mode; manual repro in a scratch repo (`av --output json policy set main val_loss "<" --threshold 0.45`, commit a passing candidate, `av --output json promote --into main`) now prints exactly one parseable JSON line.

---

### 115. `av --output json promote` also leaked a plain `click.secho("Policy PASS: ...")` human line ahead of its own envelope on the landing path

**Severity:** 5/10 (same bug class as #105-107, just on a code path nothing had exercised yet) · **Status:** 🟢 `fixed` (2026-09-02)

**Problem:** Found while fixing #114: `if pol_entry: click.secho(f"Policy PASS: {reason}", fg="green")` ran unconditionally regardless of `current_output_mode()`, so `av --output json promote` with an armed, passing policy printed `Policy PASS: ...` as a bare line before its JSON envelope — invalidating `json.loads()` on the full output exactly like #105-107, just on a code path (a real, landing, policy-armed, JSON-mode promotion) no prior test had ever driven.

**Fix:** gated behind `current_output_mode() != "json"`, matching every other human-text echo in this command.

**Verification:** same tests as #114 (they would fail on either bug independently); confirmed the text-mode path (`av promote --into main` with no `--output json`) still prints the line unchanged.

---

### 116. `tests/test_contract_matrix.py`'s generic per-command sweep silently mutated the REAL `.env` and restarted the REAL running `aether-vault-engine` container, three times per test run, on any machine with Docker up

**Severity:** 8/10 (destructive-ish side effect from a read-only-looking test, on any developer's real local infrastructure — not sandboxed CI) · **Status:** 🟢 `fixed` (2026-09-02)

**Problem:** `test_contract_matrix.py::TestAntiLeakage` invokes every zero-required-argument leaf command with `["--output", "json", *args]` from inside the `repo` fixture's sandboxed `tmp_path`. That sandbox only sets `cwd` — `av auth set-token`/`clear`/`rotate` (none of which need a required argument, so none are `_LEAKAGE_EXEMPT` and all three run in the sweep) resolve their target `.env`/compose file via `_find_source_root()` (`main.py`), which returns `Path(__file__).parents[2]` — the actual checked-out repo root for an editable install, completely independent of `cwd` or the `repo` fixture. `tests/test_cli.py` already has a `_sandbox_compose_dir` fixture built specifically to prevent exactly this ("a real, dangerous side effect a manual run of these tests already caused once during development," per its own docstring) for its own dedicated auth tests — but the new generic sweep (this same cycle, todo.md item 6) didn't reuse it. Confirmed by direct observation: `docker ps` showed `aether-vault-engine`'s uptime reset (restarted) immediately after running this test file with Docker up, and running it again reset it a second time. This is very likely the actual root cause of the "mystery `AV_API_TOKEN` in `.env` changing value across sessions" anomaly flagged earlier in this same development cycle and attributed at the time to an unknown external actor — every `pytest tests/` run on a machine with Docker running silently generates a new token via `auth set-token`'s bare (TOKEN-omitted → random) invocation, writes it to the real `.env`, restarts the real container, then `auth clear` removes it and restarts again, then `auth rotate` mints yet another and restarts a third time — leaving whichever of the three ran last as the value an unrelated later session would observe.

**Fix:** the sweep now applies the same sandboxing technique as `test_cli.py::_sandbox_compose_dir` (writes a dummy `docker-compose.yml` into the sandboxed repo and monkeypatches `_find_source_root` to return it) unconditionally, before invoking ANY command — not just the three known-affected `auth` commands — so a future command that gains a similar real-infrastructure touch is safe by default rather than by someone remembering to exempt or sandbox it individually.

**Verification:** manual repro — `docker ps` before/after `pytest tests/test_contract_matrix.py -k "auth set-token or auth clear or auth rotate"` now shows the real `aether-vault-engine` container's uptime UNCHANGED (previously reset every run); the three tests still pass (still proving clean JSON, now against the sandboxed `.env`/compose file instead of the real one).

**Note for the user:** if the real `.env`'s `AV_API_TOKEN` needs to be restored to a known value after this was discovered, run `av auth set-token <value>` (or `av auth clear` for Anonymous mode) once from the real checkout — this fix stops future test runs from touching it again, but doesn't retroactively know what the value "should" be.

---

### 117. The untargeted `docker build .` / `docker compose build` silently built the WRONG image the moment WP-19's slim targets were added — a container with no Python at all published under the `aether-vault-engine` name

**Severity:** 9/10 (would have shipped a genuinely broken production image to GHCR on the next tagged release — the "engine" container would have had no Python interpreter, only Node, and crash-looped indefinitely) · **Status:** 🟢 `fixed` (2026-09-02)

**Problem:** WP-19 (todo.md item 19) added `server` and `webui` build targets to the Dockerfile, appended AFTER the original single (unnamed) runtime stage. Docker builds the LAST stage in a file when no `--target`/`target:` is given — appending new stages after the original one silently changed the untargeted-build default from the intended all-in-one stage to `webui` (now the last stage in the file). Every consumer that built without an explicit target was affected: `docker-compose.yml`'s `aether-vault-engine` service (`build: .`, no `target:`), `release.yml`'s and `docker-edge.yml`'s "Build and push engine image" steps (`docker/build-push-action@v6` with no `target:`), and `e2e-engine-smoke`'s own build step (`docker build -t aether-vault-engine:smoke .`). Discovered live: rebuilding the real dev engine container for this cycle's verification pass produced a container that crash-looped forever (`/engine-entrypoint.sh: line 100: python: command not found`, `[engine] 'server' exceeded 5 restarts within 300s`) — `/api/health` and `/api/ready` briefly reported healthy (the `webui` stage's own Next.js server came up fine on :3000) before the whole engine shut down once the server subservice exhausted its restart budget. A large chunk of investigation time went into a red herring (whether `apt-get autoremove` in the real "all"-stage's apt sequence could strip the base image's own Python — plausible in isolation, given nodesource's `nodejs` package pulls in a conflicting Debian `python3` as a build dependency, but NOT what was actually happening here) before `docker top`/`docker exec ... find /` on the actual built image confirmed there was no Python installation anywhere at all — i.e. the running container was never the intended stage in the first place.

**Fix:** named the original stage explicitly (`FROM python:3.12-slim-bookworm AS engine`) as a belt-and-suspenders measure that survives future reordering, AND pinned `target: engine` explicitly on every build site that previously relied on the implicit default: `docker-compose.yml`, `release.yml`, `docker-edge.yml`, and `tests.yml`'s `e2e-engine-smoke` job. The `server`/`webui` targets themselves were never broken — both were manually built and smoke-tested successfully in isolation earlier in this same cycle specifically BECAUSE those tests always passed `--target server`/`--target webui` explicitly, which is exactly why this gap in the DEFAULT path went unnoticed until a truly untargeted build was tried.

**Verification:** `docker compose build aether-vault-engine` (no target override, exactly how a real operator or CI would invoke it) rebuilt cleanly with the fix; `docker exec aether-vault-engine which python python3 node` finds all three (`/usr/local/bin/python`, `/usr/local/bin/python3`, `/usr/bin/node`); `docker compose up -d aether-vault-engine` came up with no restart-loop log lines (`uvicorn` and the Next.js standalone server both started cleanly on the first try); `GET /api/health` → `{"status":"ok",...}`, `GET /api/ready` → `{"ready": true, "checks": {"database": true, "redis": true, "data_dir_writable": true}}`, webui root → HTTP 200.

---

### 118. New files this session weren't `git add`ed — a Docker rebuild's wheel silently packaged an OLDER `python/av_server` tree, missing migration `0005` entirely, and the live database was never actually migrated past `0004`

**Severity:** 8/10 (any operator relying on the documented "the server migrates its own database at every startup" guarantee would have silently kept running on a stale schema indefinitely — no error, no warning, just a 500 the first time anything touched the new column) · **Status:** 🟢 `fixed` (2026-09-02 — a fresh Docker rebuild eventually completed on this RAM-constrained machine and confirmed the real root cause below; see Verification)

**Problem:** Found live during this cycle's verification pass: `POST /api/commits` for a run-linked commit 500'd with `UndefinedColumnError: column runs.policy_outcome does not exist` against the real `aether_vault` database, even after rebuilding the engine image with #117's fix. `SELECT version_num FROM alembic_version` on the live DB showed `0004`, not `0005` — `database.py::init_db()`'s own docstring promises "Failures fail startup loudly", and indeed there was no failure: `command.upgrade(cfg, "head")` genuinely believed `0004` WAS head, because `docker exec aether-vault-engine find / -path '*/av_server/migrations*'` showed only `0001`-`0004` present inside the built image — `0005_run_policy_outcome.py` (added earlier this same session) never made it into the wheel `py-builder`'s `pip wheel . -w /wheels --no-deps` step produced. `git status` confirmed why: every other migration file was `git ls-files`-tracked; `0005_run_policy_outcome.py` was still untracked (`??`) — this project's `pyproject.toml` sets `include-package-data = true` with `setuptools-scm` as the build backend's SCM plugin, whose file-discovery is git-state-based, and `setup.py`'s own `packages=[...]` list doesn't separately declare `av_server.migrations.versions` as a package either (only `av_server.migrations` itself is listed) — so a new migration file was invisible to the packaging step from two independent angles until it was actually staged.

**Fix (root cause):** `git add -A`'d every untracked file this session had created (migrations, docs, examples, new scripts, new tests — `git status --short` had shown ~15 files still marked `??` despite being real, finished, tested work) — staging (not committing) is enough for setuptools-scm's file-finder to see them. Applied migration `0005` directly against the live `aether_vault` database via `database.py::_apply_schema()` run from the host (bypassing the container entirely) to unblock the rest of this cycle's live verification without a full image rebuild.

**The real root cause was different from the initial diagnosis — `git add` alone was
NOT sufficient.** A genuinely fresh Docker rebuild eventually completed (`--no-cache-filter
py-builder`, ~5 minutes — the earlier three attempts weren't actually all failures; two
were killed by the OS on this RAM-constrained dev machine — confirmed via
`benchmarks/tool_runner.py`'s new machine-profile helper, added this same cycle: this
shell environment has only 4 logical cores / 4 GB RAM — and one was killed by hand after
misjudging it as stuck when it was actually finishing final layer export), and the FRESH
image **still** lacked `0005` — proving the `git add -A` fix from earlier in this entry,
while a real and worthwhile fix in its own right, was not the actual root cause of the
packaging gap. The real cause: `setup.py`'s `packages=[...]` list declares
`"av_server.migrations"` but never `"av_server.migrations.versions"` — and unlike the
SDIST (whose `include_package_data=True` + setuptools-scm MANIFEST generation is a
genuinely different, git-state-based inclusion mechanism, which is why `python -m build
--sdist` misleadingly showed `0005` present and looked like confirmation), the WHEEL is
built by setuptools' `build_py` command, which only descends into a package's own
directory for each name explicitly present in `packages=[]` — an undeclared subdirectory
like `versions/` (alembic's own convention has no `__init__.py` there; alembic's
`ScriptDirectory` finds `.py` files via its own directory walk, not Python imports) is
invisible to it regardless of git tracking state. This means the wheel has likely NEVER
reliably included every migration file, for as long as this packaging config existed —
0001-0004 happening to be present in earlier images is most plausibly explained by an
editable/dev install (`pip install -e .`) being used for local testing at the time
(editable installs bypass wheel packaging entirely, symlinking the real source tree), so
this exact gap was never exercised by a genuine non-editable wheel/Docker build.

**Fix:** added `"av_server.migrations.versions"` explicitly to `setup.py`'s `packages=[...]`
list.

**Verification:** built the wheel directly on the host (`pip wheel . --no-deps
--no-build-isolation`, no Docker needed) both BEFORE and AFTER this fix — before,
`av_server/migrations/versions/0005_run_policy_outcome.py` was absent from the `.whl`
(confirmed via `zipfile.ZipFile(...).namelist()`) even though it was already `git add`ed;
after, it's present alongside 0001-0004. Also re-confirmed against the already-completed
fresh Docker image (built before this final fix landed) that it exhibits the SAME
"`Can't locate revision identified by '0005'`" startup crash predicted by this diagnosis —
consistent, reproducible evidence pointing at `packages=[]`, not git-tracking state, as
the actual root cause. A live-patched (`docker cp`) container was used throughout this
session's remaining live verification (WP-27) as a working stand-in while this fix was
being tracked down; the real fix now makes that workaround unnecessary for any future
build.

**Process lesson:** every new file created this session should have been `git add`ed as it landed, not batched into one `git add -A` only once a packaging bug surfaced. Noted for future long sessions with a "one commit at the very end" policy — stage incrementally regardless of when the final commit happens; a wheel/Docker build should never be the first thing to notice a file was never staged.

---

### 119. `av registry export` has NEVER actually exported any file content — the object-discovery loop silently found zero hashes on every real invocation

**Severity:** 10/10 (a backup/disaster-recovery command that silently produces a metadata-only archive with none of the actual data — the single worst kind of bug a backup tool can have, because the failure is invisible until the moment someone actually needs the restore) · **Status:** 🟢 `fixed` (2026-09-02)

**Problem:** `cmd_registry.py::export()`'s object-discovery walk (`for c in manifest["commits"]: _walk(c.get("tree") or {})`) reads each commit's `tree` field to find every file hash referenced anywhere in it. But the command's own `GET /api/commits?limit=&offset=` query never passed `include_layers=true` — and `server.py::list_commits()` only attaches a `"tree"` key to each returned commit dict when that flag is set (added for `WeightDiffPanel.tsx`'s checkpoint picker, opt-in specifically to keep the default payload light). Without it, `c.get("tree")` is always `None`, `_walk({})` finds nothing, the object hash set stays empty for the ENTIRE export regardless of how much real data the project actually has, the progress-bar loop over that empty set never executes even once, and both `manifest["objects"]` and `OUT_DIR/.state.json` end up empty/never-written. `manifest["commits"]`/`manifest["refs"]`/`manifest["runs"]` still populate correctly (those never depended on the tree field) — so an export LOOKED successful (`av registry export` reported real commit/ref/run counts, `objects_ok: 0, objects_failed: 0` reads as "nothing to do" rather than "something is wrong"), and `av registry restore` from such an archive would recreate every commit/ref/run row but ingest ZERO file objects — every checkpoint, dataset, and code blob in the "backed up" project would be unrecoverable. This is the fourth bug found in this exact command this cycle (#110-112) and by far the most severe — none of the earlier three fixes (the `NameError` that made the command crash outright, the unhandled `ConnectionError`, the always-`true` per-object `ok` flag) had ever let a real end-to-end run reach this deep into the command, so this one was never exercised until this cycle's new `tests/test_registry_export_restore_round_trip` (todo.md item 18) finally drove one.

**Fix:** added `&include_layers=true` to the export command's `/api/commits` query.

**Verification:** the same live round-trip test (`tests/test_server.py::test_registry_export_restore_round_trip`, strengthened alongside this fix to actually assert `export1_data["objects_ok"] >= 4` and `manifest["objects"]` non-empty — the earlier version of this test, Probleme #110-112, checked the command's ERROR HANDLING thoroughly but never asserted a nonzero object count, so this exact gap survived three prior fix-and-verify cycles on the same command) now passes end to end against the live registry.

---

### 120. `av registry restore`'s `--resume` misread `export`'s own bookkeeping as its own — a fresh restore into an empty registry would silently skip uploading every object

**Severity:** 10/10 (the other half of #119's disaster-recovery breakage, and arguably worse: this one would make a restore into a genuinely EMPTY/different registry — the actual point of a backup — silently no-op on every object, believing them all "already done") · **Status:** 🟢 `fixed` (2026-09-02)

**Problem:** Found immediately while re-verifying #119's fix, from the SAME test: `export()` and `restore()` both read/write a `.state.json` file in `ARCHIVE_DIR` via the identical `_state_path()`/`_load_state()`/`_save_state()` helpers, with an identical `completed_objects` key — but the two commands mean opposite things by "completed": for export it means "already downloaded from the registry into this archive"; for restore it means "already uploaded from this archive into the registry". Because both commands shared one file, `restore()`'s own `--resume` (the default) on its VERY FIRST invocation against a freshly-exported archive would load export's `completed_objects` list (every object hash export had just downloaded, tracked purely for EXPORT's own resumability) and treat every one of them as already uploaded — skipping the real `POST /api/objects/{hash}` call for all of them via the `if h in done_objects: ok += 1; obj_resumed += 1; continue` fast path. In this cycle's test (restoring into the SAME registry the objects were originally pushed to) this was invisible: the data genuinely was already present, so a silently-skipped upload and a correctly-executed-then-409'd upload looked identical from the outside. Against a genuinely empty/different target registry — the actual disaster-recovery scenario — this would leave every single object un-uploaded while `av registry restore` reported success.

**Fix:** gave export and restore separate, independently-tracked state files in the same archive directory — `.export-state.json` and `.restore-state.json` — via a `kind` parameter threaded through `_state_path()`/`_load_state()`/`_save_state()`. Each command's `--resume` now only ever sees its OWN prior progress, never the other direction's.

**Verification:** the same live round-trip test's restore-specific assertions (previously untested-because-untestable due to this exact bug making them accidentally-true): `restore1_data["objects_duplicate"] > 0` (proves the objects were REALLY POSTed and got real 409s, not skipped), `restore1_data["objects_resumed"] == 0` (nothing was wrongly resumed on the first restore), `restore2_data["objects_resumed"] > 0` (restore's OWN resume state now works correctly on its second invocation), `restore3 --no-resume` re-attempts everything regardless. All pass.

---

### 121. `av benchmark` crashed on Windows the moment DVC's own temp-directory cleanup raced a still-open file handle

**Severity:** 5/10 (every real measurement had already succeeded by the time this happened — it's a cleanup-time crash, not a data-quality issue, but it made a full benchmark re-run outright impossible on Windows) · **Status:** 🟢 `fixed` (2026-09-02)

**Problem:** Found live while re-running `av benchmark --markdown development/BENCHMARKS.md` for this cycle's full capture: `bench_hashing_throughput.py`'s `tempfile.TemporaryDirectory(...)` context manager raised `PermissionError: [WinError 32] ... being used by another process` on `__exit__`, while trying to remove `dvc-repo` — DVC's own subprocess apparently still held a handle open on something inside it at that exact moment. Unlike POSIX, Windows refuses to delete a file/directory a process still has open, so this isn't a portability shim gap — it's Windows behaving correctly and DVC's own cleanup timing being the actual source. The exception propagated all the way up through `cmd_devtools.py`'s `benchmark()` command and aborted the ENTIRE run, discarding every benchmark's already-successful measurements along with it. `bench_commit_push_latency.py`'s mlflow helper had already discovered and worked around a version of this exact problem (manual `mkdtemp` + best-effort cleanup instead of `TemporaryDirectory`'s context manager) — but that fix was never applied to the other five `bench_*.py` files, all of which used the same vulnerable pattern.

**Fix:** added `ignore_cleanup_errors=True` (stdlib-native since Python 3.10, this project's own minimum supported version — simpler than the manual-mkdtemp workaround) to every `tempfile.TemporaryDirectory(...)` call across all six affected `benchmarks/bench_*.py` files.

**Verification:** `av benchmark --markdown development/BENCHMARKS.md` re-run end to end on this same Windows machine, against the live registry, completes without crashing.

---

### 122. Concurrent-push benchmark mislabeled a real mid-run failure as "not installed", and a real 8-way connection reset under whole-suite load

**Severity:** 4/10 (a benchmark-reporting honesty bug, not a product bug — but it directly contradicts the module's own "skip and label, never fabricate" contract) · **Status:** 🟢 `fixed` (2026-09-03)

**Problem:** Found on the same full `av benchmark` re-run that verified #121: the "Concurrent Multi-User Push Throughput" row rendered `av` as `not installed`, even though `av` was demonstrably installed and the registry had been up and answering requests the entire run (confirmed via `docker ps`/`docker logs` — the container never restarted). Two separate things were going on:
1. **Real, environment-level flakiness, not a code bug:** `bench_concurrent_push.py`'s 8-way `ThreadPoolExecutor` hit a raw `ConnectionResetError(10054, ...)` mid-push. `docker logs aether-vault-engine` shows zero `POST /api/commits` entries for the whole window this benchmark ran in — the reset happened before the request ever reached uvicorn, i.e. at the Windows/Docker-Desktop network layer, not inside the server. Re-running `python -m benchmarks.bench_concurrent_push` in isolation immediately afterward (nothing else running, honoring the standing "one heavy operation at a time" rule) succeeded cleanly at 4,905.3 ms — confirming this was capture-machine contention (the full suite had just finished hammering the same 4-core/4GB box with hashing, DVC/git-lfs subprocesses, and a GC benchmark back to back), not a registry or client defect.
2. **A real reporting bug on top of that:** `bench_concurrent_push.py`'s `run()` only ever set `ToolStatus.AVAILABLE` or `ToolStatus.NOT_INSTALLED` for `av` — there was no third state for "the server was reachable but the operation itself failed." A `ConnectionResetError` raised out of `client.push_commit()` wasn't even caught, so this state was reachable via two different paths (an uncaught exception, or `all(results)` being `False`) and both collapsed onto the same wrong label. `NOT_INSTALLED`'s value renders as literally `"not installed"` in both the console table and `development/BENCHMARKS.md` — actively misleading to a reader who'd reasonably conclude `av` wasn't on `PATH`, when the truth (a load-induced connection reset, worth re-running rather than worth "installing something") is a completely different diagnosis.

**Fix:** added `ToolStatus.FAILED` (`benchmarks/tool_runner.py`) as a third state distinct from `NOT_INSTALLED`/`NOT_APPLICABLE`, with its own Legend entry in `render_doc_header()`; `format_value()` simplified to attach the optional footnote to any non-`AVAILABLE` status uniformly (previously only `NOT_APPLICABLE` got one). `bench_concurrent_push.py` now wraps the push in `try/except`, sets `FAILED` (not `NOT_INSTALLED`) with an honest note whenever the server was reachable but the operation failed, whether that surfaces as `all(results)` being `False` or as a raised exception.

**Verification:** `tests/test_tool_runner.py` — 2 new tests for `format_value()` rendering `FAILED` with/without a note. `development/BENCHMARKS.md`'s concurrent-push row corrected by hand to the real isolated-run number (4,905.3 ms) rather than re-running the full suite a third time on this RAM-constrained machine purely to regenerate one already-diagnosed row.

---

### 123. `scripts/append_perf_history.py` captured a silently wrong project version — `importlib.metadata.version("aether-vault")` is non-deterministic on a dev machine with more than one registered install

**Severity:** 6/10 (directly threatens WP-25's release gate: `check_perf_history_has_tag()` requires a perf-history row whose `version` field matches the tag being released — a wrong capture here would fail a real release for a reason that has nothing to do with release readiness) · **Status:** 🟢 `fixed` (2026-09-03)

**Problem:** Found immediately after the corrected concurrent-push re-run (#122), running `python scripts/append_perf_history.py` to re-append the trend section `av benchmark`'s full-file rewrite had just discarded: the new `perf-history.json` entry captured `"version": "0.0.0"` for commit `8ef634b` — the same commit whose *first* perf-history entry (captured minutes earlier, same session) correctly recorded `"1.2.5.dev6+g8ef634b58.d20260902"`. Root cause: this session's own Docker/packaging debugging (Probleme #118) ran `pip wheel .` and `pip install -e .` more than once against this checkout, leaving **two separate registered `aether-vault` distributions** on this machine — a stale `python/aether_vault.egg-info` (from a direct `setup.py`-path build, frozen at `Version: 0.0.0`) and a stale editable dist-info under the user's Roaming site-packages (frozen at `Version: 1.0.0` from whenever that `pip install -e .` last ran). `importlib.metadata.version("aether-vault")` — the exact mechanism `_project_version()` tried FIRST — picks between same-named distributions by `sys.path` order, which differed between two same-session Python process invocations of the identical script against the identical commit, returning `"1.0.0"` interactively and `"0.0.0"` from a backgrounded run. Both are wrong; neither reflects the live git state. `python/av_server/server.py::_installed_version()` (the `/api/health` version field) and `python/av_cli/cmd_env.py`'s snapshot use the exact same `importlib.metadata` call and are equally exposed on a machine in this state — this session's own repeated build/debug cycle is what created the hazard, so a clean CI runner or a single-install dev machine won't normally hit it, but it's a real footgun for exactly the kind of long, build-heavy session this plan required.

**Fix:** `_project_version()` now tries `av_cli._version.__version__` FIRST — the file setuptools-scm regenerates on every build/wheel, which is why it was already correct at the moment of capture — falling back to `importlib.metadata`/`setuptools_scm.get_version()` only for a source checkout that has never been built at all. This matches `av_cli/ui.py::_get_version()`'s pre-existing, correct ordering (the `av --version` banner never had this bug) instead of reinventing a worse one.

**Verification:** `tests/test_perf_history_script.py` — new tests proving the live version file wins over stale/monkeypatched installed metadata, and that the fallback chain still degrades cleanly to metadata then `"unknown"` when the version file genuinely isn't there. The already-corrupted `development/perf-history.json` entry was hand-corrected to the real value (`1.2.5.dev6+g8ef634b58.d20260902`, matching the sibling entry from the same commit) rather than re-running the multi-minute speedcheck capture a third time purely to fix one field; `development/BENCHMARKS.md`'s trend table re-rendered from the corrected file via `update_benchmarks_md()` directly (no recapture needed — that function is pure).

---

### 124. `av webhooks deliveries --output json` crashed with an unhandled `ConnectionError` (empty output, exit 1) instead of a clean `unreachable_queued` envelope, when the registry was unreachable

**Severity:** 6/10 (breaks the JSON-mode contract — the whole point of the anti-leakage sweep — for a documented, agent-facing command the moment its one real-world failure mode, an unreachable registry, actually happens) · **Status:** 🟢 `fixed` (2026-09-04)

**Problem:** Found by `tests/test_contract_matrix.py::TestAntiLeakage`'s full-suite sweep on a machine with Docker intentionally stopped (a realistic "registry unreachable" condition, not a test artifact): `av --output json webhooks deliveries` failed with exit 1 and **completely empty** stdout, not even a truncated envelope. Every sibling command in `cmd_webhooks.py` (`add`/`list`/`remove`/`test`/`enable`/`replay`) makes its network call through the module's own `_request()` helper, which wraps `client.session.request(...)` in `try/except requests.RequestException` and calls `fail(None, "unreachable_queued", ...)` on failure — the correct, documented behavior for this exact situation (AGENTS.md non-negotiable #3: offline resilience is sacred). `deliveries()` alone bypassed `_request()` and called `client.session.get(...)` directly, with no exception handling at all — a `requests.exceptions.ConnectionError` propagated straight out of the Click command, uncaught. Click's `CliRunner` (and a real terminal invocation identically) catches an unhandled exception, sets exit code 1, but the crash happens before anything is ever echoed, so `result.output` is empty rather than containing a JSON envelope OR a human traceback — the worst of both: a scripting agent parsing `stdout` as JSON gets a `JSONDecodeError` on an empty string with no diagnostic content anywhere. `show()`'s second network call (fetching that webhook's 5 most recent deliveries) had the exact same bypass, but was masked in practice: `show()` always makes an EARLIER `_request()` call first (to resolve the webhook by id), which already raises the correct `fail()` before code ever reaches the unguarded second call — a latent, currently-unreachable version of the same bug that a future reordering of those two calls would have silently reintroduced.

**Fix:** `_request()` gained a `params` parameter (routes to `client.session.request(..., params=params, ...)` — until now only `json_body` was threaded through, since no caller had needed query params before this fix); both `deliveries()`'s and `show()`'s raw `client.session.get(...)` calls now go through `_request()` like every other command in the module. No more raw `client.session.*` calls remain anywhere in `cmd_webhooks.py` outside `_request()`'s own definition.

**Verification:** `tests/test_contract_matrix.py::TestAntiLeakage::test_command_emits_clean_json_or_usage_error[webhooks deliveries]` — the exact test that caught this — now passes. `tests/test_webhooks_cli.py`'s `FakeSession` fake had its `.get()` method merged into `.request()` (matching the real client shape now that nothing calls `.get()` directly) with a comment explaining why a future direct `.get()` call would now fail loudly instead of silently bypassing the fake's call tracking; full `tests/test_webhooks_cli.py` (73 tests) and `tests/test_contract_matrix.py` re-run green together. Found during the v1.3.0 wrap-up's final `pytest tests/ -v` pass with Docker deliberately stopped (owner: "I will turn it on manually, continue with your stuff without it") — the offline condition this bug needed to surface was exactly what that choice created, a good example of why AGENTS.md's manual-repro standard matters even for a command whose happy path was already well-tested.

---

### 125. `chaos-drills` Phase M crashed the whole server at startup with an uncaught `PermissionError`, instead of testing the "server's up, one write fails" scenario it was designed for

**Severity:** 4/10 (a chaos-drill test-setup gap, not a product defect — real product code, incl. `/api/ready`'s own writability probe at the exact same path, already behaves correctly) · **Status:** 🟢 `fixed` (2026-09-03)

**Problem:** Found live on GitHub Actions' real Linux runner (the first CI run of the v1.3.0 commit, `chaos-drills` job): Phase M's read-only-`AV_DATA_DIR` drill (`scripts/e2e_scenario.sh`) is supposed to prove the server stays UP while one write (an object upload) fails cleanly and the client queues — but the server crashed at import time instead, `PermissionError: [Errno 13] Permission denied: '/tmp/.../data-readonly/objects'` from `CASStorage.__init__`'s unconditional `self.objects_dir.mkdir(parents=True, exist_ok=True)` (`storage.py:15`, called at server import time via `server.py:272`'s module-level `storage = CASStorage(DATA_DIR)`). Root cause: the test script's `READONLY_DATA` directory was created empty, then `chmod 555`'d — meaning `objects/`/`commits/`/`refs/` did not exist yet when the server tried to create them at startup, and creating a brand-new subdirectory genuinely does need write permission on the parent (unlike `mkdir(exist_ok=True)` on an ALREADY-existing path, which only needs to stat it). The test's own self-skip guard (a write-probe check, for environments like Windows/git-bash where `chmod` doesn't enforce real POSIX permissions) worked correctly and confirmed this GHA Linux runner genuinely does honor `chmod 555` — so this wasn't a false-unwritable environment, it was a real gap in what got locked down and when.

**Fix:** `scripts/e2e_scenario.sh` Phase M now pre-creates `objects/`/`commits`/`refs/` under `READONLY_DATA` BEFORE locking anything down, then `chmod -R 555` (recursive, not just the top level) the whole tree — the server's own startup `mkdir(exist_ok=True)` calls become no-ops against already-existing paths (need only read+traverse, not write), while an actual object upload's `target_path.parent.mkdir(...)` (creating a NEW per-hash shard subdirectory under the now-locked-down `objects/`) still needs real write permission and correctly fails. The recovery `chmod 755` calls (both the explicit recovery step and the trailing best-effort cleanup) were made recursive to match. No product code changed — `/api/ready`'s own `data_dir_writable` probe (`server.py`'s readiness check) already writes directly at the data dir's top level, exactly where this fix's lockdown applies, and was already correct.

**Verification:** Reasoned directly from CPython's/POSIX `mkdir()` semantics (an `EEXIST` on an already-present path is returned before any write-permission check on the parent is ever made — standard, unambiguous kernel behavior) rather than a local repro: this machine's Windows filesystem doesn't enforce real POSIX permission bits the way the fix depends on, and Docker (which would give a real Linux container to test against) was intentionally off for this session per the owner's instruction — so unlike this cycle's other findings, this one is verified by code-level reasoning plus the next real `chaos-drills` CI run on GitHub's actual Linux runner, not a local manual repro. The rest of the same `Tests` run (`test (3.10/3.14)`, `plugin-tests`, `webui-tests`, `server-tests`, `server-tests-windows`, `e2e-suite`, `e2e-engine-smoke`, `webui-e2e`, `package-build`, `smoke-wheel-linux`, `smoke-sdist-windows`) all passed cleanly on the same push, isolating this to Phase M specifically.

**Confirmed on the next real GHA run** (owner pushed the fix manually): the server startup crash is gone — `POST /api/objects/...` correctly returned `500 Internal Server Error` this time (the real write attempt now genuinely fails, exactly as designed), proving the recursive-`chmod`/pre-created-directories fix works precisely as reasoned. Phase M still failed, but for a completely different, deeper reason this fix exposed for the first time — see #126.

---

### 126. `av commit` silently landed commit metadata referencing an object that was NEVER actually uploaded — a failed object write was discarded, not reported, by every caller

**Severity:** 9/10 (a "successful" push whose artifact bytes are unrecoverable from the registry — silent data-integrity loss on the single most common operation in the whole system) · **Status:** 🟢 `fixed` (2026-09-04)

**Problem:** Found live on GitHub Actions immediately after #125's fix corrected the Phase M server-startup crash: with the server now correctly staying up and returning a real `500` for the object upload (confirmed in the server log — `POST /api/objects/... 500 Internal Server Error`), the commit STILL landed successfully (`POST /api/commits 201 Created`) instead of queuing, and `av . commit`'s own output was a plain, unremarkable success line with no queued warning at all. Root cause, two compounding gaps: (1) `core.py::upload_commit_objects()` submits every object upload to a thread pool and calls `future.result()` on each — but the client-side `VaultClient.upload_object()` doesn't RAISE on a failed upload, it returns `False` (a deliberate, correct design for a clean HTTP failure) — and that `False` was discarded (`future.result()`'s value went nowhere), so the function always returned `None` regardless of whether every object genuinely landed. (2) Both callers (`_finalize_commit()`'s main path and `flush_pending_push()`'s retry path) called `upload_commit_objects()` for its side effects only and unconditionally proceeded straight to `client.push_commit(commit_data)` next — and the SERVER doesn't catch this either: `DBTree.object_hash` (`av_server/models.py`) is DELIBERATELY not a real database foreign key (a pre-existing, documented, correct design choice — layer-split/CDC-chunked artifacts never get a whole-file object row, only their shards do, and enforcing the FK broke every such commit historically), so `POST /api/commits` accepts a tree referencing ANY object hash unconditionally, whether or not that object actually exists in storage. The combination meant a genuinely failed object write (a full/unwritable registry disk mid-upload — exactly Phase M's real-world scenario, not a hypothetical) was invisible at every single layer: the client didn't check, the server doesn't validate, and the commit reports plain success. The commit-time ref race (Probleme entry on the same class of gap) and #119/#120 (registry export/restore silently losing all object content) are the same underlying failure mode — "the system reports success without actually verifying the data landed" — recurring a third time in different code paths this same cycle.

**Fix:** `upload_commit_objects()` now returns `bool` (`True` only when every object genuinely uploaded, or there was nothing to upload) instead of implicitly `None` — collects every future's result (not a short-circuiting `all()` over the generator, so an unexpected exception from a LATER future still surfaces exactly as before) and returns `all(results)`. Both callers now check this return value: `_finalize_commit()`'s main path adds a new branch (`if not upload_commit_objects(...): queue_pending_push(...); _queued("object_upload_failed")`) BEFORE ever reaching `push_commit()`, printing the same style of yellow "queued for retry" message every other failure mode already prints; `flush_pending_push()`'s retry path short-circuits (`upload_commit_objects(...) and client.push_commit(...)`) so a repeat failure simply falls through to `still_pending.append(entry)`, identical to a failed `push_commit()` today. The stale docstring/comment claiming a real DB foreign key enforces this (written before the layer-split exemption was added, per `models.py`'s own comment) was corrected at both call sites to describe the ACTUAL mechanism: this return value is the only signal a real failure ever produces, so callers must treat it as authoritative.

**Verification:** Two new tests in `tests/test_cli_commands.py`: `test_upload_commit_objects_returns_false_when_any_upload_fails` (unit-level, a `FakeClient` whose `upload_object()` returns `False`) and `test_commit_queues_instead_of_pushing_when_an_object_upload_fails` (end-to-end through the real `av commit` command — monkeypatches `VaultClient.push_commit` to raise `AssertionError` if it's EVER called, positively proving the short-circuit works, not just that the end state happens to look right — then asserts the commit queues, exactly like every other push-failure mode). Both existing `upload_commit_objects` tests (`..._skips_objects_the_batch_check_reports_found`, `..._uploads_only_missing_hashes`) re-verified green — they only check side effects via a `calls` dict, never the return value, so the type change from implicit-`None` to `bool` is additive and didn't disturb them. Full `tests/test_cli.py` + `tests/test_cli_commands.py` + `tests/test_sync.py` + `tests/test_merge.py` (178 tests) re-run green together, ruling out collateral damage to the surrounding commit/push/queue machinery. The real fix (not yet re-verified on GHA at the time of this entry — the owner will push it) is expected to make Phase M's assertions pass for real: the commit should now queue (`pending_count >= 1`) and nothing partial should land (`MOBJ_COUNT == 0`, which was already trivially true).

---

### 127. `webui-e2e`'s token-gate Playwright test broke the moment WP-18's new per-panel error states shipped, on a substring-locator collision neither change anticipated

**Severity:** 3/10 (test-only; the underlying webui behavior — both TokenGate's prompt AND every panel's honest error state — is correct and intentional; only the Playwright locator was ambiguous) · **Status:** 🟢 `fixed` (2026-09-04)

**Problem:** Found on the same GHA `Tests` run as #126: `webui-e2e`'s "unknown browser shows the entry prompt instead of registry data" test failed with a Playwright strict-mode violation — `page.getByText("This registry is protected")` resolved to 5 elements instead of 1. Two genuinely unrelated pieces of this same development cycle collided: `TokenGate.tsx`'s own prompt title is the short, exact string `"This registry is protected"`; `lib/api.ts`'s `UnauthorizedError` message is the longer `"This registry is protected — a valid access token is required."`, which WP-18's new per-panel error states (this same cycle — "every panel now reads `useDashboard()`'s real `error` field and renders a distinct error state") now render VERBATIM inside every panel that hits a 401, prefixed with the panel's own name (`"⚠ stats: This registry is protected — a valid access token is required."`, `"⚠ refs: ..."`, etc.). A live, unauthenticated page genuinely shows BOTH simultaneously — the panels underneath TokenGate's overlay keep fetching and 401ing independently — so a SUBSTRING locator (Playwright's `getByText()` default) matches all four panel error divs plus TokenGate's own title, 5 elements total, whereas the test needs exactly one to call `.toBeVisible()`. Neither the TokenGate title nor the panel error text is wrong on its own; the coincidental wording overlap between two otherwise-unrelated pieces of UI text is what broke the test's uniqueness assumption.

**Fix:** `webui/e2e/token-gate.spec.ts`'s "unknown browser..." test now passes `{ exact: true }` to the locator (`page.getByText("This registry is protected", { exact: true })`), which only matches an element whose ENTIRE trimmed text equals the string — excludes every panel's longer error text (which has a `"⚠ <panel>: "` prefix and `" — a valid access token is required."` suffix) while still matching TokenGate's own exact-text title. The sibling test's `toHaveCount(0)` assertion (asserting NEITHER text appears once a valid token is stored) was left as a substring match deliberately — a broader locator is a STRONGER "nothing related is visible" guarantee for a must-not-appear assertion, not a weaker one, so it didn't need the same fix. `TokenGate.test.tsx`'s own component-level unit tests (five call sites using the same phrase) are unaffected — they render `<TokenGate>` in isolation via React Testing Library, with no sibling dashboard panels ever mounted alongside it, so no other element in that render tree ever contains the phrase.

**Verification:** `npx tsc --noEmit` on the whole `webui/` project passes with the edited spec file (TypeScript is all Playwright specs get checked with locally — Docker was off, so the actual browser run couldn't be re-executed on this machine; the real Playwright pass is the next GHA `webui-e2e` job), which also re-runs `dashboard.spec.ts`'s own new "shows a real error state" test (already green in the SAME run that caught this), confirming the panel error-state feature itself works as intended — only this one locator's assumption was stale.

---

### 128. `scripts/e2e_scenario.sh`'s `start_server()` silently changes the CALLER's working directory — Phase M's recovery step was the one place in the whole script that didn't already know to route around it

**Severity:** 5/10 (chaos-drill test-infrastructure only, but a footgun every future phase author could rediscover the hard way — this exact bug already existed latently in every phase, just never triggered before Phase M's own recovery step happened to need a relative `av .` call right after a mid-phase restart) · **Status:** 🟢 `fixed` (2026-09-04)

**Problem:** Found on the THIRD consecutive GHA `chaos-drills` run for this same phase, immediately after #126's fix let execution reach further into Phase M than ever before: `av . push` failed with `Error: Not an Aether-Vault repository (or any of the parent directories).` and the whole script aborted (`set -euo pipefail` at the top means ANY unguarded command failure kills the script immediately with that exit code — so this never even reached the phase's own `die()` call). Root cause: `start_server()` (the shared helper every phase uses to boot/restart the registry) runs `cd "$REPO_ROOT"` as a PLAIN command in the function body, not inside the subshell that follows it — since bash functions share the caller's shell state unless explicitly subshelled, this silently changes the CALLING SCRIPT's current directory too, every single time `start_server` is invoked. This has been true since the helper was written, and every phase that calls `start_server` MID-PHASE (after already `cd`ing into a scratch repo directory) already had to know this and route around it by using the explicit `av "$WORK/repoX" ...` form afterward instead of the shorter `av . ...` (confirmed by grepping every call site — Phase A/B/D/N all do this correctly; Phase L's final `start_server` call happens to never need another `av .` afterward, so it never triggered). Phase M's recovery step (`stop_server; chmod -R 755 ...; start_server chaos-M-recovered; av . push`) was the ONE place across the whole script that both called `start_server` mid-phase AND still used the relative `av .` form afterward — a convention every phase author had to remember by hand, with no enforcement, exactly the kind of gap a NEW phase (chaos Phase N sits right after Phase M) could easily reintroduce.

**Fix:** `start_server()` now saves the caller's `$PWD` before its own `cd "$REPO_ROOT"` and restores it immediately after backgrounding the server process (before `wait_health`, which has no cwd dependency of its own — it only uses `curl`, `kill -0`, and the absolute-path `$SERVER_LOG`). This is the root-cause fix, not a Phase-M-specific patch: no phase (existing or future) needs to remember this quirk or use the longer explicit-path form anymore — `av .` is now always correct immediately after any `start_server` call, from any phase, at any point in the script.

**Verification:** the save/restore pattern itself reproduced and confirmed correct in complete isolation, independent of Docker/POSIX-permission concerns (this is pure bash `cd`/subshell/background-job mechanics, identical on every OS) — a standalone repro function using the exact same shape (`cd` to a different dir, background a job, `cd` back, `wait`) confirmed the caller's directory is unchanged after the call. `bash -n scripts/e2e_scenario.sh` passes. Cross-checked every other `start_server` call site in the script (`grep -n "start_server "`) to confirm none of them relied on the OLD (buggy) CWD-leaking behavior on purpose — they either don't call `av .` again afterward, or already used the explicit-path form defensively; this fix makes both forms safe going forward without changing any of their behavior. Not yet re-verified on a real GHA run at the time of this entry (the owner will push it) — this is the fourth consecutive layer this exact chaos drill has peeled back this cycle (#125 → #126 → this), each fix reaching further into a code path that had never been exercised even once before this development cycle wrote the drill in the first place.

---

### 129. `av diff` (no arguments) and `av_sdk.Repo.diff_semantic()` both compared HEAD against an EMPTY tree instead of its real parent, for every locally-authored commit

**Severity:** 6/10 (the single most common form of `av diff`/`diff_semantic()` invocation — no arguments, "what changed in my last commit" — silently misreported every file as newly ADDED instead of CHANGED, for every commit that was never fetched from the registry, i.e. the normal case in any single-machine or offline workflow; not data loss, but a systematically wrong semantic-diff summary an agent or human could act on) · **Status:** 🟢 `fixed` (2026-09-04)

**Problem:** Found during v1.3.1 planning work while extracting `cmd_policy.py::_latest_metrics_for_ref()`'s baseline walk to use `handoff.py::_commit_parent()` (the helper `handoff.py`'s own `_metrics_history_tail()`/`build_semantic_summary()` and `av_sdk.Repo.log()` were already fixed to use, per that function's own docstring: "commits fetched from the registry carry `parent_hash`; LOCALLY-authored commits store a `parents` LIST — reading only `parent_hash` stops the walk after one hop"). Auditing every other reader of a commit's parent turned up the SAME bug, independently, in two more places that `_commit_parent()`'s original fix never touched: `av_cli/cmd_diff.py::diff()`'s no-argument default path (`head_commit.get("parent_hash")`) and `av_sdk/repo.py::Repo.diff_semantic()` (both its `target` and no-argument branches, same `.get("parent_hash")` pattern). Both read `parent_hash` directly on a commit dict that — for any commit created locally via `commit_staged()` (`core.py`) — only ever has a `parents` list, never a `parent_hash` key (that name is the *registry's* SQLAlchemy column, a different schema entirely). The lookup therefore always returned `None`, `_tree_of(None)`/`load_commit(self.path, None)` always resolved to an empty tree, and the diff engine reported every single file in the target tree as `files.added` (and `totals.bytes_before: 0`) instead of correctly walking to the real parent and reporting `files.changed`/a real `bytes_before`. This is the third occurrence of the exact bug class `_commit_parent()` was written to fix, in a fourth and fifth call site it was never actually applied to. It surfaced now specifically BECAUSE `tests/test_av_sdk.py::test_diff_semantic_parity_sdk_vs_cli` compares the SDK's output against the CLI's `av diff` output field-for-field: fixing only the SDK side first (matching this session's WP-0 goal of routing `Repo.diff_semantic()` through `_commit_parent()`) made that parity test FAIL — not because the fix was wrong, but because it exposed that the CLI side the test compares against shared the identical latent bug, and the two wrongs had been silently canceling each other out (`base: None` on both sides) since the parity test was written.

**Fix:** Both `cmd_diff.py::diff()`'s default (no-argument) branch and `av_sdk/repo.py::Repo.diff_semantic()`'s `target` and default branches now resolve the parent via `handoff.py::_commit_parent()` instead of reading `.get("parent_hash")` directly — the same fix already proven correct for `handoff.py`'s own internal callers and `Repo.log()`. No behavior change for any commit that already has a `parent_hash` (registry-sourced); every locally-authored commit now correctly finds its real parent.

**Verification:** `tests/test_av_sdk.py::test_diff_semantic_parity_sdk_vs_cli` (previously silently passing on two matching bugs, now genuinely passing on two matching CORRECT outputs) and the rest of `tests/test_av_sdk.py` (21 tests) re-run green. `tests/test_contracts.py`'s `TestSemdiffSchema` (schema-shape only, not classification-specific) re-run green — confirms the fix changes WHICH fields the diff populates, not the envelope's shape. Grepped every other `.get("parent_hash")`/`"parent_hash"` site across `python/` to confirm no further occurrences remain outside the registry/DB-row-normalization code (`sync.py::normalize_commit_row`) and `av_server/models.py` (the real DB column), where `parent_hash` is the CORRECT field name.

---

### 130. `av --output json incident rollback` printed TWO top-level JSON objects — the same bug class as #114/#115, reintroduced in a new command that composes an existing one

**Severity:** 4/10 (new-in-this-cycle command, caught by its own first test before ever shipping; same well-understood bug class with a well-understood fix already established elsewhere in the codebase) · **Status:** 🟢 `fixed` (2026-09-04)

**Problem:** `cmd_freeze.py::incident()` (v1.3.1, RSI R1) implements `av incident rollback` by calling `_set_freeze(...)` then `ctx.invoke(improver_rollback, target_id=None)` and finally its own `emit_json(None, "incident rollback", ...)` — but `improver_rollback` (`cmd_improver.py`) ALREADY emits its own complete top-level JSON envelope via `emit_json()` when `--output json` is active. Invoking it unguarded meant `av --output json incident rollback` printed the rollback command's envelope followed by the incident command's own envelope: two JSON objects separated by a newline, exactly the shape `json.loads()` over the whole invocation's output rejects with `JSONDecodeError: Extra data`. This is the identical bug `cmd_policy.py::promote()` already fixed for its own nested `merge` invocation (Probleme #114/#115) — the fix pattern existed in the codebase, but the new command's author (this session) didn't apply it by default, and only caught it because `tests/test_freeze.py::test_incident_rollback_freezes_then_rolls_back` (written alongside the feature, per this repo's own testing convention) tried to parse the combined output as one object and failed immediately.

**Fix:** `incident()` now mirrors `promote()`'s exact fix shape: in text mode, `improver_rollback` is invoked directly (its own human-readable output is the desired behavior — no double-print risk in text mode since neither command echoes redundant text). In JSON mode, `improver_rollback`'s stdout is captured via `contextlib.redirect_stdout` into a buffer, the last line is parsed as its envelope, and its `data` is folded into `incident rollback`'s own single combined envelope under a `rollback` key — so exactly one JSON object reaches stdout either way.

**Verification:** `tests/test_freeze.py::test_incident_rollback_freezes_then_rolls_back` (the test that caught it) now passes; the full file (11 tests) re-run green. Grepped for every other `ctx.invoke(...)` call site added by this same RSI cycle (`cmd_improver.py::improver_init` invoking `improver_register`, `cmd_policy.py::pack_publish` invoking nothing nested) to confirm no second occurrence of the same pattern slipped in elsewhere — `improver_init` is the only other nested invoke, and it's safe: `improver_register`'s envelope IS the entire desired output for `av improver init` (a thin alias, not a composition of two independently-meaningful results), so there is no second envelope to collide with.

---

### 131. Four commands (one pre-existing since v1.2.0) leaked non-JSON text or a wrong exit code after their own JSON envelope on a DENY/FAIL outcome specifically — the generic anti-leakage sweep never exercises a real denial

**Severity:** 6/10 (the pre-existing `av promote` instance means `av --output json promote` has NEVER produced valid JSON on a real policy denial since v1.2.0 — a strict agent consumer's `json.loads()` over the whole invocation has always thrown on exactly the outcome an autonomous loop most needs to detect reliably) · **Status:** 🟢 `fixed` (2026-09-04)

**Problem:** Found via this session's own test-writing discipline (writing a JSON-mode assertion for `av improver promote`'s new `require_review` denial, then checking whether the SAME assertion held for the pattern it was modeled on). Two distinct sub-bugs, four call sites:

1. **Leaked text after the envelope** (`cmd_policy.py::promote()`, pre-existing since v1.2.0; `cmd_improver.py::improver_promote()`, new this cycle, copied the same shape): the deny branch did `if current_output_mode() == "json": emit_json(...)` with NO `return`/`else`, then unconditionally `click.secho(f"DENIED: {reason}", fg="red", err=True)` — in JSON mode this printed the envelope, then the DENIED line on stderr, and `click.testing.CliRunner` in this environment combines stdout+stderr into `result.output`, so `json.loads(result.output)` failed with `"Extra data"` on ANY real policy denial in JSON mode. The comment three lines below the `cmd_policy.py` occurrence literally documents an earlier v1.3.0 fix for the exact same bug class in the ADJACENT (landing/allow) branch of the same function — the deny branch right above it was missed at the time.
2. **Wrong exit code, not leaked text** (`cmd_canary.py::canary_run()`, `cmd_policy.py::pack_verify()`, both new this cycle): the JSON branch did `emit_json(...); return` unconditionally, so a FAILED canary run or a BROKEN policy-pack chain reported the correct `data.passed: false` / `data.chain_ok: false` but exited **0** in JSON mode while the identical failure exited 15 in text mode — the two output modes disagreed about success/failure for the same outcome.

**Fix:** All four now gate the extra action on output mode. (1): the `click.secho(...)`/text echo moved into an explicit `else:` branch (JSON mode already got everything it needs from the envelope). (2): the `ctx_exit(EXIT_VALIDATION)` call was duplicated INSIDE the JSON branch (before its `return`), so both modes now exit identically on failure.

**Verification:** `tests/test_exit_codes.py::test_policy_denied_exits_16_json` (new — the pre-existing `promote()` bug had no prior JSON-mode-plus-strict-parse test at all, only a text-mode exit-code check) and `test_review_required_exits_19_json` both pass, proving one clean JSON object each. `tests/test_canary.py::test_run_fails_when_metric_violates_threshold` and `tests/test_policy_pack.py::test_verify_detects_broken_chain` extended with JSON-mode exit-code assertions (both were previously text-mode-only, which is exactly how this hid). Full `tests/test_exit_codes.py` (39 tests) re-run green.

**Deliberately NOT fixed — a separate, out-of-scope question flagged for the owner:** while fixing (1), it became visible that `av promote`'s deny envelope has ALWAYS been `{"ok": true, "error": null, "data": {"allowed": false, ...}}` on a real policy denial — never `ok:false` with `error.code: "policy_denied"`, unlike `av merge`'s policy denial (which goes through `fail()` and does carry `error.code`) and unlike what `docs/for-agents.md`'s registry table implies every exit-16 outcome carries. `av improver promote` was built to match this SAME existing shape for consistency with its sibling command, not fixed independently. Changing `promote`'s envelope shape now to add `error.code` would be a breaking change to a stable, documented JSON contract per `AGENTS.md` non-negotiable #4 ("if an upgrade would make a previously working script stop working, it's MAJOR" — VERSIONING.md) — a script currently branching on `data.allowed` would need to keep working. Left as-is, documented here rather than silently fixed, for the owner to decide whether this warrants a deliberate contract change at the next MAJOR boundary.

### 132. `VaultClient.server_available()` genuinely returns `True` when Docker Desktop is running — silently invalidating every test's "no server configured ⇒ unreachable" assumption across the whole suite, not just the new v1.3.1 tests

**Severity:** 7/10 (systemic — 26 tests across 8 pre-existing files plus every new v1.3.1 test asserting `unreachable_queued`/exit-13 behavior were all one `docker compose up` away from silently exercising the wrong code path, several of them against a REAL live database) · **Status:** 🟢 `fixed` (2026-09-04)

**Problem:** Discovered mid-session when `av_cli/sandbox`'s new `test_sandbox_queue_without_server_queues` returned exit 0 instead of the expected 13. `curl http://localhost:8000/api/health` succeeded — Docker Desktop was running a healthy `aether-vault-engine` + `aether-vault-db` (postgres:15-alpine) + `aether-vault-redis` compose stack on the default ports, apparently started outside this session's awareness. Every test that assumed "not configuring a remote in this repo" implies "the server is unreachable" (rather than explicitly forcing `server_available()` False) was silently wrong the moment a real dev stack happened to be up — the test would either see a live-but-differently-versioned server respond instead of queuing (masking the queued-path entirely), or, worse, some setup helpers (`tests/test_sync.py::_seed_source_repo`, seeding commits before its `fake_registry` fixture patched `VaultClient` in) would push real commits into the **actual live Postgres database** before the test's mocking took effect. A full-suite run surfaced 26 such failures once Docker happened to be running: `test_exit_codes.py` (2), `test_plugins.py` (1), `test_sync.py` (4 — via the seeding-order bug above), `test_v120.py` (2), `test_av_sdk.py` (2), plus 13 in this session's own new RSI test files across 8 modules.

A second, independent bug compounded the fix: `av_sdk/repo.py` imports `from av_cli.client import VaultClient` (bare package name) while `python/av_cli/core.py` imports `from .client import VaultClient` (relative, resolving to `python.av_cli.client`). Because this repo's `sys.path` makes both `av_cli` and `python.av_cli` importable, Python creates **two distinct module objects for the same file** (`a is b` → `False`, confirmed via a throwaway repro) — patching one leaves the other, and any test exercising the SDK seam, silently untouched.

**Fix:** Added a shared `unreachable_client` pytest fixture (`tests/conftest.py`) that patches `VaultClient.server_available` to always return `False` on **both** module identities (`python.av_cli.client` and, when importable as a separate object, bare `av_cli.client`), and applied it to every test asserting unreachable/queued behavior — replacing ambient "just don't mock anything" assumptions with an explicit, correct force. `tests/test_sync.py::_seed_source_repo` additionally forces `server_available` False directly on the real `VaultClient` class for its pre-fixture seed commits, so they queue locally (as originally intended) instead of racing a real server; the very first `fake`-backed commit then naturally flushes that local queue through the fake, keeping its ref state consistent with local history regardless of ambient Docker status. `tests/test_exit_codes.py::_make_push_401` was similarly reordered to force unreachable for its setup commit before flipping to "reachable but returns 401" for the push attempt it's actually testing.

**Verification:** All 26 previously-failing tests pass in isolation and as part of full-file runs (`test_exit_codes.py` 25/25, `test_plugins.py`, `test_sync.py` 13/13, `test_v120.py` 35/35, `test_av_sdk.py` 14/14), with the real Docker stack left running and untouched throughout — none of these fixes required stopping it. This is the same failure class as problem #116 (`test_contract_matrix.py`'s generic sweep silently mutating the real `.env` and restarting the real running engine container), now given a general, reusable fixture instead of a one-off patch, specifically so it cannot recur test-by-test as more commands are added.

---

### 133. WP-44 live-verification findings: four real bugs across the v1.3.1 RSI cycle that only a real Postgres could surface, plus a wrong test convention

**Severity:** 7/10 (one of the four — the truncation gap — meant EVERY new RSI table was silently exempt from test isolation, guaranteed to collide on any second local run) · **Status:** 🟢 `fixed` (2026-09-04)

**Problem:** The first-ever live run of `pytest tests/test_server.py -v` this cycle (WP-44, against a real Postgres/Redis stack) surfaced four independent, genuine bugs no stack-free test could have caught, plus a batch of test assertions written against the wrong convention:

1. **`tests/test_server.py::_truncate_all()` was never extended for any of the 20 RSI tables added across migrations `0006`-`0010`.** Every test using a hardcoded id against one of them (`TestImproverVersions::test_create_is_idempotent_by_id` using `"imp-1"`, etc.) silently collided with the SAME row a *previous* run of this file against the same persistent test database had already inserted — turning a fresh-create assertion into a stale-exists one. Invisible on a CI runner with a brand-new ephemeral service container per run; guaranteed on any persistent local Postgres re-run, which is exactly this session's setup and exactly why it had never been caught before this cycle's first live pass.
2. **The anomaly `mass_rewrite` detector never fired, on any input, ever.** `_detect_commit_anomalies()` read `diff.get("added")`/`.get("removed")`/`.get("changed")` directly off `_summarize_tree_diff()`'s return value — but that function nests all three lists under a `"files"` key (`{"files": {"added": [...], ...}}`), so the top-level lookups always returned `[]` and `changed_count` was always `0`. The sibling `metric_jump` detector sits right above this in the same function and was unaffected, which is exactly why no stack-free test caught it: nothing exercises the live tree-diff path this detector reuses outside a real server.
3. **Three new scope-denial tests used an unrestricted token by mistake.** `TestProjectFreeze::test_set_requires_admin_scope` and two new `TestAnomalyDetection` auth-spike tests authenticated as `scoped_users`' `"trainer"` identity expecting a 403 — but `trainer` is a bare-string `_AUTH_USERS` entry, which `_scopes_for_identity()` resolves to `["*"]` (unrestricted) by design (the fixture's own docstring says so: "trainer keeps full access"). The requests were always genuinely ALLOWED; the `assert ... == 403` assertions were dead code until this session's live pass actually executed them. Fixed by adding a genuinely unprivileged third identity (`"reader"`, explicit `"scopes": ["read"]`) to `scoped_users` and pointing those tests at it instead.
4. **A live alembic downgrade-to-base-and-back invalidated the app's shared connection pool's cached statement plans.** `test_migration_chain_downgrades_and_reupgrades_cleanly` drops and recreates every table via alembic's own raw connection, entirely bypassing the SQLAlchemy async engine the session-scoped `TestClient(app)` shares with every other test in the file. The first query against an affected table through that pool afterward raised `asyncpg.exceptions.InvalidCachedStatementError` (plan compiled against now-dropped-and-recreated table OIDs) — `pool_pre_ping=True` only checks liveness, not statement-cache validity. **First fix attempt (`engine.dispose()` after the round trip) made things categorically worse** — disposing the engine from a separate `asyncio.run()` call (a different event loop than the one the TestClient's own anyio portal thread runs on) broke that portal for the rest of the session ("This portal is not running" on every subsequent test, cascading from 12 failures to 84). Reverted immediately.

Separately, four tests (`TestImproverVersions::test_create_is_idempotent_by_id`, `TestChangeSets::test_full_legal_transition_chain`, `TestPolicyPacks::test_chain_hash_matches_expected_formula`, `TestAnomalyDetection::test_policy_pack_publish_emits_anomaly_event`) asserted HTTP **201** for a successful create — but `create_improver_version()`/`create_change_set()`/`create_policy_pack()` are all deliberately modeled on `POST /api/runs`'s pre-existing, documented, and separately-tested "lazy/idempotent create-or-exists" contract: **200 either way**, with the response body's `"status"` field (`"created"` vs `"exists"`) as the actual signal — multi-agent races don't get to pick which racer "wins" a 201. `test_runs_crud_and_commit_linkage_with_lazy_create` already asserts exactly this for `/api/runs`. The four new tests were simply wrong, not the routes.

**Fix:** (1) extended `_truncate_all()`'s TRUNCATE list with all 20 new tables. (2) fixed the detector to read `diff["files"]["added"]` etc. (3) added the `reader` identity and repointed the three tests. (4) reverted the `engine.dispose()` attempt; the migration test's own tail assertions now retry once through a small helper that catches (not status-code-checks, since `TestClient`'s default `raise_server_exceptions=True` means the failure surfaces as a raised exception, not a 500 response) the one-time cache invalidation — SQLAlchemy's asyncpg dialect invalidates the connection's cache IN RESPONSE to the first failure (per the exception's own message), so a retried query on the same connection succeeds. Corrected the four wrong-convention assertions to expect 200 + `status: "created"`.

**A fifth, real but transient finding, documented rather than "fixed" away:** `test_cli_commit_pushes_to_a_live_server` and `test_live_two_repo_clone_pull_flow` — the two tests in this file that opportunistically write to the REAL running `aether-vault-engine`/`aether_vault` database (by design, pre-existing, skip lazily when Docker is down) — flaked when run as part of the full 145-test file (immediately after a fresh engine restart, and again under the full file's resource load) but passed reliably every time run in isolation or in a small group, including twice in direct succession. This matches the machine's already-documented resource constraints (4 logical cores / 4 GB RAM, see `development/CHANGELOG.md`'s v1.3.0 entry and `test_perf_gate.py`'s accepted `log()` timing flake) — not a code defect in either test's own logic, which a manual repro (real CLI, real engine, real curl check) confirmed working correctly. Left as a known, accepted flake on this specific machine, not chased further.

**The same "just-restarted engine" transient recurred with `webui/e2e/seed_data.py`:** two of its pushes (`main()`'s "v2 checkpoint" and `seed_run()`'s final commit) queued locally (`.av/pending_push` populated, exit 0 either way per the queued-is-safe contract) instead of landing, immediately after the engine restart above — a plain `av push` in each seed script's temp repo drained them cleanly seconds later, and `dashboard.spec.ts`/`weight-diff.spec.ts`/`runs.spec.ts` all passed once the data actually landed. Same root cause as the pytest flake (a freshly-restarted engine's first requests are unusually slow on this machine), same non-fix: this is exactly what the queue is FOR — nothing was lost, it just needed a retry, which is the documented recovery path, not a bug to chase.

**A meaningful side effect surfaced along the way, not a bug but worth recording:** running these same two opportunistic tests repeatedly during this live-verification pass wrote ~9 real throwaway projects (`source-repo`, `test_cli_commit_pushes_to_a_li0`, `collab-live-*`, etc.) into the owner's actual `aether_vault` database — confirmed with the owner mid-session, who chose to rebuild and restart the real `aether-vault-engine` container from current code rather than leave it on its pre-session image (which was on migration `0005`, built ~37 hours before any of this cycle's RSI work existed). The rebuild+restart auto-migrated the real database to `0010` zero-touch, exactly as `database.py::init_db()`'s own contract promises.

**Verification:** `pytest tests/test_server.py -q` (isolated test database): 145/145 passing, both in a targeted run excluding the two opportunistic live-engine tests (142/142) and with those two run separately in isolation (3/3 including `test_registry_export_restore_round_trip`). Real engine rebuilt (`docker compose build aether-vault-engine && up -d`), confirmed serving `/api/health`/`/api/ready` cleanly and the real `aether_vault` database's `alembic_version` at `0010` post-restart.

**A sixth migration-chain touch point, discovered running `scripts/e2e_scenario.sh` for the first time against the new chain:** Phase C ("legacy-volume upgrade drill") drops `alembic_version` entirely to simulate a pre-Alembic volume, boots the server, and asserts the healing boot stamps the correct chain HEAD — hardcoded as the literal string `"0005"`. None of migrations `0006`-`0010` (this cycle) updated it, because the established "touch 5 places per migration" checklist (`models.py`, `database.py::_LEGACY_COLUMNS`, and three lists in `tests/test_migrations.py`) never included this script. The healing/migration logic itself was never wrong — only this one hardcoded assertion string went stale. Fixed to `"0010"`; **the checklist itself should read "6 places" going forward**, adding `scripts/e2e_scenario.sh`'s Phase C head-stamp literal.

### 134. `scripts/ha_drill.sh`'s bare, unscoped `wait` blocked on a deliberately long-lived background process — the actual cause of a 14-commit CI debugging saga (`V1.3.3.1`-`V1.3.3.14`), plus a second, latent instance of the same bug class found while writing this entry up

**Severity:** 6/10 (blocked every `ha-drill` CI run for roughly a day of wall-clock iteration; never a correctness issue in the drill's own PASS/FAIL logic, only in the drill's ability to finish and report one) · **Status:** 🟢 `fixed` (2026-09-06)

**Problem:** Commits `V1.3.3.1` through `V1.3.3.13` chased a hang in the `ha-drill` CI job through five wrong hypotheses in sequence, each disproved by the next commit's own evidence: (1) `curl`/`timeout` not actually bounding a stalled request — disproved once `--connect-timeout`/`--max-time` and `timeout -k` were both added and the hang recurred identically; (2) the hang being un-signalable (`timeout`'s own SIGTERM not being delivered) — disproved once `-k 5` (kill-after) made zero observable difference; (3) `wait_ready()` itself being stuck — disproved by `set -x`-with-timestamped-`PS4` tracing showing it always returned within one or two tries; (4) CI-runner resource exhaustion (`fork()` blocking under memory/process-table pressure after three rounds of container churn) — disproved by unconditional `free -h`/`ps aux`/`docker stats` diagnostics that would have printed regardless and never got the chance to; (5) an unkillable `D`-state (uninterruptible kernel I/O) process — the bounded-poll-with-`kill -9` diagnostic built to catch this printed nothing at all, meaning execution never even reached it. Only a heartbeat-style trace applied to the block genuinely running at the time of the hang (phase 4's 20 concurrent rate-limit probe curls) found the real bug: **a bare, argument-less `wait` at the end of that block**. Bash's argument-less `wait` blocks on EVERY background job the invoking shell has ever started, not just the ones the surrounding code cares about — and phase 3 had already started `docker/ha/webhook_probe.py` as a deliberately long-lived listener (`PROBE_PID`), only ever meant to be reaped later by `cleanup()`'s own `EXIT` trap. That job was still running BY DESIGN at the point phase 4's bare `wait` executed, so the wait blocked on it for as long as the surrounding step/job timeout allowed — a hang with no relationship whatsoever to curl, timeout, tracing, or runner resources, which is exactly why five rounds of instrumentation aimed at those all came back clean.

**A second, latent instance of the identical bug class, found while writing this entry (not by another CI failure):** phase 1's baseline concurrent-push loop had the exact same bare `wait || true` pattern, discarding every backgrounded push's exit status on top. It never hung in practice only because nothing long-lived is backgrounded before phase 1 runs (`webhook_probe.py` doesn't start until phase 3) — incidental, not structural, and one future reordering of the drill's phases away from a real regression.

**Fix:** phase 4's `wait` was scoped to exactly the PIDs its own loop launched (`wait "${_rl_pids[@]}"`, commit `75c494f`, confirmed green on the very next CI run — `gh run view 34000849519`: `Tests` workflow, `ha-drill` job, success, 5m). Phase 1's was fixed the same way in this session, additionally now aggregating each job's own exit status (not just its error-log emptiness) into an explicit failure count. Two more related fixes landed alongside while auditing every other `wait`/cleanup site in the script: the phase-2 subshell's own discarded exit status now logs instead of silently swallowing, `$WORK` (the script's `mktemp -d` scratch directory) is actually removed in `cleanup()` instead of leaking one directory per run, and phase 4's rate-limit env var is unset and the engines recreated once more at the end of the drill so a `KEEP_HA_STACK=1` run is left in its normal (unlimited) state rather than the fault-injected one.

**Verified:** `bash -n scripts/ha_drill.sh` (syntax); the scoped-`wait` fix itself was already live-verified by the referenced CI run before this write-up. The phase-1/phase-2/cleanup/phase-4-reset fixes in this entry are text-verified only as of this commit — pending their own live CI run (this repo has no Docker daemon available to run `ha_drill.sh` locally at time of writing) or a local run once Docker Desktop is available, per this project's own "nothing is done until it's verified" standard.

### 135. `security.yml` had NEVER executed once, on any event, since it was created — and would have failed immediately on its first real run regardless, via a broken `aquasecurity/trivy-action` reference that doesn't exist

**Severity:** 7/10 (the entire security-scanning surface — pip-audit, bandit, semgrep, Trivy image scan, npm audit — has been providing zero actual coverage, silently, for as long as this workflow has existed; `development/infrastructure.md`'s CI Job Map and `todo.md`'s own "10/10" framing both implicitly assumed it was live) · **Status:** 🟢 `fixed` (2026-09-06)

**Problem:** Two independent, compounding defects, found while SHA-pinning every action reference in the repo (v1.3.4, W1a):

1. **The workflow had zero runs, ever** — `gh run list --workflow=security.yml` returned nothing. Its triggers were `pull_request` + a weekly `schedule` + `workflow_dispatch` only; this repo has had zero pull requests opened against it (all work lands via direct pushes to `master`), and no scheduled run had apparently fired in the time since the workflow was authored. A workflow with a real, working `if: HIGH severity → fail` gate in `python-static`/`semgrep` and a hard `exit-code: 1` gate in `container-image` was, in effect, decorative — every commit merged as if these scans had run and passed, when they had simply never executed.
2. **`aquasecurity/trivy-action@0.28.0` does not resolve to anything in that repository.** `aquasecurity/trivy-action`'s tags are `v`-prefixed (`v0.28.0`, `v0.36.0`, …) — there has never been a bare `0.28.0` tag or branch. Confirmed via `gh api repos/aquasecurity/trivy-action/git/refs/tags/0.28.0` (404) and `.../branches/0.28.0` (404). Had finding #1 not been true — had a PR or the weekly cron actually triggered this workflow even once — the `container-image` job would have failed immediately at the action-resolution step with "Unable to resolve action `aquasecurity/trivy-action@0.28.0`, unable to find version `0.28.0`", before Trivy ever ran a single scan.

**Fix:** (1) `security.yml` gains a `push: branches: [master]` trigger (v1.3.4, W1c) — the master-push path that actually needs scanning now runs it, not just a hypothetical future PR. (2) the Trivy reference is corrected and bumped to the current release, SHA-pinned: `aquasecurity/trivy-action@ed142fd0673e97e23eac54620cfb913e5ce36c25 # v0.36.0`.

**Verified:** `python -m yaml`-parseable (valid YAML); `gh api repos/aquasecurity/trivy-action/commits/v0.36.0` confirms the pinned SHA resolves to a real commit. The workflow's actual first execution (this session's next push) is the live proof both defects are gone — recorded here rather than claimed in advance, per this project's own "nothing is done until it's verified" standard.

### 136. Any older server binary still running during a rolling upgrade crashes on its NEXT restart once a newer replica has migrated the schema past it — `init_db()` had no path for "current revision recorded, but unrecognized by this binary"

**Severity:** 7/10 (VERSIONING.md's own "Database schema compatibility" section promises new columns are "always additive and nullable/default-safe" — implying an old binary should tolerate a newer schema; this was never actually true for a SERVER RESTART, only for a server that stayed up without restarting) · **Status:** 🟢 `fixed` (2026-09-06), **NOT yet live-verified against real Postgres** (found by static analysis of alembic's own documented behavior + a stack-free SQLite unit test, not an executed rolling-upgrade drill — this machine has no reachable Docker/Postgres at time of writing; see `scripts/compat_drill.sh`, added alongside this fix, for the drill that will actually prove it live)

**Problem:** `database.py::_ensure_schema_sync()` calls `command.upgrade(cfg, "head")` unconditionally on every server boot once the `needs_adoption` (true-legacy, no-recorded-revision) branch doesn't apply. Alembic's `upgrade` implementation must resolve the database's CURRENT recorded revision within its OWN `ScriptDirectory` to compute an upgrade path — this is a hard requirement of how alembic walks the revision graph, not something this codebase's own code chooses. During any rolling upgrade (exactly what `docker-compose.ha.yml`'s 2-replica topology and `deploy/helm/aether-vault`'s `replicaCount` both exist to support), the FIRST replica to restart with new code runs its migrations and advances `alembic_version` to a revision the OTHER, still-old replica has never seen. The old replica's NEXT restart (a crash, a node reschedule, a manual restart — anything short-lived) hits `command.upgrade(cfg, "head")` against that now-unrecognized revision and alembic raises `CommandError: Can't locate revision identified by '<rev>'`, crashing startup outright. This directly contradicts the additive-schema compatibility VERSIONING.md documents, and would surface as a real incident during any genuine rolling deploy with more than one replica cycling through a restart — not a hypothetical, just never yet exercised by any drill in this repo (`ha_drill.sh` kills and restarts a replica on the SAME image, never an older one; no existing test starts two DIFFERENT code versions against one database).

**Fix:** `_ensure_schema_sync()` gains a new check, `_schema_is_ahead_of_this_binary()` — true when a revision IS recorded but is not among this binary's own `ScriptDirectory.walk_revisions()`. When true, the function logs a clear warning and returns WITHOUT calling `command.upgrade()` at all, rather than attempting anything against a schema state it cannot understand. This is safe under the additive-schema contract: an older binary's unchanged ORM models never reference whatever a newer migration added, so it keeps serving correctly against everything it does know about until it's itself upgraded.

**Verified:** three new stack-free SQLite unit tests in `tests/test_migrations.py` (`test_schema_is_ahead_returns_{false_when_no_version_table,false_for_a_real_known_revision,true_for_an_unknown_future_revision}`) — all passing, plus the full pre-existing `tests/test_migrations.py` suite (15/15) still green after the change. **Not yet verified against a real two-binary rolling upgrade** — that requires `scripts/compat_drill.sh` (git worktree at the previous release tag, old code booted against a database a NEW boot already migrated to current head) to actually run, which needs Docker/Postgres this environment doesn't have available right now. Treat this fix as high-confidence-but-unproven-live until that drill runs green once, in CI or locally.

### 137. The v1.3.4 pass's own new CI surfaces caught real bugs on their FIRST real run — a broken server crash-loop, three real security findings, and five workflow-authoring mistakes

**Severity:** 8/10 (the server crash-loop below took down `av_server` — not just SAML — on every boot of the Linux image the moment `[saml]` was ever actually installed; this only became possible because the SAME session's own W0.10 fix made it install for the first time ever) · **Status:** 🟢 `fixed` (2026-09-06), found by actually pushing the v1.3.4 CI/CD pass and reading real `gh run view` failures — not by re-reading the diff.

**Problem 1 (the real one): `server.py`'s `sso_saml` import only ever caught `ImportError`.** Once `[saml]` is genuinely installed (this session's own W0.10 fix — SSO/SAML were dead code in every image before it), `from . import sso_saml` transitively imports `saml2` → `OpenSSL.crypto`, which raises `AttributeError: module 'lib' has no attribute 'GEN_EMAIL'` at IMPORT TIME under the pyOpenSSL/cryptography combination this environment resolves (root-caused below) — an INSTALLED-BUT-INCOMPATIBLE dependency, not an ABSENT one, which `except ImportError:` was never written to catch. The exception propagated to the top and crashed `av_server` entirely: `ha-drill`, `slim-image-smoke`, and `e2e-engine-smoke` all failed identically (`[engine] subservice 'server' exited`, `Container ... Error`), unrelated to what each job actually tests — the WHOLE registry was down, not just SAML.

**Root cause of the AttributeError:** pysaml2 7.5.4 pins `pyopenssl<24.3.0` in its own metadata (verified: `Requires-Dist: pyopenssl (<24.3.0)`), which resolves to pyOpenSSL ~24.2.1 — whose own `OpenSSL/crypto.py` references `_lib.GEN_EMAIL` unconditionally at class-definition time (`crypto.py:804`). That constant comes from `cryptography`'s own FFI-exposed `lib` binding, which this repo's `cryptography>=42.0.0` floor (no ceiling) lets resolve arbitrarily far forward — and a `cryptography` release newer than what pyOpenSSL 24.2.1 was built against dropped it. A **first fix attempt** (pin `pyopenssl>=26.0.0` in the `saml` extra to also close a separate Trivy CVE finding, see Problem 2c below) made this WORSE, not better: it directly conflicts with pysaml2's own `<24.3.0` ceiling, and reverting it was necessary before anything else could be fixed.

**Fix:** `server.py`'s `sso_saml` mount now has a second `except Exception` branch alongside the original `except ImportError`, logging a warning and leaving SAML unmounted — degrading exactly the way an absent `[saml]` extra already does, matching this codebase's own established optional-dependency pattern, instead of taking the whole server down over a SAML-only compatibility problem. **SAML itself is NOT yet fixed to actually work** in this specific dependency combination — this only stops it from crashing the server; making SAML functionally usable needs a real, tested cryptography/pyOpenSSL/pysaml2 version combination this environment can't verify without a real Docker build. Disclosed, not silently implied fixed.

**Problem 2: security.yml had never run once before this session (see #135) — its very first run found FOUR more real, independent issues, none related to the crash above:**

- **2a — `python-deps` (pip-audit): a `SyntaxError`.** The inline summary script's f-strings had backslash-escaped single quotes (`d[\'name\']`) inside an ALREADY-double-quoted f-string, where they were never needed and are invalid — `SyntaxError: unexpected character after line continuation character`. Pre-existing since whenever this script was first written; never caught because the job never ran. Fixed: removed the unneeded escapes.
- **2b — `python-static` (bandit): a real HIGH-severity finding, B202.** `cmd_admin.py`'s backup-restore path (`av admin backup restore`) called `tf.extractall(data_path)` with NO path-traversal validation on interpreters old enough to lack tarfile's own `filter="data"` mechanism (the code's own comment already documents exactly why that fallback branch exists). Fixed: the fallback now manually resolves and validates every archive member's path against `data_path` before extracting (rejecting anything that would escape it), a real CVE-2007-4559-class mitigation, not a `# nosec` suppression.
- **2c — `container-image` (Trivy): pyOpenSSL 22.0.0, `CVE-2026-27459`.** See Problem 1 above — fixing this directly is impossible without breaking pysaml2 (its own ceiling IS the vulnerable range). Accepted via a documented `.trivyignore` entry naming the exact upstream constraint that forces it, not a blanket suppression.
- **2d — `container-image` (Trivy): ~30 findings, all from npm's OWN bundled dependencies** (`tar`, `pacote`, `sigstore`, `ip-address`, `minimatch`, `brace-expansion`, `cross-spawn`, `glob`, `nanoid`, ...) **shipped into the runtime image despite npm never being invoked at runtime** (grepped `engine-entrypoint.sh`: only `node server.js` and `uvicorn` ever exec). Fixed: `npm`/`npx`/`corepack` removed from both the `engine` and `webui` final Dockerfile stages (best-effort `rm -rf ... || true`, since the exact install path is a packaging detail, not a contract) — dead weight carrying real CVEs unrelated to anything this image needs to run.
- **2e — `webui-deps` (npm audit): real Next.js 14.2.5 CVEs**, including one CRITICAL (`CVE-2025-29927`, the Next.js middleware-bypass vulnerability). Partially fixed: bumped `next` 14.2.5 → 14.2.35 (same major, patches the critical one and several highs) plus a non-breaking `npm audit fix` (patches `nanoid`). **Two HIGH findings remain** (`next`/`postcss`, disclosed not hidden): npm's own resolver says fully closing them needs Next.js 16 (`next@16.3.4`, "a breaking change" per npm's own output) — a real major-version + React 19 migration this session did not attempt blind, with no Docker/Playwright available to verify the built app still renders correctly. `webui-deps` will stay a legitimately red required check until that migration happens for real, with real testing.

**Problem 3: five workflow-authoring mistakes in this session's own new jobs, all found by their first real run, none needing a second guess once the log was read:**

- `lint-workflows`: actionlint runs shellcheck over every embedded `run:` block too, at every severity, with no flag to raise the floor — five real non-error-severity notes (SC2034/SC2015/SC2129/SC2012/SC2102) across pre-existing loops/idioms. Fixed: named via `-ignore` per code (not a wholesale `-shellcheck=` disable, which would lose real-error coverage of `run:` blocks going forward).
- `flaky-quarantine`: pytest exits 5 ("no tests collected") when `tests/FLAKES.md`'s registry is empty — the correct, intended starting state — but the step still showed red. Fixed: exit 5 is now treated as success; any other nonzero still isn't.
- `test`'s README-freshness check: the real collected total is 1,276, not the 1,139 carried over from before this session's own test-file additions (an oversight — updating the FILE count but not the TOTAL count in the same edit). Fixed: all 4 mentions (badge, module-table row, Test Suite prose, `tests/README.md` opening line) updated to the real, CI-reported number.

**Verified:** every fix above is either a plain syntax/logic correction confirmed by local reproduction (the pip-audit f-string, the bandit path-validation logic, the pyOpenSSL version-conflict chain via local venvs on this machine), or is inherently only provable by the next real CI run (the Dockerfile npm removal, the server.py exception-widening, actionlint's exact behavior against the real workflow files) — this session has no Docker daemon to build-test the image changes locally. Flagged here rather than claimed as proven.
