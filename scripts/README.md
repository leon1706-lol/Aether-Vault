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
  tag, CHANGELOG has a signed-off entry, BENCHMARKS.md's captured sha is an ancestor of the
  tag, the tagged commit's CI run is green). Read-only toward PRs; blocks a publish, never
  merges anything.

Anything that graduates into a user- or CI-facing command belongs in
`python/av_cli/` (or `benchmarks/`) instead, so it ships with the package and gets
test coverage. Keep this folder for one-off, checkout-local tooling.
