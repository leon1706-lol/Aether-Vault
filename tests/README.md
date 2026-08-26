# `tests/` — Test Suite

~590 tests across 25+ files covering the CLI, the C++ bindings, the registry server, the
plugins, and the webui logic. Run with `pytest tests/ -q` (or `av test`). See the
[main README](../README.md).

## Layout

| File | Covers |
|---|---|
| `conftest.py` | The `repo` fixture: an initialized temp repo per test |
| `test_cli.py` | The big one: init/add/commit/checkout/branch/stash/log/doctor/file/attributes, short-hash checkout, chunk-dedup round-trips |
| `test_cli_commands.py` | Direct command-function tests (upload batching, tree building) |
| `test_sync.py` | `av clone`/`av pull` against a fake in-process registry built on the real `VaultClient`; v1.2.2: signature/env-id survive clone |
| `test_merge.py` | Pure three-way merge + merge-base algorithms, plus end-to-end CLI merges (FF, two-parent, conflict abort, `--ours`/`--theirs`) |
| `test_server.py` | FastAPI suite against live Postgres+Redis (skips when unreachable), incl. merge-parent round-trip, two-repo clone/pull E2E, and the v1.2.2 audit-outcome / webhook-delivery-ledger / signature-wire coverage |
| `test_core.py` | C++ binding contract: canonical hashing, safetensors split, CDC chunk stability |
| `test_dataset_cdc.py` | v1.2.2: CDC boundary stability + determinism + `.avattributes` matrix across EVERY chunkable extension |
| `test_signing.py` | v1.2.2: ed25519 keygen/auto-sign/verify — roundtrip, tamper on every field, unsigned-ok, canonical golden bytes (skips without `[sign]`) |
| `test_v122.py` | v1.2.2 units: dedup_efficiency → .avh flow-through, avh schema-file validation, audit-list param contract |
| `test_vault.py` | Index/pointer/handoff units incl. `find_commit_file` prefix resolution |
| `test_stash.py`, `test_client.py`, `test_repl.py`, `test_ui.py` | Stash semantics, HTTP-client contracts (401 → `AuthenticationError`), REPL degradation, rendering |
| `test_docker_runtime.py`, `test_update_check.py`, `test_speedcheck.py`, `test_graph.py`, `test_tool_runner.py`, `test_fsutil.py`, `test_dependency_guards.py`, `test_registry.py`, `test_plugins.py` | Focused units for their named modules |
| `test_perf_gate.py` | Hot-path budget gate at 2× (incl. the semdiff probe) |
| `test_migrations.py` | Alembic chain heads/DDL rendering + heal guards |

## Conventions

- **Import path**: tests import `python.av_cli...` from a checkout; this requires the
  `pythonpath = ["."]` pytest option (set in `pyproject.toml`) — bare `pytest` from any
  cwd works because of it.
- **Live-server pattern** (`test_server.py`): reachability is checked *lazily inside the
  test body*, never at collection time, so a missing Docker stack degrades to skips.
- **Fake clients**: clone/pull/merge tests monkeypatch `VaultClient` with fakes that
  implement the real method surface — network-free but code-path-faithful.
- New features add tests for every surface they touch (`tests/test_cli.py` for commands,
  `test_core.py` for C++, `test_server.py` for endpoints) — see
  [`../Aether-vault-Obsidian-Vault/Essential-Tasks.md`](../Aether-vault-Obsidian-Vault/Essential-Tasks.md).
