"""Alembic adoption tests (v1.1.x hardening).

Always-run parts validate the migration chain statically and exercise the legacy-heal
logic on SQLite (the heal DDL is deliberately dialect-portable). Live-path assertions —
schema actually brought to head against Postgres, and a legacy volume healed + stamped —
live in tests/test_server.py behind the existing reachability skip.
"""

import ast
from pathlib import Path

import pytest
import sqlalchemy as sa

from python.av_server.database import (
    MIGRATIONS_DIR,
    _LEGACY_COLUMNS,
    _alembic_config,
    _heal_legacy_columns,
    _heal_legacy_indexes,
)

_MIGRATIONS = Path(MIGRATIONS_DIR)
_VERSIONS = _MIGRATIONS / "versions"


def test_migration_chain_resolves_to_single_head():
    from alembic.script import ScriptDirectory

    script = ScriptDirectory.from_config(_alembic_config())
    heads = script.get_heads()
    assert heads == ["0010"], f"unexpected heads: {heads}"
    # walk_revisions() yields every revision reachable from head exactly once —
    # no dangling down_revisions, no surprise second branch. The chain is strictly
    # linear: 0002 (runs/events/webhooks/audit) descends from 0001 (baseline),
    # 0003 (webhook_deliveries/audit outcome/signature) descends from 0002,
    # 0004 (webhook health tracking/runs.avh_object_id/audit indexes) descends from 0003,
    # 0005 (runs.policy_outcome) descends from 0004, 0006 (RSI R1: runs.kind/improver_id,
    # improver_versions, change_sets, policy_packs, canary_results, project_freeze)
    # descends from 0005, 0007 (RSI R2: runs.integrity_signals, eval_suites,
    # eval_results, eval_adapters, tasks) descends from 0006, 0008 (RSI R3:
    # runs.plan_id/budget_id/stop_reason, plans, budgets) descends from 0007, 0009 (RSI
    # R4: runs.lessons_id, causal_links, strategy_entries, lessons, reviews, critiques,
    # blackboard_entries) descends from 0008, 0010 (RSI R5: sandbox_jobs, tool_manifests,
    # action_logs) descends from 0009.
    walked = sorted(rev.revision for rev in script.walk_revisions())
    assert walked == ["0001", "0002", "0003", "0004", "0005", "0006", "0007", "0008",
                      "0009", "0010"]


def test_env_py_is_valid_python():
    source = (_MIGRATIONS / "env.py").read_text(encoding="utf-8")
    ast.parse(source)  # raises on syntax errors
    # ast.parse alone accepts constructs that fail at compile stage — notably
    # 'async with'/'await' inside a plain def ("SyntaxError: 'async with' outside async
    # function"), which is exactly how env.py once shipped to CI and killed the server
    # at startup on every fresh database. compile() enforces those semantics here.
    compile(source, str(_MIGRATIONS / "env.py"), "exec")
    # The programmatic-startup contract: env.py must honor an injected connection.
    assert 'attributes.get("connection")' in source


def test_baseline_covers_full_current_schema():
    source = (_VERSIONS / "0001_baseline.py").read_text(encoding="utf-8")
    for table in ("objects", "trees", "commits", "refs"):
        assert f'"{table}"' in source, f"baseline missing table {table}"
    for col in ("extra_parents", "chunks", "project_id", "root_tree_hash"):
        assert f'"{col}"' in source, f"baseline missing column {col}"
    # The two deliberate no-FK decisions must be documented where future editors look.
    assert "No FK" in source


