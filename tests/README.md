# tests

Owns Aether-Vault's pytest suite: 1,276 tests across 69 files covering the CLI, the
C++ bindings, the live registry server, the plugins, and the webui logic. Run with
`pytest tests/ -q` (or `av test`); the skip-summary hook prints WHY anything skipped.

## Layout

- `conftest.py` / `skipsummary.py` - the `repo` fixture (initialized temp repo per
  test) and the end-of-run skip bucketing (prints the exact `docker compose up` hint).
- `test_cli.py`, `test_cli_commands.py` - the big behavioral surface: init/add/commit/
  checkout/branch/stash/log/doctor/file/attributes, short-hash checkout, chunk-dedup
  round-trips.
- `test_sync.py` - clone/pull against a fake in-process registry built on the real
  `VaultClient`; signatures/env ids survive clone.
- `test_merge.py` - pure three-way merge + merge-base, plus CLI-level FF/two-parent/
  conflict paths.
- `test_server.py` - live Postgres+Redis suite (lazy TCP reachability skip): wire
  round-trips, audit outcome capture, webhook delivery ledger + dead-letter,
  signature persistence, two-repo E2E, `av registry export`/`restore` full round trip
  (layers, CDC chunks, a merge commit, a signed commit), run metrics/lineage/policy-
  outcome endpoints.
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
- `test_contracts.py` / `test_contract_matrix.py` - drives the real CLI/server and
  validates live output against every published JSON Schema; table-driven exit-code +
  `error.code` matrix across every command x mode x code, plus the generic anti-leakage
  sweep (parametrized over `cli.commands`).
- `test_tool_runner.py`, `test_perf_history_script.py` - `benchmarks/` shared infra
  (tool detection, verdict rating, table/markdown rendering) and
  `scripts/append_perf_history.py`'s pure merge/render logic.
- `test_release_gate.py`, `test_ci_policy.py` - `scripts/release_gate.py`'s checks
  (perf-history tag, CHANGELOG/VERSIONING sync, benchmarks-sha ancestry + MINOR-release
  freshness, every required check green) and the permanent no-dependency-bots/no-
  auto-merge/every-action-SHA-pinned guard over `.github/`.
- `test_ci_map.py`, `test_ci_summary.py`, `test_deprecations.py`, `test_flake_registry.py`,
  `test_helm_chart.py` - CI-map/budget doc-vs-YAML consistency,
  `scripts/ci_summary.py`'s pure logic, `development/deprecations.yml`'s schema + overdue
  guard, the flake-quarantine policy, and the Helm chart's default image matching its
  real publisher.
- `test_docs_commands.py` - parses every fenced `av ...` command out of `docs/*.md` and
  resolves it against the live Click tree, so documentation rot is a test failure.
- `test_benchmark_docs_freshness.py` - guards README.md's/benchmarks/README.md's
  hand-authored benchmark tables against drifting out of sync with the real
  `benchmarks/bench_*.py` count, and against an unfilled "capture pending"-style
  placeholder surviving past the run that should have replaced it.
- `test_readme_test_count_freshness.py` - guards this file's own opening line and
  README.md's test-count mentions against the real `tests/test_*.py` file count; the
  companion check on the actual test *counts* (which need a real run, not just
  collection) lives in `scripts/check_readme_test_freshness.py`, wired into the `test`
  CI job instead.

## Conventions

- Tests import `python.av_cli...`; `pythonpath = ["."]` in `pyproject.toml` makes bare
  `pytest` work from any cwd.
- Live-server reachability is checked lazily INSIDE test bodies, never at collection.
- Fake clients implement the real method surface - network-free but code-path-faithful.
- New features add tests for every surface they touch; see
  `../Aether-vault-Obsidian-Vault/Essential-Tasks.md`.
