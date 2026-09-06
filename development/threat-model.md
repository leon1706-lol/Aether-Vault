# Threat model

Living doc — a threat→mitigation→residual-risk record for the registry, tokens,
webhooks, and signing keys, plus an annual review cadence. `SECURITY.md` is the reporting
process and the plain-language trust statements for operators; this document is the
systematic threat model those statements are drawn from. Update this file (not just
SECURITY.md) whenever a mitigation changes, and log the annual pass at the bottom.

## Assets

| Asset | Where it lives | Why it matters |
|---|---|---|
| Repository history (commits, trees, refs) | Postgres (`commits`, `trees`, `refs`) + CAS object store | The versioned artifact/dataset/code record itself — the product's core data |
| CAS object bytes | Filesystem shards under `AV_DATA_DIR/objects/` | Model weights, datasets, code — potentially large, sensitive, or proprietary |
| `AV_API_TOKEN` / per-user tokens | `.env` next to the compose file (plaintext) | Bearer credentials for Protected-mode registries |
| Webhook signing secrets | `webhooks.secret` column (plaintext) | HMAC key subscribers trust to authenticate delivery bodies |
| Commit signing private keys | `.av/keys/signing.pem` (0600), archived copies under `.av/keys/archive/` | Proves payload integrity for signed commits |
| Audit log | `audit_log` table | Who-did-what-with-what-outcome — itself a security-relevant record |
| Run/metrics metadata | `runs`, `run_commits` tables | Experiment provenance; not usually sensitive alone, but reveals project shape |
| DB-backed API tokens / sessions | `api_tokens.token_hash`, `sessions.token_hash`/`refresh_hash` (sha256, never the raw token) | The enterprise-path credential store — independent of `.env`, remotely administrable (`av token`) |
| The tenant boundary itself | `tenant_id` column + Postgres RLS policy on 28 tables, enforced when `AV_TENANCY_ENFORCE=1` | What separates one customer's data from another's on a shared registry — the asset a multi-tenant deployment's entire trust model rests on |
| Backup artifacts | `db.dump` + `objects.tar.gz` written by `av admin backup create`, wherever the operator points `OUTPUT_DIR` | A full, UNENCRYPTED copy of the database and every CAS object — as sensitive as the live system, and easier to accidentally leave somewhere less protected (a laptop, an unencrypted bucket) |
| The `av_app` non-superuser DB role's credential | A fixed literal in `docker-compose.yml` (matching the existing `av_password` posture for `av_user`) | The identity RLS actually enforces against — see T15 below |
| SSO provider client secrets | `sso_providers.config`, Fernet-encrypted at rest under `AV_SECRET_KEY` (`sso_crypto.py`) | The credential this server uses to authenticate itself to an external IdP during OIDC code exchange — compromise lets an attacker impersonate this server to the IdP |
| The IdP trust relationship itself | Implicit — whichever OIDC/SAML provider an admin registers via `av idp add` is trusted to assert user identity and group membership | Everything downstream (JIT-provisioned accounts, group→role mapping) inherits whatever the IdP asserts; a compromised or misconfigured IdP is a direct path to this server's own access control |
| SCIM provisioning tokens | An `api_tokens` row carrying the `scim` scope, same storage/hashing as any other token | Holds full create/update/deprovision power over every user and group in its tenant — a materially broader blast radius than a typical read/write token |

## Actors

- **Anonymous client** — any process that can reach the registry's HTTP port with no
  credentials (default "Anonymous" mode, or any exempt route in Protected mode).
- **Authenticated user** — holds a valid owner shared secret or per-user token.
- **Registry operator** — controls the host running Postgres/Redis/the engine container;
  has filesystem access to `AV_DATA_DIR`, `.env`, and the database.
- **Repo-local attacker** — has write access to a checkout's `.av/` directory (e.g., a
  compromised dependency running as the same OS user) but not necessarily registry
  credentials.