def test_legacy_columns_map_matches_models():
    """Every entry in _LEGACY_COLUMNS must correspond to a real models.py column."""
    import io
    import re

    models_src = io.open(
        Path(__file__).resolve().parents[1] / "python" / "av_server" / "models.py",
        encoding="utf-8",
    ).read()
    model_class = {
        "commits": "DBCommit", "trees": "DBTree", "audit_log": "DBAuditLog",
        "webhooks": "DBWebhook", "runs": "DBRun",
    }
    # If this ever assert-fails, the model class for a NEW `_LEGACY_COLUMNS` table entry
    # is missing from the map above — add it, don't skip the check.
    assert set(_LEGACY_COLUMNS) <= set(model_class), (
        f"_LEGACY_COLUMNS references tables with no model_class entry: "
        f"{set(_LEGACY_COLUMNS) - set(model_class)}"
    )
    for table, cols in _LEGACY_COLUMNS.items():
        block = re.search(
            rf"class {model_class[table]}\(Base\):(.*?)(?=\nclass |\Z)",
            models_src,
            re.S,
        )
        assert block, f"models.py lost its {model_class[table]} class?"
        for col in cols:
            assert re.search(rf"^\s*{col} = Column\(", block.group(1), re.M), \
                f"_LEGACY_COLUMNS references {table}.{col}, which is not in models.py"


def test_heal_legacy_columns_adds_only_missing_on_sqlite(tmp_path):
    """The heal DDL is dialect-portable (TEXT/JSON), so SQLite proves the logic:
    missing columns added, present columns untouched, unrelated tables ignored."""
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'legacy.db'}")
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "CREATE TABLE commits ("
            " hash VARCHAR PRIMARY KEY, message VARCHAR NOT NULL,"
            " author VARCHAR, parent_hash VARCHAR, root_tree_hash VARCHAR NOT NULL)"
        )
        conn.exec_driver_sql("CREATE TABLE trees (tree_hash VARCHAR, path_name VARCHAR)")
        # An unrelated table that shares a column name with the heal map:
        conn.exec_driver_sql("CREATE TABLE objects (hash VARCHAR PRIMARY KEY)")

    with engine.connect() as conn:
        before_commits = {c["name"] for c in sa.inspect(conn).get_columns("commits")}
        assert "extra_parents" not in before_commits

        _heal_legacy_columns(conn, {"commits", "trees", "objects"})

        after_commits = {c["name"] for c in sa.inspect(conn).get_columns("commits")}
        after_trees = {c["name"] for c in sa.inspect(conn).get_columns("trees")}
        after_objects = {c["name"] for c in sa.inspect(conn).get_columns("objects")}
    engine.dispose()

    assert "extra_parents" in after_commits
    assert "chunks" in after_trees
    assert after_objects == {"hash"}  # untouched


def test_heal_is_idempotent(tmp_path):
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'legacy.db'}")
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "CREATE TABLE commits (hash VARCHAR PRIMARY KEY, message VARCHAR NOT NULL)"
        )
    with engine.connect() as conn:
        _heal_legacy_columns(conn, {"commits"})
        # Second pass over the same database must not raise (column already there).
        _heal_legacy_columns(conn, {"commits"})
        cols = {c["name"] for c in sa.inspect(conn).get_columns("commits")}
    engine.dispose()
    assert "extra_parents" in cols


