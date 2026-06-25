import datetime
import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

import click

try:
    import aether_core
except ImportError:
    aether_core = None

# Windows consoles default to a legacy codepage (e.g. cp1252) that can't
# encode the emoji/symbols used in CLI output below, crashing with a
# UnicodeEncodeError before the command logic even runs.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

from .client import VaultClient
from .exceptions import AetherVaultException, NetworkError, StorageError, ValidationError
from .index import Index
from .pointer import create_pointer, get_pointer_path, is_pointer_file, parse_pointer

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logger = logging.getLogger("av")


def setup_logging(verbose: bool, silent: bool) -> None:
    if silent:
        logger.setLevel(logging.CRITICAL)
        return
    if verbose:
        logging.basicConfig(level=logging.DEBUG, format="%(levelname)s: %(message)s")
        logger.setLevel(logging.DEBUG)
    else:
        logging.basicConfig(level=logging.INFO, format="%(message)s")
        logger.setLevel(logging.INFO)


# ---------------------------------------------------------------------------
# Repo helpers
# ---------------------------------------------------------------------------

# Directories that must never be walked when collecting working-tree files.
# NOTE: matched per *path component* — a substring test (e.g. `".av" in root`) would
# wrongly skip legitimate folders like `data.average`, and failing to prune means
# os.walk descends into `.av/objects` (potentially tens of thousands of CAS shards)
# on every `add`/`status`.
_IGNORED_DIRS = {".av", ".git", "__pycache__"}


def iter_working_files(root: Path):
    """Yield every working-tree file path under `root`, skipping ignored dirs and noise.

    Prunes ignored directories in-place so the CAS object store is never traversed.
    """
    for dirpath, dirnames, files in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _IGNORED_DIRS]
        for f in files:
            if f.endswith(".pyc") or f.endswith(".av-pointer"):
                continue
            yield Path(dirpath) / f


def find_repo_root() -> Path | None:
    cwd = Path.cwd().resolve()
    for parent in [cwd] + list(cwd.parents):
        if (parent / ".av").is_dir():
            return parent
    return None


def _find_source_root() -> Path:
    """Locate the aether-vault source checkout this package was installed from.

    Only meaningful for an editable/dev install (`pip install -e .`); a wheel install has no
    `tests/` directory underneath it. Factored out as its own function (rather than inlined in
    `test_cmd`) so it can be monkeypatched independently in tests.
    """
    return Path(__file__).parents[2]  # av_cli/ → python/ → aether-vault/


def ensure_repo() -> Path:
    repo_root = find_repo_root()
    if not repo_root:
        raise ValidationError(
            "Not an Aether-Vault repository (or any of the parent directories)."
        )
    return repo_root


def load_config(repo_root: Path) -> dict:
    config_path = repo_root / ".av" / "config"
    if config_path.exists():
        try:
            with open(config_path, "r") as f:
                cfg = json.load(f)
            # Repos initialized before per-project separation was added have no
            # project_id/project_name. Backfill once and persist immediately — generating a
            # fresh uuid4 on every load_config() call without saving it would give the same
            # repo a different identity on each command invocation (every push would look
            # like a new project).
            if "project_id" not in cfg or "project_name" not in cfg:
                cfg.setdefault("project_id", uuid.uuid4().hex)
                cfg.setdefault("project_name", repo_root.name)
                save_config(repo_root, cfg)
            return cfg
        except (json.JSONDecodeError, OSError) as exc:
            print(f"Warning: Failed to load config, using defaults: {exc}", file=sys.stderr)
    return {
        "lfs_threshold_mb": 50,
        "remote_url": "http://localhost:8000",
        "project_id": uuid.uuid4().hex,
        "project_name": repo_root.name,
    }


def save_config(repo_root: Path, config: dict) -> None:
    config_path = repo_root / ".av" / "config"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(config_path, json.dumps(config, indent=2))


# ---------------------------------------------------------------------------
# Atomic write helpers
# ---------------------------------------------------------------------------

def atomic_write_text(path: Path, text: str) -> None:
    """Write text to `path` atomically (write to a temp file in the same dir, then replace).

    Prevents a crash mid-write from leaving a truncated/corrupt file: readers always see
    either the old or the new complete content. os.replace is atomic on POSIX and Windows.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    # Short random suffix (not pid + full uuid4 hex): commit filenames are already a 64-char
    # hash, and on Windows the combined path can exceed the 260-char MAX_PATH once a long
    # temp suffix is appended, which makes the "atomic" write fail outright instead of just
    # being verbose.
    tmp = path.with_name(f"{path.name}.tmp.{uuid.uuid4().hex[:8]}")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def atomic_write_json(path: Path, data) -> None:
    atomic_write_text(path, json.dumps(data, indent=2))


# ---------------------------------------------------------------------------
# Metadata registry helpers (tags & dynamic metric keys)
# ---------------------------------------------------------------------------

def load_registry(repo_root: Path) -> dict:
    """Load the local metadata registry (.av/registry.json)."""
    reg_path = repo_root / ".av" / "registry.json"
    if reg_path.exists():
        try:
            with open(reg_path, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {"tags": [], "metrics": []}


def update_registry(repo_root: Path, tags: list[str], metrics: dict) -> None:
    """Merge new tags and metric keys into the local registry."""
    reg = load_registry(repo_root)
    reg["tags"] = sorted(set(reg["tags"]) | set(tags))
    reg["metrics"] = sorted(set(reg["metrics"]) | set(metrics.keys()))
    atomic_write_json(repo_root / ".av" / "registry.json", reg)


# ---------------------------------------------------------------------------
# Pending-push queue: commits made while the remote server was unreachable
# ---------------------------------------------------------------------------

def load_pending_push(repo_root: Path) -> list[dict]:
    """Load the queue of commits not yet pushed to the remote server."""
    path = repo_root / ".av" / "pending_push"
    if path.exists():
        try:
            with open(path, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return []


def save_pending_push(repo_root: Path, pending: list[dict]) -> None:
    """Persist the pending-push queue, removing the file once it's empty."""
    path = repo_root / ".av" / "pending_push"
    if not pending:
        path.unlink(missing_ok=True)
        return
    atomic_write_json(path, pending)


def queue_pending_push(repo_root: Path, commit_hash: str, ref_name: str | None) -> None:
    """Append a commit/ref pair to the pending-push queue."""
    pending = load_pending_push(repo_root)
    pending.append({"commit_hash": commit_hash, "ref_name": ref_name})
    save_pending_push(repo_root, pending)


