from datetime import datetime, timezone
import uuid

from sqlalchemy import BigInteger, Boolean, Column, DateTime, Float, ForeignKey, Index, Integer, JSON, String, Text
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
    """Represents a stored CAS blob (model weights, dataset shard, code file).

    v1.3.2 (migration 0014): `tenant_id` joins the primary key — `(tenant_id, hash)`, not
    bare `hash` — the schema prerequisite for a future genuinely-separate per-tenant
    object store. Every existing row's `tenant_id` is the same `DEFAULT_TENANT_ID` every
    other table backfills to, so the composite key behaves identically to the old
    bare-`hash` key today — zero behavioral change. Physical per-tenant storage
    separation itself (a real second dedup domain, plus the matching Bloom-filter split)
    is NOT implemented yet — see migration 0014's own docstring for why that is
    deliberately scoped out of this schema change rather than half-shipped."""
    __tablename__ = "objects"

    tenant_id = Column(String, ForeignKey("tenants.id"), primary_key=True)
    hash = Column(String, primary_key=True)
    # BigInteger, not Integer: ML weights/datasets routinely exceed the 2.1 GB INT4 ceiling.
    size = Column(BigInteger, nullable=False)
    created_at = Column(DateTime, default=utcnow_naive)


class DBTree(Base):
    """
    One row per entry inside a Merkle tree node.
    (tenant_id, tree_hash, path_name) form a composite primary key — see DBObject's own
    docstring for why `tenant_id` joined this PK in migration 0014.
    Either child_tree_hash (sub-directory) or object_hash (leaf blob) is set.
    """
    __tablename__ = "trees"

    tenant_id = Column(String, ForeignKey("tenants.id"), primary_key=True)
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
    # v1.3.2 (migration 0012/0013): tenant boundary. Auto-populated on insert
    # by database.py's before_flush listener (session.info["tenant_id"], falling
    # back to DEFAULT_TENANT_ID) -- no call site that constructs this model needs
    # to set it explicitly. RLS-enforced (migration 0013) once AV_TENANCY_ENFORCE=1.
    tenant_id = Column(String, ForeignKey("tenants.id"), nullable=False, index=True)

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
    # v1.3.2 (migration 0012/0013): tenant boundary. Auto-populated on insert
    # by database.py's before_flush listener (session.info["tenant_id"], falling
    # back to DEFAULT_TENANT_ID) -- no call site that constructs this model needs
    # to set it explicitly. RLS-enforced (migration 0013) once AV_TENANCY_ENFORCE=1.
    tenant_id = Column(String, ForeignKey("tenants.id"), nullable=False, index=True)

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
    # v1.3.2 (migration 0012/0013): tenant boundary. Auto-populated on insert
    # by database.py's before_flush listener (session.info["tenant_id"], falling
    # back to DEFAULT_TENANT_ID) -- no call site that constructs this model needs
    # to set it explicitly. RLS-enforced (migration 0013) once AV_TENANCY_ENFORCE=1.
    tenant_id = Column(String, ForeignKey("tenants.id"), nullable=False, index=True)

    id = Column(String, primary_key=True, default=_new_uuid)
    project_id = Column(String, nullable=False, index=True)
    name = Column(String, nullable=True)
    # created | running | completed | failed — plain strings keep SQLite heal-tests portable.
    status = Column(String, nullable=False, default="created")
    # v1.3.1 (RSI R1, todo.md A.1): train | meta | scoring | eval — a "meta" run improves
    # the improver (agent code/prompts/tools/policy), not the target model directly.
    # server_default="train" so migration 0006 backfills every pre-v1.3.1 row without a
    # NULL, matching what every existing run always semantically was.
    kind = Column(String, nullable=False, default="train")
    # v1.3.1: which improver version (DBImproverVersion.id) authored this run, when known.
    # No FK — same shallow/out-of-order-write rationale as parent_run_id below.
    improver_id = Column(String, nullable=True, index=True)
    # v1.3.1 (RSI R2, migration 0007): metric-gaming detection signals — train/eval gap,
    # eval-only improvement, data-overlap (exact set intersection of CAS object hashes
    # between the training tree and an eval suite's objects). Computed and attached by
    # the CLI/SDK at run-finish time; null until then.
    integrity_signals = Column(JSON, nullable=True)
    # v1.3.1 (RSI R3, migration 0008): experiment planner + budget account pointers, and
    # why an auto-stopped run stopped (plateau|divergence|nan|canary_failure|budget|None).
    plan_id = Column(String, nullable=True, index=True)
    budget_id = Column(String, nullable=True, index=True)
    stop_reason = Column(String, nullable=True)
    # v1.3.1 (RSI R4, migration 0009): which lessons-object version this run's agent
    # last read before starting — `av run start` warns (never blocks) when unset.
    lessons_id = Column(String, nullable=True, index=True)
    parent_run_id = Column("parent_run_id", String, ForeignKey("runs.id"), nullable=True)
    created_by = Column(String, nullable=True)  # resolved auth identity ('owner'/username)
    config_hash = Column(String, nullable=True)
    code_pointer = Column(JSON, nullable=True)  # {git_remote, git_sha, dirty}
    # v1.2.5: opt-in pointer to a published `.avh` context-memory object (`av handoff
    # --publish`). Null unless the repo owner explicitly published — notes can hold
    # private reasoning, so this is never set implicitly by a normal commit/push.
    avh_object_id = Column(Text, nullable=True)
    env_snapshot_id = Column(String, nullable=True)
    # v1.3.0 (todo.md item 7): the most recent av promote/merge policy decision made for
    # THIS run's active commit, reported by the CLI via POST /api/runs/{id}/policy-outcome
    # right after enforce_policy()/promote() decides — {"decision": "allow"|"deny",
    # "rule": str|None, "at": ISO-8601}. Null until the first decision for this run.
    policy_outcome = Column(JSON, nullable=True)
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
    # v1.3.2 (migration 0012/0013): tenant boundary. Auto-populated on insert
    # by database.py's before_flush listener (session.info["tenant_id"], falling
    # back to DEFAULT_TENANT_ID) -- no call site that constructs this model needs
    # to set it explicitly. RLS-enforced (migration 0013) once AV_TENANCY_ENFORCE=1.
    tenant_id = Column(String, ForeignKey("tenants.id"), nullable=False, index=True)

    run_id = Column(String, ForeignKey("runs.id"), primary_key=True)
    commit_hash = Column(String, ForeignKey("commits.hash"), primary_key=True)
    created_at = Column(DateTime, default=utcnow_naive)