def test_heal_legacy_indexes_creates_only_whats_missing_on_sqlite(tmp_path):
    """v1.3.0: the index-shaped sibling of the column-heal test above — an adopted
    legacy volume's audit_log table (created by an earlier phase, before migration 0004
    added ix_audit_log_username/ix_audit_log_action) must come out of healing with both
    indexes, without touching a column-level index (ix_audit_ts) that's unrelated."""
    from python.av_server.models import Base

    engine = sa.create_engine(f"sqlite:///{tmp_path / 'legacy.db'}")
    with engine.begin() as conn:
        # Same shape Base.metadata declares for audit_log, MINUS the two indexes this
        # heal function is responsible for adding — i.e. exactly what an adopted volume
        # whose create_all() predates their declaration would look like.
        conn.exec_driver_sql(
            "CREATE TABLE audit_log (id INTEGER PRIMARY KEY, ts DATETIME NOT NULL,"
            " username VARCHAR, action VARCHAR NOT NULL, project_id VARCHAR,"
            " details JSON, status_code INTEGER)"
        )
        conn.exec_driver_sql("CREATE INDEX ix_audit_ts ON audit_log (ts)")

    with engine.connect() as conn:
        before = {ix["name"] for ix in sa.inspect(conn).get_indexes("audit_log")}
        assert before == {"ix_audit_ts"}

        _heal_legacy_indexes(conn, {"audit_log"})

        after = {ix["name"] for ix in sa.inspect(conn).get_indexes("audit_log")}
    engine.dispose()

    # Confirms the fix is genuinely sourced from Base.metadata (the model), not a
    # hardcoded name list that could itself drift from what DBAuditLog actually declares
    # — including project_id's own `index=True` column-level index, which this healed
    # too (not just the two explicit `Index(...)` entries in __table_args__).
    declared = {ix.name for ix in Base.metadata.tables["audit_log"].indexes}
    assert {"ix_audit_log_username", "ix_audit_log_action"} <= after
    assert after == declared


def test_heal_legacy_indexes_is_idempotent(tmp_path):
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'legacy.db'}")
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "CREATE TABLE audit_log (id INTEGER PRIMARY KEY, ts DATETIME NOT NULL,"
            " username VARCHAR, action VARCHAR NOT NULL, project_id VARCHAR,"
            " details JSON, status_code INTEGER)"
        )
    with engine.connect() as conn:
        _heal_legacy_indexes(conn, {"audit_log"})
        # Second pass over the same database must not raise (indexes already there).
        _heal_legacy_indexes(conn, {"audit_log"})
        after = {ix["name"] for ix in sa.inspect(conn).get_indexes("audit_log")}
    engine.dispose()
    assert {"ix_audit_log_username", "ix_audit_log_action"} <= after


def test_heal_legacy_indexes_ignores_a_genuinely_missing_table(tmp_path):
    """A table not in the passed-in `tables` set (i.e. genuinely absent from the
    database, _create_missing_tables' job) must never be touched — no crash from trying
    to inspect or index a table that isn't there."""
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'legacy.db'}")
    with engine.connect() as conn:
        _heal_legacy_indexes(conn, set())  # nothing present at all
    engine.dispose()  # reaching here without raising is the assertion


# ---------------------------------------------------------------------------
# Stack-free execution proof (v1.1.8): render the full chain to Postgres DDL offline
# ---------------------------------------------------------------------------
# Real-PG execution of the chain debuts on CI's server-tests run — until then nothing has
# ever executed 0001_baseline's op.* calls against a live database. Alembic's offline
# ("--sql") mode executes every op for real and renders the dialect DDL instead of hitting
# a server: any op-level runtime error (bad column type, wrong constraint signature) still
# raises here, so this catches the whole class without a database.

