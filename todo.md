# To-Do — Objectives Canvas

This is the owner's planning space, not a generated backlog. Whatever is written below is
the current objective(s) and any personal notes/context for it — read it before starting
work in this repo, and treat it as the live brief for what an AI agent should do next.
Expect this file to be rewritten or cleared out entirely as objectives change; it does not
accumulate history (that's what `development/CHANGELOG.md` and `development/Probleme.md`
are for — see `AGENTS.md`).

-----

# Main Objective:

main objektive v1.3.2:
- going through readme and fixing all open gaps if any open
    • SSO/RBAC/SCIM, hard multi-tenancy
    • HA/DR product packaging
    • Security review + reference customers
    • Support/SLA motion
- extra: generall execution and full end to  end implementation  of enterprise roadmap


Buyer reality after phases: serious pilots and early enterprise deals in labs, quant R&D, safety/eval, agent platforms — not automatic “replace everything” sales.

-----

## Status as of 2026-09-05 (session 2) — NOT complete, in progress

Migrations `0011`-`0015` applied and live-verified against the real dev stack (not just
the test DB) — the real `aether-vault-engine` image was rebuilt and is running this
session's code, DB confirmed at head `0015`, `/api/ready` green. E0/E1/E4/E5/E6/E7/E8 are
now real, live-verified code; E2/E3 (SSO/SCIM) remain entirely unstarted. The full
stack-free suite is green (1147 passed, 0 failed) and the DR drill (Phase U, real
destroy-and-restore) passed end to end — measured restore time 17s this run. See
`development/CHANGELOG.md` Phase 60 for the full writeup, including all three real bugs
this session's live verification caught (none by design review or unit tests alone).

### Done

- **Identity schema** — `tenants`/`projects`/`users`/`roles`/`role_bindings`/`api_tokens`/
  `sessions`/etc. (migration `0011`), 6 built-in roles, `DEFAULT_TENANT_ID`.
- **RBAC core** — DB-backed tokens work independent of `.env` (`identity.py`), 6
  previously-unscoped admin routes now require `admin` scope (real gap closed), remote
  admin API + CLI: `av token`, `av tenant`, `av user`, `av role`.
- **Hard multi-tenancy** — `tenant_id` on 28 tables + RLS policies + app-level guard
  (migrations `0012`/`0013`/`0014`), gated behind `AV_TENANCY_ENFORCE` (off by default).
- **The RLS-superuser gap is FIXED** (migration `0015`) — a new non-superuser `av_app`
  Postgres role now handles all request-serving DB sessions (`AV_APP_DATABASE_URL`,
  optional/additive); migrations and the two legitimately-cross-tenant background workers
  keep using the superuser role. **Live-verified end-to-end on the real dev stack**: a raw
  SQL probe as `av_app` with only the `app.tenant_id` GUC set sees exactly one tenant's
  rows with no application code involved at all — RLS is now a real backstop, not just
  documented as inert. `docker-compose.yml` wires this up by default.
- **3 of the HA cross-replica bugs** — webhook-retry-worker duplicate delivery
  (`SKIP LOCKED`), the rate limiter, and the auth-spike counter each now have an opt-in
  Redis-backed mode for correctness under N replicas (`AV_RATE_LIMIT_BACKEND`,
  `AV_AUTH_SPIKE_BACKEND`, both default off/unchanged).
- **HA product packaging** — `docker-compose.ha.yml` (nginx LB + 2 engine replicas +
  Postgres primary/streaming-replica + Redis primary/replica), `scripts/ha_drill.sh`
  (real drill: concurrent pushes, kills a replica mid-load, proves zero failed pushes,
  zero double webhook delivery, and a globally-enforced rate limit). A Helm chart
  (`deploy/helm/aether-vault/`) is `helm template | kubeconform -strict` verified
  (installed both tools locally and ran it for real — all resource kinds valid across 4
  value permutations) — **not** drilled against a real cluster (stated scope decision).
  Two new CI jobs (`helm-lint`, `ha-drill`) wired into `tests.yml`.