class DBEvent(Base):
    """Append-only event stream: the resumable cursor feed agents/orchestrators poll.

    id is an autoincrementing integer — its monotonicity IS the cursor contract
    (?since=<last seen id> returns strictly newer events in id order).
    """
    __tablename__ = "events"
    # v1.3.2 (migration 0012/0013): tenant boundary. Auto-populated on insert
    # by database.py's before_flush listener (session.info["tenant_id"], falling
    # back to DEFAULT_TENANT_ID) -- no call site that constructs this model needs
    # to set it explicitly. RLS-enforced (migration 0013) once AV_TENANCY_ENFORCE=1.
    tenant_id = Column(String, ForeignKey("tenants.id"), nullable=False, index=True)

    id = Column(Integer, primary_key=True, autoincrement=True)
    ts = Column(DateTime, default=utcnow_naive, nullable=False)
    project_id = Column(String, nullable=True, index=True)
    kind = Column(String, nullable=False)  # commit | ref | run | promote | gc | webhook_test
    payload = Column(JSON, nullable=True)

    __table_args__ = (Index("ix_events_project_kind_id", "project_id", "kind", "id"),)


class DBWebhook(Base):
    """A subscriber URL that receives signed POSTs for matching events."""
    __tablename__ = "webhooks"
    # v1.3.2 (migration 0012/0013): tenant boundary. Auto-populated on insert
    # by database.py's before_flush listener (session.info["tenant_id"], falling
    # back to DEFAULT_TENANT_ID) -- no call site that constructs this model needs
    # to set it explicitly. RLS-enforced (migration 0013) once AV_TENANCY_ENFORCE=1.
    tenant_id = Column(String, ForeignKey("tenants.id"), nullable=False, index=True)

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
    # v1.2.5 delivery health — updated by _deliver_one() on every attempt outcome, so
    # "is this webhook currently healthy?" doesn't require joining webhook_deliveries.
    last_success_at = Column(DateTime, nullable=True)
    last_failure_at = Column(DateTime, nullable=True)
    consecutive_failures = Column(Integer, nullable=False, default=0)
    # Set (with `active` flipped false) when consecutive_failures crosses
    # AV_WEBHOOK_DISABLE_AFTER; cleared by `av webhooks enable`.
    disabled_reason = Column(Text, nullable=True)


class DBAuditLog(Base):
    """Immutable who-did-what trail for mutating API calls (trust/enterprise surface)."""
    __tablename__ = "audit_log"
    # v1.3.2 (migration 0012/0013): tenant boundary. Auto-populated on insert
    # by database.py's before_flush listener (session.info["tenant_id"], falling
    # back to DEFAULT_TENANT_ID) -- no call site that constructs this model needs
    # to set it explicitly. RLS-enforced (migration 0013) once AV_TENANCY_ENFORCE=1.
    tenant_id = Column(String, ForeignKey("tenants.id"), nullable=False, index=True)

    id = Column(Integer, primary_key=True, autoincrement=True)
    ts = Column(DateTime, default=utcnow_naive, nullable=False)
    username = Column(String, nullable=True)  # resolved identity; None in Anonymous mode
    action = Column(String, nullable=False)   # e.g. 'commit.push', 'ref.update', 'run.create'
    project_id = Column(String, nullable=True, index=True)
    details = Column(JSON, nullable=True)
    # v1.2.2 audit depth: the HTTP outcome of the mutation (201 created, 409 idempotent
    # duplicate, ...) so the trail answers "did it actually land?", not just "was it tried".
    status_code = Column(Integer, nullable=True)

    # v1.2.5 (migration 0004): username/action indexes support the richer audit filters
    # added that same phase. Declared here too (not just in the migration) so this model
    # stays the single source of truth `_heal_legacy_indexes` diffs an adopted volume
    # against — see Probleme.md: an adopted legacy volume's stamp-to-head skipped these
    # entirely (only _LEGACY_COLUMNS was healed, never index-only migration additions).
    __table_args__ = (
        Index("ix_audit_ts", "ts"),
        Index("ix_audit_log_username", "username"),
        Index("ix_audit_log_action", "action"),
    )


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
    # v1.3.2 (migration 0012/0013): tenant boundary. Auto-populated on insert
    # by database.py's before_flush listener (session.info["tenant_id"], falling
    # back to DEFAULT_TENANT_ID) -- no call site that constructs this model needs
    # to set it explicitly. RLS-enforced (migration 0013) once AV_TENANCY_ENFORCE=1.
    tenant_id = Column(String, ForeignKey("tenants.id"), nullable=False, index=True)

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


# ---------------------------------------------------------------------------
# RSI R1 (v1.3.1, migration 0006): improver versioning, self-edit proposals, signed
# policy packs, capability canaries, and project freeze state.
#
# Each of ImproverVersion/ChangeSet/PolicyPack is a lightweight server-side index row
# over a CAS object (`python/av_cli/casobj.py`) — the actual manifest/diff/policy
# document lives content-addressed in `.av/objects/`, exactly like `runs.env_snapshot_id`
# already indexes an env snapshot object. No new persistence mechanism.
# ---------------------------------------------------------------------------

