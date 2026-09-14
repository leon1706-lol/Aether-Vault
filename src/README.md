# src

Owns the C++17 performance core bound into Python as the `aether_core` pybind11
extension: canonical hashing, safetensors layer-splitting, and content-defined chunking,
all parallelized on a shared thread pool. Built by `setup.py`; everything above the
pybind11 boundary lives in `python/`.

- `core.cpp` - all bindings: `hash_file` (canonical), `hash_file_tree`
  (benchmark-only), `hash_bytes`, `hash_and_copy`, metadata helpers, `hash_backend`/
  `set_hash_backend`, `release_pool`, `split_and_hash_safetensors` (per-tensor layer split +
  hash, fully parallel per-layer re-read), `chunk_and_hash_file` (CDC chunking for opaque
  checkpoints), `stage_cdc` (V1.6.0: fused single-read staging -- whole-file hash + per-chunk
  hash + CAS write in one pass, sharing `chunk_and_hash_file`'s exact cut logic via the
  `CdcCutter` struct both use; see invariant 7), `stage_safetensors` (V1.6.0: the safetensors
  counterpart -- header parse + per-layer hash + whole-file hash + CAS write in one
  sequential pass, streaming a layer over `buffer_cap_bytes` straight to a temp file instead
  of buffering it whole; throws on overlapping declared layer ranges rather than guessing,
  see invariant 8). Every path-taking call goes through the file-local `to_path()` helper
  (`fs::u8path`), never a bare `fs::path(std::string)` conversion -- see invariant 6.
- `sha256.h/.cpp` - the `SHA256` class (`update`/`hexdigest`, one-shot `hash_bytes`,
  `backend_name`/`set_backend`). `transform()` delegates to whichever `Sha256BlockFn` the
  process resolved (see below) instead of a fixed compression routine.
- `sha256_backend.h/.cpp` - the portable scalar SHA-256 compression function (always
  available, the correctness reference every other backend is self-tested against) plus
  `sha256_pick_backend()`: capability-checks a requested/auto-detected backend AND runs it
  through a correctness self-test (two digests computed independently via Python's
  `hashlib`, not typed from memory) before trusting it. `AV_SHA256_BACKEND` env var
  (`auto`/`scalar`/`sha-ni`/`arm-sha2`) forces a choice for diagnosis/testing.
- `sha256_shani.cpp` - x86(-64) SHA-NI backend (GCC/Clang: function-level
  `__attribute__((target("sha,sse4.1,ssse3")))`, no extension-wide compile flag needed; MSVC:
  intrinsics work unconditionally, gated purely by the runtime CPUID check). **Written and
  reviewed but not execution-verified on real SHA-NI hardware** (this project's reference
  machine predates SHA-NI by several CPU generations) -- see the file's own top-of-file
  caution comment before removing it.
