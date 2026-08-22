# `av_server` — FastAPI Content-Addressable Storage Registry

The Dockerized backend every `av` client talks to: object upload/download, commit & ref
sync, project discovery, dashboard APIs, and mark-and-sweep garbage collection. Deployed
via the compose files in [`../../python/av_cli/docker/`](../av_cli/docker/) and
[`../../docker-compose.yml`](../../docker-compose.yml). See the
[main README](../../README.md).

## Contents

| File | Purpose |
|---|---|
| `server.py` | All routes (`/api/objects`, `/api/commits`, `/api/refs`, `/api/projects`, `/api/sync/*`, `/api/admin/gc`, dashboard summaries), Merkle-tree construction, GC, optional shared-secret token middleware |
| `models.py` | SQLAlchemy schema: `DBObject`, `DBTree` (Merkle nodes incl. per-layer + per-chunk manifests), `DBCommit` (incl. merge-commit `extra_parents`), `DBRef` |
| `storage.py` | CASStorage filesystem shards (hash-verified streaming writes); legacy fallback when the DB is empty |
| `redis_cache.py` | RedisBloom filter for O(1) object-existence checks |
| `database.py` | Async engine/session setup; schema created via `create_all` at startup (see migration caveat below) |

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
