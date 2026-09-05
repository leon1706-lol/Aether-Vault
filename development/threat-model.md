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
| DB-backed API tokens / sessions (v1.3.2) | `api_tokens.token_hash`, `sessions.token_hash`/`refresh_hash` (sha256, never the raw token) | The enterprise-path credential store — independent of `.env`, remotely administrable (`av token`) |
| The tenant boundary itself (v1.3.2) | `tenant_id` column + Postgres RLS policy on 28 tables, enforced when `AV_TENANCY_ENFORCE=1` | What separates one customer's data from another's on a shared registry — the asset a multi-tenant deployment's entire trust model rests on |
| Backup artifacts (v1.3.2) | `db.dump` + `objects.tar.gz` written by `av admin backup create`, wherever the operator points `OUTPUT_DIR` | A full, UNENCRYPTED copy of the database and every CAS object — as sensitive as the live system, and easier to accidentally leave somewhere less protected (a laptop, an unencrypted bucket) |
| The `av_app` non-superuser DB role's credential (v1.3.2) | A fixed literal in `docker-compose.yml` (matching the existing `av_password` posture for `av_user`) | The identity RLS actually enforces against — see T15 below |

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
- **Tenant admin (v1.3.2)** — holds the `admin` permission via a role binding SCOPED to
  one tenant (or one project within it) — distinct from a registry operator, who has
  infrastructure-level access regardless of any tenant boundary.
- **Cross-tenant attacker (v1.3.2)** — an authenticated identity belonging to tenant B,
  attempting to read or write tenant A's projects/commits/runs on the same shared
  registry. The threat model's newest actor, and the one the tenancy guard + RLS backstop
  (T14) exist specifically for.
