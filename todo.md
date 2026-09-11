# To-Do — Objectives Canvas

This is the owner's planning space, not a generated backlog. Whatever is written below is
the current objective(s) and any personal notes/context for it — read it before starting
work in this repo, and treat it as the live brief for what an AI agent should do next.
Expect this file to be rewritten or cleared out entirely as objectives change; it does not
accumulate history (that's what `development/CHANGELOG.md` and `development/Probleme.md`
are for — see `AGENTS.md`).

-----

**Status: complete, release-ready.** Every phase below shipped, verified, and documented
(`development/CHANGELOG.md` Phase 68, `development/Probleme.md` #142-151,
`Aether-vault-Obsidian-Vault/HANDOFF.MD`). Nothing committed or tagged — owner pushes
`v1.5.0` when ready. This section is left in place as the record of what shipped; replace
it with the next objective whenever ready.

### Main Objective: V1.5.0 — Performance release, end to end, nothing deferred

Beat every competitor benchmark, make the commit path fast, push more into the C++ core,
add deterministic multithreading (git-style), and ship it release-ready — full tests,
docs, CHANGELOG, benchmark recapture. Every real bug hit along the way gets fixed, not
deferred. Owner pushes the final tag; everything up to that point is this agent's job.

**Version is v1.5.0, not v1.4.1** — a new `av daemon` command + default-ignore changes are
MINOR per `VERSIONING.md`'s own surface table, and MINOR arms
`release_gate.py::check_benchmarks_fresh_on_minor()`, which forces a real benchmark
recapture anyway (wanted here).

#### Measured baseline (this machine, warm cache, median of 5)

| Probe | Time |
|---|---|
| bare Python interpreter | 235 ms |
| `import requests` | 800 ms |
| `import av_cli.main` (all ~45 `cmd_*`, eager) | 1,172 ms |
| `import aether_core` (C++ ext) | 227 ms — effectively free, contrary to the `~90ms` pessimistic comment at `core.py:76` |
| `av --version` | 2,154 ms |
| `av status` (this repo) | 49,216 ms |

