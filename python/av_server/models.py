from datetime import datetime, timezone
import uuid

from sqlalchemy import BigInteger, Boolean, Column, DateTime, ForeignKey, Index, Integer, JSON, String, Text
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
    # v1.2.2 signed commits: JSON blob {"algo","public_key","sig"} — an ed25519 signature
    # over the canonical (sorted-keys, signature-stripped) commit JSON. Nullable: unsigned
    # commits are and stay valid ("tamper evidence, not a trust network" — SECURITY.md).
    # Stored so signatures survive clone/pull round trips instead of living only in the
    # authoring repo's local commit file.
    signature = Column(Text, nullable=True)
    # v1.2.2 env snapshot/replay: content id of the environment snapshot object this
    # commit was made under. Persisted so cloned repos keep BOTH the replay pointer AND
    # signature validity — the id is part of the hashed/signed payload, and a clone
    # missing it would fail every `av verify` (found by the manual wire pass).
    env_snapshot_id = Column(Text, nullable=True)
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


# ---------------------------------------------------------------------------
# v1.2.0 — autonomous-loop layer (runs, events, webhooks, audit). All additive;
# migration 0002 creates them (see python/av_server/migrations/versions/).
# ---------------------------------------------------------------------------

def _new_uuid() -> str:
    return str(uuid.uuid4())


class DBRun(Base):
    """A first-class Experiment/Run: groups commits produced by one training effort.

    Agents and humans alike start runs (`av run start` / SDK / POST /api/runs); every
    commit pushed while a run is active is filed into run_commits, and metrics_summary
    keeps the LATEST value per metric so lineage queries never need to walk trees.
    parent_run_id enables 'this fine-tune descended from that run' chains.
    """
    __tablename__ = "runs"

    id = Column(String, primary_key=True, default=_new_uuid)
    project_id = Column(String, nullable=False, index=True)
    name = Column(String, nullable=True)
    # created | running | completed | failed — plain strings keep SQLite heal-tests portable.
    status = Column(String, nullable=False, default="created")
    parent_run_id = Column("parent_run_id", String, ForeignKey("runs.id"), nullable=True)
    created_by = Column(String, nullable=True)  # resolved auth identity ('owner'/username)
    config_hash = Column(String, nullable=True)
    code_pointer = Column(JSON, nullable=True)  # {git_remote, git_sha, dirty}
    env_snapshot_id = Column(String, nullable=True)
    # Latest value per metric name: {"val_loss": 0.31, "steps": 12000} — refreshed on
    # every linked commit push. Query-friendly without walking commit trees.
    metrics_summary = Column(JSON, nullable=True)
    created_at = Column(DateTime, default=utcnow_naive)
    updated_at = Column(DateTime, default=utcnow_naive, onupdate=utcnow_naive)
    completed_at = Column(DateTime, nullable=True)

    __table_args__ = (
        Index("ix_runs_project_status", "project_id", "status"),
        Index("ix_runs_parent", "parent_run_id"),
    )


class DBRunCommit(Base):
    """Join table: which commits belong to which run (a run usually spans many)."""
    __tablename__ = "run_commits"

    run_id = Column(String, ForeignKey("runs.id"), primary_key=True)
    commit_hash = Column(String, ForeignKey("commits.hash"), primary_key=True)
    created_at = Column(DateTime, default=utcnow_naive)


class DBEvent(Base):
    """Append-only event stream: the resumable cursor feed agents/orchestrators poll.

    id is an autoincrementing integer — its monotonicity IS the cursor contract
    (?since=<last seen id> returns strictly newer events in id order).
    """
    __tablename__ = "events"

    id = Column(Integer, primary_key=True, autoincrement=True)
    ts = Column(DateTime, default=utcnow_naive, nullable=False)
    project_id = Column(String, nullable=True, index=True)
    kind = Column(String, nullable=False)  # commit | ref | run | promote | gc | webhook_test
    payload = Column(JSON, nullable=True)

    __table_args__ = (Index("ix_events_project_kind_id", "project_id", "kind", "id"),)


class DBWebhook(Base):
    """A subscriber URL that receives signed POSTs for matching events."""
    __tablename__ = "webhooks"

    id = Column(String, primary_key=True, default=_new_uuid)
    url = Column(String, nullable=False)
    # The signing secret is stored verbatim because deliveries must be SIGNED with it
    # (HMAC-SHA256 over the body). It is never returned by any API response (masked
    # listing only). A registry compromise exposes these secrets — the same trust domain
    # as the .env auth tokens themselves, documented in SECURITY.md.
    secret = Column(String, nullable=False)
    project_id = Column(String, nullable=True, index=True)  # null = all projects
    kinds = Column(JSON, nullable=True)  # null = all kinds; else list of kind strings
    active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime, default=utcnow_naive)


class DBAuditLog(Base):
    """Immutable who-did-what trail for mutating API calls (trust/enterprise surface)."""
    __tablename__ = "audit_log"

    id = Column(Integer, primary_key=True, autoincrement=True)
    ts = Column(DateTime, default=utcnow_naive, nullable=False)
    username = Column(String, nullable=True)  # resolved identity; None in Anonymous mode
    action = Column(String, nullable=False)   # e.g. 'commit.push', 'ref.update', 'run.create'
    project_id = Column(String, nullable=True, index=True)
    details = Column(JSON, nullable=True)
    # v1.2.2 audit depth: the HTTP outcome of the mutation (201 created, 409 idempotent
    # duplicate, ...) so the trail answers "did it actually land?", not just "was it tried".
    status_code = Column(Integer, nullable=True)

    __table_args__ = (Index("ix_audit_ts", "ts"),)


class DBWebhookDelivery(Base):
    """Per-attempt webhook delivery ledger (v1.2.2, migration 0003).

    Every fan-out attempt is persisted BEFORE the POST goes out ('pending') and updated
    after ('delivered' / 'failed'). Failed rows carry next_retry_at and are re-driven by
    the server's retry worker until AV_WEBHOOK_MAX_ATTEMPTS is exhausted → 'dead'
    (dead-letter). The event's kind/payload/project_id are snapshotted onto the row so a
    retry reconstructs the byte-identical signed body even if the original event has since
    been retention-swept from `events`.
    """
    __tablename__ = "webhook_deliveries"

    id = Column(Integer, primary_key=True, autoincrement=True)
    webhook_id = Column(String, ForeignKey("webhooks.id"), nullable=False, index=True)
    event_id = Column(Integer, nullable=True, index=True)
    event_kind = Column(String, nullable=True)
    project_id = Column(String, nullable=True)
    payload = Column(JSON, nullable=True)
    attempt = Column(Integer, nullable=False, default=1)
    # pending | delivered | failed | dead
    status = Column(String, nullable=False, default="pending", index=True)
    response_code = Column(Integer, nullable=True)
    last_error = Column(String, nullable=True)
    next_retry_at = Column(DateTime, nullable=True, index=True)
    created_at = Column(DateTime, default=utcnow_naive)
    updated_at = Column(DateTime, default=utcnow_naive, onupdate=utcnow_naive)