- `sha256_arm.cpp` - ARMv8-A crypto-extension backend. Capability detection
  (`sha256_arm_supported()`'s underlying check) is real; the accelerated kernel itself is
  **deliberately not implemented yet** (`sha256_arm_supported()` returns `false`
  unconditionally) -- this development environment has no ARM toolchain to compile-check
  even a syntax error against, let alone verify correctness. ARM64 builds run scalar until
  this lands on real ARM64 hardware or CI; see the file's top-of-file comment.
- `thread_pool.h` - C++11 future-based pool, capped at `kMaxPoolThreads` (16, `core.cpp`)
  regardless of `hardware_concurrency()`; shared by the parallel hasher, safetensors
  splitter, and CDC pass 2. `release_pool()` frees it between requests (an idle daemon).
- `json.hpp` - vendored nlohmann/json (safetensors header parsing).
- `launcher/` - the native `av` launcher (a separate executable, `av_launcher.cpp`, not part
  of the `aether_core` extension). One shared implementation for Windows and POSIX
  (Linux/macOS); see `launcher/README.md` for exactly what's build-and-run-verified locally
  (Windows) versus CI-only-verified (POSIX), the precompiled Windows fallback shim, and the
  real bugs its own test suite found.

## Invariants you must not break

1. **`hash_file` is the canonical whole-file SHA-256** and must equal
   `hashlib.sha256(data).hexdigest()` - the server re-verifies every upload against it.
   The parallel *tree* hash is a different value, bound separately as
   `hash_file_tree`; never swap them.
2. **CDC determinism**: `chunk_and_hash_file`'s gear table is generated from a fixed
   seed; boundaries (and therefore shard hashes) must reproduce identically on every
   machine, or dedup silently stops working.
3. **Untrusted inputs**: safetensors headers are attacker-controllable - keep the
   bounds checks on `header_size` / `data_offsets` intact.
4. **`chunk_and_hash_file`'s `max_chunk` is a true hard cap, `min_chunk` a true floor,
   on every chunk including the last.** Both directions have broken before (Probleme.md):
   a near-EOF cut got silently suppressed and let a chunk exceed `max_chunk`; the fix for
   that then over-fired and split ordinary files nowhere near `max_chunk`. Any change to
   the cut logic needs `test_chunk_and_hash_file_max_chunk_is_a_hard_cap_deterministic`
   (deterministic) green, not just the random-data test (only ~e^-15 odds of ever
   reaching the edge).
5. **Every SHA-256 backend must produce byte-identical output to `hashlib.sha256`, always,
   on every input length.** `sha256_pick_backend()`'s mandatory self-test is the runtime
   safety net (a backend that fails it is never selected, full stop), but that self-test
   exercises exactly one 64-byte block -- any change to a backend needs
   `tests/test_core.py`'s golden-vector, randomized-length, and boundary-size tests green,
   and `AV_SHA256_BACKEND=scalar` is the reference to diff a new backend's output against
   during development. Never widen a path-taking function's signature to accept a bare
   `std::string` passed straight to `fs::path`/`std::ifstream`/`std::ofstream` without going
   through `to_path()` (`core.cpp`) -- see invariant 6, a real bug this project shipped.
6. **Paths from Python are UTF-8; never let one reach `fs::path`'s `std::string`
   constructor directly.** On Windows that constructor decodes via the process's ANSI code
   page, not UTF-8 -- a real, reproduced bug (`tests/test_core.py::test_hash_file_non_ascii_path`,
   Probleme.md): a file with a non-ASCII name existed on disk but `hash_file` reported "File
   not found". `core.cpp`'s `to_path()` (`fs::u8path`) is the only correct conversion; use it
   for every `fs::exists`/`fs::file_size`/`fs::last_write_time`/`std::ifstream`/
   `std::ofstream` call, no exceptions.
7. **`stage_cdc` and `chunk_and_hash_file` must never disagree on a cut boundary.** Both
   share the exact same `CdcCutter` struct (`core.cpp`) specifically so this is structurally
   impossible rather than merely tested for -- do not give `stage_cdc` its own copy of the
   boundary logic, even a "simplified" one, no matter how small the change looks. Any edit
   to `CdcCutter::feed()` needs both `test_chunk_and_hash_file_max_chunk_is_a_hard_cap_deterministic`
   AND `tests/test_core.py`'s `test_stage_cdc_*` suite green (they cross-check each other,
   not just their own oracle) plus `tests/test_staging_internals.py`'s fused-vs-legacy
   byte-for-byte comparison at the Python integration level.
8. **`stage_safetensors` must produce byte-identical per-layer name/hash/size/offset to
   `split_and_hash_safetensors`, in the same order, for every file both can handle.** Unlike
   `stage_cdc`/`chunk_and_hash_file`, the two don't share one boundary-decision struct --
   layer boundaries come straight from the header's own declared `data_offsets`, so there's
   no rolling-hash logic to factor out, but that also means there's no structural guarantee
   against the two implementations drifting on layer ORDER, gap handling, or the
   `__header__` pseudo-layer's own bytes (a real, fixed bug this project shipped: the fused
   path's header parse consumes the header's bytes from the stream to parse it, and a first
   draft then re-read from the now-advanced stream position for the `__header__` layer
   itself, hashing the wrong bytes entirely -- caught by `tests/test_core.py`'s
   `test_stage_safetensors_header_only_no_tensors`, which fails immediately and loudly
   rather than silently on any file with header content, exactly why that test exists as
   the simplest possible case, not just the more elaborate multi-layer ones). Any change to
   either function needs `tests/test_core.py`'s `test_stage_safetensors_*` suite green
   (cross-checks against `split_and_hash_safetensors` as the oracle for every case) plus
   `tests/test_staging_internals.py`'s fused-vs-legacy byte-for-byte comparison. The one
   case they deliberately do NOT need to agree on: `stage_safetensors` throws on
   overlapping declared layer ranges (structurally unrepresentable in a single sequential
   pass), while `split_and_hash_safetensors` handles them fine (each layer independently
   re-reads its own range) -- that's the documented fallback trigger, not a bug.
9. **Memory envelope is bounded per in-flight call, never per file size (V1.6.3).** Read
   buffers are 1 MiB; `stage_safetensors` holds one layer buffer of at most
   `buffer_cap_bytes` (larger layers stream through a temp file) and the header exactly
   ONCE (raw bytes parsed in place, DOM dropped after the layer table is built, raw bytes
   dropped after `__header__` is published); `stage_cdc` holds one chunk (≤ `max_chunk`);
   `hash_file_tree` keeps at most `min(pool, 4)` chunk tasks (and their `chunk_size`
   buffers) in flight, consuming results oldest-first so its output is unchanged
   (`tests/test_core.py::test_hash_file_tree_matches_python_oracle_*` is the oracle -- it
   had none before). The numbers in `development/MEMORY.md` derive from these bounds; a
   change that reads a whole file or layer into memory breaks them.

## Rebuild after editing

```bash
pip install -e . --no-build-isolation --no-deps   # recompiles the extension in-place
pytest tests/test_core.py -q                      # binding-level sanity
```