- **Webhook subscriber** — an external HTTP endpoint the operator configured to receive
  signed event deliveries.
- **Passive network observer** — can see traffic between client and registry (relevant
  only where TLS termination is the operator's own responsibility — see SECURITY.md's
  "put TLS termination in front of uvicorn" recommendation).
- **Tenant admin** — holds the `admin` permission via a role binding SCOPED to
  one tenant (or one project within it) — distinct from a registry operator, who has
  infrastructure-level access regardless of any tenant boundary.
- **Cross-tenant attacker** — an authenticated identity belonging to tenant B,
  attempting to read or write tenant A's projects/commits/runs on the same shared
  registry. The actor the tenancy guard + RLS backstop (T14) exist specifically for.
- **Backup operator** — whoever runs `av admin backup create/restore`; needs
  either direct Postgres client-tool access or `docker exec` rights into the DB/engine
  containers, and ends up holding a full, unencrypted copy of the database and CAS tree
  on their own machine.
- **Identity provider** — the external OIDC/SAML system an admin registers via
  `av idp add`; trusted to assert user identity, email, display name, and group
  membership. Not controlled by this codebase — a trust boundary distinct from every
  other actor here, none of which requires trusting a third-party system's own assertions.
- **SCIM provisioner** — an external system (an IdP's SCIM connector) holding a
  `scim`-scoped token, driving `/scim/v2/*` to create/update/deprovision users and groups
  in one tenant. Distinct from a tenant admin: it acts through a fixed protocol surface,
  not the general admin API, but with materially broad power within that surface.

## Trust boundaries

```
CLI/SDK/plugin  --HTTP(S)-->  registry (FastAPI)  <-->  Postgres
      |                            |    \
      |                        (auth        \--> Redis (bloom filter, perf-only)
      |                         middleware)
      |                            |
      +-- reads/writes .av/ -------+
      |   (local trust boundary:                     registry  --HTTP(S)-->  webhook subscriber
      |    OS-user filesystem                                  (signed, HMAC-SHA256)
      |    permissions)
      v
  local disk (.av/keys/, .env, pending_push)
```

Every arrow crossing a box is a place credentials or payloads can be intercepted, forged,
or replayed if the mitigation on that edge fails.

## Threats, mitigations, residual risk

| # | Threat | Mitigation | Residual risk |
|---|---|---|---|
| T1 | Unauthenticated read/write against an Anonymous-mode registry exposed to an untrusted network | Documented as the deployment default only for trusted networks; `av auth set-token` switches to Protected mode, gating every route except `/api/health`, `/api/ready`, `/docs`, `/openapi.json`, `/redoc` | An operator who exposes an Anonymous registry publicly has no protection — this is a configuration risk, not a code defect; `av init`'s interactive prompt and SECURITY.md both surface the choice explicitly |
| T2 | Token theft from `.env` (plaintext on disk) | File lives outside version control (`.dockerignore`/`.gitignore`); `av auth rotate` invalidates a leaked token immediately; per-user tokens (`av auth add-user`) limit blast radius to one identity, individually revocable | No at-rest encryption of `.env` — an attacker with filesystem read access to the compose host reads tokens directly. Documented, not silently assumed away |
| T3 | Token replay / brute force | `secrets.compare_digest` (timing-safe compare) on every candidate; tokens are `secrets.token_urlsafe(32)` (256 bits of entropy) — brute force is computationally infeasible | No rate limiting specifically on auth attempts today (GC has its own limiter; the auth middleware itself doesn't) — an attacker inside the trusted network could hammer the token check. Tracked, not yet mitigated |
| T4 | Expired-but-leaked per-user token still works | Optional `--expires-in-days` on `av auth add-user`; `_resolve_identity()` rejects an expired token server-side even if the raw string is still known | Expiry is opt-in per user — a token added without it never expires; this is a deliberate default (matches the owner shared secret's own no-expiry model), not an oversight |
| T5 | Ref race / concurrent-write corruption from two agents | Compare-and-swap (`expected_hash`) unconditional on every ref push; a losing race queues via `.av/pending_push` rather than silently overwriting or losing a commit | None identified beyond normal CAS limitations (a third concurrent writer between CAS check and this client's next attempt just races again, safely) |
| T6 | Hostile/oversized push payload (resource exhaustion) | Server-side caps: 100,000 tree entries, 1,000 metrics, 200 tags, 20,000-char message, 200-char tag length; ref names pass a strict regex rejecting path-traversal (`..`) before touching the filesystem fallback | Caps are fixed constants, not configurable per-deployment — a legitimate use case that needs a larger tree would need a code change, not just a config bump |
| T7 | Object-hash spoofing (claiming a hash for different bytes) | Every upload is re-verified server-side against its claimed SHA-256 before acceptance; `hash_file` is the canonical hasher shared by client and the verification path | None identified — SHA-256 collision is not a practical threat at this scale |
| T8 | Webhook secret exposure via registry compromise | Secrets are masked in every list/read API response (only the last few characters shown); full value only ever transmitted once at creation | By design, the registry itself DOES hold the plaintext secret (deliveries must be signed with it) — a full registry DB compromise exposes every subscriber's secret. SECURITY.md states this explicitly ("treat registry compromise as secret compromise for subscribers") |
| T9 | Forged webhook delivery (a subscriber accepting a spoofed event) | `X-AV-Signature: hex(hmac-sha256(secret, body))` on every delivery; the canonical signed body is reconstructed byte-identically on retry so a replay-detection subscriber can also check `X-AV-Event-Id` for duplicates | Aether-Vault does not mandate subscribers actually verify the signature — that's the subscriber's own responsibility, documented but not enforceable from this side |
| T10 | Signing-key theft enabling forged "signed" commits | Private key file permissions 0600; rotation archives (never deletes) old keys so history keeps verifying; SECURITY.md states plainly "anyone who can write to `.av/keys/` can sign as you" | This is fundamentally a local-filesystem trust boundary (T-local in effect) — signing is tamper EVIDENCE, explicitly not identity binding or a PKI; a stolen key produces validly-signed-but-illegitimate commits indistinguishable from legitimate ones without out-of-band key verification |
| T11 | Audit log tampering/deletion by a privileged attacker | `av audit prune` is admin-scoped (same auth as every other admin route) and supports `--dry-run` so an operator can review before an irreversible delete; every mutating route is enforced-covered by `tests/test_audit_coverage.py`'s CI matrix. Every row is hash-chained (`chain_hash`, migration `0016`) — tampering or deleting a row breaks `av audit verify`'s chain check from that point forward, and optional ed25519 signing (`AV_AUDIT_SIGNING_KEY_PATH`) adds non-repudiation | The chain is stored in the SAME table it protects — an attacker with direct DB write access (not just an admin-scoped API credential) can still rewrite the whole chain from any point forward, recomputing every subsequent `chain_hash` consistently; genuine tamper-evidence against that class of attacker needs the OPTIONAL signing key kept somewhere the DB compromise doesn't also reach (`AV_AUDIT_SIGNING_KEY_PATH` off the DB host), or external log shipping (still not built) |
| T12 | Path traversal via a crafted ref name reaching the filesystem CAS fallback | `validate_ref_name()` rejects traversal-shaped names before any filesystem path is built; `CASStorage._safe_ref_path()` additionally resolves and checks the result stays under `refs_dir` (defense in depth — two independent checks, not just one) | None identified — both layers were exercised by the existing signing/registry test suite |
| T13 | Registry export/restore archive tampering | Every object is hash-re-verified on both export (download) and restore (before upload) — a corrupted or tampered archive shard is rejected rather than silently re-ingested | An attacker with write access to an export archive on disk between export and restore could still substitute a *valid* (hash-matching) but different snapshot's shard for one whose hash they also control — outside this threat model's scope (that's a filesystem trust boundary, not a protocol one) |
| T14 | Cross-tenant data access via a route that forgets its own tenant guard | Two independent layers: an application-level guard (`_enforce_project_tenant`, a GLOBAL FastAPI dependency — every route gets it by construction, not opt-in per route) AND Postgres row-level security enforced by the non-superuser `av_app` role (migration `0015`) as a genuine backstop, live-verified via a raw SQL probe with the application layer entirely bypassed. Both gated behind `AV_TENANCY_ENFORCE` (off by default) | RLS's bypass is GUC-based (`app.bypass_rls`, for the two legitimately cross-tenant background workers), which is a SOFTWARE boundary, not a hard privilege boundary — it defends against an application bug, not a fully compromised app process with arbitrary SQL execution. Per-tenant CAS object storage isolation is NOT built (schema prerequisite only, migration `0014`) — identical content uploaded by two tenants currently still dedups globally, which is a deliberate, documented scope decision, not an oversight |
| T15 | `av_app`/`av_user` DB credential exposure | Same posture this repo already accepts for `av_password` (a fixed literal in `docker-compose.yml`, this repo's own reference dev topology) — `av_app` is additionally the LOW-PRIVILEGE role (no DDL, no superuser, no BYPASSRLS), so its compromise alone cannot escalate to schema changes or an RLS bypass the way `av_user`'s always could | A real production deployment is expected to rotate both passwords the same way it would rotate any other database credential (`ALTER ROLE ... WITH PASSWORD`) — this repo's dev compose file, like `av_password` before it, ships a placeholder, not a secret-manager-issued value |
| T16 | Backup artifact theft | `av admin backup create` requires an explicit, operator-chosen `OUTPUT_DIR` — never a predictable or auto-selected location | Neither `db.dump` nor `objects.tar.gz` is encrypted by this tool — a full, plaintext copy of the database and every CAS object. `docs/dr.md` states plainly that encrypting `OUTPUT_DIR` before it leaves the host (e.g., before shipping off to cold storage) is the operator's own responsibility |
| T17 | Stale DB-backed token/session after revocation | `identity.py`'s principal-resolution TTL cache (`AV_AUTH_CACHE_TTL_SECS`, default 30s) trades a small, bounded revocation-latency window for avoiding a DB round trip on every single request | A token revoked via `av token revoke` can still successfully authenticate for up to the cache TTL on any replica that already cached it — documented, bounded, not silent |
| T18 | `av admin backup restore` targeting the wrong database | No auto-detection of "the local docker stack" (unlike `av auth`) — every invocation requires an explicit `--database-url`/`--db-container`, and `restore` additionally refuses a non-empty target without `--force`. This design choice exists because an auto-detecting mutating command silently hitting the wrong stack is a real incident class — see `development/CHANGELOG.md` Phase 60 | An operator who manually copy-pastes the wrong `--database-url` and also passes `--force` can still overwrite the wrong database — the same residual risk any destructive admin tool has once a human explicitly overrides its one safety check |
| T19 | A compromised or malicious IdP asserting false identity/group claims | ID-token/assertion signature is verified against the IdP's own live JWKS (OIDC) or via `pysaml2`'s signature/condition checks (SAML) — this server only trusts claims from a key the IdP itself controls, not an unauthenticated bearer of a token shape. Group→role mapping is scoped to whatever `group_role_map` an admin explicitly configured — an unmapped group name asserted by the IdP grants nothing | The IdP itself is fully trusted once registered — this server has no way to detect an IdP that has been compromised and is asserting false-but-correctly-signed claims (e.g. a hijacked group membership). This is the SAME trust posture every SSO integration in the industry has; documented here rather than silently assumed |
| T20 | OIDC/SAML replay — a captured callback code, ID token, or SAML assertion reused by an attacker | OIDC: PKCE (code_verifier never leaves this server, so a captured `code` alone is unusable) plus nonce-replay checking on the ID token. SAML: a Redis-backed assertion-ID dedup (atomic `SET NX`) rejects a second POST of the identical assertion outright | The nonce/assertion-ID replay windows are Redis-TTL-bounded (minutes) — an attacker who captures and replays within that window before the legitimate exchange completes could still race it, though PKCE specifically closes the OIDC code-interception case even then. Standard residual risk for both protocols, not specific to this implementation |
| T21 | JIT provisioning creating or hijacking an account via a spoofed/reused email claim | JIT is OFF by default per provider — an admin must explicitly opt in (`av idp add --jit`). When on, `upsert_user_from_claims` links by `(provider_id, subject)` first, falling back to email match only for a genuinely new identity — an existing `user_identities` link always wins, so a later claim with the same email cannot silently re-target an already-linked account to a different IdP subject | With `--jit` on, an IdP that allows self-service email changes (many do) could, in principle, let one of its own users claim a DIFFERENT existing local account's email on first-ever login for that provider, before any link exists — this is a known class of JIT-provisioning risk industry-wide, not unique to this implementation; documented here as a reason to keep `--jit` off for any IdP whose email verification an admin doesn't fully trust |
| T22 | SCIM token compromise | Same storage/hashing as every other `api_tokens` row (sha256, never plaintext, immediately revocable via `av scim token revoke`/`av token revoke`) — no special exemption from existing token hygiene | A `scim`-scoped token's blast radius is broader than a typical token by design (full user/group provisioning power for its tenant) — an admin who treats it as casually as a read-only token underestimates its reach. `av scim token create`'s own output states this explicitly |

## Out of scope (see SECURITY.md's own "Out of scope" section)

Social engineering, brute force against deployments this project doesn't operate,
vulnerabilities in Docker/Postgres/Redis themselves, and self-hosted misconfigurations
that expose ports publicly without Protected mode enabled.

## Annual review log

Review this document at least once a year (or after any change to auth, signing,
webhooks, or the registry's trust boundaries) — re-read every row above against the
current code, add new threats introduced by new features, and record the pass here:

| Date | Reviewer | Notes |
|---|---|---|
| 2026-09-02 | Claude Sonnet 5 (v1.3.0 depth pass) | Initial version of this document — threats T1–T13 derived from the current codebase (auth middleware, ref CAS, webhook signing, commit signing, audit coverage, registry export/restore) and cross-checked against SECURITY.md's existing plain-language statements for consistency. |
| 2026-09-05 | Claude Sonnet 5 (v1.3.2 enterprise-readiness pass) | Added T14–T18 for the new surfaces shipped this pass: hard multi-tenancy (app guard + RLS via the new non-superuser `av_app` role, migration `0015`), DB-backed tokens/sessions, and `av admin backup`. T11's "future mitigation" (signed/hash-chained audit logs) is still NOT built this pass — remains open. SSO/SCIM introduce no new threats here because they introduce no code here — zero lines shipped this pass. |
| 2026-09-05 | Claude Sonnet 5 (v1.3.3 SSO & SCIM pass) | T11 updated — audit-log hash-chaining/signing shipped this pass (a prior pass this same day), closing part of that gap; residual risk narrowed to "an attacker with direct DB write access can still rewrite the whole chain consistently" rather than "no tamper-evidence at all". Added three new assets (SSO provider client secrets, the IdP trust relationship itself, SCIM provisioning tokens), two new actors (identity provider, SCIM provisioner), and T19–T22 for SSO/SCIM's genuinely new attack surface: a compromised/malicious IdP asserting false claims, OIDC/SAML replay, JIT-provisioning account-hijack risk, and SCIM token compromise. All four residual risks are industry-standard for SSO/SCIM integrations generally, not specific implementation gaps — documented as such rather than overstated. |
