from datetime import datetime, timezone

from sqlalchemy import BigInteger, Column, DateTime, ForeignKey, JSON, String
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import declarative_base

Base = declarative_base()


def utcnow_naive() -> datetime:
    """Timezone-aware UTC 'now', returned as a naive datetime.

    Replaces the deprecated datetime.utcnow(). We strip tzinfo so the value stays
    compatible with the naive DateTime columns used throughout the schema (mixing naive
    and aware datetimes in comparisons raises TypeError).
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)


class DBObject(Base):
    """Represents a stored CAS blob (model weights, dataset shard, code file)."""
    __tablename__ = "objects"

    hash = Column(String, primary_key=True)
    # BigInteger, not Integer: ML weights/datasets routinely exceed the 2.1 GB INT4 ceiling.
    size = Column(BigInteger, nullable=False)
    created_at = Column(DateTime, default=utcnow_naive)


class DBTree(Base):
    """
    One row per entry inside a Merkle tree node.
    tree_hash + path_name form a composite primary key.
    Either child_tree_hash (sub-directory) or object_hash (leaf blob) is set.
    """
    __tablename__ = "trees"

    tree_hash = Column(String, primary_key=True)
    path_name = Column(String, primary_key=True)
    child_tree_hash = Column(String, nullable=True)
    # No ForeignKey on objects.hash: a `.safetensors` artifact that gets split into per-layer
    # shards (see `layers` below) never has its whole-file blob uploaded as a single object —
    # only the layer shards are (avoids storing the same bytes twice). object_hash still holds
    # the real whole-file content hash (used by clients to name the reconstructed local file
    # and as the canonical content-address), it just isn't guaranteed to exist as its own row
    # in `objects`. Enforcing the FK made every layer-split commit fail to insert.
    object_hash = Column(String, nullable=True)
    size = Column(BigInteger, nullable=True)  # see DBObject.size — multi-GB artifacts
    type = Column(String)  # 'tree' | 'file' | 'artifact'
    layers = Column(JSON, default=list)
    # CDC chunk manifests for opaque checkpoints (.pt/.pth/.ckpt): [{"hash","size","offset"}].
    # Mirrors `layers` — same storage pattern, same create_all migration caveat for existing
    # DBs (`ALTER TABLE trees ADD COLUMN chunks JSON;` or drop/recreate).
    chunks = Column(JSON, default=list)


class DBCommit(Base):
    """
    A commit record persisted to PostgreSQL.
    tags   → JSONB array of free-form string labels.
    metrics → JSONB dict of float/int experiment metrics.
    """
    __tablename__ = "commits"

    hash = Column(String, primary_key=True)
    message = Column(String, nullable=False)
    author = Column(String, default="anonymous")
    timestamp = Column(DateTime, default=utcnow_naive)
    # No ForeignKey on parent_hash: commits can be pushed shallow / out-of-order (e.g. from
    # the offline pending-push queue, or a clone that never received older history), and a
    # missing parent must not raise an IntegrityError → HTTP 500. Integrity of the DAG is
    # still anchored by the content-addressed hashes themselves.
    parent_hash = Column(String, nullable=True, index=True)
    # Merge commits have more than one parent; parent_hash keeps parents[0] and everything
    # beyond it lands here as a JSON array string (e.g. '["abc..."]'). Nullable — normal
    # commits are single-parent. NOTE (no-migrations caveat): existing databases created via
    # create_all need a one-time `ALTER TABLE commits ADD COLUMN extra_parents TEXT;`
    # (create_all only adds missing tables, never missing columns).
    extra_parents = Column(String, nullable=True)
    root_tree_hash = Column(String, nullable=False)
    tags = Column(ARRAY(String), default=list)
    metrics = Column(JSON, default=dict)
    # Per-project separation: every `av init` repo gets a stable project_id (see
    # python/av_cli/main.py's load_config/init). Multiple local repos share this one
    # registry by default, so without this a dashboard has no way to tell which folder a
    # commit came from. project_id is included in the client's hashed commit payload (so two
    # projects can never collide on the same hash); project_name is a denormalized display
    # label (mutable via `av config --name`, intentionally not part of the hash).
    project_id = Column(String, nullable=False, index=True)
    project_name = Column(String, nullable=False)


class DBRef(Base):
    """Branch / tag reference pointing to a commit hash."""
    __tablename__ = "refs"

    name = Column(String, primary_key=True)
    commit_hash = Column(String, ForeignKey("commits.hash"), nullable=False)
    updated_at = Column(DateTime, default=utcnow_naive, onupdate=utcnow_naive)
