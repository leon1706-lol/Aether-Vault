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
av benchmark                                  # run all 8 benchmarks, console output
av benchmark --only hashing_throughput        # scope to one (repeatable)
av benchmark --vs git-lfs --vs dvc            # scope competitor columns (repeatable; default: all 3)
av benchmark --markdown development/BENCHMARKS.md   # also (re)write the Markdown tables used there
```

Benchmark names (for `--only`): `hashing_throughput`, `safetensors_dedup`,
`commit_push_latency`, `noop_status_speed`, `cold_clone`, `partial_checkpoint_fetch`,
`storage_footprint_curve`, `concurrent_push`.

Each script can also be run directly for faster iteration on one benchmark:

```bash
python -m benchmarks.bench_hashing_throughput
```

## Interpreting results

Every row shows a real absolute number per tool, plus a **verdict**:

- **GOOD** — Aether is at least 1.5x better than the best real competitor number.
- **OK** — within 1.5x either way, or no competitor produced a real number to compare against.
- **BAD** — Aether is more than 1.5x worse than the best real competitor number.

1.5x (not 1.0x) accounts for single-run, single-machine noise. **`N/A`** means the
benchmark's primitive doesn't map onto that tool at all (e.g. MLflow has no file-hashing
primitive) — set explicitly per-benchmark, never inferred, with a footnote explaining why.
`not installed` means the tool simply isn't on `PATH` in the environment the suite ran in.

## Adding a 9th benchmark

Drop a new `bench_<name>.py` exposing `run(tool_order: list[str]) -> BenchmarkResult` (see
`tool_runner.py` for the dataclasses) and add `<name>` to `BENCHMARK_NAMES` in
`python/av_cli/main.py`'s `benchmark` command — no other registry to update.
