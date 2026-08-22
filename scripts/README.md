# `scripts/` — Developer Utility Scripts

Standalone helper scripts that don't belong to the installed package. See the
[main README](../README.md).

## Contents

| File | Purpose |
|---|---|
| [`run_benchmark_comparison.py`](run_benchmark_comparison.py) | One-off convenience wrapper that runs the full cross-tool benchmark suite (`benchmarks/`) and writes the Markdown report — the same thing `av benchmark --markdown` does, usable without an editable install's console script |

## Note

Anything that graduates into a user- or CI-facing command should live in
`python/av_cli/` (or `benchmarks/`) instead, so it ships with the package and gets test
coverage. Keep this folder for one-off, checkout-local tooling.
