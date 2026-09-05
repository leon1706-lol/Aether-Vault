# Runbook: upgrade and rollback

## Upgrading

1. **Take a backup first, always** — an upgrade that goes wrong is exactly what
   [`dr-restore.md`](dr-restore.md) exists for:
   ```bash
   av admin backup create ./pre-upgrade-backup --database-url $DATABASE_URL --data-dir $AV_DATA_DIR
   ```
2. Pull/build the new image:
   ```bash
   docker compose pull aether-vault-engine   # published images
   # or: docker compose build aether-vault-engine   # from a source checkout
   ```
3. Recreate (not `docker restart` — that does NOT re-read `.env`/pick up new image
   layers; see `development/CHANGELOG.md` Phase 60 for exactly this class of incident):
   ```bash
   docker compose up -d aether-vault-engine
   ```
4. The new image's own startup runs the migration chain to head automatically
   (`init_db()`) — verify:
   ```bash
   curl -sf http://localhost:8000/api/ready
   ```
5. Smoke-test a real push/read round trip from a scratch repo before considering the
   upgrade complete.

## Rolling back

**The hard constraint**: an OLDER image's migration chain does not know about revisions
a NEWER image already applied to the database. Running an old image against an
already-upgraded database schema crash-loops it (`alembic.util.exc.CommandError: Can't
locate revision`) — this is not hypothetical, it happened during this feature's own
development (`development/CHANGELOG.md` Phase 60).

1. If the database has NOT yet been touched by the new version (upgrade failed at image
   pull/start, before `init_db()` ran): simply revert the image tag and recreate.
2. If the database HAS already been migrated to the new head: restore from the
   pre-upgrade backup (step 1 above) — see [`dr-restore.md`](dr-restore.md) — rather than
   attempting to downgrade migrations against a live, possibly-already-written-to
   database.
3. Never use `docker restart` to "undo" an upgrade — it doesn't revert the image or
   re-read environment changes; use `docker compose up -d` with the reverted tag.
