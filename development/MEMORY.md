# Memory envelope — budgets, how to measure, and V1.6.3 before/after

Hand-maintained (unlike `BENCHMARKS.md`, which `av benchmark --markdown` regenerates
wholesale). Every number here is measured with `scripts/rss_scoreboard.py` on the reference
dev box named in `BENCHMARKS.md`'s machine profile (3.9 GB RAM, 4 logical cores, Windows 10,
Python 3.14) unless a row says otherwise. Peak RSS = high-water mark of the **whole process
tree** the command spawns (`av` on PATH is a launcher that re-execs the real CLI, so a
parent-only probe would report a 4 MB stub — see `av_cli/sysres.py`).

## How to measure

```bash
# Everything a laptop feels (no Docker rows), ~10 min on the reference box:
python scripts/rss_scoreboard.py --label after --out-json /tmp/after.json --out-md /tmp/after.md

# Add the container rows (docker stats + in-container ps) and a pytest peak:
python scripts/rss_scoreboard.py --with-docker --pytest-target tests/test_server.py ...

# One row, one run, quick sanity check:
python scripts/rss_scoreboard.py --scenarios add_safetensors --runs 1 --out-json /tmp/x.json

# The opt-in gate (budgets in python/av_cli/speedcheck.py::_MEMORY_BUDGETS_MB):
AV_MEMORY_GATE=1 pytest tests/test_memory_gate.py -v
```

`av doctor --resources` prints the live picture (this process, the daemon, free RAM, the
effective staging worker count) and recommends the low-memory knobs when the box is small.

## Budgets and V1.6.3 before/after

Before = `development/memory-baseline-v1.6.2.json` (captured before any V1.6.3 change);
after = `development/memory-scoreboard-v1.6.3.json` (captured after every code change, same
fixtures). Budget = after × 1.25, rounded up — a regression tripwire, not a target.
`tests/test_memory_gate.py` (opt-in, `AV_MEMORY_GATE=1`; multiplier
`AV_MEMORY_BUDGET_MULTIPLIER`, default 1.5 for CI-runner variance) judges the median of 3 runs
on its own smaller fixtures (`python/av_cli/speedcheck.py::_MEMORY_BUDGETS_MB`).

| Scenario | Fixture | V1.6.2 (before) | V1.6.3 (after) | Budget |
|---|---|---:|---:|---:|
| `av status` (no daemon) | 2000 files | 31.2 MB | 31.0 MB | 40 MB |
| `av add .` unchanged tree | 2000 files | 35.8 MB | 35.9 MB | 45 MB |
| `av add` one safetensors | 1 × 256 MiB (8 layers) | 63.6 MB | 63.5 MB | 80 MB |
| `av add` one safetensors | 1 × **1 GiB** (32 layers) | — | 63.3 MB | 80 MB |
| `av add` many safetensors, defaults | 8 × 64 MiB in one add (4 workers) | 163.0 MB | 160.7 MB | 210 MB |
| … with `AV_STAGE_WORKERS_MAX=2` | same | — | **97.0 MB** | 125 MB |
| … with `AV_STAGE_WORKERS_MAX=2 AV_STAGE_BUFFER_MB=8` | same | — | **33.3 MB** | 45 MB |
| `av commit --no-upload` | 2000 entries | 53.2 MB | **45.0 MB** | 60 MB |
| `av log --all --limit 30` | 60 commits | 30.8 MB | 29.9 MB | 40 MB |
| daemon-served `av status`, client (peak / avg) | 500 files | 22.9 / 7.9 MB | 21.9 / 7.5 MB | 30 MB |
| daemon RSS after serving `status` ×5 | 500 files | 32.2 MB | 31.6 MB | 40 MB |
| daemon RSS right after a 256 MiB add | 500 files | 32.4 MB | 31.4 MB | 40 MB |
| daemon RSS idle, after trim (self-reported) | 500 files | 25.5 MB | 25.2 MB | 32 MB |
| engine container idle (`role=all`) | compose defaults | 212.0 MB | 207.9 MB | 260 MB |
| uvicorn process idle | in-container | 103.9 MB | 114.5 MB | 145 MB |
| Next.js process idle (`--max-old-space-size=256`) | in-container | 84.8 MB | 79.4 MB | 100 MB |
| Postgres container idle (`shared_buffers=64MB`) | compose defaults | 93.1 MB | **38.4 MB** | 50 MB |
| Redis-stack container idle (`maxmemory 128mb`) | compose defaults | 21.6 MB | 19.4 MB | 25 MB |
| `pytest tests/test_server.py` (25-test chunk) | live db+redis | 279.4 MB | 276.6 MB ² | 350 MB |
| `pytest tests/test_server.py` via `run_tests_lowmem.py` | 40-test chunks | — | **~170 MB / chunk** | 220 MB |