class DBImproverVersion(Base):
    """One version of the improver (agent code/prompts/tool schemas/policy-pack ref),
    content-addressed via `manifest_object_id`. `parent_id` (no FK — see module-level
    rationale) forms the improver lineage graph `GET /api/improvers/{id}/lineage` walks."""
    __tablename__ = "improver_versions"
    # v1.3.2 (migration 0012/0013): tenant boundary. Auto-populated on insert
    # by database.py's before_flush listener (session.info["tenant_id"], falling
    # back to DEFAULT_TENANT_ID) -- no call site that constructs this model needs
    # to set it explicitly. RLS-enforced (migration 0013) once AV_TENANCY_ENFORCE=1.
    tenant_id = Column(String, ForeignKey("tenants.id"), nullable=False, index=True)

    id = Column(String, primary_key=True, default=_new_uuid)
    project_id = Column(String, nullable=False, index=True)
    manifest_object_id = Column(String, nullable=False)
    parent_id = Column(String, nullable=True, index=True)
    created_by = Column(String, nullable=True)
    created_at = Column(DateTime, default=utcnow_naive)


class DBChangeSet(Base):
    """A structured self-edit proposal (diff + rationale + predicted risk) against an
    improver version. `object_id` is the CAS id of the actual proposal document."""
    __tablename__ = "change_sets"
    # v1.3.2 (migration 0012/0013): tenant boundary. Auto-populated on insert
    # by database.py's before_flush listener (session.info["tenant_id"], falling
    # back to DEFAULT_TENANT_ID) -- no call site that constructs this model needs
    # to set it explicitly. RLS-enforced (migration 0013) once AV_TENANCY_ENFORCE=1.
    tenant_id = Column(String, ForeignKey("tenants.id"), nullable=False, index=True)

    id = Column(String, primary_key=True, default=_new_uuid)
    project_id = Column(String, nullable=False, index=True)
    improver_id = Column(String, nullable=True, index=True)
    object_id = Column(String, nullable=False)
    # proposed | approved | rejected | applied | rolled_back
    status = Column(String, nullable=False, default="proposed")
    risk = Column(String, nullable=True)  # low | medium | high
    created_by = Column(String, nullable=True)
    created_at = Column(DateTime, default=utcnow_naive)
    updated_at = Column(DateTime, default=utcnow_naive, onupdate=utcnow_naive)

    __table_args__ = (Index("ix_change_sets_project_status", "project_id", "status"),)


class DBPolicyPack(Base):
    """A published, signed policy pack — append-only and hash-chained (`prev_id` +
    `chain_hash`) so the sequence of promotion-rule changes is itself tamper-evident,
    the same "tamper evidence, not a trust network" guarantee signed commits carry
    (`python/av_cli/signing.py`). `object_id` is the CAS id of the actual pack document
    (see `.av/policies.json`'s `policy_version: 2` shape, `cmd_policy.py`)."""
    __tablename__ = "policy_packs"
    # v1.3.2 (migration 0012/0013): tenant boundary. Auto-populated on insert
    # by database.py's before_flush listener (session.info["tenant_id"], falling
    # back to DEFAULT_TENANT_ID) -- no call site that constructs this model needs
    # to set it explicitly. RLS-enforced (migration 0013) once AV_TENANCY_ENFORCE=1.
    tenant_id = Column(String, ForeignKey("tenants.id"), nullable=False, index=True)

    id = Column(String, primary_key=True, default=_new_uuid)
    project_id = Column(String, nullable=False, index=True)
    object_id = Column(String, nullable=False)
    prev_id = Column(String, nullable=True)
    chain_hash = Column(String, nullable=False)
    published_by = Column(String, nullable=True)
    created_at = Column(DateTime, default=utcnow_naive)

    __table_args__ = (Index("ix_policy_packs_project_created", "project_id", "created_at"),)


class DBCanaryResult(Base):
    """One capability-canary run's outcome for a given improver version — the mandatory
    pass gate `av improver promote` checks before allowing an improver promotion."""
    __tablename__ = "canary_results"
    # v1.3.2 (migration 0012/0013): tenant boundary. Auto-populated on insert
    # by database.py's before_flush listener (session.info["tenant_id"], falling
    # back to DEFAULT_TENANT_ID) -- no call site that constructs this model needs
    # to set it explicitly. RLS-enforced (migration 0013) once AV_TENANCY_ENFORCE=1.
    tenant_id = Column(String, ForeignKey("tenants.id"), nullable=False, index=True)

    id = Column(Integer, primary_key=True, autoincrement=True)
    project_id = Column(String, nullable=False, index=True)
    improver_id = Column(String, nullable=False, index=True)
    suite_object_id = Column(String, nullable=False)
    passed = Column(Boolean, nullable=False)
    details = Column(JSON, nullable=True)
    run_id = Column(String, nullable=True, index=True)
    created_at = Column(DateTime, default=utcnow_naive)


class DBEvalSuite(Base):
    """A task/eval suite definition — content-addressed like `DBImproverVersion`. `frozen`
    (todo.md B.7): once true, no route may mutate this row's `object_id`/metadata again —
    a training run may not modify the eval it's scored against. `blind` (todo.md F.26):
    results against this suite are score-redacted for any reader without the `scorer`
    scope until explicitly revealed (`DBEvalResult.revealed`)."""
    __tablename__ = "eval_suites"
    # v1.3.2 (migration 0012/0013): tenant boundary. Auto-populated on insert
    # by database.py's before_flush listener (session.info["tenant_id"], falling
    # back to DEFAULT_TENANT_ID) -- no call site that constructs this model needs
    # to set it explicitly. RLS-enforced (migration 0013) once AV_TENANCY_ENFORCE=1.
    tenant_id = Column(String, ForeignKey("tenants.id"), nullable=False, index=True)

    id = Column(String, primary_key=True, default=_new_uuid)
    project_id = Column(String, nullable=False, index=True)
    object_id = Column(String, nullable=False)
    name = Column(String, nullable=True)
    frozen = Column(Boolean, nullable=False, default=False)
    blind = Column(Boolean, nullable=False, default=False)
    created_by = Column(String, nullable=True)
    created_at = Column(DateTime, default=utcnow_naive)
    updated_at = Column(DateTime, default=utcnow_naive, onupdate=utcnow_naive)


