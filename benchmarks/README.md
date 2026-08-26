# benchmarks

Owns the nine cross-tool benchmarks comparing Aether-Vault against Git LFS, DVC, and
MLflow. Every number is a real subprocess/HTTP measurement on the same fixture each
tool actually processes - never estimated; tools that aren't installed report as
`not installed`/N/A with a footnote. Run via `av benchmark`; the captured report is
`development/BENCHMARKS.md`.

- `tool_runner.py` - shared runner: tool detection, timing, verdicts (GOOD/OK/BAD),
  Markdown rendering.
- `fixtures.py` - deterministic synthetic fixtures shared across benchmarks.
- `bench_hashing_throughput.py` - #1 SHA-256 throughput at 10-200 MB.
- `bench_safetensors_dedup.py` - #2 storage after 6 fine-tune commits.
- `bench_commit_push_latency.py` - #3 end-to-end init/add/commit/push.
- `bench_noop_status_speed.py` - #4 no-op `status`/`add` at scale (open finding).
- `bench_cold_clone.py` - #5 fresh clone from a registry (`av clone`); measured row
  capture pending - needs the Docker stack.
- `bench_partial_checkpoint_fetch.py` - #6 single-layer fetch vs whole file.
- `bench_storage_footprint_curve.py` - #7 cumulative storage over N versions.
- `bench_concurrent_push.py` - #8 eight concurrent pushes against av_server.
- `bench_gc_throughput.py` - #9 server-side mark-and-sweep GC.

```bash
av benchmark --only hashing_throughput --vs dvc     # scope one benchmark / competitor
av benchmark --markdown development/BENCHMARKS.md   # regenerate the full report
av benchmark --baseline prior.json --save-json new.json   # regression tracking
```

Benchmarks #8/#9 need the Docker registry stack running; #5's `av` column needs it
too and otherwise reports "registry unreachable".