Root causes, confirmed by direct measurement (not re-derived from old Probleme.md
write-ups, which misdiagnosed this as "DVC never uploads during commit" /
"CPython/Click startup cost, out of scope" — `Probleme.md:580-600` #48/#49):

1. `main.py:132+` eagerly imports all ~45 `cmd_*` modules →  `cmd_login` →
   `session_store.py:14` → `update_check.py:15` → `import requests`. `session_store`
   imports `update_check` **only** to reuse the constant `USER_CONFIG_DIR`. This drags the
   entire `requests`/`urllib3`/`ssl` stack into every `av` invocation and defeats every
   existing deliberate lazy-import (`core.py:73-89`, the PEP-562 `VaultClient` shim at
   `main.py:46-55`).
2. `_IGNORED_DIRS = {".av", ".git", "__pycache__"}` (`core.py:113`) — no `venv`, no
   `node_modules`, no `build`. `av status` walks the whole virtualenv, **twice**
   (`cmd_staging.py:264` and `:282` are identical `compute_status()` calls — dead code).
3. Console-script entry point (`av = "av_cli.main:run"`) means the *heaviest* import
   happens before any daemon/fast-path check could ever run — a daemon is worthless
   without also restructuring the entry point.

`development/BENCHMARKS.md` (captured 2026-09-03 @ `8ef634b`) BAD rows against
`VERDICT_THRESHOLD = 1.5` (`tool_runner.py:76`):

| Row | av | best competitor | ratio |
|---|---|---|---|
| `commit` | 11,280.9 ms | 1,072.9 ms (dvc) | 10.5x |
| `re-add unchanged` (60 files) | 13,023.1 ms | 206.0 ms (git-lfs) | 63x |
| `fetch whole checkpoint` | 1,707.3 ms | 997.4 ms (mlflow) | 1.7x |

(The "36x" figure in earlier notes mixed two different captures/machine states — the real,
same-capture ratios above are the target. Ratios, not absolutes: both av's and
competitors' numbers moved ~4-5x between captures purely from machine load.)

**Benchmark methodology caveat to fix before recapturing**: the capture almost certainly
ran against an *editable* install (`pip install -e .`), whose import finder alone adds real
overhead not present for a real `pip install` user. Recapture with a clean `pip install .`
into a fresh venv, `av` resolved via `PATH`.

#### Owner decisions (fixed)

1. Commit semantics unchanged — commit keeps uploading by default; speed comes from real
   work, never from deferring uploads.
2. Persistent daemon in scope, as a first-class `av daemon` command.
3. Multithreading on by default, auto-sized to CPU count, `AV_THREADS`/`--threads`
   override, deterministic-output guarantee, documented and tested.
4. Ignore rules: built-in sane defaults (venv, node_modules, build, dist, .tox,
   .mypy_cache, .pytest_cache, site-packages, .eggs, …) **and** honor `.gitignore` when
   present, with a documented opt-out.
5. Release-ready only — CI green, docs/CHANGELOG/benchmarks done, gate dry-run clean;
   owner pushes the tag.

#### Invariants — must not break (see full trap table in session history / CHANGELOG entry)

- `hash_file` must equal `hashlib.sha256(data).hexdigest()` forever — server re-verifies
  every upload (`av_server/storage.py:50-60`). Never replace with the tree hash
  (`hash_file_parallel`/`hash_file_tree` — that's a *different* hash, not a faster version
  of the same one).
- CDC gear table + min/avg/max (512KB/2MB/8MB) must produce byte-identical boundaries
  forever — golden test `tests/test_core.py:168-215`. `max_chunk` hard cap / `min_chunk`
  hard floor has regressed twice already (Probleme.md #140) — do not re-break it.
- `json.dumps(commit_data, sort_keys=True)` **default-separator** byte form
  (`core.py:867`, `casobj.py:22`) is the signature/hash input, pinned by
  `test_signing.py:86-94`. Never swap in orjson/compact separators for this specific call.
- `compare_metadata`/`get_file_metadata` (C++) stay unused — Windows `last_write_time`
  epoch mismatch vs Python `st_mtime_ns` (`core.py:491-498`). Do not wire them in.
- `.av/objects/<hh>/<62hex>` loose-object layout stays (12+ call sites, 15+ test
  assertions) — no packfile scheme this release.
- `_finalize_commit` stays the sole persister, ordering commit object → ref → clear staged
  → push/queue unchanged. `cmd_sync.py:476` (merge) calls it directly — don't miss it.
- `Index.add_entry(auto_save=True)` default must NOT flip to `False` (silent data loss for
  unaudited callers) — add an explicit `idx.batch()` context manager instead.
- `py::call_guard<gil_scoped_release>` only on scalar-returning C++ bindings (`hash_file`,
  `hash_file_sequential`, `hash_bytes`). `split_and_hash_safetensors`/`chunk_and_hash_file`
  build `py::list`/`py::dict` inside the function body — releasing the GIL across that is
  undefined behavior, not a test failure. These need a core/binding split: pure-C++ compute
  under an explicit `gil_scoped_release`, then build the Python container after
  re-acquiring.
- Adding/removing a `benchmarks/bench_*.py` file forces README + BENCHMARKS.md +
  benchmarks/README.md updates in the same commit (`test_benchmark_docs_freshness.py`).
  Measure the daemon as a second `Row` inside `bench_noop_status_speed.py` instead of a new
  file.
- `multiprocessing.connection` is NOT viable for the daemon's client side — 337ms import
  cost (mostly the `_multiprocessing` C extension), more than the entire startup budget
  it's meant to save. Use raw framing (4-byte length + JSON) over `AF_UNIX` (POSIX) / a
  named pipe opened as a binary file (Windows) instead — ~61ms client import.
- Daemon binds to nothing network-visible (no localhost TCP) — `AF_UNIX` in a `0700`
  runtime dir with `SO_PEERCRED` check, or a named pipe with
  `FILE_FLAG_FIRST_PIPE_INSTANCE`, plus mutual HMAC token auth on top of OS ACLs either way.
  Off by default; never auto-spawns unless explicitly opted in (`AV_DAEMON=1` or config);
  `AV_NO_DAEMON=1` always wins and must be set globally in `tests/conftest.py`.
- Daemon never caches a write-back copy of anything — revalidate-by-stat every request, no
  filesystem watcher this release (missed events = silent skipped file = worse than slow).

#### Phases (each ends green + committable + benchmarkable; sequenced cheapest/safest first)

0. **Instrument** — `AV_TIMING=1` phase timers around commit's stages; add `av --version`
   to `speedcheck.run_av_cli_probes` as a scoreboard metric. Decompose commit's real vs.
   upload cost before optimizing blind.
1. **Dead work** — delete duplicate `compute_status()` call (`cmd_staging.py:282`);
   `update_registry` writes only on real change; lazy-import `speedcheck` in `core.py`.
2. **Import graph** — break `session_store → update_check → requests`; lazy `click.Group`
   command registration preserving `--help` order; guard test asserting `requests` stays
   out of `sys.modules` after `import av_cli.main`.
3. **Ignore rules** — built-in sane defaults + `.gitignore` support, documented opt-out.
4. **C++ GIL + shared pool** — `call_guard` on scalar bindings only; core/binding split for
   the two list-returning functions; one shared lazily-sized `ThreadPool` replacing the
   three per-call constructions (`core.cpp:48,192,338`); LTO build flags.
5. **SHA-256 bulk update** — rewrite the byte-at-a-time `update()` (`sha256.cpp:57-67`) to
   a bulk-copy fast path; property test against `hashlib` under randomized chunk splits.
6. **Deterministic parallel `add`** — Python compute/apply split (workers do pure
   stat+hash+write, serial apply in sorted-input order); `resolve_threads()` chain
   (`--threads` > `AV_THREADS` > config > auto); sorted walk + `sort_keys` index so ordering
   is OS-independent; atomic CAS writes (temp + `os.replace`, fixing a same-content-race
   torn-object risk that exists today even single-threaded).
7. **Single-read staging** — hash-while-writing to a temp file then `os.replace` (stop
   reading every file twice); single-open+seek shard extraction for layers/chunks instead
   of reopening per shard.
8. **Commit path** — cache `server_available()` per process (drop the redundant second
   probe); scan only the changed subtree in `upload_commit_objects` instead of every
   tracked file; serialize the commit payload once and reuse for hash/sign/disk/wire;
   `av log` walk parents from HEAD instead of loading every commit file; hoist
   `server_available()` out of the per-shard materialize loop (the `fetch whole checkpoint`
   BAD row).
9. **Index format** — drop `indent=2` for index/pending_push (keep it for commits/refs);
   `Index.batch()` context manager; collapse `commit_scoped_paths`' 3 reads+3 writes+deepcopy
   to 1+1.
10. **`av daemon`** — `av_cli/launcher.py` tiny entry point (only os/sys/json/socket
    imported before the daemon-or-fallback decision), `av_cli/daemon.py` server, allowlist
    of exactly `add`/`status`/`commit` routed through the real click command objects
    (byte-identical-output test vs in-process), `av daemon start/stop/status/restart`,
    version-skew handling (name-keyed endpoint + handshake check + self-check against
    `av_cli`/`aether_core` file mtimes), orphan/lock cleanup, idle timeout.
11. **Fix every real bug found along the way** — condensed `Probleme.md` entries
    (title + severity/status + Problem/Fix/Verification, ~1-3 sentences each), starting at
    #142.
12. **Recapture + release-ready** — clean (non-editable) install benchmark recapture,
    `perf-history.json` `1.5.0` entry, fix the stale hand-typed "`commit ~6x slower`" line
    in README (real figure was 10.5x), re-baseline the `log()`/`compute_status()`
    speedcheck budgets if Phase 8 doesn't get them under their existing budgets, full
    wrap-up checklist (`Aether-vault-Obsidian-Vault/Essential-Tasks.md`), release-gate dry
    run. Stop before the tag — owner pushes it.

#### Verification (non-negotiable — unit tests alone have missed real bugs here before)

- Scratch-repo manual session outside this checkout, real `av` binary, mixed fixture
  (small code files + a `.safetensors` + a chunkable `.bin`), full lifecycle including
  daemon on/off and `AV_THREADS` ∈ {1,2,4,8}.
- Determinism proof: identical `.av/index` bytes, identical `.av/objects` set, identical
  tree hash, identical stdout line order across all thread counts, every run.
- `av test` green; let CI's own test count drive the README badge resync, not this
  machine (it collects more tests due to extra local optional deps — Probleme.md #133's
  lesson).
- Cross-OS: C++ changes must build clean on cibuildwheel's cp310-cp314 × {ubuntu, windows,
  macos} — a local Windows-only green is not proof.
- `python scripts/release_gate.py` dry run clean, including
  `check_benchmarks_fresh_on_minor` (this is a MINOR release).

### Future testing not in scope for current plans:

- **A live external IdP run** (Keycloak compose overlay, or a real Okta/Entra tenant) —
  the protocol code (PKCE, JWKS verification, SAML signature/conditions) is implemented
  and tested against this server's own routes, but has not been driven end-to-end
  against a genuinely external IdP in this environment.
- **Real Kubernetes HA drill** — the Helm chart is schema-verified, not cluster-drilled.
- **Reference customers / pilot onboarding kit** — not started (a sales outcome).
- **Third-party security audit / SOC2 / staffed support rotation** — need a firm/hires.
- **Docker image rebuild + post-rebuild verification** — the owner does this manually.
- **Daemon filesystem watcher (fsmonitor-style)** — deliberately deferred past v1.5.0; a
  missed event is a correctness bug, not just a slowness one. If added later it may only
  *shrink* the stat set, never replace it, gated behind a health-flag cookie test.
- **C++ inter-file batch hashing binding** (`hash_files(list[str])`) — the Python-side
  compute/apply pool over GIL-released `hash_file` gets most of this win already; revisit
  only if profiling after v1.5.0 shows it's still worth the added C++ surface.