def upload_commit_objects(repo_root: Path, client: "VaultClient", tree: dict) -> None:
    """Upload every tracked file's object/layer shards referenced by a commit tree.

    Covers every type (`code` and `artifact` alike), not just artifacts — `add()` now writes
    a CAS object for every tracked file (see its comment), so a remote checkout/clone needs
    code's bytes uploaded too, or `av checkout` against the remote would restore artifacts but
    silently leave code files at whatever the puller's working tree already had.

    Must run BEFORE push_commit(): the server stores each tree entry's object hash as a
    foreign key into its objects table (see DBTree.object_hash in av_server/models.py), so
    pushing the commit first makes that insert violate the FK — the commit silently never
    lands in the database even though the server used to (incorrectly) report success.
    """
    for rel_path, info in tree.items():
        # Upload individual layer shards first (PR #8)
        for layer in info.get("layers", []):
            l_hash = layer["hash"]
            l_obj = repo_root / ".av" / "objects" / l_hash[:2] / l_hash[2:]
            if l_obj.exists():
                client.upload_object(l_obj, l_hash)
        # Upload the whole-file object only if layers weren't successfully chunked
        if not info.get("layers"):
            obj_file = repo_root / ".av" / "objects" / info["hash"][:2] / info["hash"][2:]
            if obj_file.exists():
                client.upload_object(obj_file, info["hash"])


def flush_pending_push(repo_root: Path, client: "VaultClient") -> list[dict]:
    """Retry pushing queued commits to the remote server. Returns the entries still pending."""
    pending = load_pending_push(repo_root)
    if not pending or not client.server_available():
        return pending

    still_pending: list[dict] = []
    for entry in pending:
        commit_path = repo_root / ".av" / "commits" / f"{entry['commit_hash']}.json"
        if not commit_path.exists():
            continue
        with open(commit_path, "r") as f:
            commit_data = json.load(f)
        upload_commit_objects(repo_root, client, commit_data.get("tree", {}))
        if client.push_commit(commit_data):
            ref_ok = True
            if entry.get("ref_name"):
                ref_ok = client.update_ref(entry["ref_name"], entry["commit_hash"])
            if ref_ok:
                continue
        still_pending.append(entry)

    save_pending_push(repo_root, still_pending)
    return still_pending


# ---------------------------------------------------------------------------
# File hashing / metadata helpers (wraps aether_core with Python fallback)
# ---------------------------------------------------------------------------

def hash_file_safe(path: str) -> str:
    if aether_core:
        try:
            return aether_core.hash_file(path)
        except Exception as exc:
            print(
                f"Warning: aether_core.hash_file failed, using Python fallback: {exc}",
                file=sys.stderr,
            )
    sha256 = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(8 * 1024 * 1024):
            sha256.update(chunk)
    return sha256.hexdigest()


# IMPORTANT — single source of truth for file metadata (Unix epoch).
# These deliberately do NOT use the C++ core: std::filesystem::last_write_time has an
# implementation-defined clock epoch (e.g. 1601 on Windows / 100ns ticks) that does not
# match Python's Unix-epoch st_mtime_ns. Routing some calls through C++ and others through
# Python (e.g. after an aether_core fallback) would store one epoch and compare against the
# other, exact-equality change detection would then flag unchanged files as "modified".
# os.stat is a single cheap syscall, so there is no meaningful speed loss in keeping all
# size/mtime handling in Python and reserving the C++ core for hashing only.
def get_file_meta_safe(path: str) -> dict:
    p = Path(path)
    if not p.exists():
        return {"exists": False, "size": 0, "mtime_ns": 0}
    stat = p.stat()
    return {"exists": True, "size": stat.st_size, "mtime_ns": stat.st_mtime_ns}


def compare_meta_safe(path: str, exp_size: int, exp_mtime: int) -> bool:
    # Mirrors get_file_meta_safe exactly (same Unix-epoch source) so a freshly captured
    # entry always compares equal to itself.
    meta = get_file_meta_safe(path)
    return meta["exists"] and meta["size"] == exp_size and meta["mtime_ns"] == exp_mtime


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

@click.group()
@click.option("--verbose", is_flag=True, default=False, help="Enable debug logging.")
@click.option("--silent", is_flag=True, default=False, help="Suppress all output.")
@click.pass_context
def cli(ctx: click.Context, verbose: bool, silent: bool) -> None:
    """Aether-Vault: High-performance version control for ML models & datasets."""
    ctx.ensure_object(dict)
    setup_logging(verbose, silent)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

@cli.command()
def init() -> None:
    """Initialize a new Aether-Vault repository in the current directory."""
    repo_root = Path.cwd()
    av_dir = repo_root / ".av"
    if av_dir.exists():
        click.secho(f"Repository already initialized at {av_dir}", fg="yellow")
        return

    (av_dir / "objects").mkdir(parents=True, exist_ok=True)
    (av_dir / "refs" / "heads").mkdir(parents=True, exist_ok=True)
    (av_dir / "commits").mkdir(parents=True, exist_ok=True)

    save_config(repo_root, {
        "lfs_threshold_mb": 50,
        "remote_url": "http://localhost:8000",
        "project_id": uuid.uuid4().hex,
        "project_name": repo_root.name,
    })

    idx = Index(repo_root)
    idx.save()

    with open(av_dir / "HEAD", "w") as f:
        f.write("ref: refs/heads/main\n")

    with open(av_dir / "refs" / "heads" / "main", "w") as f:
        f.write("")

    click.secho(f"Initialized empty Aether-Vault repository in {av_dir}", fg="green")


@cli.command()
@click.argument("value", type=int, required=False, default=None)
@click.option("--remote-url", default=None, help="Set the remote registry URL for this repo.")
@click.option("--name", "project_name", default=None, help="Rename this repo's project (display name only — does not change its project_id).")
def config(value: int | None, remote_url: str | None, project_name: str | None) -> None:
    """Set the LFS threshold in MB, the remote registry URL, and/or the project name.

    Run with no arguments to print the current configuration.
    """
    repo_root = ensure_repo()
    cfg = load_config(repo_root)

    if value is None and remote_url is None and project_name is None:
        click.echo(f"LFS threshold : {cfg.get('lfs_threshold_mb')} MB")
        click.echo(f"Remote URL    : {cfg.get('remote_url')}")
        click.echo(f"Project name  : {cfg.get('project_name')}")
        click.echo(f"Project ID    : {cfg.get('project_id')}")
        return

    if value is not None:
        cfg["lfs_threshold_mb"] = value
        click.secho(f"Configured LFS threshold to {value} MB", fg="green")
    if remote_url is not None:
        cfg["remote_url"] = remote_url
        click.secho(f"Configured remote URL to {remote_url}", fg="green")
    if project_name is not None:
        cfg["project_name"] = project_name
        click.secho(f"Configured project name to {project_name}", fg="green")

    save_config(repo_root, cfg)


