# Disaster recovery

Aether-Vault's durable state is two things: the Postgres database (commits, refs, runs,
identity/RBAC, everything queryable) and the CAS objects tree (the actual bytes — model
weights, datasets, code blobs). A backup is only real if it covers both together and has
actually been restored once, not just taken.

## What `av admin backup` does

```
av admin backup create OUTPUT_DIR --database-url URL --data-dir PATH
av admin backup verify BACKUP_DIR
av admin backup restore BACKUP_DIR --database-url URL --data-dir PATH --force
```

`create` writes three files to `OUTPUT_DIR`:

- `db.dump` — a `pg_dump -Fc` logical dump of the whole database.
- `objects.tar.gz` — the CAS objects tree, gzip-compressed.
- `manifest.json` — sha256 + byte size of both parts, the alembic head, the tenant list,
  and approximate per-table row counts (`backup-manifest-1.0`, see `docs/contracts.md`).

`verify` recomputes both hashes against the manifest and checks the recorded alembic head
is one this build's own migration chain actually knows about — a backup taken with a
newer/older build than the one restoring it is flagged, not silently accepted.

`restore` refuses to run against a database that already has tables in it, unless you
pass `--force` — a restore is destructive (it loads the dump on top of, and extracts the
objects tree into, whatever is already there), and this command has no way to
distinguish "an old scratch database" from "the wrong production database" on its own.
After restoring both parts, it runs the same schema-healing path the server's own
startup (`init_db()`) uses, so a backup taken on an older migration chain still lands at
the CURRENT build's head.

**Either point it at a reachable Postgres directly** (`--database-url`, needs `pg_dump`/
`pg_restore`/`psql` on PATH) **or run those same binaries inside a named Postgres
container** (`--db-container NAME` — the container always has them, since they ship with
the server package). Same choice for the objects tree: a local `--data-dir`, or
`--engine-container NAME` to reach a container's `/data` via `docker exec`/`docker cp`.
Deliberately does NOT auto-detect "the local docker stack" the way `av auth` does for
`.env`/token management — see this doc's "Why no auto-detection" section below.

## The drill (what makes this real, not aspirational)

`scripts/ha_drill.sh` proves HA; `scripts/e2e_scenario.sh`'s Phase U (gated behind
`AV_E2E_DR=1`, same opt-in pattern as the chaos phases) proves DR: it pushes a real
commit, takes a backup, **genuinely destroys** the e2e run's own database schema
(`DROP SCHEMA public CASCADE`) and CAS objects directory, restores from the backup, and
asserts the pre-destruction commit's bytes and its ref both read back identical. Run it
locally:

```bash
AV_E2E_DR=1 AV_TEST_DATABASE_URL=postgresql+asyncpg://av_user:av_password@localhost:5432/aether_vault_test \
  AV_TEST_REDIS_URL=redis://localhost:6379/1 \
  E2E_DB_CONTAINER=aether-vault-db \
  bash scripts/e2e_scenario.sh
```

It destroys and restores ONLY the e2e run's own test database and its own `$WORK/data`
CAS directory — never the real dev database, even when both live on the same shared
local Postgres container (the drill's `--database-url` always points at the e2e's own
database by name; `pg_dump`/`pg_restore` operate on exactly the one database named in the
connection string, nothing else on the cluster).

## Measured RTO / stated RPO

**RTO (recovery time objective) — measured, not estimated.** Phase U prints the actual
wall-clock seconds `av admin backup restore` took on the machine running it, in its own
`[PASS]` line. This varies with database/objects size and disk speed — it is a
methodology (a repeatable, automated measurement you can re-run on your own
infrastructure and dataset size), not a single number this doc can promise for every
deployment. Re-run the drill against production-representative data size for a number
that means something for your deployment.

**RPO (recovery point objective) — stated, not measured**: "since the last backup you
took." There is no continuous replication/WAL-shipping-based point-in-time recovery
built here (that is a real gap, not implied otherwise) — `pg_dump` is a point-in-time
logical snapshot at the moment `backup create` ran. An operator's actual RPO is
determined entirely by how often `av admin backup create` runs (a cron job, a scheduled
CI job, etc.) — this repo does not ship that scheduler; `docs/runbooks/` has the manual
procedure.

## Why no auto-detection of "the local docker stack"

`av auth`'s mutating subcommands intentionally auto-detect and target whatever local
compose file `docker_runtime.resolve_compose_file()` finds, so joining/managing a
locally-running registry needs no extra flags. `av admin backup` does the opposite on
purpose: every subcommand requires an EXPLICIT `--database-url`/`--db-container`, every
time, with no fallback chain. A real incident during this feature's own development
(see `development/CHANGELOG.md` Phase 60) is exactly why: an auto-detecting mutating
command run from the wrong working directory can silently target the wrong stack, and
`restore` in particular can overwrite a real database. Backup/restore is infrastructure
administration, not repo tooling — it should never guess which infrastructure you mean.

## What this does not cover

- **Point-in-time recovery** (continuous WAL archiving) — not built; see RPO above.
- **Per-tenant selective restore** — `backup create`/`restore` operate on the whole
  database; a `--tenant` scoped mode is not built (per-tenant CAS storage separation
  itself isn't built either — see `development/CHANGELOG.md`'s notes on migration
  `0014`).
- **Encryption at rest for the backup artifacts themselves** — `db.dump`/`objects.tar.gz`
  are not encrypted by this tool; encrypt the `OUTPUT_DIR` yourself (e.g., before
  shipping it off-host) if your compliance posture requires it.
- **Automatic scheduling** — `av admin backup create` is a one-shot command; wire it into
  cron/CI/your orchestrator's own scheduled-job primitive.
