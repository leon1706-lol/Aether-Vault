import datetime
import hashlib
import json
import logging
import os
import shutil
import sys
from pathlib import Path

import click

try:
    import aether_core
except ImportError:
    aether_core = None

from .client import VaultClient
from .exceptions import AetherVaultException, NetworkError, StorageError, ValidationError
from .index import Index
from .pointer import create_pointer, get_pointer_path, is_pointer_file

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

def find_repo_root() -> Path | None:
    cwd = Path.cwd().resolve()
    for parent in [cwd] + list(cwd.parents):
        if (parent / ".av").is_dir():
            return parent
    return None


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
                return json.load(f)
        except (json.JSONDecodeError, OSError) as exc:
            print(f"Warning: Failed to load config, using defaults: {exc}", file=sys.stderr)
    return {"lfs_threshold_mb": 50, "remote_url": "http://localhost:8000"}


def save_config(repo_root: Path, config: dict) -> None:
    config_path = repo_root / ".av" / "config"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    with open(config_path, "w") as f:
        json.dump(config, f, indent=2)


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
    with open(repo_root / ".av" / "registry.json", "w") as f:
        json.dump(reg, f, indent=2)


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


def get_file_meta_safe(path: str) -> dict:
    if aether_core:
        try:
            return aether_core.get_file_metadata(path)
        except Exception as exc:
            print(
                f"Warning: aether_core.get_file_metadata failed, using Python fallback: {exc}",
                file=sys.stderr,
            )
    p = Path(path)
    if not p.exists():
        return {"exists": False, "size": 0, "mtime_ns": 0}
    stat = p.stat()
    return {"exists": True, "size": stat.st_size, "mtime_ns": stat.st_mtime_ns}


def compare_meta_safe(path: str, exp_size: int, exp_mtime: int) -> bool:
    if aether_core:
        try:
            return aether_core.compare_metadata(path, exp_size, exp_mtime)
        except Exception as exc:
            print(
                f"Warning: aether_core.compare_metadata failed, using Python fallback: {exc}",
                file=sys.stderr,
            )
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

    save_config(repo_root, {"lfs_threshold_mb": 50, "remote_url": "http://localhost:8000"})

    idx = Index(repo_root)
    idx.save()

    with open(av_dir / "HEAD", "w") as f:
        f.write("ref: refs/heads/main\n")

    with open(av_dir / "refs" / "heads" / "main", "w") as f:
        f.write("")

    click.secho(f"Initialized empty Aether-Vault repository in {av_dir}", fg="green")


@cli.command()
@click.argument("value", type=int)
def config(value: int) -> None:
    """Set the LFS threshold in MB."""
    repo_root = ensure_repo()
    cfg = load_config(repo_root)
    cfg["lfs_threshold_mb"] = value
    save_config(repo_root, cfg)
    click.secho(f"Configured LFS threshold to {value} MB", fg="green")


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
            for root, _, files in os.walk(path_obj):
                if ".av" in root or ".git" in root or "__pycache__" in root:
                    continue
                for f in files:
                    if not f.endswith(".pyc"):
                        files_to_process.append(Path(root) / f)

    for fpath in files_to_process:
        rel_path = str(fpath.relative_to(repo_root)).replace("\\", "/")
        if is_pointer_file(fpath):
            continue

        meta = get_file_meta_safe(str(fpath))
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
                            with open(fpath, "rb") as src_f:
                                src_f.seek(l_offset)
                                with open(l_obj_path, "wb") as dst_f:
                                    dst_f.write(src_f.read(l_size))
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
                idx.entries[rel_path]["layers"] = layers
            click.secho(f"Staged [ARTIFACT] {rel_path} (LFS, {len(layers)} layers)", fg="green")
        else:
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
    for root, _, files in os.walk(repo_root):
        if ".av" in root or ".git" in root or "__pycache__" in root:
            continue
        for f in files:
            if not f.endswith(".pyc") and not f.endswith(".av-pointer"):
                disk_files.add(
                    str((Path(root) / f).relative_to(repo_root)).replace("\\", "/")
                )

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
    }

    # Deterministic hash over sorted JSON (preserves DAG integrity)
    commit_str = json.dumps(commit_data, sort_keys=True)
    commit_hash = hashlib.sha256(commit_str.encode()).hexdigest()
    commit_data["hash"] = commit_hash

    # --- Persist locally ---
    with open(repo_root / ".av" / "commits" / f"{commit_hash}.json", "w") as f:
        json.dump(commit_data, f, indent=2)

    if ref_path:
        with open(ref_path, "w") as f:
            f.write(commit_hash)
    else:
        with open(head_path, "w") as f:
            f.write(commit_hash)

    idx.clear_staged()
    click.secho(f"[{commit_hash[:7]}] {message}", fg="green")
    if tags:
        click.secho(f"  Tags: {', '.join(tags)}", fg="cyan")
    if metrics:
        click.secho(f"  Metrics: {metrics}", fg="cyan")

    # --- Push to remote if available ---
    if client.server_available():
        client.push_commit(commit_data)
        if ref_path:
            client.update_ref(ref_path.name, commit_hash)

        for rel_path, info in tree.items():
            if info["type"] == "artifact":
                # Upload individual layer shards first (PR #8)
                for layer in info.get("layers", []):
                    l_hash = layer["hash"]
                    l_obj = repo_root / ".av" / "objects" / l_hash[:2] / l_hash[2:]
                    if l_obj.exists():
                        client.upload_object(l_obj, l_hash)
                # Upload the whole-file object
                obj_file = repo_root / ".av" / "objects" / info["hash"][:2] / info["hash"][2:]
                if obj_file.exists():
                    client.upload_object(obj_file, info["hash"])


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
def checkout(target: str) -> None:
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

            if file_type == "artifact":
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


if __name__ == "__main__":
    cli()