class DBEvalResult(Base):
    """One scoring outcome against an eval suite — `POST /api/eval/results` requires the
    `scorer` scope (todo.md F.25: the held-out eval vault's actual enforcement is this
    scope check, not a separate mechanism). `revealed` defaults True (ordinary, non-blind
    scoring); a blind suite's results are created with `revealed=False` and only flipped
    by `POST /api/eval/results/{id}/reveal` (also `scorer`-scoped)."""
    __tablename__ = "eval_results"
    # v1.3.2 (migration 0012/0013): tenant boundary. Auto-populated on insert
    # by database.py's before_flush listener (session.info["tenant_id"], falling
    # back to DEFAULT_TENANT_ID) -- no call site that constructs this model needs
    # to set it explicitly. RLS-enforced (migration 0013) once AV_TENANCY_ENFORCE=1.
    tenant_id = Column(String, ForeignKey("tenants.id"), nullable=False, index=True)

    id = Column(Integer, primary_key=True, autoincrement=True)
    project_id = Column(String, nullable=False, index=True)
    suite_id = Column(String, nullable=False, index=True)
    run_id = Column(String, nullable=True, index=True)
    score = Column(JSON, nullable=True)
    details = Column(JSON, nullable=True)
    revealed = Column(Boolean, nullable=False, default=True)
    scored_by = Column(String, nullable=True)
    created_at = Column(DateTime, default=utcnow_naive)


class DBEvalAdapter(Base):
    """An external eval adapter registration (todo.md F.27): `command` is a JSON argv
    list — the subprocess contract is JSON on stdin, JSON on stdout, non-zero exit =
    failed scoring (see `av eval adapter run`), so success can't be silently redefined
    in-tree by whatever's currently checked in."""
    __tablename__ = "eval_adapters"
    # v1.3.2 (migration 0012/0013): tenant boundary. Auto-populated on insert
    # by database.py's before_flush listener (session.info["tenant_id"], falling
    # back to DEFAULT_TENANT_ID) -- no call site that constructs this model needs
    # to set it explicitly. RLS-enforced (migration 0013) once AV_TENANCY_ENFORCE=1.
    tenant_id = Column(String, ForeignKey("tenants.id"), nullable=False, index=True)

    id = Column(String, primary_key=True, default=_new_uuid)
    project_id = Column(String, nullable=False, index=True)
    name = Column(String, nullable=False)
    command = Column(JSON, nullable=False)
    created_by = Column(String, nullable=True)
    created_at = Column(DateTime, default=utcnow_naive)


class DBTask(Base):
    """A curriculum task/difficulty-ramp proposal (todo.md B.8)."""
    __tablename__ = "tasks"
    # v1.3.2 (migration 0012/0013): tenant boundary. Auto-populated on insert
    # by database.py's before_flush listener (session.info["tenant_id"], falling
    # back to DEFAULT_TENANT_ID) -- no call site that constructs this model needs
    # to set it explicitly. RLS-enforced (migration 0013) once AV_TENANCY_ENFORCE=1.
    tenant_id = Column(String, ForeignKey("tenants.id"), nullable=False, index=True)

    id = Column(String, primary_key=True, default=_new_uuid)
    project_id = Column(String, nullable=False, index=True)
    title = Column(String, nullable=False)
    description = Column(String, nullable=True)
    difficulty = Column(String, nullable=True)
    status = Column(String, nullable=False, default="proposed")  # proposed|accepted|rejected
    created_by = Column(String, nullable=True)
    created_at = Column(DateTime, default=utcnow_naive)
    updated_at = Column(DateTime, default=utcnow_naive, onupdate=utcnow_naive)


class DBPlan(Base):
    """An experiment plan (hypotheses, ablations, budget, stop rules) — content-addressed
    like every other RSI artifact (todo.md D.16)."""
    __tablename__ = "plans"
    # v1.3.2 (migration 0012/0013): tenant boundary. Auto-populated on insert
    # by database.py's before_flush listener (session.info["tenant_id"], falling
    # back to DEFAULT_TENANT_ID) -- no call site that constructs this model needs
    # to set it explicitly. RLS-enforced (migration 0013) once AV_TENANCY_ENFORCE=1.
    tenant_id = Column(String, ForeignKey("tenants.id"), nullable=False, index=True)

    id = Column(String, primary_key=True, default=_new_uuid)
    project_id = Column(String, nullable=False, index=True)
    object_id = Column(String, nullable=False)
    created_by = Column(String, nullable=True)
    created_at = Column(DateTime, default=utcnow_naive)


class DBBudget(Base):
    """A compute/storage/step quota, scoped to one run or a whole lineage (todo.md D.17).
    Counters are stored inline (not JSON) so SQL can increment/aggregate them directly —
    `av budget consume` does an atomic `UPDATE ... SET x_used = x_used + :delta`, never a
    read-modify-write race between two processes consuming the same budget concurrently."""
    __tablename__ = "budgets"
    # v1.3.2 (migration 0012/0013): tenant boundary. Auto-populated on insert
    # by database.py's before_flush listener (session.info["tenant_id"], falling
    # back to DEFAULT_TENANT_ID) -- no call site that constructs this model needs
    # to set it explicitly. RLS-enforced (migration 0013) once AV_TENANCY_ENFORCE=1.
    tenant_id = Column(String, ForeignKey("tenants.id"), nullable=False, index=True)

    id = Column(String, primary_key=True, default=_new_uuid)
    project_id = Column(String, nullable=False, index=True)
    scope = Column(String, nullable=False)  # "run" | "lineage"
    scope_ref = Column(String, nullable=False, index=True)  # a run_id, or a lineage root run_id
    compute_seconds_limit = Column(Float, nullable=True)
    storage_bytes_limit = Column(BigInteger, nullable=True)
    step_limit = Column(Integer, nullable=True)
    compute_seconds_used = Column(Float, nullable=False, default=0.0)
    storage_bytes_used = Column(BigInteger, nullable=False, default=0)
    steps_used = Column(Integer, nullable=False, default=0)
    created_by = Column(String, nullable=True)
    created_at = Column(DateTime, default=utcnow_naive)
    updated_at = Column(DateTime, default=utcnow_naive, onupdate=utcnow_naive)