@cli.command()
@click.argument("paths", nargs=-1, type=click.Path(exists=True))
def add(paths: tuple) -> None:
    """Add files (or directories) to the staging index."""
    repo_root = ensure_repo()
    idx = Index(repo_root)
    cfg = load_config(repo_root)
    threshold_bytes = cfg.get("lfs_threshold_mb", 50) * 1024 * 1024

    files_to_process: list[Path] = []
    for p in paths:
        path_obj = Path(p).resolve()
        if path_obj.is_file():
            files_to_process.append(path_obj)
        elif path_obj.is_dir():
            files_to_process.extend(iter_working_files(path_obj))

    for fpath in files_to_process:
        rel_path = str(fpath.relative_to(repo_root)).replace("\\", "/")
        if is_pointer_file(fpath):
            continue

        meta = get_file_meta_safe(str(fpath))

        # Skip the expensive full-file re-hash when the file is unchanged since it was last
        # indexed (same size + mtime). Without this, `av add .` re-reads every tracked file
        # (potentially many GB of artifacts) just to discover nothing changed.
        existing = idx.get_entry(rel_path)
        if existing and compare_meta_safe(str(fpath), existing["size"], existing["mtime_ns"]):
            continue

        file_hash = hash_file_safe(str(fpath))
        file_type = idx.classify_file(rel_path)
        pointer_rel_path = None

        if file_type == "artifact" and meta["size"] > threshold_bytes:
            layers: list[dict] = []

            # PR #8 — displacement-resistant safetensors layer hashing
            if rel_path.endswith(".safetensors") and aether_core and hasattr(aether_core, "split_and_hash_safetensors"):
                logger.info(f"Splitting safetensors layers for {rel_path}...")
                try:
                    layer_results = aether_core.split_and_hash_safetensors(str(fpath))
                    for lr in layer_results:
                        l_hash = lr["hash"]
                        l_size = lr["size"]
                        l_offset = lr["offset"]
                        l_obj_dir = repo_root / ".av" / "objects" / l_hash[:2]
                        l_obj_dir.mkdir(parents=True, exist_ok=True)
                        l_obj_path = l_obj_dir / l_hash[2:]
                        if not l_obj_path.exists():
                            # Copy the layer byte-range in 8 MB chunks. Reading the whole
                            # layer with src_f.read(l_size) would spike memory by a full
                            # layer's size (hundreds of MB / GB for large tensors).
                            with open(fpath, "rb") as src_f:
                                src_f.seek(l_offset)
                                with open(l_obj_path, "wb") as dst_f:
                                    remaining = l_size
                                    while remaining > 0:
                                        chunk = src_f.read(min(8 * 1024 * 1024, remaining))
                                        if not chunk:
                                            break
                                        dst_f.write(chunk)
                                        remaining -= len(chunk)
                        layers.append({"name": lr["name"], "hash": l_hash, "size": l_size})
                except Exception as exc:
                    logger.warning(f"Layer splitting failed for {rel_path}, falling back to whole-file: {exc}")

            obj_dir = repo_root / ".av" / "objects" / file_hash[:2]
            obj_dir.mkdir(parents=True, exist_ok=True)
            obj_path = obj_dir / file_hash[2:]
            if not obj_path.exists():
                shutil.copy2(fpath, obj_path)

            ptr_path = get_pointer_path(fpath)
            ptr_content = create_pointer(fpath, file_hash, meta["size"])
            with open(ptr_path, "w") as ptr_f:
                ptr_f.write(ptr_content)

            pointer_rel_path = rel_path + ".av-pointer"
            idx.add_entry(rel_path, file_hash, meta["size"], meta["mtime_ns"], file_type, pointer_rel_path)
            if layers:
                # add_entry() above already wrote the index to disk via its own auto_save
                # (without "layers", which isn't one of its parameters), so the per-layer
                # data was previously lost the moment `av add` exited: a fresh `av commit`
                # process re-loads the index from disk and finds no layers, silently
                # degrading every per-layer weight diff (`av handoff --diff-weights`, and the
                # Web UI's checkpoint diff) into a whole-file hash comparison. Persist it.
                idx.entries[rel_path]["layers"] = layers
                idx.save()
            click.secho(f"Staged [ARTIFACT] {rel_path} (LFS, {len(layers)} layers)", fg="green")
        else:
            # Every tracked file — including code and sub-threshold artifacts, not just
            # LFS-pointered ones — needs a durable copy under .av/objects/. Without this,
            # `av checkout` to an older commit has no content to restore a code/small file
            # from (only its hash was ever recorded), so rolling back code was silently a
            # no-op. See development/Probleme.md for the full writeup.
            obj_dir = repo_root / ".av" / "objects" / file_hash[:2]
            obj_dir.mkdir(parents=True, exist_ok=True)
            obj_path = obj_dir / file_hash[2:]
            if not obj_path.exists():
                shutil.copy2(fpath, obj_path)

            idx.add_entry(rel_path, file_hash, meta["size"], meta["mtime_ns"], file_type, None)
            click.secho(f"Staged [{file_type.upper()}] {rel_path}", fg="green")


@cli.command()
def status() -> None:
    """Show the working tree status."""
    repo_root = ensure_repo()
    idx = Index(repo_root)

    head_path = repo_root / ".av" / "HEAD"
    branch = "detached"
    if head_path.exists():
        head_content = head_path.read_text().strip()
        if head_content.startswith("ref: refs/heads/"):
            branch = head_content.split("/")[-1]

    click.secho(f"On branch {branch}\n", bold=True)

    staged, modified, deleted, untracked = [], [], [], []

    disk_files: set[str] = set()
    for fpath in iter_working_files(repo_root):
        disk_files.add(str(fpath.relative_to(repo_root)).replace("\\", "/"))

    for rel_path, entry in idx.entries.items():
        if rel_path not in disk_files:
            deleted.append(rel_path)
        elif entry.get("staged"):
            staged.append(rel_path)
        elif not compare_meta_safe(str(repo_root / rel_path), entry["size"], entry["mtime_ns"]):
            modified.append(rel_path)

    for rel_path in disk_files:
        if rel_path not in idx.entries:
            untracked.append(rel_path)

    if staged:
        click.secho("Changes to be committed:", fg="green")
        for f in staged:
            click.echo(f"  modified: {f}")
        click.echo("")
    if modified:
        click.secho("Changes not staged for commit:", fg="yellow")
        for f in modified:
            click.echo(f"  modified: {f}")
        click.echo("")
    if deleted:
        click.secho("Deleted files:", fg="red")
        for f in deleted:
            click.echo(f"  deleted:  {f}")
        click.echo("")
    if untracked:
        click.secho("Untracked files:", fg="red")
        for f in untracked:
            click.echo(f"  {f}")
        click.echo("")

    if not staged and not modified and not deleted and not untracked:
        click.secho("Nothing to commit, working tree clean", fg="green")


