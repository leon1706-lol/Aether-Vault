import os
import json
import click
import hashlib
import shutil
import datetime
import logging
from pathlib import Path
from tabulate import tabulate

try:
    import aether_core
except ImportError:
    aether_core = None

from .index import Index
from .pointer import create_pointer, is_pointer_file, get_pointer_path
from .client import VaultClient
from .exceptions import AetherVaultException, ValidationError, StorageError, NetworkError

# Global logger setup
logger = logging.getLogger("av")

def setup_logging(verbose: bool, silent: bool):
    if silent: logger.setLevel(logging.CRITICAL)
    elif verbose:
        logger.setLevel(logging.DEBUG)
        logging.basicConfig(level=logging.DEBUG, format='%(levelname)s: %(message)s')
    else:
        logger.setLevel(logging.INFO)
        logging.basicConfig(level=logging.INFO, format='%(message)s')

def find_repo_root() -> Path | None:
    cwd = Path.cwd().resolve()
    for parent in [cwd] + list(cwd.parents):
        if (parent / '.av').is_dir(): return parent
    return None

def ensure_repo() -> Path:
    repo_root = find_repo_root()
    if not repo_root: raise ValidationError("Not an Aether-Vault repository.")
    return repo_root

def load_config(repo_root: Path) -> dict:
    config_path = repo_root / '.av' / 'config'
    if config_path.exists():
        try:
            with open(config_path, 'r') as f: return json.load(f)
        except: pass
    return {"lfs_threshold_mb": 50, "remote_url": "http://localhost:8000"}

def save_config(repo_root: Path, config: dict) -> None:
    config_path = repo_root / '.av' / 'config'
    config_path.parent.mkdir(parents=True, exist_ok=True)
    with open(config_path, 'w') as f: json.dump(config, f, indent=2)

def load_registry(repo_root: Path) -> dict:
    reg_path = repo_root / '.av' / 'registry.json'
    if reg_path.exists():
        with open(reg_path, 'r') as f: return json.load(f)
    return {"tags": [], "metrics": []}

def update_registry(repo_root: Path, tags: list, metrics: dict):
    reg = load_registry(repo_root)
    reg["tags"] = list(set(reg["tags"] + tags))
    reg["metrics"] = list(set(reg["metrics"] + list(metrics.keys())))
    with open(repo_root / '.av' / 'registry.json', 'w') as f: json.dump(reg, f, indent=2)

def hash_file_safe(path: str) -> str:
    if aether_core:
        try: return aether_core.hash_file(path)
        except: pass
    sha256 = hashlib.sha256()
    with open(path, 'rb') as f:
        while chunk := f.read(8 * 1024 * 1024): sha256.update(chunk)
    return sha256.hexdigest()

@click.group()
@click.option('--verbose', is_flag=True)
@click.option('--silent', is_flag=True)
def cli(verbose, silent):
    """Aether-Vault CLI: High-performance version control for ML."""
    setup_logging(verbose, silent)

@cli.command()
def init():
    """Initialize a new repository"""
    repo_root = Path.cwd()
    av_dir = repo_root / '.av'
    if av_dir.exists(): return
    (av_dir / 'objects').mkdir(parents=True, exist_ok=True)
    (av_dir / 'refs' / 'heads').mkdir(parents=True, exist_ok=True)
    (av_dir / 'commits').mkdir(parents=True, exist_ok=True)
    save_config(repo_root, {"lfs_threshold_mb": 50, "remote_url": "http://localhost:8000"})
    Index(repo_root).save()
    with open(av_dir / 'HEAD', 'w') as f: f.write('ref: refs/heads/main\n')
    with open(av_dir / 'refs' / 'heads' / 'main', 'w') as f: f.write('')
    click.secho("Initialized Aether-Vault repository", fg='green')

@cli.command()
@click.argument('paths', nargs=-1, type=click.Path(exists=True))
def add(paths):
    repo_root = ensure_repo()
    idx = Index(repo_root)
    cfg = load_config(repo_root)
    threshold = cfg.get("lfs_threshold_mb", 50) * 1024 * 1024
    for p in paths:
        path_obj = Path(p).resolve()
        if path_obj.is_file():
            rel_path = str(path_obj.relative_to(repo_root)).replace('\\', '/')
            h = hash_file_safe(str(path_obj))
            size = path_obj.stat().st_size
            ftype = idx.classify_file(rel_path)
            idx.add_entry(rel_path, h, size, path_obj.stat().st_mtime_ns, ftype, None)
            click.echo(f"Staged {rel_path}")

@cli.command()
@click.option('-m', '--message', required=True)
@click.option('--tag', 'tags', multiple=True, help="Custom tags for the commit")
@click.option('--metric', 'metrics_raw', multiple=True, help="Metrics in key=value format")
@click.option('--metric-sharpe', type=float, help="Legacy Sharpe ratio support")
def commit(message, tags, metrics_raw, metric_sharpe):
    repo_root = ensure_repo()
    idx = Index(repo_root)
    cfg = load_config(repo_root)
    client = VaultClient(cfg.get("remote_url", "http://localhost:8000"))
    
    metrics = {}
    for m in metrics_raw:
        if '=' in m:
            k, v = m.split('=', 1)
            try: metrics[k] = float(v) if '.' in v else int(v)
            except: metrics[k] = v
    if metric_sharpe is not None: metrics["sharpe"] = metric_sharpe

    update_registry(repo_root, list(tags), metrics)
    
    tree = {p: {"hash": e["hash"], "size": e["size"], "type": e["type"]} for p, e in idx.entries.items()}
    commit_data = {
        "hash": hashlib.sha256(str(datetime.datetime.now()).encode()).hexdigest(),
        "message": message,
        "author": os.environ.get("AV_AUTHOR", "anonymous"),
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "tags": list(tags),
        "metrics": metrics,
        "tree": tree
    }
    
    if client.server_available():
        client.push_commit(commit_data)
        click.secho(f"Committed: {commit_data['hash'][:7]}", fg='green')

@cli.command()
def list_meta():
    """List all registered tags and metrics keys."""
    repo_root = ensure_repo()
    reg = load_registry(repo_root)
    
    click.secho("\n--- Registered Metadata ---", bold=True)
    if reg["tags"]:
        click.echo("\nTags:")
        click.echo(tabulate([[t] for t in sorted(reg["tags"])], headers=["Tag Name"], tablefmt="grid"))
    if reg["metrics"]:
        click.echo("\nMetrics Keys:")
        click.echo(tabulate([[m] for m in sorted(reg["metrics"])], headers=["Metric Key"], tablefmt="grid"))
    if not reg["tags"] and not reg["metrics"]:
        click.echo("No metadata registered yet.")

@cli.command()
def gc():
    repo_root = ensure_repo()
    cfg = load_config(repo_root)
    client = VaultClient(cfg.get("remote_url", "http://localhost:8000"))
    result = client.run_gc()
    if result: click.secho("GC Success!", fg='green')

if __name__ == '__main__': cli()
