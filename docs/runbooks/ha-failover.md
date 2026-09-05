# Runbook: HA failover (a replica or a Postgres node goes down)

Applies to the `docker-compose.ha.yml` topology (nginx LB + N engine replicas + Postgres
primary/streaming-replica + Redis primary/replica). See `development/architecture.md`'s
High Availability Contract for the design, and `scripts/ha_drill.sh` for the drill that
actually proves this topology's failure-handling live.

## An engine replica goes down

**Expected behavior, no action needed**: the nginx LB's passive health check
(`max_fails`/`fail_timeout`) stops routing to it after the first failed request;
in-flight requests to OTHER replicas are unaffected. Docker's own `restart:
unless-stopped` + healthcheck bring it back automatically in most cases.

If it does NOT come back on its own:

```bash
docker compose -f docker-compose.ha.yml -p aether-vault-ha logs --tail 100 engine-2
docker compose -f docker-compose.ha.yml -p aether-vault-ha restart engine-2
```

## The Postgres primary goes down

This IS a real outage — the standby is hot but not automatically promoted (no
auto-failover orchestrator is shipped; this is a stated scope limit, not an oversight).
Manual promotion:

```bash
docker compose -f docker-compose.ha.yml -p aether-vault-ha exec db-replica pg_ctl promote
```

After promotion, `db-replica` accepts writes. Point `DATABASE_URL`/`AV_APP_DATABASE_URL`
at it (an env change + `docker compose up -d` recreate of the engine replicas) and
provision a NEW standby from the promoted node before the next drill or incident —
running with zero standbys after a promotion is a real, temporary risk window.

## The Postgres replica goes down

**Expected behavior, no action needed for availability** — the primary is unaffected;
only the standby's own future promotability is degraded until it's restored. Restore it
from a fresh `pg_basebackup` (the same bootstrap `docker/ha/postgres-replica-entrypoint.sh`
runs automatically on an empty data volume):

```bash
docker compose -f docker-compose.ha.yml -p aether-vault-ha rm -sf db-replica
docker volume rm aether-vault-ha_ha_postgres_replica_data
docker compose -f docker-compose.ha.yml -p aether-vault-ha up -d db-replica
```

## Verifying the topology is genuinely healthy after any of the above

```bash
./scripts/ha_drill.sh
```

The same drill CI runs — a real, repeatable proof, not a visual inspection of container
status.