@cli.command()
@click.option("-m", "--message", required=True, help="Commit message.")
@click.option("--tag", "tags", multiple=True, help="Free-form tag label (repeatable).")
@click.option(
    "--metric",
    "metrics_raw",
    multiple=True,
    help="Metric in key=value format (repeatable). E.g. --metric sharpe=2.45",
)
@click.option("--metric-sharpe", type=float, default=None, help="Sharpe ratio (legacy shorthand).")
@click.option("--metric-drawdown", type=float, default=None, help="Max drawdown (legacy shorthand).")
def commit(
    message: str,
    tags: tuple,
    metrics_raw: tuple,
    metric_sharpe: float | None,
    metric_drawdown: float | None,
) -> None:
    """Record staged changes to the repository with optional tags and metrics."""
    repo_root = ensure_repo()
    idx = Index(repo_root)
    cfg = load_config(repo_root)
    client = VaultClient(cfg.get("remote_url", "http://localhost:8000"))

    staged = idx.get_staged_entries()
    if not staged:
        click.secho("Nothing to commit", fg="yellow")
        return

    # --- Build metrics dict ---
    metrics: dict = {}
    for raw in metrics_raw:
        if "=" in raw:
            k, v = raw.split("=", 1)
            try:
                metrics[k.strip()] = float(v) if "." in v else int(v)
            except ValueError:
                metrics[k.strip()] = v
    if metric_sharpe is not None:
        metrics["sharpe"] = metric_sharpe
    if metric_drawdown is not None:
        metrics["drawdown"] = metric_drawdown

    # --- Update local metadata registry ---
    update_registry(repo_root, list(tags), metrics)

    # --- Build tree snapshot (unified flat format, PR #8) ---
    tree: dict = {}
    for rel_path, e in idx.entries.items():
        tree[rel_path] = {
            "hash": e["hash"],
            "size": e["size"],
            "type": e["type"],
            "layers": e.get("layers", []),
        }

    # --- Resolve parent commit ---
    head_path = repo_root / ".av" / "HEAD"
    parents: list[str] = []
    ref_path = None
    if head_path.exists():
        head_content = head_path.read_text().strip()
        if head_content.startswith("ref: "):
            ref_path = repo_root / ".av" / head_content.split(": ", 1)[1]
            if ref_path.exists() and ref_path.read_text().strip():
                parents.append(ref_path.read_text().strip())
        else:
            parents.append(head_content)

    commit_data: dict = {
        "parents": parents,
        "author": os.environ.get("AV_AUTHOR", "anonymous"),
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "message": message,
        "tree": tree,
        "tags": list(tags),
        "metrics": metrics,
        # Included in the hashed payload (not just attached after) so two different
        # projects can never collide on the same commit hash even if their tree/message/
        # timestamp happened to be byte-identical — the shared registry's `commits` table
        # is keyed by hash alone.
        "project_id": cfg["project_id"],
        "project_name": cfg["project_name"],
    }

    # Deterministic hash over sorted JSON (preserves DAG integrity)
    commit_str = json.dumps(commit_data, sort_keys=True)
    commit_hash = hashlib.sha256(commit_str.encode()).hexdigest()
    commit_data["hash"] = commit_hash

    # --- Persist locally (atomically: write the commit object before moving the ref, and
    # write each file via temp+replace so a crash can't leave a ref pointing at a half-written
    # or missing commit) ---
    atomic_write_json(repo_root / ".av" / "commits" / f"{commit_hash}.json", commit_data)

    if ref_path:
        atomic_write_text(ref_path, commit_hash)
    else:
        atomic_write_text(head_path, commit_hash)

    idx.clear_staged()
    click.secho(f"[{commit_hash[:7]}] {message}", fg="green")
    if tags:
        click.secho(f"  Tags: {', '.join(tags)}", fg="cyan")
    if metrics:
        click.secho(f"  Metrics: {metrics}", fg="cyan")

    # --- Push to remote if available ---
    # Refs are namespaced as "<project_id>/<branch>" on the shared registry so two projects
    # can each have a branch named "main" without overwriting each other's ref.
    remote_ref_name = f"{cfg['project_id']}/{ref_path.name}" if ref_path else None

    flush_pending_push(repo_root, client)
    if client.server_available():
        # Objects must reach the server before the commit: the server's tree rows store each
        # entry's object hash as a foreign key, so pushing the commit first makes that insert
        # fail (previously misreported as a successful 409 "already exists" — see
        # upload_commit_objects()'s docstring / development/Probleme.md).
        upload_commit_objects(repo_root, client, tree)
        if client.push_commit(commit_data):
            ref_ok = True
            if remote_ref_name:
                ref_ok = client.update_ref(remote_ref_name, commit_hash)
            if not ref_ok:
                queue_pending_push(repo_root, commit_hash, remote_ref_name)
                click.secho("  Ref update failed — commit queued for retry (run `av push` later)", fg="yellow")
        else:
            queue_pending_push(repo_root, commit_hash, remote_ref_name)
            click.secho("  Push failed — commit queued for retry (run `av push` later)", fg="yellow")
    else:
        queue_pending_push(repo_root, commit_hash, remote_ref_name)
        click.secho("  Server unreachable — commit queued for push (run `av push` later)", fg="yellow")


@cli.command()
@click.argument("name", required=False)
def branch(name: str | None) -> None:
    """List existing branches, or create a new one."""
    repo_root = ensure_repo()
    heads_dir = repo_root / ".av" / "refs" / "heads"

    if not name:
        head_path = repo_root / ".av" / "HEAD"
        current = ""
        if head_path.exists():
            head_content = head_path.read_text().strip()
            if head_content.startswith("ref: refs/heads/"):
                current = head_content.split("/")[-1]

        for br in heads_dir.iterdir():
            if br.name == current:
                click.secho(f"* {br.name}", fg="green")
            else:
                click.echo(f"  {br.name}")
    else:
        head_path = repo_root / ".av" / "HEAD"
        commit_hash = ""
        if head_path.exists():
            head_content = head_path.read_text().strip()
            if head_content.startswith("ref: "):
                ref_path = repo_root / ".av" / head_content.split(": ")[1]
                if ref_path.exists():
                    commit_hash = ref_path.read_text().strip()
            else:
                commit_hash = head_content

        with open(heads_dir / name, "w") as f:
            f.write(commit_hash)
        click.secho(f"Created branch '{name}'", fg="green")


@cli.command()
@click.argument("target")
@click.option("--force", "-f", is_flag=True, default=False,
              help="Discard uncommitted local changes instead of aborting.")