- **Backup operator (v1.3.2)** — whoever runs `av admin backup create/restore`; needs
  either direct Postgres client-tool access or `docker exec` rights into the DB/engine
  containers, and ends up holding a full, unencrypted copy of the database and CAS tree
  on their own machine.

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
| T2 | Token theft from `.env` (plaintext on disk) | File lives outside version control (`.dockerignore`/`.gitignore`); `av auth rotate` (v1.3.0) invalidates a leaked token immediately; per-user tokens (`av auth add-user`) limit blast radius to one identity, individually revocable | No at-rest encryption of `.env` — an attacker with filesystem read access to the compose host reads tokens directly. Documented, not silently assumed away |
| T3 | Token replay / brute force | `secrets.compare_digest` (timing-safe compare) on every candidate; tokens are `secrets.token_urlsafe(32)` (256 bits of entropy) — brute force is computationally infeasible | No rate limiting specifically on auth attempts today (GC has its own limiter; the auth middleware itself doesn't) — an attacker inside the trusted network could hammer the token check. Tracked, not yet mitigated |
| T4 | Expired-but-leaked per-user token still works | v1.3.0 optional `--expires-in-days` on `av auth add-user`; `_resolve_identity()` rejects an expired token server-side even if the raw string is still known | Expiry is opt-in per user — a token added without it never expires; this is a deliberate default (matches the owner shared secret's own no-expiry model), not an oversight |
| T5 | Ref race / concurrent-write corruption from two agents | Compare-and-swap (`expected_hash`) unconditional on every ref push (v1.2.5); a losing race queues via `.av/pending_push` rather than silently overwriting or losing a commit | None identified beyond normal CAS limitations (a third concurrent writer between CAS check and this client's next attempt just races again, safely) |
| T6 | Hostile/oversized push payload (resource exhaustion) | Server-side caps: 100,000 tree entries, 1,000 metrics, 200 tags, 20,000-char message, 200-char tag length; ref names pass a strict regex rejecting path-traversal (`..`) before touching the filesystem fallback | Caps are fixed constants, not configurable per-deployment — a legitimate use case that needs a larger tree would need a code change, not just a config bump |
| T7 | Object-hash spoofing (claiming a hash for different bytes) | Every upload is re-verified server-side against its claimed SHA-256 before acceptance; `hash_file` is the canonical hasher shared by client and the verification path | None identified — SHA-256 collision is not a practical threat at this scale |
| T8 | Webhook secret exposure via registry compromise | Secrets are masked in every list/read API response (only the last few characters shown); full value only ever transmitted once at creation | By design, the registry itself DOES hold the plaintext secret (deliveries must be signed with it) — a full registry DB compromise exposes every subscriber's secret. SECURITY.md states this explicitly ("treat registry compromise as secret compromise for subscribers") |
| T9 | Forged webhook delivery (a subscriber accepting a spoofed event) | `X-AV-Signature: hex(hmac-sha256(secret, body))` on every delivery; the canonical signed body is reconstructed byte-identically on retry so a replay-detection subscriber can also check `X-AV-Event-Id` for duplicates | Aether-Vault does not mandate subscribers actually verify the signature — that's the subscriber's own responsibility, documented but not enforceable from this side |
| T10 | Signing-key theft enabling forged "signed" commits | Private key file permissions 0600; rotation archives (never deletes) old keys so history keeps verifying; SECURITY.md states plainly "anyone who can write to `.av/keys/` can sign as you" | This is fundamentally a local-filesystem trust boundary (T-local in effect) — signing is tamper EVIDENCE, explicitly not identity binding or a PKI; a stolen key produces validly-signed-but-illegitimate commits indistinguishable from legitimate ones without out-of-band key verification |
| T11 | Audit log tampering/deletion by a privileged attacker | `av audit prune` is admin-scoped (same auth as every other admin route) and, since v1.3.0, supports `--dry-run` so an operator can review before an irreversible delete; every mutating route is enforced-covered by `tests/test_audit_coverage.py`'s CI matrix | The audit log itself is a normal DB table with no separate immutability guarantee (no WORM storage, no external log shipping) — a compromised registry credential with admin scope can delete audit history. Enterprise-tier "cryptographically signed, immutable" audit logging (SECURITY.md) is the stated future mitigation |
| T12 | Path traversal via a crafted ref name reaching the filesystem CAS fallback | `validate_ref_name()` rejects traversal-shaped names before any filesystem path is built; `CASStorage._safe_ref_path()` additionally resolves and checks the result stays under `refs_dir` (defense in depth — two independent checks, not just one) | None identified — both layers were exercised by the existing signing/registry test suite |
| T13 | Registry export/restore archive tampering | Every object is hash-re-verified on both export (download) and restore (before upload) — a corrupted or tampered archive shard is rejected rather than silently re-ingested | An attacker with write access to an export archive on disk between export and restore could still substitute a *valid* (hash-matching) but different snapshot's shard for one whose hash they also control — outside this threat model's scope (that's a filesystem trust boundary, not a protocol one) |
| T14 | Cross-tenant data access via a route that forgets its own tenant guard (v1.3.2) | Two independent layers: an application-level guard (`_enforce_project_tenant`, a GLOBAL FastAPI dependency — every route gets it by construction, not opt-in per route) AND Postgres row-level security enforced by the non-superuser `av_app` role (migration `0015`) as a genuine backstop, live-verified via a raw SQL probe with the application layer entirely bypassed. Both gated behind `AV_TENANCY_ENFORCE` (off by default — MINOR-release additive guardrail) | RLS's bypass is GUC-based (`app.bypass_rls`, for the two legitimately cross-tenant background workers), which is a SOFTWARE boundary, not a hard privilege boundary — it defends against an application bug, not a fully compromised app process with arbitrary SQL execution. Per-tenant CAS object storage isolation is NOT built (schema prerequisite only, migration `0014`) — identical content uploaded by two tenants currently still dedups globally, which is a deliberate, documented scope decision, not an oversight |
| T15 | `av_app`/`av_user` DB credential exposure | Same posture this repo already accepts for `av_password` (a fixed literal in `docker-compose.yml`, this repo's own reference dev topology) — `av_app` is additionally the LOW-PRIVILEGE role (no DDL, no superuser, no BYPASSRLS), so its compromise alone cannot escalate to schema changes or an RLS bypass the way `av_user`'s always could | A real production deployment is expected to rotate both passwords the same way it would rotate any other database credential (`ALTER ROLE ... WITH PASSWORD`) — this repo's dev compose file, like `av_password` before it, ships a placeholder, not a secret-manager-issued value |
| T16 | Backup artifact theft (v1.3.2) | `av admin backup create` requires an explicit, operator-chosen `OUTPUT_DIR` — never a predictable or auto-selected location | Neither `db.dump` nor `objects.tar.gz` is encrypted by this tool — a full, plaintext copy of the database and every CAS object. `docs/dr.md` states plainly that encrypting `OUTPUT_DIR` before it leaves the host (e.g., before shipping off to cold storage) is the operator's own responsibility |
| T17 | Stale DB-backed token/session after revocation (v1.3.2) | `identity.py`'s principal-resolution TTL cache (`AV_AUTH_CACHE_TTL_SECS`, default 30s) trades a small, bounded revocation-latency window for avoiding a DB round trip on every single request | A token revoked via `av token revoke` can still successfully authenticate for up to the cache TTL on any replica that already cached it — documented, bounded, not silent |
| T18 | `av admin backup restore` targeting the wrong database (v1.3.2) | No auto-detection of "the local docker stack" (unlike `av auth`) — every invocation requires an explicit `--database-url`/`--db-container`, and `restore` additionally refuses a non-empty target without `--force`. This design choice exists BECAUSE this exact incident class (an auto-detecting mutating command silently hitting the wrong stack) happened once during this feature's own development — see `development/CHANGELOG.md` Phase 60 | An operator who manually copy-pastes the wrong `--database-url` and also passes `--force` can still overwrite the wrong database — the same residual risk any destructive admin tool has once a human explicitly overrides its one safety check |

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
