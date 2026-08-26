# tests

Owns Aether-Vault's pytest suite: ~590 tests across 25+ files covering the CLI, the
C++ bindings, the live registry server, the plugins, and the webui logic. Run with
`pytest tests/ -q` (or `av test`); the skip-summary hook prints WHY anything skipped.

## Layout

- `conftest.py` / `skipsummary.py` - the `repo` fixture (initialized temp repo per
  test) and the end-of-run skip bucketing (prints the exact `docker compose up` hint).
- `test_cli.py`, `test_cli_commands.py` - the big behavioral surface: init/add/commit/
  checkout/branch/stash/log/doctor/file/attributes, short-hash checkout, chunk-dedup
  round-trips.
- `test_sync.py` - clone/pull against a fake in-process registry built on the real
  `VaultClient`; signatures/env ids survive clone (v1.2.2).
- `test_merge.py` - pure three-way merge + merge-base, plus CLI-level FF/two-parent/
  conflict paths.
- `test_server.py` - live Postgres+Redis suite (lazy TCP reachability skip): wire
  round-trips, audit outcome capture, webhook delivery ledger + dead-letter,
  signature persistence, two-repo E2E.
- `test_core.py` / `test_dataset_cdc.py` - binding contract; CDC boundary stability +
  `.avattributes` matrix across EVERY chunkable extension.
- `test_signing.py` - ed25519 keygen/auto-sign/verify: roundtrip, tamper on every
  field, unsigned-ok, canonical golden bytes (skips without `[sign]`).
- `test_v122.py` / `test_v120.py` / `test_av_sdk.py` / `test_semdiff.py` /
  `test_webhooks_cli.py` - version-surface units incl. dedup_efficiency flow-through,
  schema-file validation, SDK seam parity.
- `test_perf_gate.py`, `test_speedcheck.py`, `test_migrations.py`,
  `test_docker_runtime.py`, `test_auth_users.py`, `test_rate_limit.py`, ... -
  focused units for their named surfaces.

## Conventions

- Tests import `python.av_cli...`; `pythonpath = ["."]` in `pyproject.toml` makes bare
  `pytest` work from any cwd.
- Live-server reachability is checked lazily INSIDE test bodies, never at collection.
- Fake clients implement the real method surface - network-free but code-path-faithful.
- New features add tests for every surface they touch; see
  `../Aether-vault-Obsidian-Vault/Essential-Tasks.md`.
