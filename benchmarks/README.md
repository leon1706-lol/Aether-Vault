# benchmarks

Owns the nine cross-tool benchmarks comparing Aether-Vault against Git LFS, DVC, and
MLflow. Every number is a real subprocess/HTTP measurement on the same fixture each
tool actually processes - never estimated; tools that aren't installed report as
`not installed`/N/A with a footnote. Run via `av benchmark`; the captured report is
`development/BENCHMARKS.md`.

- `tool_runner.py` - shared runner: tool detection, timing (`time_subprocess`/`time_call`,
  both median-of-`repeat`), `repeat_median()` (wraps a whole `_bench_<tool>()` call N
  times), verdicts (GOOD/OK/BAD), `claim_scope` (speed/efficiency/unique/internal) and
  `render_claim_summary()` (the machine-derived "faster in every published domain"
  verdict), Markdown rendering, `daemon_mode_label()`.
- `fixtures.py` - deterministic synthetic fixtures shared across benchmarks.
- `bench_hashing_throughput.py` - #1 SHA-256 throughput at 10-200 MB.
- `bench_safetensors_dedup.py` - #2 storage after 6 fine-tune commits.
- `bench_commit_push_latency.py` - #3 end-to-end init/add/commit/push. `commit` times
  `av commit --no-upload` (local-only, comparable to DVC/Git LFS's own network-free
  commit); `push` times the real upload, comparable to `dvc push`.
- `bench_noop_status_speed.py` - #4 no-op `add` AND a plain `status` on a clean tree, two
  rows.
- `bench_cold_clone.py` - #5 fresh clone from a registry (`av clone`); needs the Docker
  stack up to produce a real number, otherwise reports "registry unreachable".
- `bench_partial_checkpoint_fetch.py` - #6 single-layer fetch (via `av fetch --layer`) vs
  whole file (via `av fetch`), both real CLI subprocesses now — and against MLflow served
  over a real local `mlflow server` (HTTP), not its local-disk artifact store, so the
  "whole checkpoint" row is a fair network-fetch-vs-network-fetch comparison.
- `bench_storage_footprint_curve.py` - #7 cumulative storage over N versions.
- `bench_concurrent_push.py` - #8 eight concurrent pushes against av_server.
  `claim_scope="internal"` — excluded from the "every domain" claim (no competitor has a
  comparable concurrent-server primitive), still tracked and optimized.
- `bench_gc_throughput.py` - #9 server-side mark-and-sweep GC.
  `claim_scope="internal"`, same reasoning as #8.

```bash
av benchmark --only hashing_throughput --vs dvc     # scope one benchmark / competitor
av benchmark --repeat 5                             # median of 5 independent runs per row (default 3)
av benchmark --no-daemon                            # capture cold (no background daemon) numbers
av benchmark --markdown development/BENCHMARKS.md   # regenerate the full report
av benchmark --baseline prior.json --save-json new.json   # regression tracking
```

Benchmarks #8/#9 need the Docker registry stack running; #5's `av` column needs it
too and otherwise reports "registry unreachable". Every `av` number is captured with the
product default (native launcher + auto-spawned daemon, once those land — see `todo.md`);
`--no-daemon` (or `AV_NO_DAEMON` leaking in from the calling shell) is called out
explicitly in the captured report rather than silently changing what was measured.
