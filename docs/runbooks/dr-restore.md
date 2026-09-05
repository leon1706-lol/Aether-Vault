# Runbook: DR restore (recovering from real data loss)

See [`docs/dr.md`](../dr.md) for the full design and the real destroy-and-restore drill
this procedure is drawn from (`scripts/e2e_scenario.sh` Phase U).

## 1. Confirm you actually need to restore

A `/api/ready` failure or a crash-looping engine is usually NOT a data-loss event — see
[`incident-response.md`](incident-response.md) first. Restore only once you've confirmed
the database or CAS objects are genuinely gone or corrupted beyond repair.

## 2. Locate your most recent backup and verify it BEFORE restoring

```bash
av admin backup verify /path/to/your/backup
```

This recomputes hashes against the manifest and checks the recorded alembic head is one
your current build actually knows — catching a corrupted or incompatible backup before
you've committed to using it.

## 3. Stop traffic to the registry

Take the engine (or the whole HA topology) offline first — restoring into a database
still receiving writes produces an inconsistent result:

```bash
docker compose stop aether-vault-engine
```

## 4. Restore

```bash
av admin backup restore /path/to/your/backup --database-url $DATABASE_URL --data-dir $AV_DATA_DIR --force
```

`--force` is required because the target database still has your (broken/lost) schema in
it. Double-check `--database-url` names the RIGHT database before running this — see
`development/threat-model.md` T18 for exactly the incident class this step guards
against, and why this command never auto-detects "the local stack" for you.

## 5. Bring the registry back and verify

```bash
docker compose up -d aether-vault-engine
curl -sf http://localhost:8000/api/ready
```

Spot-check a known commit hash from before the incident actually reads back correctly
before declaring the restore complete.

## 6. Record it

Log the incident, the backup used, and the measured restore time in your own operations
log (this repo's own equivalent is `development/Probleme.md` — a real per-incident
record, not a template to skip).
