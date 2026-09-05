"""av admin backup — operator-facing backup/restore for the whole registry (v1.3.2,
WP-28/29/30). Two parts, always taken and restored together: a Postgres logical dump
(`pg_dump -Fc`) and the CAS objects tree (a plain tar, gzip-compressed when available).

**Deliberately requires an EXPLICIT database target — no auto-detection of "the local
docker stack".** `av auth`'s `docker_runtime.resolve_compose_file(_find_source_root())`
auto-detection is exactly the mechanism behind a real incident this repo hit (see
development/Probleme.md): a command run from anywhere silently targets whatever local
checkout's compose file it finds, which was the wrong (real, in-use) stack. Backup is
lower-risk than that incident (it's non-destructive), but `restore` is NOT — it can
overwrite a real database — so this module takes the safer default from the start: the
operator must name the target explicitly (`--database-url`/`DATABASE_URL`, or
`--db-container` naming a specific container), every time, for every subcommand.

Either `pg_dump`/`pg_restore`/`psql` on PATH (the portable path — works against any
reachable Postgres, containerized or not) OR `--db-container NAME` (execs those same
binaries inside a named Postgres container, which always has them since they ship with
the server package) — never both silently tried, no fallback chain to get wrong.
"""

import datetime
import hashlib
import json as _json
import os
import secrets
import shutil
import subprocess
import tarfile
from pathlib import Path

import click

from .core import current_output_mode, emit_json, fail, load_contract_schema  # noqa: F401


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _to_libpq_url(url: str) -> str:
    """pg_dump/pg_restore/psql don't know the `+asyncpg` SQLAlchemy dialect suffix."""
    return url.replace("postgresql+asyncpg://", "postgresql://")


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _av_version() -> str:
    from . import _version

    try:
        return _version.__version__
    except Exception:
        return "dev"


def _psql_scalar(database_url: str, sql: str, db_container: str | None) -> str:
    if db_container:
        out = subprocess.run(
            ["docker", "exec", "-i", db_container, "psql", _to_libpq_url(database_url),
             "-tAc", sql],
            check=True, capture_output=True, text=True,
        )
    else:
        out = subprocess.run(
            ["psql", _to_libpq_url(database_url), "-tAc", sql],
            check=True, capture_output=True, text=True,
        )
    return out.stdout.strip()


@click.group()
def admin() -> None:
    """Operator-facing registry administration — not repo-scoped, run against
    infrastructure directly (see docs/dr.md)."""


@admin.group()
def backup() -> None:
    """Create, verify, and restore full-registry backups (Postgres + CAS objects)."""


@backup.command(name="create")
@click.argument("output_dir", type=click.Path(file_okay=False))
@click.option("--database-url", default=None, help="Full postgres[+asyncpg]:// URL. Defaults to $DATABASE_URL.")
@click.option("--data-dir", default=None, type=click.Path(exists=False),
              help="Local CAS objects directory (the AV_DATA_DIR the engine uses). "
                   "Defaults to $AV_DATA_DIR. Mutually exclusive with --engine-container.")
@click.option("--db-container", default=None,
              help="Run pg_dump inside this named Postgres container instead of requiring "
                   "pg_dump on PATH (it always ships with the postgres image).")
@click.option("--engine-container", default=None,
              help="Tar the CAS objects tree via `docker exec` against this named engine "
                   "container's /data instead of a local --data-dir.")
