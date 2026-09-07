# To-Do — Objectives Canvas

This is the owner's planning space, not a generated backlog. Whatever is written below is
the current objective(s) and any personal notes/context for it — read it before starting
work in this repo, and treat it as the live brief for what an AI agent should do next.
Expect this file to be rewritten or cleared out entirely as objectives change; it does not
accumulate history (that's what `development/CHANGELOG.md` and `development/Probleme.md`
are for — see `AGENTS.md`).

-----

### Main Objektive:

**Full plan written and approved — see
`C:\Users\Blackhead\.claude\plans\pleas-now-create-an-shiny-giraffe.md` for the complete
end-to-end spec (design, every file to touch, tests, CI, docs, release-gate steps). If
this session ended before finishing, resume from that plan file rather than re-deriving
this from scratch.**

main objektive v1.4.0 (bumped from v1.3.8 — MINOR per AGENTS.md: new CLI command + new
extra + new checkpoint format is additive contract surface; see plan's Context section)
- add a vanilla PyTorch plugin (`python/av_plugins/pytorch.py`, `AetherVaultCheckpointer`),
  fully end to end: checkpoint commit + training-end push, `import_checkpoint()` backfill
  behind `av import-pytorch`, optimizer/scheduler state + resume, dataset_paths one-shot
  commit — all four scoped in
- add tests with CI, and normal test suite for it
- do verification tasks afterwards (manual scratch-repo run, full `av test`, vault regen,
  release-gate pre-flight) and AGENTS.md update


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

