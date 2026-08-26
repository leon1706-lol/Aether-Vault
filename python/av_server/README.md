# `av_server` — FastAPI Content-Addressable Storage Registry

The Dockerized backend every `av` client talks to: object upload/download, commit & ref
sync, project discovery, dashboard APIs, and mark-and-sweep garbage collection. Deployed
via the compose files in [`../../python/av_cli/docker/`](../av_cli/docker/) and
[`../../docker-compose.yml`](../../docker-compose.yml). See the
[main README](../../README.md).

## Contents

| File | Purpose |
|---|---|
| `server.py` | All routes (`/api/objects`, `/api/commits`, `/api/refs`, `/api/projects`, `/api/runs*`, `/api/events`, `/api/webhooks*`, `/api/sync/*`, `/api/admin/gc`, `/api/admin/audit` + prune, `/api/admin/webhook-deliveries`, dashboard summaries), Merkle-tree construction, GC with retention sweeps, webhook delivery ledger + retry worker, optional shared-secret token middleware |
| `models.py` | SQLAlchemy schema: `DBObject`, `DBTree` (Merkle nodes incl. per-layer + per-chunk manifests), `DBCommit` (incl. merge-commit `extra_parents`, v1.2.2 `signature`/`env_snapshot_id`), `DBRef`, and the v1.2.0+ tables `DBRun`/`DBRunCommit`/`DBEvent`/`DBWebhook`/`DBAuditLog` (with `status_code`)/`DBWebhookDelivery` |
| `storage.py` | CASStorage filesystem shards (hash-verified streaming writes); legacy fallback when the DB is empty |
| `redis_cache.py` | RedisBloom filter for O(1) object-existence checks |
| `database.py` | Async engine/session setup; schema owned by Alembic (chain 0001→0003) applied at startup; legacy create_all volumes detected, missing tables created, column drift healed, chain stamped zero-touch |

## API notes

- Commits are pushed **after** their objects (the tree rows reference object hashes);
  duplicate-hash pushes are idempotent 409s.
- Merge commits store `parents[0]` in `parent_hash` and the rest in
  `extra_parents` (JSON string); read endpoints reconstruct a full `parents` array.
- Refs are namespaced `<project_id>/<branch>` by convention; `/api/refs?project_id=`
  filters on that prefix.
- **Migration caveat**: `create_all` only creates missing tables, never columns. Existing
  databases need manual `ALTER TABLE`s when the schema grows (documented in
  [`../../development/CHANGELOG.md`](../../development/CHANGELOG.md) per change).
