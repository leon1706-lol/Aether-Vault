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

    id = Column(String, primary_key=True, default=_new_uuid)
    project_id = Column(String, nullable=False, index=True)
    name = Column(String, nullable=False)
    command = Column(JSON, nullable=False)
    created_by = Column(String, nullable=True)
    created_at = Column(DateTime, default=utcnow_naive)


class DBTask(Base):
    """A curriculum task/difficulty-ramp proposal (todo.md B.8)."""
    __tablename__ = "tasks"

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

    id = Column(String, primary_key=True, default=_new_uuid)
    project_id = Column(String, nullable=False, index=True)
    improver_id = Column(String, nullable=False, index=True)
    object_id = Column(String, nullable=False)
    created_by = Column(String, nullable=True)
    created_at = Column(DateTime, default=utcnow_naive)


class DBActionLog(Base):
    """A published, content-addressed snapshot of `.av/actions.jsonl` (todo.md G.31) —
    `av replay-actions` fetches this alongside a run's env snapshot to reconstruct not
    just the training code but the AGENT'S DECISIONS."""
    __tablename__ = "action_logs"

    id = Column(String, primary_key=True, default=_new_uuid)
    project_id = Column(String, nullable=False, index=True)
    run_id = Column(String, nullable=True, index=True)
    object_id = Column(String, nullable=False)
    created_by = Column(String, nullable=True)
    created_at = Column(DateTime, default=utcnow_naive)