- **Security scanning CI** — `.github/workflows/security.yml` (`pip-audit`, `bandit`,
  `semgrep`, `trivy` on the built image, `npm audit`), gating on high/critical only. Ran
  `bandit`/`pip-audit` locally for real: 0 HIGH findings after fixing 3 real MEDIUM ones
  (predictable `/tmp/` paths in the new `cmd_admin.py`'s `docker exec` calls) it actually
  caught. `pip-audit` found 3 real advisories on this MACHINE's globally-installed
  package versions (click/cryptography/requests) — not a repo code issue, since CI's own
  fresh `pip install` picks up current compatible versions regardless.
  `development/threat-model.md` gains T14–T18 for the new surfaces + an annual-review
  log entry. Audit-log hash-chaining/signing is NOT built — still open.
- **Support tooling** — `av support-bundle` (redacted diagnostics: versions, health/
  ready, container status, a speed probe; every credential-shaped config key masked,
  plus a belt-and-braces raw-bytes check). `docs/support.md`/`slo.md`/`sla.md` (the
  latter two explicit about no live `/api/metrics` endpoint existing yet) and 5
  `docs/runbooks/` (incident response, HA failover, DR restore, tenant provisioning,
  upgrade/rollback).
- **Docs pass** — README (CLI reference for all new command groups, Enterprise Roadmap
  table rewritten from aspiration to shipped-state), `development/architecture.md` (5 new
  contract sections: Identity & Session, RBAC, Tenancy Isolation, High Availability,
  Backup & DR), `development/infrastructure.md` (env-var table, CI job map),
  `docs/enterprise-operator-guide.md` (new, states plainly what's NOT built),
  `SECURITY.md` (the stale "still enterprise-tier: RBAC/audit logging" claim corrected).
- **DR backup/restore, fully drilled** — `av admin backup create/verify/restore` (new
  `cmd_admin.py`): `pg_dump -Fc` + a gzip'd CAS objects tar + a `backup-manifest-1.0.schema.json`
  manifest (hashes, alembic head, tenant list, approximate row counts). Deliberately
  requires an EXPLICIT `--database-url`/`--db-container` — no auto-detection of "the local
  docker stack" (that exact pattern caused this session's second incident, see below).
  `restore` refuses a non-empty target DB without `--force`. **Phase U in
  `e2e_scenario.sh` (gated `AV_E2E_DR=1`) ran for real and passed**: a real commit pushed
  → backed up → verified → the database schema and CAS objects genuinely destroyed
  (`DROP SCHEMA public CASCADE` + wiped directory) → restored → the pre-destruction
  commit and its ref both read back byte-identical. Measured restore time this run:
  **17 seconds** (the actual RTO printed by the drill, not an estimate). `docs/dr.md`
  states the measured-RTO/stated-RPO distinction. One real bug the drill itself caught
  (see below) that no unit test or design review found.
- New exit code `tenant_denied` (22), wired through `av freeze on` as the first real
  caller. `backup-manifest-1.0` published schema. Docs updated: `AGENTS.md`,
  `docs/for-agents.md`, `docs/contracts.md`, `VERSIONING.md`, `development/CHANGELOG.md`.

### Three real bugs this session's live verification caught (none by design review or unit tests alone)

1. **A live migration DDL bug**: migration `0015`'s `downgrade()` tried to `DROP ROLE
   av_app`, which fails whenever that (cluster-wide) role still holds a grant in ANY
   OTHER database on the same Postgres cluster (this dev machine runs `aether_vault` +
   `aether_vault_test` on one cluster) — found via a full live
   `pytest tests/test_server.py` run cascading 5 unrelated failures from a poisoned
   connection pool. Fixed: `downgrade()` now only revokes this database's own grants and
   deliberately never drops the cluster-wide role (documented in the migration's own
   docstring). Also fixed the underlying pool-poisoning class itself
   (`engine.dispose(close=False)` after the live migration round-trip test, verified live
   not to break the TestClient's portal — a real, escalating risk the existing "one-shot
   retry absorb" mitigation didn't fully cover).
2. **Applying migrations 0011-0015 to the real dev DB while the (old-image)
   `aether-vault-engine` container stayed running left its connection pool serving
   stale/WRONG query results** (confirmed live: fresh object uploads were falsely
   rejected as duplicates — `InvalidCachedStatementError`'s silent-wrong-answer cousin,
   not just a clean error). Recreating the container then exposed the SAME
   image/migration-head mismatch class as the incident from the previous session
   (old image, DB already at a head it doesn't know) — it crash-looped. **Resolved with
   the user's explicit go-ahead**: rebuilt the engine image with this session's code and
   recreated the container. Verified clean afterward: `/api/ready` green, a real
   push→read round trip succeeds, `pg_stat_activity` confirms `av_app` genuinely handling
   request traffic. Also discovered and FIXED: the stack-free test suite and
   `test_server.py`'s live tests shared the SAME Redis instance/DB index as the real dev
   engine (`redis://localhost:6379/0` for both, no isolation) — the local test default
   moved to db 1 in `test_server.py`/`test_auth_users.py`/`test_scopes.py`.
3. **`cmd_admin.py`'s schema-healing step imported `python.av_server.database`** (this
   repo's own test-suite import spelling) instead of the installed package's real
   top-level `av_server.database` — invisible to every unit test (which all run inside
   the `python.*`-spelled process) and even to `av admin backup verify`'s own
   alembic-head check (silently swallowed by a bare `except Exception`). Only surfaced
   running the REAL installed `av` console script end to end — exactly what
   `scripts/e2e_scenario.sh`'s Phase U does, and exactly why it caught this when eleven
   passing unit tests for the same file did not. Fixed in both call sites; the test's own
   mock updated to patch the same spelling the code actually uses.

### Missing — not started

- **SSO** (OIDC + SAML against a real Keycloak container) — zero code. `av login`, exit
  code 21 (`login_required`), real IdP integration. The single heaviest remaining piece.
- **SCIM 2.0 provisioning** — zero code.
- **Per-tenant CAS storage isolation** — the schema prerequisite shipped (migration
  `0014`, PK widening), but the actual feature (physically separate object storage per
  tenant, a second Bloom filter, `AV_CAS_ISOLATION`) is NOT built. Explicitly scoped out,
  documented in that migration's own docstring.
- **Audit-log hash-chaining/signing** — `audit_log` rows are still plain and unsigned;
  the pattern to reuse (`policy_packs`' `prev_id`+`chain_hash`) is identified but not
  applied here.
- **`/api/metrics` (Prometheus)** — no live metrics endpoint. Deliberately not attempted
  this pass: it needs new instrumentation in the ASGI middleware stack, whose ordering is
  explicitly documented as fragile (`server.py`'s own comment on auth/CORS/rate-limit
  ordering) — adding a layer there without a full, careful re-verification pass risked
  destabilizing something already working, under time pressure. `docs/slo.md` states
  this gap plainly rather than pretending the SLIs are continuously measured.
- **Real Kubernetes HA drill** — the Helm chart is schema-verified, not cluster-drilled
  (stated scope decision from the start of this phase).
- **Reference customers / pilot onboarding kit** — not started (and the customers
  themselves are a sales outcome regardless, not something code produces).
- **Third-party security audit / SOC2 / staffed support rotation** — need a firm/hires,
  not code; the strongest in-repo substitutes (scanning CI, threat model, runbooks,
  support-bundle) are what's actually built instead.
- **Obsidian vault regen** — the full wrap-up sequence (`Essential-Tasks.md`) has not
  been run this pass.

### Suggested next order (not yet done, just a recommendation)

1. E2/E3 (SSO+SCIM) — the only remaining major code surface; needs new dependencies
   (`authlib`/`pysaml2`) + a Keycloak compose overlay for live verification.
2. `/api/metrics` — needs its own careful pass given the middleware-ordering risk noted
   above.
3. Audit-log hash-chaining/signing (reuses the `policy_packs` pattern directly).
4. The wrap-up sequence: Obsidian vault regen, a final `git status --short` review, then
   ask the owner whether to commit and whether the Docker image needs rebuilding again.
