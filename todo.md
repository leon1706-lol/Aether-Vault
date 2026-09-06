# To-Do — Objectives Canvas

This is the owner's planning space, not a generated backlog. Whatever is written below is
the current objective(s) and any personal notes/context for it — read it before starting
work in this repo, and treat it as the live brief for what an AI agent should do next.
Expect this file to be rewritten or cleared out entirely as objectives change; it does not
accumulate history (that's what `development/CHANGELOG.md` and `development/Probleme.md`
are for — see `AGENTS.md`).

-----

### Main Objektive:




### FUture testing not in scope for current plans:

- **A live external IdP run** (Keycloak compose overlay, or a real Okta/Entra tenant) —
  the protocol code (PKCE, JWKS verification, SAML signature/conditions) is implemented
  and tested against this server's own routes, but has not been driven end-to-end
  against a genuinely external IdP in this environment. The single most important
  remaining verification gap for the SSO/SCIM work.
- **Real Kubernetes HA drill** — the Helm chart is schema-verified, not cluster-drilled
  (a stated scope decision).
- **Reference customers / pilot onboarding kit** — not started (a sales outcome, not
  something code produces).
- **Third-party security audit / SOC2 / staffed support rotation** — need a firm/hires,
  not code.
- **Docker image rebuild + post-rebuild verification** — the owner is doing this step
  manually; not run by the agent this session.