def checkout(target: str, force: bool) -> None:
    """Checkout a branch or a specific commit hash."""
    repo_root = ensure_repo()
    cfg = load_config(repo_root)
    client = VaultClient(cfg.get("remote_url", "http://localhost:8000"))

    heads_dir = repo_root / ".av" / "refs" / "heads"
    commit_hash = target
    ref_name = None

    if (heads_dir / target).exists():
        commit_hash = (heads_dir / target).read_text().strip()
        ref_name = target

    commit_file = repo_root / ".av" / "commits" / f"{commit_hash}.json"
    commit_data = None

    if commit_file.exists():
        with open(commit_file, "r") as f:
            commit_data = json.load(f)
    elif client.server_available():
        commit_data = client.get_commit(commit_hash)
        if commit_data:
            with open(commit_file, "w") as f:
                json.dump(commit_data, f)

    if not commit_data:
        click.secho(f"Error: Commit '{target}' not found.", fg="red")
        return

    idx = Index(repo_root)

    # Guard against silent data loss: checkout overwrites/deletes tracked working files.
    # Refuse to proceed if there are uncommitted changes (modified/deleted tracked files
    # or staged-but-uncommitted edits) unless the user explicitly passes --force.
    if not force:
        dirty: list[str] = []
        for rel_path, entry in idx.entries.items():
            fpath = repo_root / rel_path
            if not fpath.exists():
                dirty.append(rel_path)
            elif entry.get("staged") or not compare_meta_safe(
                str(fpath), entry["size"], entry["mtime_ns"]
            ):
                dirty.append(rel_path)
        if dirty:
            click.secho(
                "Error: You have uncommitted changes that would be overwritten by checkout:",
                fg="red",
            )
            for d in dirty[:20]:
                click.echo(f"  {d}")
            if len(dirty) > 20:
                click.echo(f"  … and {len(dirty) - 20} more")
            click.secho("Commit them, or re-run with --force to discard.", fg="yellow")
            return

    old_entries = dict(idx.entries)
    idx.entries.clear()

    tree = commit_data.get("tree", {})
    if "code" in tree or "artifacts" in tree:
        for rel_path, h in tree.get("code", {}).items():
            idx.add_entry(rel_path, h, 0, 0, "code", auto_save=False)
        for rel_path, artifact in tree.get("artifacts", {}).items():
            h = artifact["hash"]
            size = artifact["size"]
            pointer = artifact.get("pointer")
            idx.add_entry(rel_path, h, size, 0, "artifact", pointer, auto_save=False)
            if pointer:
                obj_path = repo_root / ".av" / "objects" / h[:2] / h[2:]
                dest = repo_root / rel_path
                dest.parent.mkdir(parents=True, exist_ok=True)
                if obj_path.exists():
                    shutil.copy2(obj_path, dest)
                elif client.server_available():
                    click.echo(f"Downloading {rel_path}...")
                    if client.download_object(h, dest):
                        obj_path.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(dest, obj_path)
    else:
        for rel_path, info in tree.items():
            h = info["hash"]
            size = info.get("size", 0)
            file_type = info.get("type", "file")
            layers = info.get("layers", [])
            pointer = rel_path + ".av-pointer" if file_type == "artifact" else None

            idx.add_entry(rel_path, h, size, 0, file_type, pointer, auto_save=False)
            if layers:
                idx.entries[rel_path]["layers"] = layers

            # Restore every tracked file's content from the CAS, not just artifacts — `code`
            # files are written to .av/objects by `add()` too (see its comment there), so an
            # older commit's code must be materialized here the same way, or `av checkout`
            # would silently leave the working tree's code untouched while still claiming
            # success (development/Probleme.md).
            obj_path = repo_root / ".av" / "objects" / h[:2] / h[2:]
            dest = repo_root / rel_path
            dest.parent.mkdir(parents=True, exist_ok=True)

            if layers and not obj_path.exists():
                click.echo(f"Reassembling {rel_path} from {len(layers)} layers...")
                try:
                    with open(dest, "wb") as f_out:
                        for layer in layers:
                            lh = layer["hash"]
                            l_obj = repo_root / ".av" / "objects" / lh[:2] / lh[2:]
                            if not l_obj.exists() and client.server_available():
                                client.download_object(lh, l_obj)
                            if not l_obj.exists():
                                raise click.ClickException(
                                    f"Missing layer {lh} for {rel_path}; checkout aborted to avoid a corrupt artifact"
                                )
                            with open(l_obj, "rb") as f_in:
                                shutil.copyfileobj(f_in, f_out)
                except click.ClickException:
                    dest.unlink(missing_ok=True)
                    raise

                obj_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(dest, obj_path)
            else:
                if obj_path.exists():
                    shutil.copy2(obj_path, dest)
                elif client.server_available():
                    click.echo(f"Downloading {rel_path}...")
                    if client.download_object(h, dest):
                        obj_path.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(dest, obj_path)

    for rel_path in old_entries:
        if rel_path not in idx.entries:
            file_path = repo_root / rel_path
            if file_path.exists() and file_path.is_file():
                file_path.unlink()
                try:
                    for parent in file_path.parents:
                        if parent == repo_root or parent.name == '.av':
                            break
                        if not any(parent.iterdir()):
                            parent.rmdir()
                        else:
                            break
                except Exception:
                    pass
            ptr_path = repo_root / (rel_path + ".av-pointer")
            if ptr_path.exists() and ptr_path.is_file():
                ptr_path.unlink()

    # Record the real on-disk size/mtime for every materialized file and clear the staged
    # flag, so `av status` reports a clean tree right after checkout. Previously entries were
    # written with mtime_ns=0 (and re-adding into a cleared index marked them staged), which
    # made every file appear "modified"/"to be committed" immediately after a checkout.
    for rel_path, entry in idx.entries.items():
        fpath = repo_root / rel_path
        if fpath.exists():
            m = get_file_meta_safe(str(fpath))
            entry["size"] = m["size"]
            entry["mtime_ns"] = m["mtime_ns"]
        entry["staged"] = False

    idx.save()

    head_path = repo_root / ".av" / "HEAD"
    with open(head_path, "w") as f:
        if ref_name:
            f.write(f"ref: refs/heads/{ref_name}\n")
        else:
            f.write(f"{commit_hash}\n")

    click.secho(f"Checked out '{target}'", fg="green")


@cli.command("list-meta")
def list_meta() -> None:
    """Display all registered tag labels and metric keys for this repository."""
    repo_root = ensure_repo()
    reg = load_registry(repo_root)

    click.secho("\n─── Registered Metadata ───", bold=True)

    if reg["tags"]:
        click.secho("\n  Tags:", fg="cyan")
        for t in sorted(reg["tags"]):
            click.echo(f"    • {t}")
    else:
        click.echo("\n  Tags: (none)")

    if reg["metrics"]:
        click.secho("\n  Metric Keys:", fg="cyan")
        for m in sorted(reg["metrics"]):
            click.echo(f"    • {m}")
    else:
        click.echo("\n  Metric Keys: (none)")


@cli.command()
def push() -> None:
    """Retry pushing locally committed but not-yet-synced commits to the remote server."""
    repo_root = ensure_repo()
    cfg = load_config(repo_root)
    client = VaultClient(cfg.get("remote_url", "http://localhost:8000"))

    pending_before = load_pending_push(repo_root)
    if not pending_before:
        click.secho("Nothing pending — all commits are synced.", fg="green")
        return

    if not client.server_available():
        click.secho("Error: Remote server is not reachable.", fg="red")
        return

    still_pending = flush_pending_push(repo_root, client)
    pushed = len(pending_before) - len(still_pending)
    if pushed:
        click.secho(f"✓ Pushed {pushed} commit(s) to the remote server", fg="green")
    if still_pending:
        click.secho(f"  {len(still_pending)} commit(s) still pending", fg="yellow")


@cli.command()
def gc() -> None:
    """Trigger garbage collection on the remote CAS server."""
    repo_root = ensure_repo()
    cfg = load_config(repo_root)
    client = VaultClient(cfg.get("remote_url", "http://localhost:8000"))

    if not client.server_available():
        click.secho("Error: Remote server is not reachable.", fg="red")
        return

    result = client.run_gc()
    if result:
        click.secho("✓ Garbage collection complete", fg="green")
        click.echo(
            f"  Alive objects : {result.get('alive_objects', '?')}\n"
            f"  Deleted objects: {result.get('deleted_objects', '?')}\n"
            f"  Reused trees  : {result.get('reused_trees', '?')}"
        )
    else:
        click.secho("GC request failed. Check server logs.", fg="red")


