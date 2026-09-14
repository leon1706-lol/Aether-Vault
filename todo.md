# To-Do — Objectives Canvas

This is the owner's planning space, not a generated backlog. Whatever is written below is
the current objective(s) and any personal notes/context for it — read it before starting
work in this repo, and treat it as the live brief for what an AI agent should do next.
Expect this file to be rewritten or cleared out entirely as objectives change; it does not
accumulate history (that's what `development/CHANGELOG.md` and `development/Probleme.md`
are for — see `AGENTS.md`).

-----

## V1.6.3 — footprint / RAM phase — COMPLETE (2026-09-14), awaiting commit

Everything shipped end to end (code, tests, measurements, docs, image rebuild, full suite
through the low-memory runner, vault regen). The record is `development/CHANGELOG.md`
Phase 71 + `development/Probleme.md` #179–#183; the numbers are in `development/MEMORY.md`.
Honest caveats: `tests/test_perf_gate.py`'s `log()` probe fails on this box (cold-file AV
scans of 150 just-written files under the running stack; the read path is unchanged,
warm it is 6x under budget) — CI is the authority; `scripts/ha_drill.sh`'s new 5 MiB
upload step is verified in CI, not locally (the HA stack doesn't fit this box).

Owner's next steps:
- [ ] Review + commit the working tree (~90 files). Nothing has been committed.
- [ ] Push and watch CI — the README test badge is provisional (2005/2006, red: one
      box-specific perf-gate timing failure kept honest; CI's `check_readme_test_freshness`
      will assert its own count, as in Probleme #178).
- [ ] Optional: `pytest tests/test_perf_gate.py` on a quiet box (its `log()` probe fails
      here under the running stack because of cold-file AV scans; the read path is unchanged).

-----

## Blocked by environment, not by choice

- [x] ~~`tests/test_server.py`'s remaining tests~~ — the 22 "unconfirmed" names
      (push_commit/list_commits/get_ref/audit cluster) **passed** this session in a real
      run (25 tests, 279 MB peak, exit 0), and 57 more passed through
      `scripts/run_tests_lowmem.py` in 40-test chunks. The runner is the way to run this
      file here from now on; the never-reached remainder just needs one full
      `python scripts/run_tests_lowmem.py --files tests/test_server.py --chunk-size 40`.
- [ ] **The full `development/BENCHMARKS.md` re-capture** — now possible:
      `av benchmark --lowmem --markdown development/BENCHMARKS.md` runs each benchmark in
      its own process with a free-RAM floor (`hashing_throughput` and `noop_status_speed`
      already verified on this box this session). Docker-dependent rows still need the
      stack up. Not yet run as a full capture.

### Future testing not in scope for current plans

- **A live external IdP run** (Keycloak compose overlay, or a real Okta/Entra tenant) — the
  protocol code (PKCE, JWKS verification, SAML signature/conditions) is implemented and
  tested against this server's own routes, but has not been driven end-to-end against a
  genuinely external IdP in this environment.
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
