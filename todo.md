# To-Do — Objectives Canvas

This is the owner's planning space, not a generated backlog. Whatever is written below is
the current objective(s) and any personal notes/context for it — read it before starting
work in this repo, and treat it as the live brief for what an AI agent should do next.
Expect this file to be rewritten or cleared out entirely as objectives change; it does not
accumulate history (that's what `development/CHANGELOG.md` and `development/Probleme.md`
are for — see `AGENTS.md`).

-----

## Main Objective: V1.5.0 — Performance release, end to end, nothing deferred







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
