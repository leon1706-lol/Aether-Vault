# To-Do — Objectives Canvas

This is the owner's planning space, not a generated backlog. Whatever is written below is
the current objective(s) and any personal notes/context for it — read it before starting
work in this repo, and treat it as the live brief for what an AI agent should do next.
Expect this file to be rewritten or cleared out entirely as objectives change; it does not
accumulate history (that's what `development/CHANGELOG.md` and `development/Probleme.md`
are for — see `AGENTS.md`).

-----


V1.6.3
- this phase will be all about resource and ram optimisation to make it also more lightweight for users
- please scope an indepth plan on hoe to make it leass ram intensive but also keep it faast low latency based on these points and I deas
- make it  fully end to end without defering anythin
- It should Also be able to run stuff like benchmark and full suit lokaly aain afterwards

plan ideas:


V1.6.3 — footprint / RAM phase (practical list)
Goal: same features, much smaller steady-state and peak memory, so the project runs on a constrained machine without feeling “heavy.”

Principles

Measure first — peak RSS by process (av CLI, daemon, engine, Postgres, Redis, WebUI).
One owner per leak class — imports, caches, buffers, workers, Docker.
Don’t break hash/CDC/signing invariants or multi-tenant correctness.
Prefer streaming + bounds over “load whole model/tree.”


A. Measure & budget (do first)

RSS scoreboard — script: peak/avg RSS for av status, av add, av commit, daemon idle, engine idle, full compose.
Import graph cost — what each cmd_* pulls in; keep lazy registration (1.5/1.6).
Hard budgets in docs — e.g. daemon idle < X MB, CLI no-op < Y MB, engine without WebUI < Z MB.
CI optional job — fail or warn if daemon/CLI RSS exceeds budget on a fixed fixture (best-effort).


B. CLI & daemon (usually the laptop killers)

Daemon idle-trim — you already listed WS6.1 (malloc_trim / periodic trim after idle); implement with AV_DAEMON_TRIM_SECS.
Daemon allowlist stays narrow for hot path — don’t load commit/push stacks until needed.
No unbounded caches — status/index: one index in memory; cap any path caches.
Stream file reads — fixed buffer sizes for hash/CDC (already chunked; audit any read() of full files in Python).
Index format — compact on-disk index (no fat JSON indent); optional mmap-friendly layout later.
Agent mode — AV_NO_RICH=1 / never import heavy TUI stacks on daemon path (you started AST checks; keep them).
Process split — document: CLI without daemon vs with; engine without WebUI for weak machines.


C. C++ core

Bound parallel workers — thread pool size min(CPU, cap); env AV_THREADS already — default cap on low RAM.
No giant intermediate buffers in SHA/CDC/safetensors split — reuse buffers; verify peak under 1 large file.
Release pools after batch — don’t keep per-call vectors forever on the daemon process.


D. Server (engine)

Slim engine profile — server-only without Next.js when WebUI not needed (compose profile).
Worker/connection caps — uvicorn/gunicorn workers = 1–2 on small boxes; document.
Upload path — stream to CAS; never hold full object in RAM.
Rate-limiter buckets — periodic prune (your WS5.8 gap).
Query/result limits — pagination defaults on list/log/audit; no “return entire history.”
Bloom/Redis — fixed memory policy; don’t grow filters without bound.


E. Postgres / Redis / Docker

Postgres shared_buffers / work_mem — low defaults in docker-compose for dev (e.g. small shared_buffers).
Redis maxmemory + eviction — explicit in compose.
Compose profiles — minimal = db + redis + engine API only (no HA, no WebUI).
Healthcheck lightness — avoid expensive checks that pile up under load.


F. WebUI

Don’t start WebUI by default on low-spec docs path.
Next.js — production build only in image; no dev server in prod compose.
API polling — backoff; don’t open huge payloads on dashboard home.


G. Data-plane behavior (footprint under real ML files)

Checkout/smudge — no whole-blob duplicate when chunked (you already care; keep tests).
GC — bounded batch size; don’t load all object IDs at once if possible.
Export/restore — streaming; temp files on disk not RAM.


H. Packaging & docs

av doctor --resources — print RSS, daemon on/off, recommended compose profile.
README “low-memory mode” — one page: AV_NO_DAEMON, single worker, no WebUI, Postgres tunables.
CHANGELOG 1.6.3 — footprint numbers before/after on your machine.



Suggested phase order (1.6.3)













































