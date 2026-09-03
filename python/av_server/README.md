# av_server

Owns the FastAPI content-addressable storage registry every `av` client talks to:
object upload/download, commit & ref sync, runs/events/webhooks, project discovery,
dashboard APIs, admin GC + audit, and the delivery ledger with its retry worker.
Deployed inside the engine container (see `docker/README.md`) or bare-metal with a
writable `AV_DATA_DIR`.

- `server.py` - all routes (`/api/objects`, `/api/commits` incl.
  `/api/commits/{a}/diff/{b}` (v1.3.0, the same schema `av diff` produces client-side),
  `/api/refs`, `/api/projects`, `/api/runs*` incl. `/api/runs/{id}/metrics` and
  `/api/runs/{id}/lineage` (v1.3.0, cursor-paginated full history/chain — `/summary`
  keeps its capped inline copy) and `/api/runs/{id}/policy-outcome`, `/api/events`
  (`run_id` filter + gap detection, v1.3.0), `/api/webhooks*`, `/api/sync/*`,
  `/api/admin/gc`, `/api/admin/audit` + prune (with `dry_run`, v1.3.0),
  `/api/admin/webhook-deliveries`, `/api/health` + `/api/ready` (liveness vs.
  readiness — see `development/infrastructure.md`)), Merkle-tree construction,
  retention sweeps, webhook fan-out ledger, optional shared-secret token middleware
  with per-user expiry (v1.3.0), CORS + rate limiting pipeline.
- `models.py` - SQLAlchemy schema: `DBObject`, `DBTree` (per-layer + per-chunk
  manifests), `DBCommit` (`extra_parents`, v1.2.2 `signature`/`env_snapshot_id`),
  `DBRef`, and the v1.2.0+ tables `DBRun` (incl. v1.3.0 `policy_outcome`)/
  `DBRunCommit`/`DBEvent`/`DBWebhook`/`DBAuditLog` (with `status_code`)/
  `DBWebhookDelivery`.
- `storage.py` - `CASStorage` filesystem shards, hash-verified streaming writes;
  legacy fallback when the DB is empty.
- `redis_cache.py` - RedisBloom O(1) existence filter; degrades to DB-only checks.
- `database.py` - async engine/session setup; schema owned by Alembic
  (chain `0001`->`0005`), applied at startup; legacy volumes adopted zero-touch
  (missing tables created, column drift healed, chain stamped).
- `migrations/versions/` - the append-only revision chain; never edit an applied
  migration, always append.
- `rate_limit.py` - fixed-window limiter; GC bucket on by default, data plane opt-in.

## API notes

- Commits push AFTER their objects (tree rows reference object hashes); duplicate
  pushes are idempotent 409s - but an IntegrityError is re-checked before being mapped
  to 409, so FK violations still surface honestly.
- Merge commits store `parents[0]` in `parent_hash` + the rest in `extra_parents`;
  read endpoints reconstruct the full `parents` array.
- Refs are namespaced `<project_id>/<branch>`; `/api/refs?project_id=` filters the prefix.
