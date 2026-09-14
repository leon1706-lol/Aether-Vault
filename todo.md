# To-Do — Objectives Canvas

This is the owner's planning space, not a generated backlog. Whatever is written below is
the current objective(s) and any personal notes/context for it — read it before starting
work in this repo, and treat it as the live brief for what an AI agent should do next.
Expect this file to be rewritten or cleared out entirely as objectives change; it does not
accumulate history (that's what `development/CHANGELOG.md` and `development/Probleme.md`
are for — see `AGENTS.md`).

-----

# Main Objective: V1.#.#

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