StepWorkEffect1RSS scoreboard + budgetsKnow the truth2Daemon idle-trim + cache capsSteady-state laptop3Engine slim profile + worker=1 + stream uploadsCompose fits RAM4Postgres/Redis memory caps in composeBiggest multi-process win5Rate-limiter prune + list paginationLong-run server6C++ buffer/thread capsPeak during add/commit7av doctor --resources + low-mem docsUsable by humans

Explicit non-goals for 1.6.3

Full rewrite in Rust
Packfiles (big design)
Killing features to “save RAM” without a profile flag
Chasing Git’s RSS on cold start without native launcher (that’s 1.6+/1.7)


One-line definition of done
On your machine: minimal compose + daemon idle + av status on a medium repo stay under documented RSS budgets; one large safetensors add/commit peaks under a stated limit; no functional regression on hash/CDC tests.
That’s a coherent v1.6.3 footprint phase: measure → daemon/CLI → engine/compose → DB/Redis → polish.
-----

## Blocked by environment, not by choice

- [ ] **`tests/test_server.py`'s remaining tests, re-attempted 2026-09-13 (2nd session,
      Docker up)**: a one-test-per-subprocess resumable driver got through **79/193** (56
      passed, 23 failed) before the *harness itself* killed the whole background process
      for low system memory — not the driver's own safety check, which had been passing.
      Re-ran one failure alone right after (`test_alembic_brings_schema_to_head`) and it
      **passed clean** — strong evidence the 23 are the same documented class of false
      failure as Probleme.md #167 (a DB fixture's teardown timing out under memory
      pressure, not a real bug), but a retry batch for the other 22 couldn't even start a
      single test before being killed again, so only that one is actually *confirmed* a
      flake. The 22 still-unconfirmed names (all `test_server.py::`, mostly clustered
      around `push_commit`/`list_commits`/`get_ref`/`gc`):
      `test_protected_mode_gates_writes_too_not_just_reads`,
      `test_push_commit_stamps_authenticated_username_as_author`,
      `test_push_commit_respects_explicit_author_from_authenticated_user`,
      `test_owner_shared_secret_stamps_owner_as_author`,
      `test_anonymous_mode_keeps_author_untouched`,
      `test_push_commit_then_get_commit_roundtrip`,
      `test_list_commits_omits_tree_by_default`,
      `test_list_commits_include_layers_matches_get_commit`,
      `test_list_commits_include_layers_handles_a_commit_with_no_tree`,
      `test_list_commits_include_layers_resolves_all_roots_in_one_shared_call`,
      `test_push_commit_duplicate_returns_409`, `test_push_commit_rejects_oversized_tree`,
      `test_push_commit_rejects_too_many_tags`, `test_push_commit_rejects_oversized_tag`,
      `test_push_commit_rejects_too_many_metrics`,
      `test_push_commit_rejects_oversized_message`, `test_update_ref_then_get_ref_roundtrip`,
      `test_list_refs_filters_by_project_id`, `test_dashboard_summary_and_projects_endpoints`,
      `test_gc_respects_grace_period_then_sweeps_when_aged`,
      `test_merge_commit_round_trips_both_parents`,
      `test_single_parent_commit_reports_one_parent`. Re-run these 22 specifically first on
      a future attempt (fastest path to confirming they're all flakes too) before touching
      the other 114 never-yet-reached tests. Root cause unchanged from before: this box's
      free RAM sat at 0.3-0.5GB throughout, mostly consumed by Docker's own `vmmem` WSL VM
      (~490-520MB) plus this session's own `claude` process(es) (up to ~625MB combined) —
      not something a smaller test batch works around, since even a single subprocess
      couldn't get a safe memory floor on the second retry attempt.
- [ ] **The full `development/BENCHMARKS.md` re-capture (still `a73ebde`/v1.4.0.2-era, now
      badly stale)** — attempted same session as above; a memory-checking driver correctly
      refused to even start the *cheapest* of the 9 named benchmarks (`hashing_throughput`,
      no server/subprocess tools needed), measuring 308MB free against its own 400MB safety
      floor. Never actually ran anything, so no data to salvage or resume from — a clean
      re-attempt from scratch once there's real headroom. git-lfs 3.7.1/dvc/mlflow are all
      confirmed present on PATH and the live registry stack is confirmed up+healthy, so
      nothing else blocks this besides free memory.

### Future testing not in scope for current plans

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