@cli.command()
@click.option("--fix", is_flag=True, default=False,
              help="Repair fixable issues: re-link pointers, clear tmp leftovers, clear/retry pending-push entries.")
@click.option("--dry-run", "dry_run", is_flag=True, default=False,
              help="With --fix, preview what would be repaired without changing anything.")
def doctor(fix: bool, dry_run: bool) -> None:
    """Diagnose common repo and environment problems.

    Read-only by default: reports issues (native core availability, server reachability,
    index/pointer consistency, pending-push queue, leftover temp files) without modifying
    anything. Pass --fix to repair what's safely recoverable, or --fix --dry-run to preview
    what --fix would do without changing anything.
    """
    repo_root = ensure_repo()
    cfg = load_config(repo_root)
    client = VaultClient(cfg.get("remote_url", "http://localhost:8000"))
    av_dir = repo_root / ".av"

    click.secho("Aether-Vault Doctor", bold=True)
    click.secho("-------------------", bold=True)

    warning_count = 0
    fixed_count = 0
    preview = fix and dry_run  # --dry-run only means anything alongside --fix

    def ok(msg: str) -> None:
        click.secho(f"[OK]    {msg}", fg="green")

    def warn(msg: str) -> None:
        nonlocal warning_count
        warning_count += 1
        click.secho(f"[WARN]  {msg}", fg="yellow")

    def fixed(msg: str) -> None:
        nonlocal fixed_count
        fixed_count += 1
        label = "[WOULD FIX]" if preview else "[FIXED]"
        click.secho(f"{label} {msg}", fg="cyan")

    required = [av_dir / "objects", av_dir / "refs" / "heads", av_dir / "commits", av_dir / "HEAD"]
    missing = [str(p.relative_to(repo_root)) for p in required if not p.exists()]
    if missing:
        warn(f"Missing repository structure: {', '.join(missing)}")
    else:
        ok(f"Repository found at {av_dir}")

    if aether_core:
        ok("Native core (aether_core) loaded — hashing runs at full speed")
    else:
        warn("Native core (aether_core) not loaded — falling back to slower Python hashing")

    if client.server_available():
        ok(f"Remote server {cfg.get('remote_url')} reachable")
    else:
        warn(f"Remote server {cfg.get('remote_url')} unreachable — commits will queue locally")

    idx = Index(repo_root)
    ok(f"Index (.av/index) is valid JSON, {len(idx.entries)} entries")

    # --- Orphaned pointer entries: index entry has a pointer but its CAS object is missing ---
    # An entry with a pointer but no matching CAS object means `av checkout` would silently
    # fail to materialize that file's real content.
    orphaned = [
        rel_path
        for rel_path, entry in idx.entries.items()
        if entry.get("pointer")
        and not (av_dir / "objects" / entry["hash"][:2] / entry["hash"][2:]).exists()
    ]
    recovered_orphans = []
    unrecovered_orphans = []
    for rel_path in orphaned:
        h = idx.entries[rel_path]["hash"]
        obj_path = av_dir / "objects" / h[:2] / h[2:]
        can_recover = False
        if fix and preview:
            can_recover = client.object_exists(h)
        elif fix:
            can_recover = client.server_available() and client.download_object(h, obj_path)
        (recovered_orphans if can_recover else unrecovered_orphans).append(rel_path)

    if recovered_orphans:
        verb = "Would re-link" if preview else "Re-linked"
        for rel_path in recovered_orphans:
            fixed(f"{verb} {rel_path} by downloading its object from the remote")
    if unrecovered_orphans:
        note = " (--fix could not recover: not available locally or on the remote)" if fix else ""
        warn(f"{len(unrecovered_orphans)} pointer entry(ies) missing their object: {', '.join(unrecovered_orphans)}{note}")
    elif not orphaned:
        pointer_count = sum(1 for e in idx.entries.values() if e.get("pointer"))
        ok(f"No orphaned pointer entries ({pointer_count} pointer(s), all matched)")

    # --- Stale .av-pointer files: pointer file on disk with no corresponding tracked entry ---
    # Left behind by a manual delete/rename of the original file outside of `av`.
    stale_pointers = []  # list of (pointer_rel_path, original_rel_path, pointer_abs_path)
    for dirpath, dirnames, files in os.walk(repo_root):
        dirnames[:] = [d for d in dirnames if d not in _IGNORED_DIRS]
        for f in files:
            if not f.endswith(".av-pointer"):
                continue
            ptr_path = Path(dirpath) / f
            original_rel = str(ptr_path.relative_to(repo_root))[: -len(".av-pointer")].replace("\\", "/")
            if original_rel not in idx.entries:
                ptr_rel = str(ptr_path.relative_to(repo_root)).replace("\\", "/")
                stale_pointers.append((ptr_rel, original_rel, ptr_path))

    recovered_stale = []
    unrecovered_stale = []
    for ptr_rel, original_rel, ptr_path in stale_pointers:
        try:
            parsed = parse_pointer(ptr_path.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            parsed = None
        if not parsed:
            note = " (--fix could not parse this pointer file)" if fix else ""
            unrecovered_stale.append(f"{ptr_rel}{note}")
            continue

        h, size = parsed["hash"], parsed["size"]
        obj_path = av_dir / "objects" / h[:2] / h[2:]
        object_available = obj_path.exists()

        if not object_available and fix:
            if preview:
                object_available = client.object_exists(h)
            elif client.server_available() and client.download_object(h, obj_path):
                object_available = True

        if object_available and fix:
            if not preview:
                idx.add_entry(original_rel, h, size, 0, "artifact", ptr_rel)
            recovered_stale.append(ptr_rel)
        elif object_available:
            unrecovered_stale.append(ptr_rel)
        else:
            note = " (--fix could not recover: object missing locally and on the remote)" if fix else ""
            unrecovered_stale.append(f"{ptr_rel}{note}")

    if recovered_stale:
        verb = "Would re-link" if preview else "Re-linked"
        for ptr_rel in recovered_stale:
            fixed(f"{verb} {ptr_rel} back into the index")
    if unrecovered_stale:
        warn(f"{len(unrecovered_stale)} stale .av-pointer file(s) with no tracked entry: {', '.join(unrecovered_stale)}")

    # --- Pending-push queue ---
    pending = load_pending_push(repo_root)
    if not fix:
        if pending:
            warn(f"{len(pending)} commit(s) pending push (.av/pending_push) — run `av push` once the server is back")
        else:
            ok("No commits pending push")
    else:
        commits_dir = av_dir / "commits"
        recoverable_pending = []
        unrecoverable_pending = []
        for entry in pending:
            if (commits_dir / f"{entry['commit_hash']}.json").exists():
                recoverable_pending.append(entry)
            else:
                unrecoverable_pending.append(entry)

        if unrecoverable_pending:
            verb = "Would clear" if preview else "Cleared"
            fixed(f"{verb} {len(unrecoverable_pending)} unrecoverable pending-push entry(ies) (commit object missing locally)")
            if not preview:
                save_pending_push(repo_root, recoverable_pending)

        if recoverable_pending:
            if preview:
                if client.server_available():
                    fixed(f"Would retry pushing {len(recoverable_pending)} remaining pending commit(s) (server reachable)")
                else:
                    warn(f"{len(recoverable_pending)} commit(s) pending push (.av/pending_push) — run `av push` once the server is back")
            elif client.server_available():
                still_pending = flush_pending_push(repo_root, client)
                pushed = len(recoverable_pending) - len(still_pending)
                if pushed:
                    fixed(f"Retried pending push queue: pushed {pushed}, {len(still_pending)} still pending")
                if still_pending:
                    warn(f"{len(still_pending)} commit(s) pending push (.av/pending_push) — run `av push` once the server is back")
            else:
                warn(f"{len(recoverable_pending)} commit(s) pending push (.av/pending_push) — run `av push` once the server is back")
        elif not pending:
            ok("No commits pending push")

    # --- *.tmp.* leftovers from an interrupted atomic write ---
    tmp_leftovers = list(av_dir.rglob("*.tmp.*"))
    if tmp_leftovers:
        names = [str(p.relative_to(repo_root)) for p in tmp_leftovers]
        if fix:
            verb = "Would remove" if preview else "Removed"
            fixed(f"{verb} {len(tmp_leftovers)} leftover temp file(s): {', '.join(names)}")
            if not preview:
                for p in tmp_leftovers:
                    p.unlink(missing_ok=True)
        else:
            warn(f"{len(tmp_leftovers)} leftover temp file(s) from an interrupted write: {', '.join(names)}")
    else:
        ok("No *.tmp.* leftover files in .av/")

    click.echo("")
    suffix = " (dry run — nothing was changed)" if preview else ""
    if fixed_count and warning_count:
        verb = "would be fixed" if preview else "fixed"
        click.secho(f"{fixed_count} issue(s) {verb}, {warning_count} warning(s) remain, 0 errors.{suffix}", fg="cyan", bold=True)
    elif fixed_count:
        verb = "would be fixed" if preview else "fixed"
        click.secho(f"{fixed_count} issue(s) {verb}, 0 warnings remain, 0 errors.{suffix}", fg="cyan", bold=True)
    elif warning_count:
        click.secho(f"{warning_count} warning(s), 0 errors.{suffix}", fg="yellow", bold=True)
    else:
        click.secho(f"Everything looks good.{suffix}", fg="green", bold=True)


@cli.command(name="test")
@click.option("-k", "test_filter", default=None, help="Only run tests matching this substring (forwarded to pytest -k).")
@click.option("--cov", is_flag=True, default=False, help="Run with a coverage report (--cov=python --cov-report=term-missing).")
def test_cmd(test_filter: str | None, cov: bool) -> None:
    """(Development only) Run Aether-Vault's own pytest suite from source.

    Requires an editable/dev install (`pip install -e .[dev]`) — this is not a tool for
    inspecting an end user's .av/ repository; see `av doctor` for that.
    """
    source_root = _find_source_root()
    tests_dir = source_root / "tests"
    if not tests_dir.is_dir():
        click.secho(
            "av test requires a development install; run from a git clone with `pip install -e .[dev]`",
            fg="red",
        )
        sys.exit(1)

    args = [sys.executable, "-m", "pytest", str(tests_dir)]
    if test_filter:
        args += ["-k", test_filter]
    if cov:
        args += ["--cov=python", "--cov-report=term-missing"]

    click.secho(f"Running Aether-Vault's test suite (pytest {' '.join(args[3:])})...", fg="cyan")
    result = subprocess.run(args, cwd=source_root)
    sys.exit(result.returncode)


@cli.command()
@click.option("--update", is_flag=True, help="Regenerate and update the markdown vault.")
def graph(update: bool) -> None:
    """Generate or update a markdown vault of code dependencies for Obsidian."""
    repo_root = ensure_repo()
    vault_dir = repo_root / "Aether-Graph"
    
    if update or not vault_dir.exists():
        click.echo(f"Generating graph vault at {vault_dir}...")
        vault_dir.mkdir(exist_ok=True)
        from .graph import generate_full_graph
        generate_full_graph(repo_root, vault_dir)
        click.secho("Graph vault updated successfully.", fg="green")
    
    if not update:
        click.secho(f"To visualize, open the following folder as a vault in Obsidian:\n  {vault_dir.resolve()}", fg="cyan")
        import webbrowser
        import urllib.parse
        encoded_path = urllib.parse.quote(str(vault_dir.resolve()))
        obsidian_uri = f"obsidian://open?path={encoded_path}"
        try:
            webbrowser.open(obsidian_uri)
            click.echo("Attempted to launch Obsidian.")
        except Exception:
            pass


@cli.group(invoke_without_command=True)
@click.option("--update", is_flag=True, help="Update the existing handoff.avh with the latest repo state.")
@click.option("--note", default=None, help="Freeform agent instruction text.")
@click.option("--instructions-file", type=click.Path(exists=True), default=None, help="Read agent instructions from a file.")
@click.option("--diff-weights", is_flag=True, help="Include a per-layer weight-diff against the parent commit.")
@click.option("--since", default=None, help="Diff weights/metrics against this commit hash instead of the direct parent.")
@click.pass_context
def handoff(ctx: click.Context, update: bool, note: str | None, instructions_file: str | None, diff_weights: bool, since: str | None) -> None:
    """Generate (or update) a .avh agent handoff snapshot and a Markdown log entry in Aether-Handoff/."""
    if ctx.invoked_subcommand is not None:
        return

    repo_root = ensure_repo()
    from .handoff import generate_handoff
    note_text = Path(instructions_file).read_text() if instructions_file else note
    avh_path, md_path = generate_handoff(
        repo_root, update=update, agent_instructions=note_text, diff_weights=diff_weights, since=since
    )
    click.secho(f"Handoff snapshot written: {avh_path.relative_to(repo_root)}", fg="green")
    click.secho(f"Markdown log entry: {md_path.relative_to(repo_root)}", fg="cyan")


@handoff.command("init")
def handoff_init() -> None:
    """Create the Aether-Handoff/ folder structure without generating a snapshot."""
    repo_root = ensure_repo()
    from .handoff import init_handoff_dir
    vault_dir = init_handoff_dir(repo_root)
    click.secho(f"{vault_dir.relative_to(repo_root)}/ initialized.", fg="green")


@handoff.command("log")
def handoff_log() -> None:
    """List all handoff snapshots in chronological order."""
    repo_root = ensure_repo()
    snapshots_dir = repo_root / "Aether-Handoff" / "snapshots"
    if not snapshots_dir.exists():
        click.secho("No handoff snapshots yet. Run `av handoff` first.", fg="yellow")
        return

    entries = sorted(snapshots_dir.glob("*.md"))
    if not entries:
        click.secho("No handoff snapshots yet. Run `av handoff` first.", fg="yellow")
        return

    for entry in entries:
        click.echo(entry.stem)


@handoff.command("show")
@click.argument("snapshot_id")
def handoff_show(snapshot_id: str) -> None:
    """Print a previously generated handoff Markdown note."""
    repo_root = ensure_repo()
    snapshots_dir = repo_root / "Aether-Handoff" / "snapshots"
    matches = sorted(snapshots_dir.glob(f"{snapshot_id}*.md")) if snapshots_dir.exists() else []

    if not matches:
        click.secho(f"No snapshot found matching '{snapshot_id}'.", fg="red")
        return

    click.echo(matches[-1].read_text(encoding="utf-8"))


@cli.command("webui")
@click.option(
    "--rebuild", is_flag=True, default=False,
    help="Force a fresh Docker image build even if the container is already running.",
)
def webui_cmd(rebuild: bool) -> None:
    """Start the Aether-Vault Web UI dashboard and open it in the browser.

    \b
    1. Checks that Docker is running
    2. Starts the aether-vault-webui container (+ deps) via docker-compose
    3. Waits for the UI to become ready
    4. Opens http://localhost:3000 in the default browser

    If the container is already running and healthy, this skips straight to opening the
    browser instead of re-running compose (use --rebuild to force a fresh image build after
    changing webui source code).
    """
    import time
    import webbrowser
    import urllib.request
    import urllib.error

    repo_root = _find_source_root()
    compose_file = repo_root / "docker-compose.yml"
    url = "http://localhost:3000"

    # ── 1. Check Docker ──────────────────────────────────────────────────────
    click.secho("🔍 Checking Docker…", fg="cyan")
    try:
        result = subprocess.run(
            ["docker", "info"],
            capture_output=True,
            timeout=30,
        )
        if result.returncode != 0:
            click.secho(
                "✗ Docker is not running. Please start Docker Desktop and try again.",
                fg="red",
            )
            return
    except FileNotFoundError:
        click.secho("✗ Docker not found. Install Docker Desktop from https://docker.com", fg="red")
        return
    except subprocess.TimeoutExpired:
        click.secho("✗ Docker daemon timed out. Is Docker Desktop running?", fg="red")
        return

    click.secho("  ✓ Docker is running", fg="green")

    # ── 1b. Already running? Skip straight to the browser ───────────────────
    # `docker compose up -d --build` unconditionally re-evaluates/builds the image on every
    # invocation — slow, and pointless if nothing changed and the container is already
    # serving traffic. Short-circuit unless the caller explicitly wants a rebuild.
    if not rebuild:
        try:
            health = subprocess.run(
                ["docker", "inspect", "--format={{.State.Health.Status}}", "aether-vault-webui"],
                capture_output=True, text=True, timeout=10,
            )
            if health.returncode == 0 and health.stdout.strip() == "healthy":
                click.secho("  ✓ Web UI container already running and healthy", fg="green")
                click.secho(f"🌐 Opening {url} in your browser…", fg="green")
                try:
                    webbrowser.open(url)
                except Exception as exc:
                    click.secho(f"  Could not open browser automatically: {exc}", fg="yellow")
                click.secho(
                    f"\n✅ Aether-Vault Web UI is running at {url}\n"
                    "   Press Ctrl+C or run 'docker compose down' to stop.\n"
                    "   (Use `av webui --rebuild` if you changed webui source and need a fresh image.)",
                    fg="green",
                    bold=True,
                )
                return
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass  # fall through to the normal startup path below

    # ── 2. Start containers ──────────────────────────────────────────────────
    click.secho("🚀 Starting Web UI container…", fg="cyan")

    if not compose_file.exists():
        click.secho(f"✗ docker-compose.yml not found at {compose_file}", fg="red")
        return

    compose_args = ["docker", "compose", "-f", str(compose_file), "up", "-d"]
    if rebuild:
        compose_args.append("--build")
    compose_args.append("aether-vault-webui")

    try:
        proc = subprocess.run(
            compose_args,
            capture_output=False,
            timeout=1200,
        )
        if proc.returncode != 0:
            click.secho("✗ Failed to start containers. Check docker compose logs for details.", fg="red")
            return
    except subprocess.TimeoutExpired:
        click.secho("✗ Container startup timed out.", fg="red")
        return

    # ── 3. Wait for UI to be ready ───────────────────────────────────────────
    click.secho(f"⏳ Waiting for Web UI at {url}…", fg="cyan")

    for attempt in range(30):
        time.sleep(2)
        try:
            urllib.request.urlopen(url, timeout=3)
            break
        except (urllib.error.URLError, OSError):
            click.echo(f"  … waiting ({attempt + 1}/30)")
    else:
        click.secho("⚠ Web UI did not respond in time — opening browser anyway.", fg="yellow")

    # ── 4. Open browser ──────────────────────────────────────────────────────
    click.secho(f"🌐 Opening {url} in your browser…", fg="green")
    try:
        webbrowser.open(url)
    except Exception as exc:
        click.secho(f"  Could not open browser automatically: {exc}", fg="yellow")

    click.secho(
        f"\n✅ Aether-Vault Web UI is running at {url}\n"
        "   Press Ctrl+C or run 'docker compose down' to stop.",
        fg="green",
        bold=True,
    )


@cli.command("import-lightning")
@click.argument("checkpoint_path", type=click.Path(exists=True))
@click.option("--tag", default=None, help="Additional tag to attach to the imported commit.")
def import_lightning(checkpoint_path: str, tag: str | None) -> None:
    """Backfill a pre-existing PyTorch Lightning checkpoint not captured live by the callback."""
    repo_root = ensure_repo()
    from av_plugins.lightning import import_checkpoint
    import_checkpoint(checkpoint_path, repo_root=repo_root, tag=tag)
    click.secho(f"Imported Lightning checkpoint: {checkpoint_path}", fg="green")


@cli.command("import-transformers")
@click.argument("checkpoint_dir", type=click.Path(exists=True, file_okay=False))
@click.option("--tag", default=None, help="Additional tag to attach to the imported commit.")
def import_transformers(checkpoint_dir: str, tag: str | None) -> None:
    """Backfill a pre-existing HuggingFace Transformers checkpoint directory."""
    repo_root = ensure_repo()
    from av_plugins.transformers import import_checkpoint
    import_checkpoint(checkpoint_dir, repo_root=repo_root, tag=tag)
    click.secho(f"Imported Transformers checkpoint: {checkpoint_dir}", fg="green")


@cli.command("import-mlflow")
@click.argument("run_id")
@click.option("--tracking-uri", default=None, help="MLflow tracking URI (defaults to MLflow's own resolution).")
@click.option("--tag", default=None, help="Additional tag to attach to the imported commit.")
def import_mlflow(run_id: str, tracking_uri: str | None, tag: str | None) -> None:
    """Import an existing MLflow run's artifacts and metrics as a new Aether-Vault commit."""
    repo_root = ensure_repo()
    from av_plugins.mlflow import import_run
    import_run(run_id, repo_root=repo_root, tracking_uri=tracking_uri, tag=tag)
    click.secho(f"Imported MLflow run: {run_id}", fg="green")


if __name__ == "__main__":
    cli()

