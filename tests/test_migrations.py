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
    assert heads == ["0001"], f"unexpected heads: {heads}"
    # walk_revisions() yields every revision reachable from head exactly once —
    # no dangling down_revisions, no surprise second branch.
    walked = [rev.revision for rev in script.walk_revisions()]
    assert sorted(walked) == ["0001"]


def test_env_py_is_valid_python():
    source = (_MIGRATIONS / "env.py").read_text(encoding="utf-8")
    ast.parse(source)  # raises on syntax errors
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