class DBCausalLink(Base):
    """An agent-authored (or verified) claim: this change CAUSED that metric delta
    (todo.md E.21) — explicit, beyond `parent_run_id`'s bare lineage pointer."""
    __tablename__ = "causal_links"
    # v1.3.2 (migration 0012/0013): tenant boundary. Auto-populated on insert
    # by database.py's before_flush listener (session.info["tenant_id"], falling
    # back to DEFAULT_TENANT_ID) -- no call site that constructs this model needs
    # to set it explicitly. RLS-enforced (migration 0013) once AV_TENANCY_ENFORCE=1.
    tenant_id = Column(String, ForeignKey("tenants.id"), nullable=False, index=True)

    id = Column(Integer, primary_key=True, autoincrement=True)
    project_id = Column(String, nullable=False, index=True)
    cause_type = Column(String, nullable=False)  # "change_set" | "commit"
    cause_ref = Column(String, nullable=False, index=True)
    effect_metric = Column(String, nullable=False)
    effect_delta = Column(Float, nullable=True)
    verified = Column(Boolean, nullable=False, default=False)
    created_by = Column(String, nullable=True)
    created_at = Column(DateTime, default=utcnow_naive)


class DBStrategyEntry(Base):
    """A searchable record of what worked/failed across lineages (todo.md E.22) — beyond
    `.avh` context-memory notes, which are per-repo and not cross-run-queryable."""
    __tablename__ = "strategy_entries"
    # v1.3.2 (migration 0012/0013): tenant boundary. Auto-populated on insert
    # by database.py's before_flush listener (session.info["tenant_id"], falling
    # back to DEFAULT_TENANT_ID) -- no call site that constructs this model needs
    # to set it explicitly. RLS-enforced (migration 0013) once AV_TENANCY_ENFORCE=1.
    tenant_id = Column(String, ForeignKey("tenants.id"), nullable=False, index=True)

    id = Column(String, primary_key=True, default=_new_uuid)
    project_id = Column(String, nullable=False, index=True)
    technique = Column(String, nullable=False)
    hyperparameters = Column(JSON, nullable=True)
    data_mix = Column(JSON, nullable=True)
    outcome = Column(String, nullable=False)  # worked | failed | inconclusive
    run_ids = Column(JSON, nullable=True)
    created_by = Column(String, nullable=True)
    created_at = Column(DateTime, default=utcnow_naive)


class DBLessons(Base):
    """A versioned "what we believe now" document (todo.md E.23) — content-addressed and
    append-only-by-convention (each update is a NEW row; `/latest` by `created_at`
    resolves the current version), same pattern as `policy_packs` minus the hash-chain
    (lessons revise freely; they are not a tamper-evident policy log)."""
    __tablename__ = "lessons"
    # v1.3.2 (migration 0012/0013): tenant boundary. Auto-populated on insert
    # by database.py's before_flush listener (session.info["tenant_id"], falling
    # back to DEFAULT_TENANT_ID) -- no call site that constructs this model needs
    # to set it explicitly. RLS-enforced (migration 0013) once AV_TENANCY_ENFORCE=1.
    tenant_id = Column(String, ForeignKey("tenants.id"), nullable=False, index=True)

    id = Column(String, primary_key=True, default=_new_uuid)
    project_id = Column(String, nullable=False, index=True)
    object_id = Column(String, nullable=False)
    created_by = Column(String, nullable=True)
    created_at = Column(DateTime, default=utcnow_naive)


class DBReview(Base):
    """A reviewer's decision on a change set OR an improver version (todo.md H.34) —
    `av improver promote`'s dual gate consults reviews against the CANDIDATE improver id
    directly when `.av/improver_policy.json` sets `require_review`, since one improver
    version can be the eventual promotion target regardless of which change set (if any)
    produced it."""
    __tablename__ = "reviews"
    # v1.3.2 (migration 0012/0013): tenant boundary. Auto-populated on insert
    # by database.py's before_flush listener (session.info["tenant_id"], falling
    # back to DEFAULT_TENANT_ID) -- no call site that constructs this model needs
    # to set it explicitly. RLS-enforced (migration 0013) once AV_TENANCY_ENFORCE=1.
    tenant_id = Column(String, ForeignKey("tenants.id"), nullable=False, index=True)

    id = Column(String, primary_key=True, default=_new_uuid)
    project_id = Column(String, nullable=False, index=True)
    target_type = Column(String, nullable=False)  # "change_set" | "improver"
    target_id = Column(String, nullable=False, index=True)
    reviewer = Column(String, nullable=True)
    decision = Column(String, nullable=False)  # approve | reject
    comment = Column(String, nullable=True)
    created_at = Column(DateTime, default=utcnow_naive)


class DBCritique(Base):
    """A structured objection attached to a change set OR an improver version (todo.md
    H.35) — must be resolved or explicitly waived before promotion clears; a waiver is
    itself audited. Same target_type/target_id generalization as `DBReview`."""
    __tablename__ = "critiques"
    # v1.3.2 (migration 0012/0013): tenant boundary. Auto-populated on insert
    # by database.py's before_flush listener (session.info["tenant_id"], falling
    # back to DEFAULT_TENANT_ID) -- no call site that constructs this model needs
    # to set it explicitly. RLS-enforced (migration 0013) once AV_TENANCY_ENFORCE=1.
    tenant_id = Column(String, ForeignKey("tenants.id"), nullable=False, index=True)

    id = Column(String, primary_key=True, default=_new_uuid)
    project_id = Column(String, nullable=False, index=True)
    target_type = Column(String, nullable=False)  # "change_set" | "improver"
    target_id = Column(String, nullable=False, index=True)
    author = Column(String, nullable=True)
    objection = Column(String, nullable=False)
    status = Column(String, nullable=False, default="open")  # open | resolved | waived
    resolution = Column(String, nullable=True)
    created_at = Column(DateTime, default=utcnow_naive)
    updated_at = Column(DateTime, default=utcnow_naive, onupdate=utcnow_naive)


