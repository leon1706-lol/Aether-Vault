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
)

_MIGRATIONS = Path(MIGRATIONS_DIR)
_VERSIONS = _MIGRATIONS / "versions"


def test_migration_chain_resolves_to_single_head():
    from alembic.script import ScriptDirectory

    script = ScriptDirectory.from_config(_alembic_config())
    heads = script.get_heads()
    assert heads == ["0002"], f"unexpected heads: {heads}"
    # walk_revisions() yields every revision reachable from head exactly once —
    # no dangling down_revisions, no surprise second branch. The chain is strictly
    # linear: 0002 (runs/events/webhooks/audit) descends from 0001 (baseline).
    walked = sorted(rev.revision for rev in script.walk_revisions())
    assert walked == ["0001", "0002"]


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
    model_class = {"commits": "DBCommit", "trees": "DBTree"}
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
