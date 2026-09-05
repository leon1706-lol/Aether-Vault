# To-Do — Objectives Canvas

This is the owner's planning space, not a generated backlog. Whatever is written below is
the current objective(s) and any personal notes/context for it — read it before starting
work in this repo, and treat it as the live brief for what an AI agent should do next.
Expect this file to be rewritten or cleared out entirely as objectives change; it does not
accumulate history (that's what `development/CHANGELOG.md` and `development/Probleme.md`
are for — see `AGENTS.md`).

-----

### Main Objektive:


main objektive v1.3.4:

CI/CD backlog

(dependency bots excluded, per your preference)

A. Security & supply chain
    1. Secret scanning on every PR/push (gitleaks or GitHub native), fail on findings 
    2. SAST on Python + TypeScript (e.g. CodeQL or equivalent) 
    3. Container image scan on engine builds (Trivy/Grype) — fail on critical CVEs 
    4. SBOM generation for wheels + engine image, attached to releases 
    5. Action pinning by full commit SHA for all third-party actions 
    6. Provenance / attestation for release artifacts (SLSA-style where practical) 
    7. OIDC already used for PyPI — document + extend same pattern for GHCR if not fully locked down 

B. Test completeness & reliability
    8. Linux core pytest job matching the Windows matrix (same suite, not only server/e2e slices) 
    9. macOS smoke (wheel import + a short CLI path) on release or nightly 
    10. Chaos job: kill Redis mid-test, fill disk, kill mid-push → assert queue/recovery 
    11. Contract matrix job: every CLI command × text/json × critical exit codes 
    12. Flake quarantine policy: known flakes tracked; no silent retry-forever 
    13. Coverage gates with a real minimum (and exclude only justified paths) 
    14. Perf trend artifact per main run (commit/status/log/semdiff) stored as CI artifact 

C. Engine / Docker CD depth
    15. Multi-arch engine image (linux/amd64 + linux/arm64) on release 
    16. Image smoke against release compose file (not only local Dockerfile tags) 
    17. Slim build variants tested (server-only / webui-only) if you add them 
    18. Legacy-alias removal dry-run job when deprecation window starts 
    19. Health probe assertions in CD: /api/health vs /api/ready after every image push 

D. Release quality gates
    20. Tag release blocked unless required checks are green (branch protection already started — make tag pipeline enforce the same) 
    21. Changelog / VERSION bump check on tag (refuse tag if VERSIONING/CHANGELOG out of sync) 
    22. Benchmark row update check on MINOR tags (or explicit “benchmarks unchanged” attestation) 
    23. Essential-Tasks signed-off artifact or checklist job before release publish 
    24. Install verification: pip install from built wheel on clean Python, av --version + one-commit smoke 

E. Deploy / environments (still CI/CD)
    25. Staging registry deploy on master (ephemeral or fixed staging host) 
    26. Smoke against staging after deploy (health, auth, one push/pull) 
    27. Optional preview environment per PR (even if only API, not full WebUI) 
    28. Rollback runbook encoded: re-publish previous image tag + compose pin documented in workflow comments 

F. Observability of the pipeline itself
    29. CI summary dashboard comment on PR (failed job names, duration, flake notes) 
    30. Slowest tests report uploaded each main run 
    31. Runner minute budget alerts (workflow annotations when jobs exceed thresholds) 
    32. Central CI map in infrastructure.md auto-checked (job names in YAML match docs) 

G. Policy & process (automation, not bots)
    33. Required status checks complete set for master (all meaningful jobs, strict) 
    34. CODEOWNERS for critical paths (server, signing, docker, workflows) 
    35. Workflow lint (actionlint) on every PR that touches .github/workflows 
    36. Forbid latest action tags via CI policy check 
    37. No-force-push / signed commits optional enforcement on master 

H. Data / migration safety
    38. Alembic upgrade + downgrade on a fresh DB for every revision in CI 
    39. Migration compatibility test: old server binary vs new DB schema (or reverse) on MINOR 
    40. Backup/restore drill of registry volume in a scheduled workflow (synthetic data) 

Suggested order (to actually reach 10)
Phase	Add	Effect
C1	1–4, 8, 11, 20, 24, 35	Trust + parity + safe release
C2	10, 14, 15, 16, 19, 38	Production-hard verification
C3	25–27, 29–31	Real CD + visibility
C4	5–7, 21–23, 33–34, 39–40	Enterprise-grade discipline

Explicitly out of scope (per you)
    • Dependabot / Renovate / automated dependency PR bots 
(Manual dependency review can stay; optionally add a scheduled “outdated report” workflow that only comments, never opens PRs.)

What “10/10” means here
    • Every product surface has a tripwire 
    • Releases are non-social-engineerable (checks + attestations) 
    • Failures are recoverable and tested 
    • Images and packages are scanned and multi-arch 
    • Docs and workflows cannot drift silently 
    • Still no dependency bots 
One line: Lock supply chain, equalize OS coverage, add chaos + contract matrices, gate releases on install/smoke, then add staging smoke and CI observability.

### FUture testing not in scope for current plans:

- **A live external IdP run** (Keycloak compose overlay, or a real Okta/Entra tenant) —
  the protocol code (PKCE, JWKS verification, SAML signature/conditions) is implemented
  and tested against this server's own routes, but has not been driven end-to-end
  against a genuinely external IdP in this environment. The single most important
  remaining verification gap for the SSO/SCIM work.
- **Real Kubernetes HA drill** — the Helm chart is schema-verified, not cluster-drilled
  (stated scope decision, unchanged from v1.3.2).
- **Reference customers / pilot onboarding kit** — not started (a sales outcome, not
  something code produces).
- **Third-party security audit / SOC2 / staffed support rotation** — need a firm/hires,
  not code.
- **Docker image rebuild + post-rebuild verification** — the owner is doing this step
  manually; not run by the agent this session.

