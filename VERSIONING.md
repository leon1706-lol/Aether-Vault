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

Known standing deprecation candidates (tracked, not yet scheduled):
- **Legacy GHCR alias tags** `ghcr.io/leon1706-lol/aether-vault-server` and
  `.../aether-vault-webui` — since v1.2.2 they are ALIASES of the consolidated
  `aether-vault-engine` image (same digest, role auto-detected per container).
  Announced in the v1.2.2 release notes + CHANGELOG Phase 56; removal lands in the NEXT
  release (one full MINOR grace cycle honored). Pinned installs should switch compose
  files / pulls to `aether-vault-engine`.

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

1. Merge work to `master`; CI (5 test jobs) must be green.
2. Curate the release notes: collect the `CHANGELOG.md` phase entries since the previous
   tag into a short highlights list.
3. `git tag vX.Y.Z && git push origin vX.Y.Z`.
4. The [`release.yml`](.github/workflows/release.yml) pipeline then automatically:
   builds sdist + wheels (cp310–cp312, three OSes) → publishes to PyPI via trusted
   publishing → creates a **GitHub Release for the tag** with auto-generated notes
   (commit highlights + full changelog link) and every wheel/sdist attached → pushes
   `:latest` + version-tagged images to GHCR.
5. Installed users pick the update up via `av update` (opt-in silent auto-update exists).

## Hotfix policy

Security/correctness regressions may be released as a PATCH off the current `master`
(there are no long-lived maintenance branches yet); they skip the grace-window rules but
never reintroduce removed surfaces.
