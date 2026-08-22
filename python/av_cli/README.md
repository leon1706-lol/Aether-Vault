# `av_cli` — The `av` Command Line Interface

Everything behind the `av` command: repo initialization, the staging index, the local
commit DAG, content-addressed storage, remote sync, merges, and the diagnostic/benchmark
tooling. See [`../README.md`](../README.md) and the [main project README](../../README.md).

## Commands & their modules

| Module | Covers |
|---|---|
| `main.py` | All Click commands (`init/add/commit/checkout/branch/push/webui/gc/auth/...`) plus the shared internals: `_materialize_tree`, `_finalize_commit`, `_collect_dirty_paths`, `materialize_file`, `upload_commit_objects`, pending-push queue, doctor. Keep it an orchestration layer — real logic belongs in a sibling module |
| `history.py` | `av log`: parent-chain walking, branch decorations, rendering (pure-local, zero network) |
| `sync.py` | `av clone` / `av pull` primitives: project resolution, paginated history fetch, object pre-fetch (batch-check + parallel downloads), ancestry checks |
| `merge.py` | Pure merge algorithms: nearest common ancestor + per-path three-way tree merge (no I/O — unit-testable in isolation) |
| `attributes.py` | `.avattributes` parsing (`no-chunk`, `no-layer-split` staging directives) |
| `index.py` | The staging index (`.av/index`): entries with hash/size/mtime/type/layers/chunks |
| `client.py` | `VaultClient` — every registry HTTP call (objects, commits, refs, projects, batch existence checks) |
| `pointer.py`, `fsutil.py`, `exceptions.py` | LFS pointer files, atomic-write helpers, error types |
| `docker_runtime.py` | Local backend lifecycle: compose detection, start/rebuild/update of server+webui containers |
| `update_check.py` | PyPI version check, opt-in silent auto-update |
| `speedcheck.py` | Hot-path probes behind `av doctor --speed` / `av test --speed` |
| `handoff.py`, `graph.py` | Agent handoff snapshots (`.avh`) and the Obsidian code-graph generator |
| `repl.py`, `ui.py`, `enterprise.py` | Interactive session, rich/questionary rendering, the Enterprise auth seam |

## Design invariants worth knowing before editing

- **Canonical hashing**: `hash_file_safe` must always produce plain SHA-256; layer/chunk
  shards are additional objects, never replacements for content addressing.
- **One restore path**: checkout, clone, pull, and merge all go through
  `_materialize_tree()` — don't add another working-tree writer.
- **One commit-creation path**: normal commits and merges both end in `_finalize_commit()`
  (deterministic sorted-JSON hash → atomic persist → ref move → push/queue).
- **Latency discipline**: heavy imports are lazy (`client`, `aether_core`, `ui`),
  multi-object network work is batch-checked then parallelized.