class DBBlackboardEntry(Base):
    """A durable shared claim with authors and evidence links (todo.md H.36) — beyond the
    ordered event stream, a place for hypotheses that outlive any one event."""
    __tablename__ = "blackboard_entries"
    # v1.3.2 (migration 0012/0013): tenant boundary. Auto-populated on insert
    # by database.py's before_flush listener (session.info["tenant_id"], falling
    # back to DEFAULT_TENANT_ID) -- no call site that constructs this model needs
    # to set it explicitly. RLS-enforced (migration 0013) once AV_TENANCY_ENFORCE=1.
    tenant_id = Column(String, ForeignKey("tenants.id"), nullable=False, index=True)

    id = Column(String, primary_key=True, default=_new_uuid)
    project_id = Column(String, nullable=False, index=True)
    claim = Column(String, nullable=False)
    author = Column(String, nullable=True)
    evidence = Column(JSON, nullable=True)  # [{"type": "run"|"commit"|"critique", "ref": str}]
    status = Column(String, nullable=False, default="open")  # open | resolved
    created_at = Column(DateTime, default=utcnow_naive)
    updated_at = Column(DateTime, default=utcnow_naive, onupdate=utcnow_naive)


class DBProjectFreeze(Base):
    """Global per-project kill-switch state (todo.md C.15/I.40): while `frozen`, the
    server denies every write except reads and rollback — enforced both client-side
    (`_AuthRetryGroup.invoke()`) and here, so a compromised/rogue local client can't just
    skip the check."""
    __tablename__ = "project_freeze"
    # v1.3.2 (migration 0012/0013): tenant boundary. Auto-populated on insert
    # by database.py's before_flush listener (session.info["tenant_id"], falling
    # back to DEFAULT_TENANT_ID) -- no call site that constructs this model needs
    # to set it explicitly. RLS-enforced (migration 0013) once AV_TENANCY_ENFORCE=1.
    tenant_id = Column(String, ForeignKey("tenants.id"), nullable=False, index=True)

    project_id = Column(String, primary_key=True)
    frozen = Column(Boolean, nullable=False, default=False)
    reason = Column(String, nullable=True)
    frozen_by = Column(String, nullable=True)
    frozen_at = Column(DateTime, nullable=True)
    updated_at = Column(DateTime, nullable=True, onupdate=utcnow_naive)


# ---------------------------------------------------------------------------
# RSI R5 (v1.3.1, migration 0010): sandbox jobs, tool manifests, action logs. These are
# server-side INDEX/AUDIT records, not the sandbox executor's own job state — a driver's
# LIVE state lives wherever that driver can actually re-query it (a container, a Pod, a
# Slurm job — see python/av_cli/sandbox/base.py's module docstring). `tool_manifests` and
# `action_logs` follow the same content-addressed version-history pattern as
# `policy_packs`/`lessons`.
# ---------------------------------------------------------------------------

class DBSandboxJob(Base):
    """A server-side record of one sandbox job submission (todo.md G.29) — lets `av
    sandbox queue` list jobs across drivers/machines without every driver needing its own
    listing capability (a `local` job on a laptop has none at all)."""
    __tablename__ = "sandbox_jobs"
    # v1.3.2 (migration 0012/0013): tenant boundary. Auto-populated on insert
    # by database.py's before_flush listener (session.info["tenant_id"], falling
    # back to DEFAULT_TENANT_ID) -- no call site that constructs this model needs
    # to set it explicitly. RLS-enforced (migration 0013) once AV_TENANCY_ENFORCE=1.
    tenant_id = Column(String, ForeignKey("tenants.id"), nullable=False, index=True)

    id = Column(String, primary_key=True)  # the caller-assigned job_id (see sandbox.base.JobSpec)
    project_id = Column(String, nullable=False, index=True)
    improver_id = Column(String, nullable=True, index=True)
    driver = Column(String, nullable=False)  # local | docker | kubernetes | slurm
    state = Column(String, nullable=False, default="pending")
    exit_code = Column(Integer, nullable=True)
    command = Column(JSON, nullable=True)
    created_by = Column(String, nullable=True)
    created_at = Column(DateTime, default=utcnow_naive)
    updated_at = Column(DateTime, default=utcnow_naive, onupdate=utcnow_naive)


class DBToolManifest(Base):
    """A published version of an improver version's tool permission manifest (todo.md
    G.30) — append-only version history, `/latest` resolved by `created_at`, same pattern
    as `DBLessons`/`DBPolicyPack` minus the hash-chain."""
    __tablename__ = "tool_manifests"
    # v1.3.2 (migration 0012/0013): tenant boundary. Auto-populated on insert
    # by database.py's before_flush listener (session.info["tenant_id"], falling
    # back to DEFAULT_TENANT_ID) -- no call site that constructs this model needs
    # to set it explicitly. RLS-enforced (migration 0013) once AV_TENANCY_ENFORCE=1.
    tenant_id = Column(String, ForeignKey("tenants.id"), nullable=False, index=True)

    id = Column(String, primary_key=True, default=_new_uuid)
    project_id = Column(String, nullable=False, index=True)
    improver_id = Column(String, nullable=False, index=True)
    object_id = Column(String, nullable=False)
    created_by = Column(String, nullable=True)
    created_at = Column(DateTime, default=utcnow_naive)


