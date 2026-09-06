# scripts

Owns standalone developer tooling that isn't part of the installed package - things
you run by hand, occasionally, while working on the codebase.

- `run_benchmark_comparison.py` - convenience wrapper running the full cross-tool
  benchmark suite (`benchmarks/`) and writing the Markdown report - what
  `av benchmark --markdown` does, usable without an editable install's console script.
- `check_eager_annotations.py` - AST guard flagging annotations that reference names
  imported later in the file: the py3.10-vs-3.14 eager-annotation trap that once broke
  CI collection while dev machines (PEP 649) never saw it. Resolves the cmd modules'
  `from .core import *` preludes one level deep.
- `e2e_scenario.sh` - the full offline-first live-stack scenario (phases A-N, chaos drills
  incl.) driven against a real `docker compose` stack. See `development/infrastructure.md`
  for what each phase proves and the `AV_E2E_CHAOS=1` gate.
- `append_perf_history.py` - release step (run locally, never in CI - see its own
  docstring): captures a speedcheck entry, appends it to `development/perf-history.json`,
  and re-renders the trend table in `development/BENCHMARKS.md`. Run AFTER
  `av benchmark --markdown development/BENCHMARKS.md`, which fully overwrites that file.
- `release_gate.py` - the checks behind `release.yml`'s `gate` job (perf-history has this
  tag, CHANGELOG/VERSIONING are in sync with it, BENCHMARKS.md is current and — on a
  MINOR-or-above release — genuinely fresh, every required CI check is green, `--report
  PATH` renders a Markdown outcome table). Read-only toward PRs; blocks a publish, never
  merges anything.
- `release_smoke.sh` (v1.3.4) - boots the REAL release compose file
  (`python/av_cli/docker/docker-compose.release.yml`) against a given image ref and
  asserts health/ready/push-pull/protected-mode. One script, several call sites:
  `docker-edge.yml`'s staging smoke, `tests.yml`'s PR preview environment,
  `release.yml`'s rollback drill, and local ad-hoc use.
- `migrations_drill.py` (v1.3.4) - per-revision upgrade→downgrade→re-upgrade drill
  against a real Postgres, stronger than the one full-round-trip test in
  `tests/test_server.py`.
- `compat_drill.sh` (v1.3.4) - old-binary-vs-newer-schema rolling-upgrade drill via a
  `git worktree` at a previous release tag; self-calibrates its expected outcome against
  whether that tag actually contains Probleme.md #136's fix, so it never produces a false
  failure regardless of tag history — see its own header before wiring it into any gate.
- `rollback_drill.sh` (v1.3.4) - deploys the previous release image → this release →
  back to the previous, asserting no data loss across the round trip. Reuses
  `release_smoke.sh`'s compose-override technique but keeps the SAME stack/volumes
  across all three deploys (release_smoke.sh's own cleanup always tears volumes down).
- `check_deprecations.py` (v1.3.4) - reads `development/deprecations.yml`, reports every
  entry's status, and (`--current-version`) flags anything overdue for removal at this
  repo's own MAJOR-only removal policy (VERSIONING.md).
- `ci_summary.py` (v1.3.4) - renders a per-job duration-vs-`.github/ci-budgets.yml`-
  budget table for a CI run; `tests.yml`'s `ci-summary` job posts it as a PR comment.
  Never gating — an overrun is a `::warning::` annotation, not a failure.

Anything that graduates into a user- or CI-facing command belongs in
`python/av_cli/` (or `benchmarks/`) instead, so it ships with the package and gets
test coverage. Keep this folder for one-off, checkout-local tooling.