¹ Container rows were captured on the rebuilt image with the new compose live (limits
768M/256M/192M applied, `shared_buffers=64MB`, `volatile-lru`, node heap cap, the
`/dev/tcp` healthcheck passing). The uvicorn process is a few MB heavier than before —
the `av_process_*` gauges themselves and the streaming-audit code path are new imports —
and Postgres dropped by more than half; the engine total is bounded by the container limit
either way. ² The server test process is dominated by the `av_server`
import graph (fastapi + pydantic + sqlalchemy + asyncpg + redis); smaller pools
(`AV_DB_POOL_SIZE=2`/`AV_DB_MAX_OVERFLOW=3`, set by the test file) cut Postgres backends,
not this process — what makes the file runnable on a 3.9 GB box is the chunked runner.

**What moved and what did not.** The CLI's steady-state numbers were already small
(V1.5.0/V1.6.x import work); V1.6.3's wins are the *bounds*: the staging peak is now capped
by free RAM and by two knobs (160 → 97 → 33 MB above), a 1 GiB safetensors costs the same
63 MB as a 256 MiB one, `commit` lost its second index parse (−8 MB), `log --all` no longer
holds every tree, `clone`/`registry export|restore` never hold an object or the whole
history in RAM, the server's GC marks over column tuples instead of ORM instances, audit
export/verify stream, every list endpoint is bounded, three unbounded in-process dicts are
capped, and the whole suite + benchmarks run locally again through the low-memory runners.

## Where the staging peak comes from (derivation)

`av add` stages files through a `ThreadPoolExecutor` of `effective_stage_workers()` workers
(`core.py`), each calling the fused C++ path with the GIL released. Per in-flight file:

- safetensors (`stage_safetensors`): 1 MiB read buffer + one layer buffer of up to
  `AV_STAGE_BUFFER_MB` (default 32; larger layers stream through a temp file instead) + the
  header, held once → **≈ `AV_STAGE_BUFFER_MB + 2` MB per worker**.
- CDC-chunked files (`stage_cdc`): 1 MiB read buffer + one chunk buffer ≤ `max_chunk`
  (8 MiB) → ≈ 9.5 MB per worker.

So peak ≈ `workers × (AV_STAGE_BUFFER_MB + 2) + interpreter baseline (~30 MB)`. Measured:
4 workers × 34 + 30 ≈ 166 MB predicted vs 161 MB observed for the 8 × 64 MiB row above;
2 workers → 98 predicted vs 97 observed; 2 workers + 8 MiB buffer → ~36 predicted vs 33 observed.
V1.6.3 caps `workers` by free RAM (`(available_mb − AV_STAGE_RESERVE_MB) // (AV_STAGE_BUFFER_MB + 2)`)
and by `AV_STAGE_WORKERS_MAX`, so the envelope never exceeds what the box can actually give
— the earlier "≤ 384 MB at 8 workers" figure in `architecture.md` was a pre-measurement
estimate and is retired.

## Low-memory knobs (one line each; the full recipe is README "Low-memory mode")

| Knob | Effect |
|---|---|
| `AV_STAGE_WORKERS_MAX=2` | hard cap on staging workers (peak ≈ 2 × 34 + 30 MB) |
| `AV_STAGE_BUFFER_MB=8` | stream any layer > 8 MiB through disk instead of RAM |
| `AV_STAGE_RESERVE_MB=512` | keep more free RAM out of the automatic worker cap |
| `AV_NO_DAEMON=1` | no resident daemon at all (pay interpreter startup per command) |
| `AV_DAEMON_TRIM_SECS=5` | trim the daemon sooner after it goes idle |
| `AV_ENGINE_ROLE=server` | engine container without the Next.js process |
| `AV_PG_SHARED_BUFFERS` / `AV_REDIS_MAXMEMORY` / `*_MEM_LIMIT` | compose-level caps (interpolated into every compose file) |
| `.wslconfig` `memory=` | the only thing that bounds Docker Desktop's `vmmem` on Windows |