# ---------------------------------------------------------------------------
# v1.3.2 — Enterprise identity & tenancy (migration 0011). Additive: every table here is
# NEW, and every pre-existing tenant-scoped table gains a `tenant_id` column across two
# follow-up migrations (0012 adds it nullable + backfills, 0013 makes it NOT NULL and
# enables row-level security) rather than one fat migration, to avoid a single long lock
# across add-column + backfill + NOT-NULL + RLS-enable on tables the size `audit_log`/
# `events` can reach in a live deployment. See development/architecture.md's Tenancy
# Isolation contract section for the full design.
#
# DEFAULT_TENANT_ID is a fixed, well-known UUID (not `_new_uuid()`) so it can be a literal
# in the RLS policy's COALESCE fallback (see migration 0013) and in this module — every
# pre-existing row across every table backfills to exactly this tenant, which is what
# makes an unconfigured deployment behave byte-identically to pre-v1.3.2 (VERSIONING.md:
# "if an upgrade would make a previously working script, repo, or client stop working,
# it's MAJOR" — this feature is a MINOR, so it must not).
# ---------------------------------------------------------------------------

DEFAULT_TENANT_ID = "00000000-0000-0000-0000-000000000001"


class DBTenant(Base):
    """A billing/isolation boundary. `slug` is the human-facing identifier (`av tenant
    show <slug>`); `id` is the UUID every tenant-scoped row's `tenant_id` FK's to."""
    __tablename__ = "tenants"

    id = Column(String, primary_key=True, default=_new_uuid)
    slug = Column(String, nullable=False, unique=True, index=True)
    name = Column(String, nullable=False)
    status = Column(String, nullable=False, default="active")  # active | suspended
    settings = Column(JSON, nullable=True)
    created_at = Column(DateTime, default=utcnow_naive)


class DBProject(Base):
    """The tenant that owns a `project_id`. Was purely virtual before v1.3.2 — `av init`
    mints `project_id` client-side and no server-side row ever existed for it (`GET
    /api/projects` was a `GROUP BY DBCommit.project_id`, see server.py::list_projects).
    `id` reuses the existing free-text project_id string verbatim — no rename anywhere
    else in the schema. First writer for an unseen project_id claims it for their tenant
    (`_enforce_project_tenant()`, server.py) — this table is lazily populated, not
    pre-provisioned, matching `av init`'s zero-ceremony flow."""
    __tablename__ = "projects"

    id = Column(String, primary_key=True)
    tenant_id = Column(String, ForeignKey("tenants.id"), nullable=False, index=True)
    name = Column(String, nullable=False)
    created_by = Column(String, nullable=True)
    created_at = Column(DateTime, default=utcnow_naive)
    archived_at = Column(DateTime, nullable=True)


class DBUser(Base):
    """A durable identity — local, SSO-provisioned, or SCIM-provisioned (`source`).
    Replaces nothing: `AV_API_TOKEN`/`AV_AUTH_USERS` (server.py's `.env`-based identities)
    keep working completely unchanged and resolve to DEFAULT_TENANT_ID at runtime — this
    table is the additive path for real multi-user/multi-tenant deployments."""
    __tablename__ = "users"

    id = Column(String, primary_key=True, default=_new_uuid)
    tenant_id = Column(String, ForeignKey("tenants.id"), nullable=False, index=True)
    username = Column(String, nullable=False)
    email = Column(String, nullable=True)
    display_name = Column(String, nullable=True)
    status = Column(String, nullable=False, default="active")  # active | suspended
    source = Column(String, nullable=False, default="local")  # local | sso | scim
    external_id = Column(String, nullable=True, index=True)  # SCIM externalId
    created_at = Column(DateTime, default=utcnow_naive)
    updated_at = Column(DateTime, default=utcnow_naive, onupdate=utcnow_naive)
    last_login_at = Column(DateTime, nullable=True)

    __table_args__ = (
        Index("ix_users_tenant_username", "tenant_id", "username", unique=True),
        Index("ix_users_tenant_email", "tenant_id", "email", unique=True),
    )


class DBUserIdentity(Base):
    """One external-IdP identity linked to a local `DBUser` — a user may hold identities
    at more than one provider (an OIDC `sub` from one IdP, a SAML NameID from another)."""
    __tablename__ = "user_identities"

    id = Column(String, primary_key=True, default=_new_uuid)
    user_id = Column(String, ForeignKey("users.id"), nullable=False, index=True)
    provider_id = Column(String, nullable=False, index=True)
    issuer = Column(String, nullable=False)
    subject = Column(String, nullable=False)
    email = Column(String, nullable=True)
    created_at = Column(DateTime, default=utcnow_naive)

    __table_args__ = (
        Index("ix_user_identities_provider_subject", "provider_id", "subject", unique=True),
    )


class DBGroup(Base):
    """An IdP/SCIM group, mapped to roles via `role_bindings` (`subject_type="group"`)."""
    __tablename__ = "groups"

    id = Column(String, primary_key=True, default=_new_uuid)
    tenant_id = Column(String, ForeignKey("tenants.id"), nullable=False, index=True)
    name = Column(String, nullable=False)
    external_id = Column(String, nullable=True, index=True)  # SCIM externalId
    source = Column(String, nullable=False, default="local")  # local | sso | scim
    created_at = Column(DateTime, default=utcnow_naive)


class DBGroupMember(Base):
    __tablename__ = "group_members"

    group_id = Column(String, ForeignKey("groups.id"), primary_key=True)
    user_id = Column(String, ForeignKey("users.id"), primary_key=True)
    created_at = Column(DateTime, default=utcnow_naive)


