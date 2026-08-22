# `src/` — C++ Performance Core (`aether_core`)

The pybind11 extension that makes Aether-Vault fast: canonical hashing, safetensors
layer-splitting, and content-defined chunking, all parallelized. Built by `setup.py`
(C++17); imported from Python as `aether_core`. See the [main README](../README.md).

## Contents

| File | Purpose |
|---|---|
| `core.cpp` | All pybind11 bindings: `hash_file` (canonical), `hash_file_tree` (benchmark-only), `hash_bytes`, metadata helpers, `split_and_hash_safetensors` (per-tensor layer split + hash), `chunk_and_hash_file` (CDC chunking for opaque checkpoints) |
| `sha256.h/.cpp` | Streaming SHA-256 implementation (`update`/`hexdigest`, plus one-shot `hash_bytes`) |
| `thread_pool.h` | C++11 future-based thread pool sized to `hardware_concurrency()`; shared by the parallel hasher, safetensors splitter, and CDC pass 2 |
| `json.hpp` | Vendored [nlohmann/json](https://github.com/nlohmann/json) (safetensors header parsing) |

## Invariants you must not break

1. **`hash_file` is the canonical whole-file SHA-256** and must equal
   `hashlib.sha256(data).hexdigest()` — the server re-verifies every upload against it.
   The parallel *tree* hash is a different value and is bound separately as
   `hash_file_tree`; never swap them.
2. **CDC determinism**: `chunk_and_hash_file`'s gear table is generated from a fixed seed;
   boundaries (and therefore shard hashes) must reproduce identically on every machine,
   or dedup silently stops working.
3. **Untrusted inputs**: safetensors headers are attacker-controllable — keep the
   bounds checks on `header_size` / `data_offsets` intact.

## Rebuild after editing

```bash
pip install -e . --no-build-isolation --no-deps   # recompiles the extension in-place
pytest tests/test_core.py -q                      # binding-level sanity
```
