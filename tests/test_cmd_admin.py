"""Stack-free tests for `av admin backup` (v1.3.2, WP-28/29). The genuine end-to-end
round trip (real pg_dump/pg_restore, real destroy, real restore) is
`scripts/e2e_scenario.sh`'s Phase U — this file covers argument validation, manifest
verification, and the "never auto-detect the local docker stack" contract with mocked
subprocess calls, so it runs with no Postgres/Docker needed.
"""
import hashlib
import json
import os

import pytest
from click.testing import CliRunner

from python.av_cli import cmd_admin
from python.av_cli.main import cli


def invoke(*args, env=None):
    return CliRunner().invoke(cli, list(args), env=env or {})


# ---------------------------------------------------------------------------
# Explicit-target-only contract — the whole reason this module exists in this shape
# ---------------------------------------------------------------------------

def test_backup_create_requires_a_database_target(tmp_path, monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    result = invoke("admin", "backup", "create", str(tmp_path / "out"))
    assert result.exit_code != 0
    assert "DATABASE_URL" in result.output or "database-url" in result.output


def test_backup_create_requires_a_data_source(tmp_path, monkeypatch):
    monkeypatch.delenv("AV_DATA_DIR", raising=False)
    result = invoke("admin", "backup", "create", str(tmp_path / "out"),
                     "--database-url", "postgresql://u:p@h/db")
    assert result.exit_code != 0
    assert "data-dir" in result.output or "AV_DATA_DIR" in result.output


def test_backup_restore_requires_a_database_target(tmp_path, monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    result = invoke("admin", "backup", "restore", str(tmp_path))
    assert result.exit_code != 0
    assert "DATABASE_URL" in result.output or "database-url" in result.output


# ---------------------------------------------------------------------------
# verify — recomputes hashes, never trusts the manifest's own claims
# ---------------------------------------------------------------------------

def test_backup_verify_fails_cleanly_with_no_manifest(tmp_path):
    result = invoke("admin", "backup", "verify", str(tmp_path))
    assert result.exit_code != 0
    assert "manifest" in result.output.lower()


def _write_fake_backup(tmp_path, db_bytes=b"fake-dump"):
    import io
    import tarfile

    db_path = tmp_path / "db.dump"
    obj_path = tmp_path / "objects.tar.gz"
    db_path.write_bytes(db_bytes)
    # A genuinely valid (if empty) gzip tar -- restore's local-path extraction really
    # opens this with tarfile.open(..., "r:gz"), so fake non-gzip bytes would fail for a
    # reason unrelated to whatever this test is actually checking.
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        info = tarfile.TarInfo(name="placeholder.txt")
        data = b"placeholder"
        info.size = len(data)
        tf.addfile(info, io.BytesIO(data))
    obj_bytes = buf.getvalue()
    obj_path.write_bytes(obj_bytes)
    manifest = {
        "schema": "backup-manifest-1.0",
        "created_at": "2026-09-05T00:00:00Z",
        "av_version": "test",
        "alembic_head": "0015",
        "tenant_ids": [],
        "approx_row_counts": {},
        "database": {
            "file": "db.dump",
            "sha256": hashlib.sha256(db_bytes).hexdigest(),
            "bytes": len(db_bytes),
        },
        "objects": {
            "file": "objects.tar.gz",
            "sha256": hashlib.sha256(obj_bytes).hexdigest(),
            "bytes": len(obj_bytes),
            "codec": "gzip",
        },
    }
    (tmp_path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return manifest


def test_backup_verify_passes_on_an_untampered_backup(tmp_path):
    _write_fake_backup(tmp_path)
    result = invoke("--output", "json", "admin", "backup", "verify", str(tmp_path))
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)["data"]
    assert data["ok"] is True
    assert data["problems"] == []


def test_backup_verify_detects_tampered_bytes(tmp_path):
    _write_fake_backup(tmp_path)
    # Tamper the objects archive AFTER the manifest was written -- exactly what a
    # corrupted transfer or a bit-rotted disk would look like.
    (tmp_path / "objects.tar.gz").write_bytes(b"corrupted!!")
    result = invoke("admin", "backup", "verify", str(tmp_path))
    assert result.exit_code != 0
    assert "sha256 mismatch" in result.output or "objects" in result.output


def test_backup_verify_detects_a_missing_part_file(tmp_path):
    _write_fake_backup(tmp_path)
    (tmp_path / "db.dump").unlink()
    result = invoke("admin", "backup", "verify", str(tmp_path))
    assert result.exit_code != 0
    assert "missing" in result.output.lower()


def test_backup_verify_flags_an_unknown_schema_version(tmp_path):
    _write_fake_backup(tmp_path)
    manifest = json.loads((tmp_path / "manifest.json").read_text())
    manifest["schema"] = "backup-manifest-99.0"
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    result = invoke("admin", "backup", "verify", str(tmp_path))
    assert result.exit_code != 0
    assert "schema" in result.output.lower()


# ---------------------------------------------------------------------------
# restore — the destructive one: must refuse a non-empty target without --force
# ---------------------------------------------------------------------------

def test_backup_restore_refuses_a_nonempty_database_without_force(tmp_path, monkeypatch):
    _write_fake_backup(tmp_path)
    monkeypatch.setattr(cmd_admin, "_psql_scalar", lambda *a, **kw: "5")
    result = invoke("admin", "backup", "restore", str(tmp_path),
                     "--database-url", "postgresql://u:p@h/db", "--data-dir", str(tmp_path / "data"))
    assert result.exit_code != 0
    assert "force" in result.output.lower()


def test_backup_restore_proceeds_on_an_empty_database(tmp_path, monkeypatch):
    _write_fake_backup(tmp_path)
    monkeypatch.setattr(cmd_admin, "_psql_scalar", lambda *a, **kw: "0")
    calls = []
    # `cmd_admin.subprocess` IS the real, process-wide `subprocess` module (a bare
    # `import subprocess`, not a copy) -- this monkeypatch is unavoidably GLOBAL for the
    # duration of the test, not scoped to cmd_admin's own calls. Found live: `stdout=b""`
    # matters, not just `returncode` -- `subprocess.check_output()`'s own stdlib
    # implementation calls `run(..., stdout=PIPE).stdout`, and `platform.win32_ver()`
    # (transitively imported the FIRST time anything imports far enough into
    # `sqlalchemy.util.compat`, which happens somewhere in this exact test depending on
    # what other tests already ran first) uses `subprocess.check_output()` under the
    # hood on Windows -- a fake result object missing `.stdout` crashes that completely
    # unrelated stdlib call with `AttributeError: 'R' object has no attribute 'stdout'`
    # the moment it's the first thing in the whole process to trigger that import path.
    def _fake_run(cmd, **kw):
        calls.append(cmd)
        # Text vs bytes mode must match what a REAL `subprocess.run`/`check_output`
        # would return for these kwargs -- the unrelated stdlib caller documented above
        # (`platform.win32_ver()`) calls `check_output(..., text=True)` and then runs a
        # regex `str` match against the result; a bytes stdout there raises `TypeError:
        # cannot use a string pattern on a bytes-like object` instead of the original
        # AttributeError, found live by fixing the first crash and rerunning.
        text_mode = bool(kw.get("text") or kw.get("universal_newlines") or kw.get("encoding"))
        return type("R", (), {"returncode": 0, "stdout": "" if text_mode else b""})()

    monkeypatch.setattr(cmd_admin.subprocess, "run", _fake_run)
    monkeypatch.setattr(cmd_admin.shutil, "which", lambda name: f"/usr/bin/{name}")

    async def _fake_heal():
        return None

    # _apply_schema is imported inside the function; patch the module it's imported
    # from -- av_server.database (the bare top-level import cmd_admin.py itself uses,
    # matching how the INSTALLED package actually resolves it; `python.av_server...`
    # is a separate sys.modules entry only this repo's own test suite ever uses).
    import av_server.database as db_module
    monkeypatch.setattr(db_module, "_apply_schema", lambda engine: _fake_heal())

    result = invoke("admin", "backup", "restore", str(tmp_path),
                     "--database-url", "postgresql+asyncpg://u:p@h/db", "--data-dir", str(tmp_path / "data"))
    assert result.exit_code == 0, result.output
    # pg_restore must actually have been invoked, not silently skipped.
    assert any("pg_restore" in c for c in calls)


# ---------------------------------------------------------------------------
# The manifest schema itself
# ---------------------------------------------------------------------------

def test_backup_manifest_schema_loads_and_matches_a_real_manifest_shape(tmp_path):
    from python.av_cli.core import load_contract_schema

    schema = load_contract_schema("backup-manifest-1.0")
    manifest = _write_fake_backup(tmp_path)
    manifest = json.loads((tmp_path / "manifest.json").read_text())

    try:
        import jsonschema
        jsonschema.validate(manifest, schema)
    except ImportError:
        # Same fallback posture as every other contract test in this repo -- structural
        # checks only when jsonschema isn't installed.
        for key in schema["required"]:
            assert key in manifest
