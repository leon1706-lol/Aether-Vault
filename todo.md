# To-Do — Objectives Canvas

This is the owner's planning space, not a generated backlog. Whatever is written below is
the current objective(s) and any personal notes/context for it — read it before starting
work in this repo, and treat it as the live brief for what an AI agent should do next.
Expect this file to be rewritten or cleared out entirely as objectives change; it does not
accumulate history (that's what `development/CHANGELOG.md` and `development/Probleme.md`
are for — see `AGENTS.md`).

-----

**v1.6.0 "Performance Closure" — in progress.** Full plan (still the source of truth for
scope): `C:\Users\Blackhead\.claude\plans\please-now-make-an-velvety-clarke.md`. What's
already shipped: `development/CHANGELOG.md` Phase 69 + `development/Probleme.md` #153-167,
`VERSIONING.md`'s v1.6.0 section, the Obsidian vault regen (`Project-Map.md`/folder
indexes/`HANDOFF.MD`), and a real manual scratch-repo session covering every local-only
checklist item from the original plan (native launcher `--version` parity, daemon
auto-spawn, byte-identical daemon vs `AV_NO_DAEMON=1` JSON, fused-vs-legacy CDC staging
byte-identical, `AV_THREADS` ∈ {1,4,8} determinism, `AV_SHA256_BACKEND=scalar` matches auto
(no SHA-NI on this CPU either way), CDC-chunked `stash push`/`pop` round-trip, `checkout`
writing no whole-blob duplicate for a chunked entry, non-ASCII filename staging, clean
`daemon stop`) — the network-dependent half (`push`/`pull`/`clone`/`fetch`/`gc`/`registry
export` against the live registry) was NOT exercised this pass: Docker's own API was
independently returning 500s and a bare health-check curl took 120s+ under this box's
sustained strain, confirmed unrelated to `av` itself. Uncommitted on `master` — nothing
pushed/committed yet, the owner does that.

## Missing / still to do

Real gaps in the original plan, found by checking the code directly (not by memory) —
none of these are exercised or caught by any existing CI job, since CI only tests what
exists; these are missing features/optimizations, not missing test coverage.

- [ ] **WS2.6 — commit path still does 3 separate `json.dumps` calls** (hash, signature,
      file write each re-serialize `commit_data` independently) instead of the planned
      single canonical-bytes reuse. `python/av_cli/core.py`'s `commit_staged()`.
- [ ] **WS2.7 — `Index.remove_entry()` still saves unconditionally on every call** (no
      `auto_save=False` batching option exists), and `cmd_sync.py::merge()` still
      constructs `Index(repo_root)` three separate times within one call, exactly what the
      plan flagged as the thing to fix.
- [ ] **WS4.8 — clone/pull round-trip optimizations never implemented**: no page
      pipelining in `sync.fetch_project_commits`, no `durable=False` fast path for clone's
      commit-file writes, `av pull` still does one `GET /api/commits/{hash}` per new commit
      instead of the planned paginated `include_layers=true` listing.
- [ ] **WS5.8 — `WindowRateLimiter._buckets` (python/av_server/rate_limit.py) has no
      periodic pruning** of stale entries — only a full `.clear()` exists. Unbounded growth
      under long server uptime with many distinct rate-limit keys.
- [ ] **WS6.1 — the daemon idle-trim watchdog was never implemented at all**, not even a
      stub: no `release_pool()`/`gc.collect()`/`malloc_trim` call anywhere in
      `python/av_cli/daemon.py`, no `AV_DAEMON_TRIM_SECS` anywhere in the codebase (checked
      via a full-repo grep — zero matches). A long-lived auto-spawned daemon's memory only
      ever grows, never gives anything back while idle.

## Blocked by environment, not by choice

- [ ] **`tests/test_server.py`'s remaining 131 tests** and the **full
      `development/BENCHMARKS.md` re-capture** — this box's free RAM held at 0.2-0.5GB (of
      3.9GB) throughout the 2026-09-13 attempt, with Docker's own WSL VM growing from
      ~370MB to 500MB+ under the sustained DB-backed test load. Genuinely retried many ways
      (batch-of-50, batch-of-15, one-test-per-subprocess restarted ~25 times) — 62/193
      server tests did get individually verified passing with zero real failures found; see
      `development/CHANGELOG.md`'s 2026-09-13 entry for the full account. Re-run once there
      is real headroom; the resumable driver script's design means a future attempt resumes
      rather than restarts.
- [ ] **`development/architecture.md`'s memory-envelope section (WS6.6) was never
      written** — directly downstream of WS6.1 above not existing yet; there is no idle-trim
      behavior to document. Write this once WS6.1 is implemented, not before (documenting a
      memory envelope for a trim mechanism that doesn't exist would be describing something
      that isn't true).

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