def test_chain_renders_complete_postgres_ddl_offline():
    import contextlib
    import io

    from alembic import command

    cfg = _alembic_config()
    # A sync postgres URL gives clean Postgres-dialect rendering. Offline mode never opens
    # a connection, so pointing at localhost is safe; a real dev DATABASE_URL env must not
    # leak into what gets rendered.
    cfg.set_main_option("sqlalchemy.url", "postgresql://av_user:av_password@localhost/aether_vault")
    assert cfg.attributes.get("connection") is None  # offline path, not the startup path

    buf = io.StringIO()
    # Both capture mechanisms at once: alembic versions differ in whether they honor
    # Config.output_buffer or resolve sys.stdout when env.py configures its context.
    cfg.output_buffer = buf
    with contextlib.redirect_stdout(buf):
        command.upgrade(cfg, "head", sql=True)
    ddl = buf.getvalue()

    # The chain actually executed end-to-end and stamped itself as its final act:
    assert "CREATE TABLE alembic_version" in ddl
    assert "INSERT INTO alembic_version" in ddl

    # v1.2.0 autonomous-loop tables ride the same chain (0002):
    for table in ("runs", "run_commits", "events", "webhooks", "audit_log"):
        assert f"CREATE TABLE {table}" in ddl, f"offline DDL missing table {table}"

    # v1.2.2 delivery ledger rides the chain too (0003):
    assert "CREATE TABLE webhook_deliveries" in ddl
    for col in ("status_code", "signature", "env_snapshot_id", "next_retry_at"):
        assert col in ddl, f"offline DDL missing 0003 column {col}"

    # v1.2.5 webhook health tracking + avh publish pointer + audit indexes (0004):
    for col in ("last_success_at", "consecutive_failures", "disabled_reason", "avh_object_id"):
        assert col in ddl, f"offline DDL missing 0004 column {col}"
    assert "CREATE INDEX ix_audit_log_username" in ddl
    assert "CREATE INDEX ix_audit_log_action" in ddl

    # v1.3.0 runs.policy_outcome (0005):
    assert "policy_outcome" in ddl, "offline DDL missing 0005 column policy_outcome"

    # v1.3.1 RSI R1 (0006): runs.kind/improver_id + five new tables.
    assert "CREATE INDEX ix_runs_improver_id" in ddl
    for table in ("improver_versions", "change_sets", "policy_packs",
                  "canary_results", "project_freeze"):
        assert f"CREATE TABLE {table}" in ddl, f"offline DDL missing table {table}"
    for col in ("manifest_object_id", "chain_hash", "suite_object_id", "frozen"):
        assert col in ddl, f"offline DDL missing 0006 column {col}"

    # v1.3.1 RSI R2 (0007): runs.integrity_signals + four new tables.
    assert "integrity_signals" in ddl, "offline DDL missing 0007 column integrity_signals"
    for table in ("eval_suites", "eval_results", "eval_adapters", "tasks"):
        assert f"CREATE TABLE {table}" in ddl, f"offline DDL missing table {table}"
    for col in ("blind", "revealed", "scored_by", "difficulty"):
        assert col in ddl, f"offline DDL missing 0007 column {col}"

    # v1.3.1 RSI R3 (0008): runs.plan_id/budget_id/stop_reason + two new tables.
    for table in ("plans", "budgets"):
        assert f"CREATE TABLE {table}" in ddl, f"offline DDL missing table {table}"
    for col in ("compute_seconds_limit", "storage_bytes_used", "steps_used", "stop_reason"):
        assert col in ddl, f"offline DDL missing 0008 column {col}"

    # v1.3.1 RSI R5 (0010): three new tables — checked before R4's own assertion block so
    # the diff reads chronologically bottom-up like the chain itself (0010 depends on
    # 0009's schema existing, not the other way around; order here is cosmetic only).
    for table in ("sandbox_jobs", "tool_manifests", "action_logs"):
        assert f"CREATE TABLE {table}" in ddl, f"offline DDL missing table {table}"
    for col in ("driver", "exit_code", "improver_id"):
        assert col in ddl, f"offline DDL missing 0010 column {col}"

    # v1.3.1 RSI R4 (0009): runs.lessons_id + six new tables.
    for table in ("causal_links", "strategy_entries", "lessons", "reviews", "critiques",
                  "blackboard_entries"):
        assert f"CREATE TABLE {table}" in ddl, f"offline DDL missing table {table}"
    for col in ("effect_delta", "hyperparameters", "decision", "target_type", "objection", "claim"):
        assert col in ddl, f"offline DDL missing 0009 column {col}"

    # Every table from 0001_baseline exists in the rendered schema.
    for table in ("objects", "trees", "commits", "refs"):
        assert f"CREATE TABLE {table}" in ddl, f"offline DDL missing table {table}"

    # The columns that define post-create_all-era state (and the legacy heal map).
    for col in ("extra_parents", "chunks", "project_id", "root_tree_hash"):
        assert col in ddl, f"offline DDL missing column {col}"

    # Every index baseline declares — CREATE INDEX lines prove the op.f(...) calls ran.
    assert "CREATE INDEX ix_commits_parent_hash" in ddl
    assert "CREATE INDEX ix_commits_project_id" in ddl

    # Postgres-specific typing survived rendering: tags uses postgresql.ARRAY(String).
    assert "VARCHAR[]" in ddl

    # The refs → commits FK (the one deliberate FK in the schema) is present.
    assert "FOREIGN KEY" in ddl