class DBRole(Base):
    """A named bundle of permissions expressed in the EXISTING v1.3.1 scope vocabulary
    (`require_scope()`'s scope strings), not a parallel permission system — so every
    existing `Depends(require_scope("..."))` call site keeps working unchanged, and a
    role binding is just a different, DB-backed way to arrive at the same `scopes` list
    `_scopes_for_identity()` already resolves for an `.env`-based token. `tenant_id` NULL
    marks a built-in role (`owner`/`admin`/`maintainer`/`trainer`/`reviewer`/`reader`,
    seeded by migration 0011) shared by every tenant; non-null is a tenant's own custom
    role."""
    __tablename__ = "roles"

    id = Column(String, primary_key=True, default=_new_uuid)
    tenant_id = Column(String, ForeignKey("tenants.id"), nullable=True, index=True)
    name = Column(String, nullable=False)
    description = Column(String, nullable=True)
    permissions = Column(JSON, nullable=False, default=list)  # ["improver:write", "review", ...]
    builtin = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime, default=utcnow_naive)

    __table_args__ = (Index("ix_roles_tenant_name", "tenant_id", "name", unique=True),)


class DBRoleBinding(Base):
    """Grants a role to a user/group/token, at tenant or project scope. The permission
    resolver (`identity.py::resolve_principal`) unions every binding's role's
    `permissions` that apply to the resolved subject."""
    __tablename__ = "role_bindings"

    id = Column(String, primary_key=True, default=_new_uuid)
    tenant_id = Column(String, ForeignKey("tenants.id"), nullable=False, index=True)
    subject_type = Column(String, nullable=False)  # user | group | token
    subject_id = Column(String, nullable=False)
    role_id = Column(String, ForeignKey("roles.id"), nullable=False)
    scope_type = Column(String, nullable=False, default="tenant")  # tenant | project
    scope_id = Column(String, nullable=True)  # a project_id when scope_type == "project"
    created_by = Column(String, nullable=True)
    created_at = Column(DateTime, default=utcnow_naive)

    __table_args__ = (
        Index("ix_role_bindings_subject", "tenant_id", "subject_type", "subject_id"),
    )


class DBApiToken(Base):
    """A DB-backed bearer token — the remote-administrable alternative to
    `AV_AUTH_USERS` (which requires `docker compose` shell access to the host running the
    stack to create/rotate/revoke, cmd_auth.py). Only `token_hash` (sha256) is ever
    stored; the plaintext token is shown exactly once, at creation (`av token create`),
    exactly like `av registry keygen`'s private key never round-trips back out."""
    __tablename__ = "api_tokens"

    id = Column(String, primary_key=True, default=_new_uuid)
    tenant_id = Column(String, ForeignKey("tenants.id"), nullable=False, index=True)
    user_id = Column(String, ForeignKey("users.id"), nullable=True)  # null = service token
    name = Column(String, nullable=False)
    token_hash = Column(String, nullable=False, unique=True, index=True)
    prefix = Column(String, nullable=False)  # first 8 chars, for display in `av token list`
    scopes = Column(JSON, nullable=True)  # explicit scopes; null = role-derived only
    expires_at = Column(DateTime, nullable=True)
    last_used_at = Column(DateTime, nullable=True)
    revoked_at = Column(DateTime, nullable=True)
    created_by = Column(String, nullable=True)
    created_at = Column(DateTime, default=utcnow_naive)


class DBSsoProvider(Base):
    """An OIDC or SAML identity provider configured for a tenant. `config` holds the
    provider-specific JSON (issuer/client_id/jwks_uri for OIDC; entity_id/sso_url/cert for
    SAML); any client secret inside it is Fernet-encrypted at rest under `AV_SECRET_KEY`
    (`python/av_server/sso_crypto.py`) — creation is refused with a clear error when that
    key is unset rather than ever storing a secret in plaintext."""
    __tablename__ = "sso_providers"

    id = Column(String, primary_key=True, default=_new_uuid)
    tenant_id = Column(String, ForeignKey("tenants.id"), nullable=False, index=True)
    kind = Column(String, nullable=False)  # oidc | saml
    name = Column(String, nullable=False)
    config = Column(JSON, nullable=False)
    enabled = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime, default=utcnow_naive)


class DBSession(Base):
    """A logged-in session (`av login`, or the webui's browser flow) — distinct from
    `api_tokens`: a session comes from an interactive SSO login and carries a shorter,
    refreshable lifetime; a token is a long-lived credential minted directly. Only hashes
    are stored, matching `api_tokens`."""
    __tablename__ = "sessions"

    id = Column(String, primary_key=True, default=_new_uuid)
    user_id = Column(String, ForeignKey("users.id"), nullable=False, index=True)
    tenant_id = Column(String, ForeignKey("tenants.id"), nullable=False, index=True)
    token_hash = Column(String, nullable=False, unique=True, index=True)
    refresh_hash = Column(String, nullable=True, unique=True, index=True)
    issued_at = Column(DateTime, default=utcnow_naive)
    expires_at = Column(DateTime, nullable=False)
    revoked_at = Column(DateTime, nullable=True)
    ip = Column(String, nullable=True)
    user_agent = Column(String, nullable=True)


class DBActionLog(Base):
    """A published, content-addressed snapshot of `.av/actions.jsonl` (todo.md G.31) —
    `av replay-actions` fetches this alongside a run's env snapshot to reconstruct not
    just the training code but the AGENT'S DECISIONS."""
    __tablename__ = "action_logs"
    # v1.3.2 (migration 0012/0013): tenant boundary. Auto-populated on insert
    # by database.py's before_flush listener (session.info["tenant_id"], falling
    # back to DEFAULT_TENANT_ID) -- no call site that constructs this model needs
    # to set it explicitly. RLS-enforced (migration 0013) once AV_TENANCY_ENFORCE=1.
    tenant_id = Column(String, ForeignKey("tenants.id"), nullable=False, index=True)

    id = Column(String, primary_key=True, default=_new_uuid)
    project_id = Column(String, nullable=False, index=True)
    run_id = Column(String, nullable=True, index=True)
    object_id = Column(String, nullable=False)
    created_by = Column(String, nullable=True)
    created_at = Column(DateTime, default=utcnow_naive)
