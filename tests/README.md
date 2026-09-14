# tests

Owns Aether-Vault's pytest suite: 1,762 tests across 97 files covering the CLI, the
C++ bindings, the live registry server, the plugins, and the webui logic. Run with
`pytest tests/ -q` (or `av test`); the skip-summary hook prints WHY anything skipped.

## Running the suite on a small machine

One `pytest tests/` process gets OOM-killed on a ~4 GB box once the Docker stack is up.
`python scripts/run_tests_lowmem.py` (or `av test --lowmem`) runs one pytest subprocess per
test file — `test_server.py`/`test_daemon.py` in 40/60-test chunks — waiting for
`--min-free-mb` of free RAM before each, resumable from a state file after a kill, and
reports each file's peak RSS. It is how this project's own dev box gets a full green run.

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
- `test_plugins.py` - `av_plugins`' seam: real-callback tests for Lightning/Transformers/
  vanilla PyTorch (skip cleanly without the extra), MLflow run import, the scoped-commit
  guarantee (unrelated staged files untouched), and the seam/SDK/CLI parity section
  (payload shape, run-id linkage, env_snapshot_id, queued semantics, error codes).
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
- V1.6.3 footprint suite - `test_sysres.py` (the dependency-free RSS/RAM probes and the
  process-TREE child sampler every memory number rests on), `test_rss_scoreboard.py`,
  `test_memory_gate.py` (opt-in, `AV_MEMORY_GATE=1`: peak-RSS budgets, median-of-3),
  `test_stage_workers.py` (RAM-aware staging worker cap + cgroup-aware C++ thread count),
  `test_index_loads.py` (one `Index` parse per command, streamed byte-identical
  `Index.save`, and the `av watch` re-commit regression), `test_history_footprint.py`
  (tree-less bounded `log --all`), `test_registry_export.py` (objects streamed, never
  whole), `test_gc_mark.py` (the server's mark phase over compact tuples, differential
  against the old ORM walk), `test_server_units.py` (stack-free: every GET `limit` has a
  maximum, the bounded auth-failure/principal/metrics state, the upload cap, RSS gauges),
  `test_compose_files.py` + `test_engine_healthcheck.py` (the three compose files' memory
  knobs/limits and the fork-free `/dev/tcp` healthcheck script, driven for real against an
  HTTP stub), `test_doctor_resources.py`, `test_benchmark_lowmem.py`, `test_lowmem_tests.py`
  (the file-per-subprocess runner behind `scripts/run_tests_lowmem.py` / `av test --lowmem`,
  exercised with real pytest children). `test_server.py`'s `TestFootprintV163` class holds
  the live-stack half (streamed audit export/verify, 413 by length and mid-stream, GC
  keeping layers/chunks, paged refs, metrics gauges).
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
