# Enterprise operator guide: identity, tenancy, and DR (v1.3.2, SSO/SCIM added v1.3.3)

A continuous path through the enterprise-readiness surfaces shipped in v1.3.2 — the same
convention `docs/tutorial.md`/`docs/rsi-operator-guide.md` established, applied to the
surfaces an operator (not an individual agent/researcher) drives: provisioning tenants
and users, granting roles, minting remotely-administrable tokens, understanding the
tenancy boundary, and taking/restoring backups. Every command below is real;
`tests/test_docs_commands.py` parses every fenced `av ...` line on this page and asserts
the command and every flag it uses actually exist in the live CLI.

**Read this first — what this pass shipped and what it didn't.** RBAC, hard
multi-tenancy, and disaster recovery are real, live-verified code (see
`development/architecture.md`'s Identity & Session / RBAC / Tenancy Isolation / Backup &
DR contract sections). **v1.3.4 correction:** this paragraph used to say SSO/SCIM were not
built at all — that stopped being true in v1.3.3 (sections 10-11 below), which this intro
was never updated to reflect. SSO (OIDC/SAML) and SCIM provisioning ARE implemented and
locally tested — `av login`/`av idp add`/`av scim token create` exist — with the one
remaining gap being a live run against a genuinely external IdP (see "What's next on the
enterprise roadmap" below). Everything below still also works with the OSS `av auth`
shared-secret/per-user-token path (unchanged) or the DB-backed tokens this guide
provisions with `av token create`, entirely independent of an identity provider.

## 1. Provision a tenant

A tenant is the isolation boundary — every project, user, and token lives under exactly
one. A fresh registry already has a `default` tenant (every pre-v1.3.2 project lives
there); real multi-tenancy means provisioning more:

```bash
av tenant create acme-labs "Acme Labs"
```

Requires an `admin`-scoped credential — on an unconfigured (Anonymous) registry, any
credential qualifies (an unscoped token resolves to `["*"]`, same additive rule
v1.3.1's own scope rollout used). `av tenant show` reports which tenant your currently
configured credential resolves to.

## 2. Create users and grant roles

```bash
av user create alice --email alice@acme-labs.example --display-name "Alice"
av role list
av role grant user <alice-user-id> reviewer
```

Six built-in roles ship (`owner`, `admin`, `maintainer`, `trainer`, `reviewer`,
`reader`) — `av role list` shows each one's permission set. A grant can be scoped to one
project instead of the whole tenant:

```bash
av role grant user <alice-user-id> reviewer --project my-model-repo
```

`av role bindings` lists active grants; `av role revoke <binding-id>` removes one.
`av user suspend <user-id>` immediately revokes that user's tokens and sessions —
suspension, not deletion, so audit history survives.

## 3. Mint a remotely-administrable token

The DB-backed counterpart to `av auth set-token`/`add-user` — a token minted this way
authenticates from a completely different machine, with no Docker/shell access to the
registry host needed at all:

```bash
av token create ci-pipeline --scope improver:write --expires-in-days 90
```

`av token list` shows every token in your tenant (the hash is never displayed — only the
first few characters, matching how `av auth list-users` masks per-user tokens).
`av token revoke <token-id>` revokes immediately; a cached principal resolution on a
replica that already saw the token can still authenticate for up to
`AV_AUTH_CACHE_TTL_SECS` (default 30s) afterward — a documented, bounded window, not
silent (`development/threat-model.md` T17).

## 4. Turn on tenant enforcement (server-side)

Everything above works with tenancy enforcement OFF — tenants/roles/tokens exist and are
administrable regardless. The isolation boundary itself only becomes a hard requirement
once the registry operator sets, server-side:

```text
AV_TENANCY_ENFORCE=1
```

With it on: a write under a `project_id` your tenant doesn't own is denied
(`tenant_denied`, exit 22); a read is a bare 404 (not a 403, which would let an attacker
enumerate which project IDs exist); an unfiltered list route (`av registry export`
without `--project`, `GET /api/commits` with no filter) is implicitly scoped to your own
tenant. For the row-level-security backstop to do anything beyond the application-level
guard, also point request-serving sessions at the non-superuser `av_app` role
(`AV_APP_DATABASE_URL` — `docker-compose.yml` sets this by default) rather than the
Postgres superuser role migrations use.

## 5. Back up and restore

```bash
av admin backup create ./backup-2026-09-05 --database-url $DATABASE_URL --data-dir $AV_DATA_DIR
av admin backup verify ./backup-2026-09-05
```

`backup create` writes `db.dump` (a `pg_dump -Fc` logical dump) + `objects.tar.gz` (the
CAS tree) + `manifest.json` (hashes, alembic head, tenant list). `backup verify`
recomputes both hashes against the manifest — corruption or tampering is caught before
you ever need the backup for real. Restoring:

```bash
av admin backup restore ./backup-2026-09-05 --database-url $DATABASE_URL --data-dir $AV_DATA_DIR --force
```

`--force` is required whenever the target database already has tables in it — restore is
destructive by nature. See [`docs/dr.md`](dr.md) for the full picture, including the real
destroy-and-restore drill this pass ran (`scripts/e2e_scenario.sh`'s Phase U) and why
this command never auto-detects "the local docker stack" the way `av auth` does.

## 6. High availability (infrastructure, not CLI)

Not a CLI surface — `docker-compose.ha.yml` + `scripts/ha_drill.sh` (a real, locally-run
drill: concurrent pushes through an nginx LB, a killed replica mid-load, proofs of zero
failed requests / zero double webhook delivery / a globally-enforced rate limit) and a
Helm chart (`deploy/helm/aether-vault/`, schema-verified, not yet drilled on a real
cluster). See `development/architecture.md`'s High Availability Contract.

## 7. Verify the audit trail is intact

```bash
av audit verify
```

Every audit row is hash-chained (migration `0016`) — tampering or deleting a row breaks
verification from that point forward. `av audit verify --export audit.jsonl` (after
`av audit export --format jsonl --out audit.jsonl`) verifies OFFLINE, with no server
trust required for the chain computation itself. Optional ed25519 signing
(`AV_AUDIT_SIGNING_KEY_PATH`, server-side) adds non-repudiation for an export handed to
a party with no database access — `GET /api/admin/audit/public-key` publishes the key an
independent verifier checks signatures against.

## 8. Physical per-tenant object storage (optional, real cost)

```text
AV_CAS_ISOLATION=isolated
```

Off by default (`shared` — one global content-addressed dedup domain, unchanged from
every pre-v1.3.3 deployment). Turning it on physically separates every tenant's objects
on disk and in the Bloom filter — the real, stated cost is losing CROSS-tenant
deduplication entirely (identical bytes held by two tenants are now stored twice);
INTRA-tenant dedup, the product's actual headline claim, is completely unaffected either
way. See `development/architecture.md`'s Per-Tenant CAS Isolation Contract before
flipping this on a deployment with existing data.

## 9. Metrics

```bash
curl -H "Authorization: Bearer <admin-token>" http://<registry>:8000/api/metrics
```

Hand-rolled Prometheus text exposition — request counts/latency histogram, webhook queue
depth, DB pool state, per-tenant request counts. `admin`-scoped, like every other
observability route; point a Prometheus scrape config's `bearer_token_file` at an
admin-scoped `av token create` token. See [`docs/slo.md`](slo.md) for what this does and
does not yet back (no cross-replica aggregation — each replica is its own scrape target).

## 10. Configure SSO (OIDC or SAML)

```bash
# OIDC
av idp add my-okta --kind oidc --issuer https://your-org.okta.com --client-id <client-id> --client-secret <client-secret> --jit --claims-groups groups --group-role 'Engineering=maintainer' --group-role 'Reviewers=reviewer'

# SAML 2.0
av idp add my-adfs --kind saml --idp-metadata-url https://adfs.your-org.com/federationmetadata/2007-06/federationmetadata.xml --jit

av idp list
av idp test my-okta      # confirms the IdP's metadata/issuer is reachable (not a full login)
```

`--jit` (just-in-time provisioning) is opt-in per provider: on means an unknown IdP
subject is provisioned automatically on first login; off (the default) means only an
already-linked or pre-provisioned identity can authenticate. `--group-role` maps an
IdP-asserted group to a role, re-evaluated on every login — a user removed from a group
upstream loses that role's permissions on their next login, automatically. Client secrets
are Fernet-encrypted at rest under `AV_SECRET_KEY` — provider creation is refused with a
clear error if that env var is unset, never stored in plaintext.

Once a provider exists, any user logs in with:

```bash
av login --provider my-okta   # opens a browser (device-code flow), polls until approved
av whoami                     # confirm the identity/tenant/roles you're resolved as
```

`av init --mode enterprise` drives the same flow during repo setup. See
`AGENTS.md`/`docs/for-agents.md` for the `login_required` (21) exit code this produces
when the flow times out with no approval.

**Verification note:** SAML/OIDC protocol handling (PKCE, JWKS signature verification,
SAML assertion signature/conditions via `pysaml2`) is implemented and tested against this
server's own routes; it has not yet been driven end-to-end against a live external IdP
(Keycloak/Okta/Entra) in this environment. Treat this as implemented-and-locally-tested,
not yet field-verified — see `VERSIONING.md`'s v1.3.3 section.

## 11. Provision users via SCIM

```bash
av scim status                          # confirm /scim/v2 is mounted
av scim token create okta-connector     # mint the scim-scoped credential
```

Paste the printed token into your IdP's SCIM connector (base URL:
`https://<registry>/scim/v2`). From there the IdP owns the provisioning lifecycle — user
create/update, group membership sync, and deprovisioning (`active: false`, which suspends
and revokes sessions immediately; it never hard-deletes a row, so audit history and
authorship attribution survive) all flow through the standard SCIM 2.0 wire protocol
(RFC 7643/7644). A group synced via SCIM that's also granted a role (`av role grant
group <group-id> <role-id>`) propagates that role to every member automatically.

## What's next on the enterprise roadmap

SSO and SCIM are now implemented and locally tested (see sections 10-11 above); the
remaining gap is genuinely operational, not code: driving the same flows end-to-end
against a real external IdP (a Keycloak compose overlay, or a live Okta/Entra tenant).
Reference customers, a third-party security audit, and runtime-verified Kubernetes HA
(the Helm chart today is `helm template`-verified, not cluster-drilled) round out the
rest of the roadmap — tracked in `todo.md`, not represented as shipped here.
