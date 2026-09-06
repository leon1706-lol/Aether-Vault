# Versioning, Deprecation & Release Policy

Aether-Vault follows [Semantic Versioning 2.0.0](https://semver.org/): `MAJOR.MINOR.PATCH`.
This page defines exactly what that means for each of the project's compatibility surfaces,
when things may break, and how releases are cut.

## Version history context

- `v0.1.x` were the bootstrap line — pre-1.0, breaking changes were possible between
  minor versions (documented in `development/CHANGELOG.md` each time).
- **`v1.1.1` is the first stable-semver release line**: everything below is a hard promise
  from here on.

## What each bump means per surface

| Surface | MAJOR (breaking) | MINOR (additive) | PATCH (safe) |
|---|---|---|---|
| **CLI surface** (`av ...`) | Removing/renaming a command or flag; changing a command's output format in a way scripts parse | New commands (`log`, `clone`, `pull`, `merge`), new flags, richer human-readable output | Bug fixes, performance, docs |
| **`.av/` on-disk format** | Changing existing file layouts/index entry semantics so an older binary misreads them | Adding keys readers must tolerate as absent (`layers`, `chunks`) — old binaries stay functional | Fixes to writers only |
| **Registry HTTP API** (`/api/*`) | Removing endpoints/fields clients rely on; changing response shapes incompatibly | New endpoints (`/api/projects`, `/sync/batch-objects`, v1.2.0: `/api/runs*`, `/api/events`, `/api/webhooks*`, `/api/admin/audit`), new optional request params, new response fields (`parents`, `chunks`) — old clients ignore unknown fields | Server-side fixes with identical wire behavior |
| **Config files** (`.av/config`, `.env`) | Removing/renaming keys | New optional keys (`project_id`, `remote_api_token`, `login_mode`, `AV_AUTH_USERS` — v1.1.8's per-user token map is additive; unset keeps Anonymous/single-token behavior byte-identical) | — |
| **Python package** (`import av_cli...`) | Moving/removing public modules/functions | New modules/functions | Internal fixes |

Rule of thumb for reviewers: *if an upgrade would make a previously working script, repo,
or client stop working, it's MAJOR.*

## Deprecation policy

1. **Announce**: deprecations appear in the release's GitHub Release notes AND as a
   `## Phase N` entry in `development/CHANGELOG.md`; where technically possible the CLI
   also prints a visible warning when the deprecated thing is used.
2. **Grace window**: a deprecated CLI surface or API field stays functional for at least
   **one full MINOR cycle** (and never removed inside a PATCH).
3. **Removal** happens only at the next MAJOR boundary, with a migration note.
4. Pre-1.0 exceptions no longer apply: since `v1.1.1` the above is binding on maintainers.

Known standing deprecation candidates (tracked, not yet scheduled): none currently.

### Removed in v1.3.0: legacy GHCR alias tags

`ghcr.io/leon1706-lol/aether-vault-server` and `.../aether-vault-webui` — since v1.2.2 they
were ALIASES of the consolidated `aether-vault-engine` image (same digest, role
auto-detected per container). Announced in the v1.2.2 release notes + CHANGELOG Phase 56;
the original entry named "the next release" as the removal point, but v1.2.3/v1.2.4/v1.2.5
all kept publishing them (owner decision, reaffirmed each cycle). v1.3.0 — the next MINOR
boundary after that reaffirmed floor, and the earliest this policy ever permitted — is
where the removal actually lands: `release.yml`/`docker-edge.yml` stopped publishing both
alias tags as of this release.

- **What broke:** pulls/references to `aether-vault-server` or `aether-vault-webui` (any
  tag, including a fresh `:latest`/`:edge`/a future tagged release) now 404. Any image tag
  already pulled before v1.3.0 keeps running unaffected — this only stops NEW pulls under
  the old names.
- **The migration, if you haven't already made it:** in your compose file / pull command,
  replace `ghcr.io/leon1706-lol/aether-vault-server` or `.../aether-vault-webui` with
  `ghcr.io/leon1706-lol/aether-vault-engine`, and set `AV_ENGINE_ROLE` explicitly (`all`
  for the one-container topology both `docker-compose.yml` and
  `python/av_cli/docker/docker-compose.release.yml` use; `server` or `webui` only if you
  deliberately still run the two-container split — v1.3.0 also publishes real slim
  single-role images for this, `aether-vault-engine:server-*` / `:webui-*`, built from the
  Dockerfile's new `server`/`webui` targets) instead of relying on auto-detection from
  `DATABASE_URL`/`NEXT_PUBLIC_API_URL`.
- **Automated migration tool:** `av doctor --compose PATH` rewrites a pinned two-container
  compose file into the one-container `AV_ENGINE_ROLE=all` form — dry-run by default,
  `--write` to apply. See `docs/migrate-engine-image.md` for the full walkthrough.
- The entrypoint's legacy auto-detect (`DATABASE_URL`/`NEXT_PUBLIC_API_URL`-based role
  inference, with its `[engine] DEPRECATED: ...` log warning) is UNCHANGED and still
  works for any already-pulled legacy-shaped container — only the alias TAGS themselves
  stopped being published going forward.

## v1.2.0 additive surfaces

Runs/events/webhooks/audit endpoints, the JSON envelope + exit-code registry, .avh v2
(reads upgrade v1 documents in memory; writers emit v2), and av_sdk are all ADDITIVE
MINOR features. The one behavioral nuance: commits pushed with an active run now carry a
`run:<id>` tag — consumers matching exact tag sets must tolerate the extra element.

## v1.2.2 additive surfaces

All additive MINOR changes, per surface:
- **HTTP API**: `commits.signature` + `commits.env_snapshot_id` are NEW OPTIONAL response
  fields (older clients ignore them); `/api/admin/audit` gained optional
  `action/since/until/offset` params + `total`; NEW endpoints
  `DELETE /api/admin/audit`, `GET /api/admin/webhook-deliveries`. Commit pushes may now
  carry optional `signature` / `env_snapshot_id` payload keys (unknown-key tolerant).
- **Commit payloads**: optional `signature` and `env_snapshot_id` keys join the hashed
  payload when applicable — commit HASHES change for newly made commits only (they always
  did on any payload evolution); existing history is untouched.
- **CLI**: NEW commands `av audit list` and top-level `av replay`; `av registry keygen`
  upgraded from writing an HMAC secret to generating an ed25519 keypair under `.av/keys/`
  (the old HMAC attest flow still works with a manually configured key);
  `av verify` prefers signatures, falls back to attestation tags, reports UNSIGNED honestly.
- **DB schema**: migration `0003` adds `webhook_deliveries`, `commits.signature`,
  `commits.env_snapshot_id`, `audit_log.status_code` — applied automatically at startup,
  legacy volumes healed zero-touch.
- **Docker**: ONE engine image/container replaces the two-image split; legacy image names
  remain published as aliases of the same image for this cycle ONLY (see deprecation list).

## v1.2.5 additive surfaces

Most of this release is additive MINOR, per surface — but it also contains ONE deliberate
behavior CHANGE, called out first because it is the one a script checking `$?` will notice.

- **Behavior change (fix, not additive) — the exit-code registry is now honored.**
  `AGENTS.md`/`README.md`/`architecture.md` have always published exit codes 10–16 as a
  stable agent contract; four commands never actually raised the documented code. This
  release makes them match the docs:
  - `av commit` with nothing staged: exit **0 → 11** (`nothing_to_commit`).
  - `av merge` with unresolved conflicts: exit **0 → 14** (`merge_conflict`).
  - `not_a_repo` / `auth_failed` paths: exit **1 → 10 / 1 → 12** respectively.
  This is a fix (the documented contract wins over undocumented behavior — it's what
  agents key off), not a new feature — but any script or CI step that branches on the
  literal exit code of these four paths should re-check its assumptions.
- **HTTP API**: `PUT /api/refs/{ref_name}` gains optional `expected_hash` — omitted, it's
  exactly today's last-write-wins; present, a mismatch returns 409 instead of overwriting
  silently (compare-and-swap, additive/opt-in). NEW `GET /api/ready` (readiness, separate
  from the existing DB-free `/api/health` liveness check), `GET /api/runs/{id}/summary`,
  `POST /api/runs/{id}/avh`, `GET /api/admin/audit/export`,
  `POST /api/webhooks/{id}/enable`, `POST /api/admin/webhook-deliveries/{id}/replay`.
  `/api/admin/audit` and `/api/admin/webhook-deliveries` both gain optional filter params
  and an opaque `cursor`/`next_cursor` pagination scheme alongside the existing `offset`
  (offset keeps working; cursor is the recommended path for repeated/agent polling).
  Envelope's `error.data` is a new optional field (structured failure context — conflict
  file lists, racing run ids, etc.) alongside the existing `error.code`/`error.message`.
- **Commit payloads**: unchanged in shape; `env_snapshot_id`-bearing commits made with a
  `snapshot_version: 2` snapshot hash a narrower, machine-independent identity (see below)
  — this is a hashing-INPUT change for NEW snapshots only, not a payload schema change.
- **Env snapshot identity (`snapshot_version: 2`)**: the snapshot document splits into a
  HASHED `env` (python, os_family, pins, seeds, cuda_toolkit_version, a configurable
  critical-env-var set) and an unhashed `observed` context (gpu_names, driver_version,
  hostname, conda_env, interpreter path). `env_snapshot_id` now hashes `{snapshot_version,
  env}` only, enabling identical ids for equivalent environments across machines/OSes.
  **Ids are only comparable within the same `snapshot_version`** — a v1 id and a v2 id for
  the "same" environment will differ; both remain independently resolvable (content-
  addressed lookup is unchanged, so old snapshots never stop working, they just don't
  compare equal to a new capture of the same setup).
- **CLI**: NEW `av audit export`/`prune`; `av webhooks show`/`enable`/`deliveries`/`replay`;
  `av registry keys list`/`fingerprint`/`rotate`, `av registry export-signature`,
  `av verify --signature FILE`; `av policy set --require-signature` (METRIC/OP now
  optional, for a signature-only policy); `av env replay --validate`/`--target-venv`/
  `--conda-env`/`--out`/`--dockerfile --cuda`; `av handoff --publish`. `av pull`/`merge`/
  `clone` gain full `--output json` support (previously human-text only).
- **DB schema**: migration `0004` adds `webhooks.{last_success_at, last_failure_at,
  consecutive_failures, disabled_reason}`, indexes on `audit_log.{username, action}`,
  `runs.avh_object_id` — applied automatically at startup, legacy volumes healed
  zero-touch, same as every prior migration.
- **Docker**: engine-entrypoint.sh restarts a dying subservice independently instead of
  always tearing the whole container down (opt-out via `AV_ENGINE_RESTART_SUBSERVICE=0`);
  graceful drain on stop (`AV_ENGINE_STOP_GRACE_SECS`); legacy image aliases keep
  publishing (see the deprecation-candidates entry above for the updated removal timeline)
  but now log a deprecation warning when their auto-detect path fires.
- **Dataset CDC**: `CHUNKABLE_EXTS` grows from 8 to 15 extensions (additive — a previously
  whole-file-staged format now gets CDC by default; `no-chunk` still opts any glob out).
  New `chunk` `.avattributes` flag force-enables CDC for a glob outside the default set.
  `semdiff`'s `chunks.status` (`"measured"`/`"no_chunks"`) is a new, always-present field
  alongside the existing (still-nullable) `dedup_efficiency`.

## v1.3.1 additive surfaces

**Version-policy exception, recorded rather than silently contradicted:** by this page's
own "What each bump means per surface" table, a release this overwhelmingly additive
(new CLI commands, new HTTP endpoints, new schema fields, zero removed/renamed surfaces)
would be tagged `v1.4.0`. Shipping it as `v1.3.1` is a deliberate owner decision (2026-09),
not an oversight — noted here explicitly so the policy table and the tag don't quietly
disagree with each other. Every individual surface change below still follows the
additive-only rules this page defines; only the VERSION NUMBER chosen for the release as
a whole departs from the table's literal recommendation.

This release adds the RSI (Recursive Self-Improvement) control plane: versioned improver
artifacts, structured self-edit proposals, a dual promotion gate (model vs. improver),
signed/hash-chained policy-as-code, capability canaries, a held-out eval vault, budgets
and auto-stop, a reviewer gate, causal lineage and strategy memory, a pluggable sandbox
executor, and server-side anomaly detection — see `development/architecture.md`'s
per-surface "RSI R1"–"RSI R6" contract sections for the full design reasoning.

- **New exit codes**: `budget_exhausted` (17), `frozen` (18), `review_required` (19),
  `scope_denied` (20) — additive to the existing 10–16 registry. See
  `docs/for-agents.md`'s full table.
- **HTTP API**: ~44 new routes across improvers, change-sets, policy-packs,
  canary-results, freeze, eval suites/results/adapters, tasks, plans, budgets, scheduler,
  causal-links, strategy, lessons, reviews, critiques, blackboard, cross-run search,
  sandbox-jobs, tool-manifests, and action-logs — all new paths, no existing endpoint's
  shape changed. Scoped-token authorization (`require_scope()`) is additive to
  `AV_AUTH_USERS`: a bare-string or legacy-dict entry, and `AV_API_TOKEN`, all resolve to
  `["*"]` (unrestricted) — zero behavior change for every deployment that predates scopes.
- **New event kinds**: `improver`, `change_set`, `policy`, `canary`, `freeze`, `eval`,
  `review`, `blackboard`, `sandbox`, `anomaly` — additive to the existing `commit` · `ref`
  · `run` · `gc` · `webhook_test` set, same `GET /api/events?kinds=` filter.
- **New schema files**: `improver-1.0`, `change-set-1.0`, `policy-pack-1.0`,
  `eval-suite-1.0`, `tool-manifest-1.0`, `action-log-1.0` — see `docs/contracts.md`.
  Additive optional fields on existing schemas: `run-1.0` gains `kind`, `improver_id`,
  `integrity_signals`, `plan_id`, `budget_id`, `stop_reason` (all absent/null-tolerant on
  older rows); `avh-2.0`'s `lineage` gains `improver_id`.
- **CLI**: ~20 new command groups (`improver`, `canary`, `freeze`, `incident`, `eval`,
  `task`, `plan`, `budget`, `scheduler`, `review`, `critique`, `lineage`, `search`,
  `strategy`, `lessons`, `blackboard`, `sandbox`, `replay-actions`, `tools`) plus new
  subcommands/flags on `av run` (`--kind`, `--improver-id`, `stop`, `branch-policy`,
  `auto-stop-check`, `integrity-check`) and `av policy pack` (`publish`/`show`/`log`/
  `verify`). `av verify` registered as a top-level alias (was documented, never wired —
  a pre-existing gap, not a new feature, see Probleme.md).
- **Python SDK**: `av_sdk.Repo` gains one method per write-capable RSI surface (see
  `development/architecture.md`'s "RSI SDK Surface Contract"), each raising the matching
  typed `SDKError` subclass (`BudgetExhaustedError`, `FrozenError`,
  `ReviewRequiredError`, `ScopeDeniedError`) — additive to the existing exception set.
- **DB schema**: migrations `0006`–`0010` add `runs.{kind, improver_id,
  integrity_signals, plan_id, budget_id, stop_reason, lessons_id}` and 17 new tables
  (`improver_versions`, `change_sets`, `policy_packs`, `canary_results`,
  `project_freeze`, `eval_suites`, `eval_results`, `eval_adapters`, `tasks`, `plans`,
  `budgets`, `causal_links`, `strategy_entries`, `lessons`, `reviews`, `critiques`,
  `blackboard_entries`, `sandbox_jobs`, `tool_manifests`, `action_logs`) — applied
  automatically at startup, legacy volumes healed zero-touch, same as every prior
  migration; each has a real, tested `downgrade()`.
- **Config files**: two new local, additive-only files — `.av/improver_policy.json`
  (the improver-gate sibling of `.av/policies.json`, deliberately its own file so the
  pinned model-gate contract stays untouched) and `.av/tool_manifests/<improver_id>.json`
  (per-improver-version sandbox permissions, fails closed when absent).

## v1.3.2 additive surfaces (see development/CHANGELOG.md Phase 60)

E0/E1/E4 core, E5 (HA), E6 (DR), E7 (security scanning), and E8 (support tooling)
shipped under this version. E2 (SSO) and E3 (SCIM) did NOT ship under v1.3.2 — tracked
separately (see the v1.3.3 section below and `todo.md`). Every surface below follows the
same additive-only rules this page defines — nothing here changes existing behavior for
a deployment that sets none of the new env vars.

- **HTTP API**: ~15 new routes across identity/tenancy — `/api/tokens*`, `/api/tenants*`,
  `/api/users*`, `/api/roles`, `/api/role-bindings*`. All new paths, no existing
  endpoint's shape changed. Six previously-unscoped admin routes (`/api/admin/gc`,
  `/api/admin/audit*`, `/api/admin/webhook-deliveries*`) now require the `admin` scope —
  a documented **fix**, not additive: a token with an EXPLICIT scope list that does not
  include `admin` (never the common unrestricted default) loses access to these six
  routes specifically. Flagged here per this page's own rule of thumb ("if an upgrade
  would make a previously working script stop working, it's MAJOR") — accepted as a
  deliberate, narrow exception because the prior state was a genuine, undocumented
  security gap (any authenticated token, however narrowly scoped, could trigger GC or
  read/prune the audit trail), the same class of judgment call v1.2.5's exit-code fix
  made for a pre-existing contract violation.
- **New exit code**: `tenant_denied` (22), additive to the 10–20 registry. Exit code 21
  (`login_required`) is reserved but deliberately NOT yet registered anywhere (no real
  caller exists until `av login`/SSO sessions ship).
- **CLI**: 4 new command groups — `av token create/list/revoke`, `av tenant create/show`,
  `av user create/list/suspend`, `av role list/grant/bindings/revoke`. `av auth *` is
  completely unchanged and remains the documented `.env`-based path; these are the
  DB-backed, remote-administrable alternative alongside it, not a replacement.
- **DB schema**: migrations `0011`–`0014`. `0011` adds eleven new tables (tenants,
  projects, users, user_identities, groups, group_members, roles, role_bindings,
  api_tokens, sso_providers, sessions), seeding a default tenant and six built-in roles.
  `0012`/`0013` add `tenant_id` to 28 pre-existing tables (nullable+backfilled, then NOT
  NULL+FK+row-level-security) — every pre-existing row backfills to the same seeded
  default tenant, so an unconfigured deployment's data is untouched in substance. `0014`
  widens `objects`/`trees`' primary keys to include `tenant_id` — a schema prerequisite
  only; no runtime behavior change ships with it. Each has a real, tested `downgrade()`.
- **Config/env vars**: `AV_TENANCY_ENFORCE` (default off — row-level security and the
  application-layer tenancy guard are both fully inert until set), `AV_RATE_LIMIT_BACKEND`
  and `AV_AUTH_SPIKE_BACKEND` (default `memory` — opt into a Redis-backed counter for
  correctness under multiple server replicas; both fail open on a Redis error),
  `AV_AUTH_CACHE_TTL_SECS` (default 30 — the DB-backed-token resolution cache window).
- **Python SDK**: `av_sdk.exceptions.TenantDeniedError`, additive to the existing
  exception set.
- **The RLS-superuser gap is fixed** (migration `0015`) — a new non-superuser `av_app`
  Postgres role, granted exactly SELECT/INSERT/UPDATE/DELETE. `AV_APP_DATABASE_URL`
  (optional, additive) routes ordinary request-serving sessions through it instead of
  the superuser role migrations still use; `docker-compose.yml` sets this by default,
  closing the gap in this repo's own reference topology specifically. Live-verified: a
  raw SQL probe connected as `av_app` with only `app.tenant_id` set sees exactly one
  tenant's rows, with no application code involved at all.

## v1.3.3 additive surfaces

Closes every enterprise-readiness gap deliberately deferred out of v1.3.2 as "too large
for one pass": audit-log hash-chaining/signing, `/api/metrics`, per-tenant CAS storage
isolation, SSO (OIDC + SAML 2.0), and SCIM 2.0 provisioning. Every surface below is
additive and off-by-default (an SSO/SCIM route simply has nothing configured to act on
until an admin runs `av idp add`/mints a `scim` token; `pysaml2` absent just means the
SAML routes aren't mounted, not a startup failure).

- **HTTP API**: `GET /api/admin/audit/verify` (± `since_id`), `GET
  /api/admin/audit/public-key`, `GET /api/metrics`, `GET /api/auth/whoami`. SSO:
  `/api/sso-providers*` (CRUD), `/api/auth/oidc/{id}/login|callback`,
  `/api/auth/device/code|verify|token`, `/api/auth/saml/{id}/metadata|acs|sls`. SCIM:
  the full `/scim/v2/*` surface (RFC 7643/7644). All `admin`-scoped (SSO/SCIM CRUD) or
  gated by a dedicated `scim`-scoped token (the SCIM data routes themselves) — never
  reachable by a token that predates this release without an operator deliberately
  minting a new one.
- **DB schema**: migration `0016` adds `audit_log.chain_hash` (NOT NULL, backfilled for
  every pre-existing row) and `audit_log.signature` (nullable). No new migration for
  SSO/SCIM — every table they need (`sso_providers`, `user_identities`, `groups`,
  `group_members`, `sessions`) already exists from migration `0011`. A real, tested
  `downgrade()` for `0016`.
- **Config/env vars**: `AV_AUDIT_SIGNING_KEY_PATH` (default unset — chain-hashing works
  regardless; signing is additionally on once set), `AV_CAS_ISOLATION` (default
  `shared`), `AV_SECRET_KEY` (already existed for commit signing; now also required to
  create an SSO provider with a client secret, or to issue OIDC state cookies — refused
  with a clear error when unset, never stored/sent in plaintext), `AV_SESSION_TTL_SECS`
  (default 8h), `AV_DEVICE_CODE_TTL_SECS` (default 600), `AV_PUBLIC_URL`/`AV_WEBUI_URL`
  (already existed; now also used to build OIDC/SAML redirect URIs).
- **CLI**: `av audit verify` (± `--since-id`, `--export`/`--public-key`), `av
  login`/`logout`/`whoami`, `av idp add|list|show|test|remove`, `av scim
  status`/`token create|revoke`. `av init --mode enterprise` now genuinely logs in (was a
  stub through v1.3.2) — zero call-site changes needed in `cmd_repo.py`, since
  `enterprise.py`'s `EnterpriseAuthProvider` Protocol never changed.
- **Exit codes**: `login_required` (21) is now a real, produced code (`av login`'s
  device-code flow timing out) — it sat reserved-but-unregistered since v1.3.2
  specifically until a real caller existed; see `docs/for-agents.md`.
- **Behavior note, not a contract break**: `_audit_row_dict`'s JSON shape (list/export
  routes) gained two new keys (`chain_hash`, `signature`) — additive per this page's own
  rule (existing keys unchanged, consumers that read specific fields are unaffected; a
  consumer doing an exact-key-set comparison would need updating, but no such consumer
  exists in this codebase's own test suite, checked directly rather than assumed).
- **A real fix, not new surface**: `identity.py::_permissions_for_subject` now actually
  expands a user's effective permissions through their group memberships — `role_bindings`'
  own docstring always promised this, but it was silently inert for a `subject_type ==
  "user"` principal before this release. SSO's group→role mapping and SCIM's group sync
  both depend on this; it was untested before this release closed the gap.
- **Known, disclosed limitation**: SSO is verified end-to-end against a genuinely
  standards-conformant IdP is the intended verification path, but this release ships
  without a live Keycloak compose overlay — the protocol code (PKCE, ID-token/JWKS
  validation, SAML assertion signature/conditions via `pysaml2`, replay protection) is
  real and unit/integration-tested against this server's own routes, but has not yet been
  driven end-to-end against a real external IdP in this environment. Treat OIDC/SAML as
  implemented-and-locally-tested, not yet field-verified against Okta/Entra/Keycloak.

## v1.3.4 additive surfaces

Not a product-surface release (no new HTTP routes/DB schema/CLI commands) — this closes
`todo.md`'s 40-item CI/CD backlog. Additive in the sense that matters for this table: no
user-facing contract changed, only the pipeline that guards them and, incidentally, one
real product-code fix (below) that closes a genuine schema-compatibility gap.

- **New CI surfaces**: `codeql.yml` (SAST); `gitleaks`, `test-linux`, `contract-matrix`,
  `migrations-drill`, `lint-workflows`, `slim-image-smoke`, `preview-env`, `ci-summary`
  (`tests.yml`); `macos-install-smoke`, `dr-drill`, `compat-drill`, `deprecation-dry-run`
  (`nightly.yml`); `verify-install`, `rollback-drill` (`release.yml`); `staging-smoke`
  (`docker-edge.yml`). Full list: `development/infrastructure.md`'s CI Job Map.
- **New scripts**: `release_smoke.sh`, `migrations_drill.py`, `compat_drill.sh`,
  `rollback_drill.sh`, `check_deprecations.py`, `ci_summary.py`.
- **New config files**: `.github/CODEOWNERS`, `.github/ci-budgets.yml`,
  `.github/required-checks.txt`, `.gitleaks.toml`, `development/deprecations.yml`.
- **Image changes**: the engine/server/webui images now carry a real
  `org.opencontainers.image.version` (previously always `0.0.0.dev0`, Probleme.md #69),
  a `HEALTHCHECK`, and (release path only) linux/arm64 alongside linux/amd64. SSO/SAML
  extras (`authlib`, `pyjwt`, `pysaml2` + native `xmlsec1`/`libxml2`) are now actually
  installed in every image — previously dead code in every shipped container despite
  `pyproject.toml`'s own comment claiming otherwise.
- **A real fix, not new surface**: `database.py::_ensure_schema_sync()` no longer
  crashes when an older server binary boots against a database a newer replica has
  already migrated past what the older binary's own alembic chain recognizes — see
  Probleme.md #136. Additive/backward-compatible per this page's own schema-compatibility
  rule above; no behavior change for the common case (a binary booting against a schema
  it DOES recognize).
- **Known, disclosed limitation**: this release's Docker/release-pipeline changes
  (multi-arch build, the new drill scripts, the Dockerfile's SSO/SAML native-build
  changes) were authored and reasoned through without a reachable Docker daemon in the
  session that wrote them — real, but not yet locally end-to-end verified; their first
  genuine proof is the next CI run against them, or a local run once Docker Desktop is
  available. `scripts/compat_drill.sh` and `rollback_drill.sh` additionally cannot fully
  prove themselves until a tag past this release's own fixes exists to drill against —
  see each script's own header for exactly what that means.

## Database schema compatibility

The schema is owned by Alembic (`python/av_server/migrations/`). Server startup upgrades
to head automatically; databases created before the Alembic adoption are detected and
healed + stamped zero-touch on first boot with a v1.1.x-or-newer server image. New schema
changes append reviewed migrations — never edit an applied one. Operators running
persistent registries should still read the changelog before upgrading: new columns are
always additive and nullable/default-safe.

## Transport defaults (behavior note)

Since the v1.1.x hardening cycle, the server's CORS allow-list defaults to the webui
origin (`http://localhost:3000`) instead of `*`, and `POST /api/admin/gc` is rate-limited
to 10/minute by default. Both pre-1.1.x behaviors remain available explicitly via
`AV_CORS_ORIGINS="*"` and `AV_RATE_LIMIT_GC=off` for deployments that want them.

## How a release happens

1. Merge work to `master`; CI (all `tests.yml` jobs — see `development/infrastructure.md`'s CI Job Map for the current list) must be green.
2. Curate the release notes: collect the `CHANGELOG.md` phase entries since the previous
   tag into a short highlights list, ending that entry with the literal marker
   `Essential-Tasks: signed off` once `Aether-vault-Obsidian-Vault/Essential-Tasks.md` has
   actually been run end to end for this release — the `gate` job below checks for exactly
   that marker.
3. Re-capture perf numbers (v1.3.0, todo.md item 22), IN THIS ORDER (the first step fully
   overwrites `BENCHMARKS.md`, so the second must come after it, not before):
   ```bash
   av benchmark --markdown development/BENCHMARKS.md
   python scripts/append_perf_history.py
   ```
   Commit the updated `development/perf-history.json` and `development/BENCHMARKS.md`
   alongside the release commit — the `gate` job below fails the release if
   `perf-history.json` has no entry whose `version` matches the tag being released.
4. `git tag vX.Y.Z && git push origin vX.Y.Z`.
5. The [`release.yml`](.github/workflows/release.yml) pipeline then automatically runs a
   **`gate` job first** (v1.3.0, todo.md item 30) that every publish job depends on and
   that blocks the release if any of the following isn't true: the stack-free suite
   re-run passes; the tagged commit's `tests.yml` run is green (checked via `gh api`
   check-runs); `development/perf-history.json` has a row for this tag's version (step 3
   above); `development/CHANGELOG.md` has an entry for this tag ending with the
   `Essential-Tasks: signed off` marker (step 2 above). The gate is **read-only toward
   PRs** — it blocks a publish, it never merges, approves, or opens anything (see
   `tests/test_ci_policy.py`'s standing no-bots/no-auto-merge guard, which this job must
   never violate).
6. Once the gate passes: builds sdist + wheels (cp310–cp314, three OSes) → publishes to
   PyPI via trusted publishing → creates a **GitHub Release for the tag** with
   auto-generated notes (commit highlights + full changelog link) and every wheel/sdist
   attached → pushes `:latest` + version-tagged images (plus the slim `server-*`/`webui-*`
   variants) to GHCR.
7. Installed users pick the update up via `av update` (opt-in silent auto-update exists).

## Hotfix policy

Security/correctness regressions may be released as a PATCH off the current `master`
(there are no long-lived maintenance branches yet); they skip the grace-window rules but
never reintroduce removed surfaces.
