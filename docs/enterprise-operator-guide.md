# Enterprise operator guide: identity, tenancy, and DR (v1.3.2)

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
DR contract sections). **SSO (OIDC/SAML) and SCIM provisioning are NOT built** — there is
no `av login`, no IdP integration. Everything below authenticates with either the OSS
`av auth` shared-secret/per-user-token path (unchanged) or the new DB-backed tokens this
guide provisions with `av token create` — never an external identity provider.

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

## What's next on the enterprise roadmap

SSO (OIDC + SAML against a real IdP), SCIM 2.0 provisioning, per-tenant physical CAS
storage isolation (today's dedup is global, `AV_CAS_ISOLATION=shared` the only mode that
exists), signed/hash-chained audit logs, and an automated security-scanning CI gate are
tracked in `todo.md` — not represented as shipped here.
