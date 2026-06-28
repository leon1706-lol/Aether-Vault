# `av benchmark` — cross-tool benchmark suite

Dev-only tooling that times Aether-Vault against **Git LFS**, **DVC**, and **MLflow** on
the same synthetic fixtures, for the numbers in [`development/BENCHMARKS.md`](../development/BENCHMARKS.md).

Not to be confused with `av doctor --speed` (real-repo diagnostics) or `av test --speed`
(synthetic regression probes for av's own internal hot paths) — those track *av's own*
performance over time; this suite exists to compare *against other tools*.

## Install the comparison targets

Git LFS is assumed to already be on `PATH` (it's also used by `scripts/run_benchmark_comparison.py`).
DVC and MLflow are optional extras, not runtime dependencies:

```bash
pip install -e .[dev,benchmarks]
```

If a tool still isn't found on `PATH` when a benchmark runs, its column prints `not installed`
— never a fabricated number.

## Running it

```bash
av benchmark                                  # run all 9 benchmarks, console output
av benchmark --only hashing_throughput        # scope to one (repeatable)
av benchmark --vs git-lfs --vs dvc            # scope competitor columns (repeatable; default: all 3)
av benchmark --markdown development/BENCHMARKS.md   # write a complete, ready-to-commit report
                                                      # (header/Captured-line/legend/methodology
                                                      # notes + every benchmark's table, in one shot)
av benchmark --save-json snapshot.json        # save this run's av-only numbers for later comparison
av benchmark --baseline snapshot.json         # compare this run against a prior --save-json
                                               # snapshot; exits non-zero if any row regressed
                                               # past the 1.5x verdict threshold
```

`--save-json` and `--baseline` compose: e.g. `av benchmark --baseline last-week.json --save-json
this-week.json` checks for regressions against last week's numbers while also saving today's
for next time. This is the regression-tracking mode that the *competitor*-comparison verdicts
above don't cover — it answers "did *Aether* get slower since last time," not "is Aether faster
than DVC."

Benchmark names (for `--only`): `hashing_throughput`, `safetensors_dedup`,
`commit_push_latency`, `noop_status_speed`, `cold_clone`, `partial_checkpoint_fetch`,
`storage_footprint_curve`, `concurrent_push`, `gc_throughput`.

Each script can also be run directly for faster iteration on one benchmark:

```bash
python -m benchmarks.bench_hashing_throughput
```

## Future work: a `doctor --speed`-shaped repo-size benchmark

`av doctor --speed` diagnoses how slow a *real* repo's hot paths are, but there's currently
nothing establishing what "fast" looks like at a few repo sizes (e.g. 100 vs. 10,000 tracked
files) for a user to compare their own numbers against. This doesn't fit the cross-tool
comparison framing this suite is built around (Git LFS/DVC/MLflow don't have an equivalent
"diagnose my real repo" command to compare against), so it's noted here as a manual exercise
rather than a 10th automated `bench_*.py` — run `av doctor --speed` against repos of a few
sizes yourself and compare the printed numbers if you need this.

## Interpreting results

Every row shows a real absolute number per tool, plus a **verdict**:

- **GOOD** — Aether is at least 1.5x better than the best real competitor number.
- **OK** — within 1.5x either way, or no competitor produced a real number to compare against.
- **BAD** — Aether is more than 1.5x worse than the best real competitor number.

1.5x (not 1.0x) accounts for single-run, single-machine noise. **`N/A`** means the
benchmark's primitive doesn't map onto that tool at all (e.g. MLflow has no file-hashing
primitive) — set explicitly per-benchmark, never inferred, with a footnote explaining why.
`not installed` means the tool simply isn't on `PATH` in the environment the suite ran in.

## Adding a 10th benchmark

Drop a new `bench_<name>.py` exposing `run(tool_order: list[str]) -> BenchmarkResult` (see
`tool_runner.py` for the dataclasses) and add `<name>` to `BENCHMARK_NAMES` in
`python/av_cli/main.py`'s `benchmark` command — no other registry to update.