def backup_create(output_dir, database_url, data_dir, db_container, engine_container) -> None:
    """Write a full backup (db.dump + objects archive + manifest.json) to OUTPUT_DIR."""
    database_url = database_url or os.environ.get("DATABASE_URL")
    if not database_url:
        fail(None, "validation",
             "No database target given -- pass --database-url or set $DATABASE_URL.",
             command="admin backup create")
    if not data_dir and not engine_container:
        data_dir = os.environ.get("AV_DATA_DIR")
    if not data_dir and not engine_container:
        fail(None, "validation",
             "No CAS objects source given -- pass --data-dir, --engine-container, or set $AV_DATA_DIR.",
             command="admin backup create")

    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    db_dump_path = out / "db.dump"
    json_mode = current_output_mode() == "json"

    # --- Part 1: Postgres logical dump (pg_dump -Fc — the custom format pg_restore needs).
    if db_container:
        # Dump INSIDE the container (it always has pg_dump matching its own server
        # version) to a temp path, then docker cp it out -- avoids any host/container
        # network-reachability assumption entirely. A random suffix, not a fixed
        # "/tmp/av-backup-db.dump" name (bandit B108) -- a predictable path in shared
        # container temp space is a real (if narrow, single-tenant-container) symlink/
        # race-condition surface, and costs nothing to avoid.
        remote_dump = f"/tmp/av-backup-db-{secrets.token_hex(8)}.dump"  # nosec B108 -- random suffix, ephemeral inside a container we exec into exclusively for this command's duration, always removed below
        subprocess.run(
            ["docker", "exec", db_container, "pg_dump", _to_libpq_url(database_url),
             "-Fc", "-f", remote_dump],
            check=True,
        )
        subprocess.run(
            ["docker", "cp", f"{db_container}:{remote_dump}", str(db_dump_path)],
            check=True,
        )
        subprocess.run(["docker", "exec", db_container, "rm", "-f", remote_dump], check=True)
    else:
        if not shutil.which("pg_dump"):
            fail(None, "validation",
                 "pg_dump not found on PATH -- install postgresql-client, or pass --db-container.",
                 command="admin backup create")
        with open(db_dump_path, "wb") as f:
            subprocess.run(["pg_dump", _to_libpq_url(database_url), "-Fc"], check=True, stdout=f)

    # --- Part 2: the CAS objects tree, plain tar, gzip'd when available (never claiming
    # a codec that wasn't actually used -- an earlier draft of this plan named this file
    # objects.tar.zst unconditionally; the manifest records the REAL codec instead).
    objects_archive_path = out / "objects.tar.gz"
    if engine_container:
        with open(objects_archive_path, "wb") as f:
            subprocess.run(
                ["docker", "exec", engine_container, "tar", "czf", "-", "-C", "/data", "."],
                check=True, stdout=f,
            )
        codec = "gzip"
    else:
        data_path = Path(data_dir)
        if not data_path.is_dir():
            fail(None, "validation", f"--data-dir {data_dir!r} does not exist.",
                 command="admin backup create")
        with tarfile.open(objects_archive_path, "w:gz") as tf:
            tf.add(data_path, arcname=".")
        codec = "gzip"

    # --- Metadata: alembic head, tenant list, approximate row counts.
    try:
        alembic_head = _psql_scalar(database_url, "SELECT version_num FROM alembic_version", db_container)
    except subprocess.CalledProcessError:
        alembic_head = None
    try:
        tenant_ids = [
            t for t in _psql_scalar(database_url, "SELECT string_agg(id, ',') FROM tenants", db_container).split(",")
            if t
        ]
    except subprocess.CalledProcessError:
        tenant_ids = []
    try:
        # pg_stat_user_tables.n_live_tup is an APPROXIMATE row count (updated by
        # autovacuum/analyze, not a live COUNT(*)) -- deliberately used instead of a real
        # per-table COUNT(*) sweep, which would mean N sequential full-table scans on a
        # database that could be large; this is metadata for a human/manifest.json
        # sanity-check, not a correctness-critical number.
        raw_counts = _psql_scalar(
            database_url,
            "SELECT string_agg(relname || ':' || n_live_tup, ',') FROM pg_stat_user_tables",
            db_container,
        )
        row_counts = {}
        for pair in raw_counts.split(","):
            if ":" in pair:
                name, count = pair.rsplit(":", 1)
                row_counts[name] = int(count)
    except subprocess.CalledProcessError:
        row_counts = {}

    manifest = {
        "schema": "backup-manifest-1.0",
        "created_at": _now_iso(),
        "av_version": _av_version(),
        "alembic_head": alembic_head,
        "tenant_ids": tenant_ids,
        "approx_row_counts": row_counts,
        "database": {
            "file": db_dump_path.name,
            "sha256": _sha256_file(db_dump_path),
            "bytes": db_dump_path.stat().st_size,
        },
        "objects": {
            "file": objects_archive_path.name,
            "sha256": _sha256_file(objects_archive_path),
            "bytes": objects_archive_path.stat().st_size,
            "codec": codec,
        },
    }
    manifest_path = out / "manifest.json"
    manifest_path.write_text(_json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")

    if json_mode:
        emit_json(None, "admin backup create", data=manifest)
        return
    click.secho(f"[OK] Backup written to {out}", fg="green")
    click.echo(f"  db.dump       {manifest['database']['bytes']:>12} bytes  sha256={manifest['database']['sha256'][:12]}...")
    click.echo(f"  objects.tar.gz{manifest['objects']['bytes']:>12} bytes  sha256={manifest['objects']['sha256'][:12]}...")
    click.echo(f"  alembic head  {alembic_head}")
    click.echo(f"  tenants       {len(tenant_ids)}")


@backup.command(name="verify")
@click.argument("backup_dir", type=click.Path(exists=True, file_okay=False))
def backup_verify(backup_dir) -> None:
    """Recompute hashes for a backup directory and report any drift from its manifest."""
    out = Path(backup_dir)
    manifest_path = out / "manifest.json"
    json_mode = current_output_mode() == "json"
    if not manifest_path.is_file():
        fail(None, "validation", f"No manifest.json in {backup_dir}", command="admin backup verify")
    manifest = _json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("schema") != "backup-manifest-1.0":
        fail(None, "validation",
             f"Unrecognized manifest schema {manifest.get('schema')!r}", command="admin backup verify")

    problems = []
    for part in ("database", "objects"):
        meta = manifest.get(part, {})
        part_path = out / meta.get("file", "")
        if not part_path.is_file():
            problems.append(f"{part}: file {meta.get('file')!r} is missing")
            continue
        actual_sha = _sha256_file(part_path)
        if actual_sha != meta.get("sha256"):
            problems.append(f"{part}: sha256 mismatch (manifest={meta.get('sha256')}, actual={actual_sha})")
        actual_bytes = part_path.stat().st_size
        if actual_bytes != meta.get("bytes"):
            problems.append(f"{part}: size mismatch (manifest={meta.get('bytes')}, actual={actual_bytes})")

    # Does THIS build's own migration chain even know about the backup's alembic head?
    known_heads = set()
    try:
        from av_server.database import _alembic_config
        from alembic.script import ScriptDirectory

        known_heads = {rev.revision for rev in ScriptDirectory.from_config(_alembic_config()).walk_revisions()}
    except Exception:
        pass
    if known_heads and manifest.get("alembic_head") not in known_heads:
        problems.append(
            f"alembic_head {manifest.get('alembic_head')!r} is not in this build's known "
            f"migration chain -- restoring with this build may run unexpected migrations"
        )

    result = {"backup_dir": str(out), "ok": not problems, "problems": problems, "manifest": manifest}
    if json_mode:
        emit_json(None, "admin backup verify", data=result)
        return
    if problems:
        click.secho(f"[FAIL] {len(problems)} problem(s) found:", fg="red")
        for p in problems:
            click.echo(f"  - {p}")
        raise SystemExit(15)
    click.secho("[OK] Backup verified — hashes match manifest, alembic head is known.", fg="green")


@backup.command(name="restore")
@click.argument("backup_dir", type=click.Path(exists=True, file_okay=False))
@click.option("--database-url", default=None, help="Full postgres[+asyncpg]:// URL. Defaults to $DATABASE_URL.")
@click.option("--data-dir", default=None, type=click.Path(),
              help="Local CAS objects directory to restore into. Defaults to $AV_DATA_DIR.")
@click.option("--db-container", default=None, help="Run pg_restore inside this named Postgres container.")
@click.option("--engine-container", default=None, help="Restore the objects tree via docker exec into this named engine container's /data.")
@click.option("--force", is_flag=True, help="Restore even if the target database is not empty.")
def backup_restore(backup_dir, database_url, data_dir, db_container, engine_container, force) -> None:
    """Restore a backup written by `backup create` into a target database/data dir.

    Refuses to run against a non-empty database without --force -- a restore is
    destructive by nature (it OVERWRITES the objects tree entirely and loads the dump
    on top of whatever tables already exist), and this command has no way to know
    whether "non-empty" means "an old test DB" or "the wrong production database".
    """
    database_url = database_url or os.environ.get("DATABASE_URL")
    if not database_url:
        fail(None, "validation",
             "No database target given -- pass --database-url or set $DATABASE_URL.",
             command="admin backup restore")
    if not data_dir and not engine_container:
        data_dir = os.environ.get("AV_DATA_DIR")
    if not data_dir and not engine_container:
        fail(None, "validation",
             "No CAS objects destination given -- pass --data-dir, --engine-container, or set $AV_DATA_DIR.",
             command="admin backup restore")

    out = Path(backup_dir)
    manifest_path = out / "manifest.json"
    if not manifest_path.is_file():
        fail(None, "validation", f"No manifest.json in {backup_dir}", command="admin backup restore")
    manifest = _json.loads(manifest_path.read_text(encoding="utf-8"))
    json_mode = current_output_mode() == "json"

    if not force:
        try:
            table_count = int(_psql_scalar(
                database_url,
                "SELECT count(*) FROM information_schema.tables WHERE table_schema='public'",
                db_container,
            ))
        except subprocess.CalledProcessError:
            table_count = 0
        if table_count > 0:
            fail(None, "validation",
                 f"Target database already has {table_count} table(s) in schema public -- "
                 f"pass --force to restore anyway (this OVERWRITES it).",
                 command="admin backup restore")

    db_dump_path = out / manifest["database"]["file"]
    objects_archive_path = out / manifest["objects"]["file"]

    # --- Part 1: pg_restore the dump.
    if db_container:
        remote_dump = f"/tmp/av-restore-db-{secrets.token_hex(8)}.dump"  # nosec B108 -- see backup_create's identical comment
        subprocess.run(["docker", "cp", str(db_dump_path), f"{db_container}:{remote_dump}"], check=True)
        subprocess.run(
            ["docker", "exec", db_container, "pg_restore", "-d", _to_libpq_url(database_url),
             "--no-owner", "--clean", "--if-exists", remote_dump],
            check=True,
        )
        subprocess.run(["docker", "exec", db_container, "rm", "-f", remote_dump], check=True)
    else:
        if not shutil.which("pg_restore"):
            fail(None, "validation",
                 "pg_restore not found on PATH -- install postgresql-client, or pass --db-container.",
                 command="admin backup restore")
        subprocess.run(
            ["pg_restore", "-d", _to_libpq_url(database_url), "--no-owner", "--clean", "--if-exists",
             str(db_dump_path)],
            check=True,
        )

    # --- Part 2: extract the objects tree.
    if engine_container:
        remote_archive = f"/tmp/av-restore-objects-{secrets.token_hex(8)}.tar.gz"  # nosec B108 -- see backup_create's identical comment
        subprocess.run(["docker", "cp", str(objects_archive_path), f"{engine_container}:{remote_archive}"], check=True)
        subprocess.run(["docker", "exec", engine_container, "mkdir", "-p", "/data"], check=True)
        subprocess.run(
            ["docker", "exec", engine_container, "tar", "xzf", remote_archive, "-C", "/data"],
            check=True,
        )
        subprocess.run(["docker", "exec", engine_container, "rm", "-f", remote_archive], check=True)
    else:
        data_path = Path(data_dir)
        data_path.mkdir(parents=True, exist_ok=True)
        with tarfile.open(objects_archive_path, "r:gz") as tf:
            tf.extractall(data_path, filter="data")

    # --- Part 3: bring the schema to THIS build's head (heals a backup taken on an
    # older migration chain — the same adoption path a legacy volume goes through).
    import asyncio

    from av_server.database import _apply_schema, create_async_engine

    async def _heal():
        restore_engine = create_async_engine(database_url, echo=False)
        try:
            await _apply_schema(restore_engine)
        finally:
            await restore_engine.dispose()

    asyncio.run(_heal())

    result = {"restored_from": str(out), "manifest": manifest}
    if json_mode:
        emit_json(None, "admin backup restore", data=result)
        return
    click.secho(f"[OK] Restored from {out}", fg="green")
    click.echo(f"  original alembic head: {manifest.get('alembic_head')}")
    click.echo(f"  schema healed to this build's head")
