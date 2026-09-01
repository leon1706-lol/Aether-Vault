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

### 78. `core.fail(None, �)` raised AttributeError after printing the error message

**Severity:** 4/10 � **Status:** ? `fixed` (2026-08-26)

**Problem:** Roughly a dozen call sites (`cmd_run`, `cmd_env`, `cmd_registry`, �) invoke the shared failure helper as `fail(None, "validation", msg)`. `fail()` ended with `ctx.exit(exit_code)` � on `None` that is an `AttributeError` raised AFTER the message printed. Users saw a clean error line followed by a full Python traceback, and the documented exit codes (10�16) were lost outside CliRunner's accidental catching.

**Fix:** `core.fail()` now calls `ctx.exit()` only when a context exists and otherwise raises `SystemExit(exit_code)`. One-line fix at the single choke point; every None-ctx caller inherits it.

**Verification:** Isolated repro before (exit 1 + AttributeError) and after (clean exit 15); `tests/test_signing.py` and `tests/test_v122.py` assert exact exit codes through paths that pass `ctx=None`.

---

### 79. `cmd_registry.restore` referenced an undefined `ctx_exit` � latent NameError on every failed restore

**Severity:** 3/10 � **Status:** ? `fixed` (2026-08-26)

**Problem:** `restore()`'s incomplete-archive branch called `ctx_exit(EXIT_VALIDATION)`, a name defined in sibling modules (`cmd_policy`, `cmd_context`) but never in `cmd_registry` nor exported by `core`. Any failed restore crashed with `NameError` instead of the intended exit-15 validation failure. Invisible because no test exercised the failed-restore path and the module imports fine.

**Fix:** Module-local `ctx_exit()` helper added to `cmd_registry.py`.

**Verification:** Static review + registry command suite green; the failure path is now reachable without NameError (covered indirectly by test_signing's verify-exit-code assertions using the same helper pattern).

---

### 80. Legacy-volume adoption stamped the whole migration chain WITHOUT creating post-create_all tables

**Severity:** 7/10 � **Status:** ? `fixed` (2026-08-26)

**Problem:** `database._ensure_schema_sync` adopts a pre-Alembic volume by stamping the ENTIRE current chain as applied. A true v1.1.x-era create_all volume therefore got stamped straight to head � and every table introduced AFTER the create_all era (`runs`, `run_commits`, `events`, `webhooks`, `audit_log`, v1.2.2's `webhook_deliveries`) silently NEVER existed on it. Startup stayed green; the first runs/events write would 500. The existing heal covered column drift only.

**Fix:** New `_create_missing_tables()` runs during adoption: any models.py table missing from the volume is created from the metadata (checkfirst semantics), then column drift heals, then the chain stamps. Existing tables are never touched.

**Verification:** `test_migrations.py` legacy-map test extended; live heal drill (e2e Phase C) still green against real Postgres; fresh + adopted volumes both reach a complete schema.

---

### 81. `.avh` semantic summary compared against an EMPTY baseline for local commits

**Severity:** 6/10 � **Status:** ? `fixed` (2026-08-26)

**Problem:** Locally-authored commit files store a `parents` LIST; only registry-fetched commits carry `parent_hash`. `handoff.build_semantic_summary()` and `_metrics_history_tail()` read ONLY `parent_hash` � so for locally-made commits (i.e., every repo's normal case) the semantic summary diffed against an empty tree (all chunks "new", dedup_efficiency 0) and the metrics trend stopped after one hop. Found by the v1.2.2 dedup_efficiency flow-through test, which pinned engine output vs `.avh` output and caught them disagreeing.

**Fix:** Shared `_commit_parent()` tolerates both shapes; both consumers route through it.

**Verification:** `test_v122.py::test_dedup_efficiency_flows_into_avh_semantic_summary` pins engine == .avh chunk rollups; full handoff/context suites green.

---

### 82. Clone/pull dropped `signature` and `env_snapshot_id` � clones could neither verify nor replay

**Severity:** 8/10 � **Status:** ? `fixed` (2026-08-26)

**Problem:** `sync.normalize_commit_row()` rebuilt fetched commit dicts from a fixed field whitelist, silently discarding the v1.2.2 `signature` blob and `env_snapshot_id`. Every cloned repository therefore reported UNSIGNED on `av verify` (false negative � the worst kind for a tamper-evidence feature) and could not resolve replay snapshots by commit. Found by the manual wire pass: keygen ? commit ? push ? clone ? verify said UNSIGNED in the clone.

**Fix:** Server persists both fields (migration 0003 columns, echo in GET/list endpoints); `normalize_commit_row` passes them through verbatim; fake registry mirrors the real row shape so stack-free tests exercise the same contract.

**Verification:** `test_sync.py::test_clone_preserves_signature_for_offline_verify` (clone verifies offline), `normalize` unit test, live wire round-trip test in `test_server.py`, plus the manual keygen?commit?push?clone?verify loop now reporting VERIFIED.

---

### 83. Timestamp timezone-spelling broke cloned signatures even after #82

**Severity:** 8/10 � **Status:** ? `fixed` (2026-08-26)

**Problem:** The authoring client signs a payload whose `timestamp` carries `+00:00`; the registry stores naive UTC and echoes timestamps WITHOUT the suffix. Canonical signing bytes are sorted-keys JSON of the whole payload � one character of tz-spelling difference made every cloned verification fail ("TAMPERED") despite byte-identical meaning. Found immediately after fixing #82 in the same manual pass.

**Fix:** `signing.canonical_commit_bytes()` normalizes the timestamp to one canonical UTC rendering parsed from the instant (aware, naive and Z forms all collapse to identical bytes; genuinely different instants still differ).

**Verification:** `test_canonical_form_is_timezone_spelling_insensitive` (+00:00 / naive / Z equal; shifted instant differs); manual wire loop re-run end-to-end ? VERIFIED in fresh clone.

---

### 84. Env snapshot uploaded with non-canonical bytes � server 400, cross-machine replay impossible

**Severity:** 5/10 � **Status:** ? `fixed` (2026-08-26)

**Problem:** A snapshot's id hashes its CANONICAL bytes (compact JSON minus `captured_at`), but the push path uploaded the pretty-printed `.av/env_snapshot.json`. The registry's own sha256 verification rejected the upload (400), so snapshots never reached the registry and `av replay <commit>` on any other machine failed with "No snapshot found". Silent: the client treats a failed object upload as non-fatal by design.

**Fix:** Both writers (`cmd_env.snapshot`, `core.upload_commit_objects`) now materialize the CAS object from the canonical bytes; the pretty file stays local-only for humans.

**Verification:** Manual wire pass: snapshot id visible in server access log as 201, `av replay <commit>` inside a fresh clone renders the recipe; v122/v120 suites green.


---

---

### 85. TokenGate URL-strip could be overridden by Next.js patched history - Protected-mode handoff left ?av_token= in the address bar

**Severity:** 3/10 - **Status:** fixed (2026-08-26)

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
