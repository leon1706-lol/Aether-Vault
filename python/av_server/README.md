# av_server

Owns the FastAPI content-addressable storage registry every `av` client talks to:
object upload/download, commit & ref sync, runs/events/webhooks, project discovery,
dashboard APIs, admin GC + audit, and the delivery ledger with its retry worker.
Deployed inside the engine container (see `docker/README.md`) or bare-metal with a
writable `AV_DATA_DIR`.

- `server.py` - all routes (`/api/objects`, `/api/commits`, `/api/refs`,
  `/api/projects`, `/api/runs*`, `/api/events`, `/api/webhooks*`, `/api/sync/*`,
  `/api/admin/gc`, `/api/admin/audit` + prune, `/api/admin/webhook-deliveries`),
  Merkle-tree construction, retention sweeps, webhook fan-out ledger, optional
  shared-secret token middleware, CORS + rate limiting pipeline.
- `models.py` - SQLAlchemy schema: `DBObject`, `DBTree` (per-layer + per-chunk
  manifests), `DBCommit` (`extra_parents`, v1.2.2 `signature`/`env_snapshot_id`),
  `DBRef`, and the v1.2.0+ tables `DBRun`/`DBRunCommit`/`DBEvent`/`DBWebhook`/
  `DBAuditLog` (with `status_code`)/`DBWebhookDelivery`.
- `storage.py` - `CASStorage` filesystem shards, hash-verified streaming writes;
  legacy fallback when the DB is empty.
- `redis_cache.py` - RedisBloom O(1) existence filter; degrades to DB-only checks.
- `database.py` - async engine/session setup; schema owned by Alembic
  (chain `0001`->`0003`), applied at startup; legacy volumes adopted zero-touch
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