def test_chain_renders_downgrade_ddl_offline_for_every_revision():
    """v1.3.0 (todo.md item 21): all five revisions define downgrade() — before this
    test, NOT ONE of them was ever executed by anything (upgrade-only offline test above,
    upgrade-only live test in test_server.py). This proves every revision's downgrade
    renders real, revision-specific DDL, one step at a time from head back to base."""
    import contextlib
    import io

    from alembic import command
    from alembic.script import ScriptDirectory

    cfg = _alembic_config()
    cfg.set_main_option("sqlalchemy.url", "postgresql://av_user:av_password@localhost/aether_vault")
    script = ScriptDirectory.from_config(cfg)
    chain = [rev.revision for rev in script.walk_revisions()]  # head -> base order
    assert chain == ["0010", "0009", "0008", "0007", "0006", "0005", "0004", "0003",
                     "0002", "0001"]

    # Revision-specific DDL each downgrade step must emit, in the order downgrade() drops
    # things — proves the rendered SQL is THIS revision's downgrade, not a no-op or a
    # copy-paste of the wrong one.
    expected_per_step = {
        "0010": ["action_logs", "tool_manifests", "sandbox_jobs"],
        "0009": ["blackboard_entries", "critiques", "reviews", "lessons",
                 "strategy_entries", "causal_links", "lessons_id"],
        "0008": ["budgets", "plans", "stop_reason", "budget_id", "plan_id"],
        "0007": ["tasks", "eval_adapters", "eval_results", "eval_suites", "integrity_signals"],
        "0006": ["project_freeze", "canary_results", "policy_packs", "change_sets",
                 "improver_versions", "improver_id", "kind"],
        "0005": ["policy_outcome"],
        "0004": ["disabled_reason", "consecutive_failures", "avh_object_id"],
        "0003": ["webhook_deliveries", "signature", "status_code"],
        "0002": ["audit_log", "webhooks", "events", "run_commits", "runs"],
        "0001": ["objects", "trees", "commits", "refs"],
    }

    for i, rev in enumerate(chain):
        target = chain[i + 1] if i + 1 < len(chain) else "base"
        buf = io.StringIO()
        cfg.output_buffer = buf
        with contextlib.redirect_stdout(buf):
            command.downgrade(cfg, f"{rev}:{target}" if target != "base" else f"{rev}:base", sql=True)
        ddl = buf.getvalue()
        for token in expected_per_step[rev]:
            assert token in ddl, f"downgrade {rev}->{target} DDL missing expected {token!r}:\n{ddl}"


def test_apply_schema_runs_inside_a_committing_transaction():
    """Guard for Probleme.md #70: `_apply_schema` must use engine.begin(), never
    engine.connect(). SQLAlchemy 2.0's commit-as-you-go means connect() ROLLS BACK at
    context exit, and Postgres honours that for DDL — four CI cycles executed every
    migration statement faithfully into a transaction that was then discarded. The
    SQLite-based suites cannot catch this class (the pysqlite driver auto-commits DDL),
    so this source-level invariant is the only stack-free guard available."""
    import inspect

    from python.av_server import database

    src = inspect.getsource(database._apply_schema)
    assert "engine.begin()" in src or "target_engine.begin()" in src, \
        "_apply_schema lost its committing transaction wrapper — migrations will roll back on Postgres"
    assert ".connect(" not in src, \
        "_apply_schema opened a plain connection; its implicit transaction rolls back on exit"
