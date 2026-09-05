# Runbook: incident response (first steps)

## 1. Establish what's actually down

```bash
curl -sf http://<registry-host>:8000/api/health   # liveness — process up at all?
curl -sf http://<registry-host>:8000/api/ready     # readiness — DB/Redis/data-dir OK?
```

`/api/health` failing means the process itself is wedged — restart it
(`docker compose up -d aether-vault-engine`, or your orchestrator's pod restart).
`/api/health` OK but `/api/ready` failing means a dependency (Postgres/Redis/AV_DATA_DIR)
is the actual problem — do NOT restart the engine for this; it won't help and adds churn.

## 2. Collect a support bundle immediately, before touching anything

```bash
av support-bundle ./incident-$(date +%Y%m%d-%H%M)
```

Captures the state AS FOUND (health/ready output, container status, log tails, a speed
probe) — every credential-shaped value is redacted before it's written, so this is safe
to attach to a ticket or share with support.

## 3. Check the obvious dependency failures

```bash
docker ps --filter "name=aether-vault"     # are db/redis/engine containers actually running?
docker logs --tail 100 aether-vault-db
docker logs --tail 100 aether-vault-redis
docker logs --tail 100 aether-vault-engine
```

## 4. If it's a suspected data-integrity issue, do NOT restart the DB/schema yet

Applying migrations or restarting containers can mask (or, per this project's own
development history, WORSEN — see `development/CHANGELOG.md` Phase 60's documented
incidents) the exact state you need to diagnose. Take a backup FIRST if the database is
still reachable at all:

```bash
av admin backup create ./incident-backup --database-url $DATABASE_URL --data-dir $AV_DATA_DIR
```

Then proceed to [`dr-restore.md`](dr-restore.md) if restoration turns out to be needed.

## 5. Classify severity and escalate per `docs/sla.md`

Sev-1 (down / data-loss risk): escalate immediately per your support agreement.
Sev-2/3: file with the support bundle attached; continue investigating in parallel.
