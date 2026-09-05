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

## Status as of 2026-09-05 (session 4, v1.3.3) — feature-complete, verification-complete
## short of a live external IdP; not committed

v1.3.2 (identity/RBAC/tenancy/HA/DR/security-scanning/support-tooling) is committed
(`4887616 "V1.3.2 RLS fix, securety scanning, support tooling"`). Session 3 closed
audit-log hash-chaining/signing, `/api/metrics`, and per-tenant CAS storage isolation
(migration `0016`). **This session (4) closes the two items still open — SSO (OIDC +
SAML 2.0) and SCIM 2.0 provisioning** — completing the entire v1.3.2 enterprise-readiness
plan end to end. Nothing from sessions 3-4 is committed yet. The owner will rebuild the
Docker image manually and run verification afterward, per this session's own instruction.

### Done this session (v1.3.3)

- **Audit log hash-chaining + optional signing** (migration `0016`) — `audit_log` gains
  `chain_hash` (NOT NULL, real historical backfill for every pre-existing row, not a
  placeholder) and `signature` (nullable). Chains by `id`'s own natural order (no
  `prev_id` column needed, unlike `policy_packs`) via one canonical formula
  (`audit_chain.compute_chain_hash`) shared by the migration's backfill, the legacy-
  volume adoption heal path, and the runtime `before_flush` listener
  (`database.py::_chain_audit_log`) — so the three can never drift apart. Concurrency
  solved explicitly with `pg_advisory_xact_lock` (reasoned through the real fork
  scenario before writing the listener, not after finding it broken). Optional ed25519
  signing (`AV_AUDIT_SIGNING_KEY_PATH`, a NEW server-wide keypair, deliberately separate
  from `av_cli/signing.py`'s per-repo commit-signing keys) via new `audit_signing.py`.
  New routes `GET /api/admin/audit/verify` (± `since_id`) and `.../public-key`; new CLI
  `av audit verify` (± `--export`/`--public-key` for genuinely offline, independent
  verification — never asking the server to grade its own homework).
  **Live-verified**: an untampered chain verifies ok; a row tampered directly in
  Postgres is caught at the exact broken id; two audit rows written in the SAME request
  flush still chain correctly against each other; a configured signing key produces
  signatures that verify against the published public key.
- **`/api/metrics`** — hand-rolled Prometheus text exposition (`metrics.py`), the same
  "no new dependency" judgment call `rate_limit.py` already made. Registered as the
  OUTERMOST middleware layer (deliberately, after careful analysis of this file's own
  documented auth/CORS/rate-limit ordering fragility) so it observes every request end
  to end, including 429s and 401s a route-only view would miss. Exposes request counts/
  latency histogram (9 buckets), per-tenant request counts, webhook queue depth, and DB
  pool state. `admin`-scoped. **Live-verified**, and the FULL live `test_server.py` suite
  re-run clean afterward specifically to confirm the new middleware didn't disturb the
  existing auth/CORS/rate-limit interactions.
- **Per-tenant CAS storage isolation** (`AV_CAS_ISOLATION`, default `shared`) — the
  feature migration `0014`'s PK-widening was a schema prerequisite for. Shared mode
  (default) is byte-for-byte untouched: every existence check across `upload_object`/
  `head_object`/`POST /api/sync/batch-objects`/`build_merkle_tree`/`_object_exists` (and
  its ~10 RSI-artifact callers) stays completely unfiltered by tenant. Isolated mode
  threads `_cas_tenant_id(request)` through storage (`objects_dir/<tenant_id>/...`,
  legacy-flat-path fallback for zero-downtime migration), the Bloom filter (per-tenant
  filter names, same fallback), every one of those existence checks (now genuinely
  audited, not spot-checked), AND GC's mark/sweep (which now always computes per-tenant
  alive sets — shared mode's dead-computation uses their union, mathematically identical
  to the old flat computation; isolated mode's uses each row's own tenant's set
  specifically, which the design work identified as NECESSARILY different logic, not a
  shared shortcut, for two independent, reasoned-through reasons documented in
  `development/architecture.md`'s Per-Tenant CAS Isolation Contract). **Live-verified**:
  identical content from two tenants both succeed (not a false 409) and stay
  independently readable/HEAD-able; a second upload by the SAME tenant still correctly
  409s (isolation doesn't weaken intra-tenant dedup); files are physically separate on
  disk; batch-objects correctly reports "missing" for a tenant that never uploaded; and
  a no-isolation control test proves shared mode's global dedup is completely unchanged.
- **Docs**: `development/architecture.md` gains 3 new contract sections (Audit Log
  Hash-Chain, Metrics, Per-Tenant CAS Isolation) — also fixed a real structural bug found
  while adding them: a PREVIOUS session's edit had accidentally split the existing
  Anomaly Alerts Contract section in half, stranding its own concluding paragraph after
  an unrelated later section. `VERSIONING.md` gains a `v1.3.3 additive surfaces` section
  and the v1.3.2 section's RLS-superuser-gap bullet was corrected (that gap is fixed,
  not still open — the doc had gone stale). `infrastructure.md`'s env-var table,
  `README.md` (CLI reference, Enterprise Roadmap table), and
  `docs/enterprise-operator-guide.md` all updated.

### Real bug this session's live verification caught

`cmd_admin.py` and its own test suite both used `python.av_server.database` (this
repo's own test-only import spelling) — carried over from copy-pasting the pattern from
before it was known to be wrong. Caught by re-reading the actual fix from the v1.3.2
incident writeup rather than repeating it blind; verified the corrected `av_server.`
spelling directly.

One test flake observed and NOT treated as a real bug: a fresh two-tenant CAS-isolation
test failed once with a HEAD request returning 404 immediately after a GET on the exact
same object succeeded, then passed cleanly on 3 separate full re-runs (15 test
executions total) with no code change — consistent with this environment's
already-documented Windows/asyncio cross-loop timing flakiness class, not reproducible
in an isolated standalone repro script. Logged here rather than silently dismissed.

### Done this session (session 4, v1.3.3 — SSO & SCIM)

- **OIDC login** (`sso_oidc.py`) — authorization-code + PKCE, ID token validated in full
  (JWKS signature, issuer, audience, expiry, nonce-replay), signed-cookie state
  round-trip, JIT provisioning + IdP-group→role mapping (both per-provider, opt-in).
- **Device-code flow** (`device_flow.py`, Redis-backed) — what `av login` actually
  drives. Single-use approval collection.
- **SAML 2.0** (`sso_saml.py`, new `pysaml2` `[saml]` extra) — metadata/ACS/SLS, real
  signature/condition/audience validation via the library, Redis-backed assertion-ID
  replay protection on top (needed since `allow_unsolicited=True`'s IdP-initiated flow
  has no InResponseTo to key off).
- **SCIM 2.0** (`scim.py`, `/scim/v2/*`, RFC 7643/7644) — Users/Groups CRUD, filter
  (`userName`/`externalId`/`emails.value` eq), pagination, PATCH/DELETE deprovisioning
  (suspend + revoke sessions, never hard-delete), 409-on-duplicate-create for safe IdP
  retries.
- **The real fix this phase's own features exposed as a hard blocker**:
  `identity.py::_permissions_for_subject` never expanded through group membership for a
  user principal — SSO's group→role mapping and SCIM's group sync were both silently
  inert without it. Fixed; live-verified both directions (grant via group binding, and
  revoke-on-removal-from-group).
- **`login_required` (exit 21) activated** — was reserved since v1.3.2 pending a real
  caller; `av login`'s timeout is that caller. All six touch points updated (`core.py`,
  `av_sdk/exceptions.py`, `docs/for-agents.md`, `AGENTS.md`, plus two test-side
  registries found only by re-running the anti-drift sweep, not by inspection alone).
- **New CLI**: `av login`, `av logout`, `av whoami`, `av idp add|list|show|test|remove`,
  `av scim status`, `av scim token create|revoke`. `av init --mode enterprise` now
  genuinely works (`StubEnterpriseAuthProvider` replaced with a real
  `DeviceCodeEnterpriseAuthProvider`, zero call-site changes in `cmd_repo.py`).
- **No new migration needed** — every table SSO/SCIM uses already existed from
  migration `0011`; verified directly, not assumed from the original plan sketch.
- **Live-verified**: the FULL `test_server.py` suite (175 pre-existing + 15 new tests)
  re-run clean against real Postgres/Redis with every new module loaded. New coverage:
  `TestGroupRoleBindingGrantsUserPermission`, `TestScim`, `TestSsoCrypto`,
  `TestDeviceFlow`. One real test-harness bug found and fixed along the way (a
  cross-loop Redis-client reuse issue in the device_flow test, same class as an
  already-documented asyncpg one — fixed the same way, not worked around).
- **Docs**: `development/architecture.md` gains SSO + SCIM contract sections;
  `VERSIONING.md`'s v1.3.3 section rewritten from "in progress" to closed;
  `README.md` (roadmap table, CLI reference), `docs/enterprise-operator-guide.md`
  (new sections 10-11), `development/CHANGELOG.md` (Phase 62) all updated.

### Missing — not started

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
- **Obsidian vault regen** — the full wrap-up sequence (`Essential-Tasks.md`) has not
  been run this pass.
- **Docker image rebuild + post-rebuild verification** — the owner is doing this step
  manually; not run by the agent this session.

### Suggested next order (not yet done, just a recommendation)

1. Stand up a Keycloak compose overlay and drive `av login`/SAML ACS against it for
   real, closing the one honest verification gap left in the SSO work.
2. The wrap-up sequence — Obsidian vault regen, a final `git status --short` review,
   then ask the owner whether to commit (the whole v1.3.2 enterprise-readiness plan is
   now feature-complete across sessions 2-4).
