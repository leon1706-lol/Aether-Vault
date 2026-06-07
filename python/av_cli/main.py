import os
import json
import click
import hashlib
import shutil
import datetime
import logging
from pathlib import Path

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
    if silent:
        logger.setLevel(logging.CRITICAL)
    elif verbose:
        logger.setLevel(logging.DEBUG)
        logging.basicConfig(level=logging.DEBUG, format='%(levelname)s: %(message)s')
    else:
        logger.setLevel(logging.INFO)
        logging.basicConfig(level=logging.INFO, format='%(message)s')

def find_repo_root() -> Path | None:
    cwd = Path.cwd().resolve()
    for parent in [cwd] + list(cwd.parents):
        if (parent / '.av').is_dir():
            return parent
    return None

def ensure_repo() -> Path:
    repo_root = find_repo_root()
    if not repo_root:
        raise ValidationError("Not an Aether-Vault repository (or any of the parent directories).")
    return repo_root

def load_config(repo_root: Path) -> dict:
    config_path = repo_root / '.av' / 'config'
    if config_path.exists():
        try:
            with open(config_path, 'r') as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"Failed to load config, using defaults: {e}")
    return {"lfs_threshold_mb": 50, "remote_url": "http://localhost:8000"}

def save_config(repo_root: Path, config: dict) -> None:
    config_path = repo_root / '.av' / 'config'
    config_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(config_path, 'w') as f:
            json.dump(config, f, indent=2)
    except OSError as e:
        raise StorageError(f"Failed to save configuration: {e}")

def hash_file_safe(path: str) -> str:
    if aether_core:
        try:
            return aether_core.hash_file(path)
        except Exception as e:
            logger.debug(f"aether_core.hash_file failed, using Python fallback: {e}")
    
    sha256 = hashlib.sha256()
    try:
        with open(path, 'rb') as f:
            while chunk := f.read(8 * 1024 * 1024):
                sha256.update(chunk)
    except OSError as e:
        raise StorageError(f"Failed to read file for hashing: {path} ({e})")
    return sha256.hexdigest()

@click.group()
@click.option('--verbose', is_flag=True, help="Enable verbose output")
@click.option('--silent', is_flag=True, help="Suppress all output except critical errors")
def cli(verbose, silent):
    """Aether-Vault CLI: High-performance version control for ML."""
    setup_logging(verbose, silent)

@cli.command()
def init():
    """Initialize a new repository"""
    repo_root = Path.cwd()
    av_dir = repo_root / '.av'
    if av_dir.exists():
        click.secho(f"Repository already initialized at {av_dir}", fg='yellow')
        return

    try:
        (av_dir / 'objects').mkdir(parents=True, exist_ok=True)
        (av_dir / 'refs' / 'heads').mkdir(parents=True, exist_ok=True)
        (av_dir / 'commits').mkdir(parents=True, exist_ok=True)
        
        save_config(repo_root, {"lfs_threshold_mb": 50, "remote_url": "http://localhost:8000"})
        
        idx = Index(repo_root)
        idx.save()
        
        with open(av_dir / 'HEAD', 'w') as f:
            f.write('ref: refs/heads/main\n')
            
        with open(av_dir / 'refs' / 'heads' / 'main', 'w') as f:
            f.write('')
            
        click.secho(f"Initialized empty Aether-Vault repository in {av_dir}", fg='green')
    except OSError as e:
        raise StorageError(f"Failed to initialize repository: {e}")

@cli.command()
@click.argument('value', type=int)
def config(value):
    """Set the LFS threshold in MB"""
    repo_root = ensure_repo()
    cfg = load_config(repo_root)
    cfg["lfs_threshold_mb"] = value
    save_config(repo_root, cfg)
    click.secho(f"Configured LFS threshold to {value} MB", fg='green')

@cli.command()
@click.argument('paths', nargs=-1, type=click.Path(exists=True))
def add(paths):
    """Add files to the staging index"""
    repo_root = ensure_repo()
    idx = Index(repo_root)
    cfg = load_config(repo_root)
    threshold_bytes = cfg.get("lfs_threshold_mb", 50) * 1024 * 1024
    
    files_to_process = []
    for p in paths:
        path_obj = Path(p).resolve()
        if path_obj.is_file():
            files_to_process.append(path_obj)
        elif path_obj.is_dir():
            for root, _, files in os.walk(path_obj):
                if '.av' in root or '.git' in root or '__pycache__' in root:
                    continue
                for f in files:
                    if not f.endswith('.pyc'):
                        files_to_process.append(Path(root) / f)
                        
    for fpath in files_to_process:
        try:
            rel_path = str(fpath.relative_to(repo_root)).replace('\\', '/')
            if is_pointer_file(fpath):
                continue
                
            file_hash = hash_file_safe(str(fpath))
            stat = fpath.stat()
            file_type = idx.classify_file(rel_path)
            
            pointer_rel_path = None
            
            if file_type == 'artifact' and stat.st_size > threshold_bytes:
                layers = []
                # Safetensors Layer-Splitting
                if rel_path.endswith('.safetensors') and aether_core:
                    logger.info(f"Processing Safetensors layers for {rel_path}...")
                    layer_results = aether_core.split_and_hash_safetensors(str(fpath))
                    for lr in layer_results:
                        l_hash = lr["hash"]
                        l_size = lr["size"]
                        l_offset = lr["offset"]
                        
                        # Store layer shard in CAS
                        l_obj_dir = repo_root / '.av' / 'objects' / l_hash[:2]
                        l_obj_dir.mkdir(parents=True, exist_ok=True)
                        l_obj_path = l_obj_dir / l_hash[2:]
                        
                        if not l_obj_path.exists():
                            # Extract shard from original file
                            with open(fpath, 'rb') as src_f:
                                src_f.seek(l_offset)
                                with open(l_obj_path, 'wb') as dest_f:
                                    dest_f.write(src_f.read(l_size))
                        
                        layers.append({"name": lr["name"], "hash": l_hash, "size": l_size})
                
                obj_dir = repo_root / '.av' / 'objects' / file_hash[:2]
                obj_dir.mkdir(parents=True, exist_ok=True)
                obj_path = obj_dir / file_hash[2:]
                
                if not obj_path.exists():
                    shutil.copy2(fpath, obj_path)
                    
                ptr_path = get_pointer_path(fpath)
                ptr_content = create_pointer(fpath, file_hash, stat.st_size)
                with open(ptr_path, 'w') as ptr_f:
                    ptr_f.write(ptr_content)
                    
                pointer_rel_path = rel_path + ".av-pointer"
                idx.add_entry(rel_path, file_hash, stat.st_size, stat.st_mtime_ns, file_type, pointer_rel_path)
                # Store layers in index entry for commit
                if layers:
                    idx.entries[rel_path]["layers"] = layers
                logger.info(f"Staged [ARTIFACT] {rel_path} (LFS) with {len(layers)} layers")
            else:
                idx.add_entry(rel_path, file_hash, stat.st_size, stat.st_mtime_ns, file_type, None)
                logger.info(f"Staged [{file_type.upper()}] {rel_path}")
        except Exception as e:
            logger.error(f"Failed to add {fpath}: {e}")

@cli.command()
def status():
    """Show working tree status"""
    repo_root = ensure_repo()
    idx = Index(repo_root)
    
    head_path = repo_root / '.av' / 'HEAD'
    branch = "detached"
    if head_path.exists():
        head_content = head_path.read_text().strip()
        if head_content.startswith("ref: refs/heads/"):
            branch = head_content.split("/")[-1]
            
    click.secho(f"On branch {branch}\n", bold=True)
    
    staged = []
    modified = []
    deleted = []
    untracked = []
    
    disk_files = set()
    for root, _, files in os.walk(repo_root):
        if '.av' in root or '.git' in root or '__pycache__' in root:
            continue
        for f in files:
            if not f.endswith('.pyc') and not f.endswith('.av-pointer'):
                disk_files.add(str((Path(root) / f).relative_to(repo_root)).replace('\\', '/'))
                
    for rel_path, entry in idx.entries.items():
        if rel_path not in disk_files:
            deleted.append(rel_path)
        else:
            if entry.get("staged"):
                staged.append(rel_path)
            else:
                stat = (repo_root / rel_path).stat()
                if stat.st_size != entry["size"] or stat.st_mtime_ns != entry["mtime_ns"]:
                    modified.append(rel_path)
                
    for rel_path in disk_files:
        if rel_path not in idx.entries:
            untracked.append(rel_path)
            
    if staged:
        click.secho("Changes to be committed:", fg='green')
        for f in staged: click.echo(f"  modified: {f}")
        click.echo("")
    if modified:
        click.secho("Changes not staged for commit:", fg='yellow')
        for f in modified: click.echo(f"  modified: {f}")
        click.echo("")
    if deleted:
        click.secho("Deleted files:", fg='red')
        for f in deleted: click.echo(f"  deleted:  {f}")
        click.echo("")
    if untracked:
        click.secho("Untracked files:", fg='red')
        for f in untracked: click.echo(f"  {f}")
        click.echo("")
        
    if not staged and not modified and not deleted and not untracked:
        click.secho("Nothing to commit, working tree clean", fg='green')

@cli.command()
@click.option('-m', '--message', required=True, help="Commit message")
@click.option('--metric-sharpe', type=float, help="Sharpe ratio")
@click.option('--metric-drawdown', type=float, help="Max drawdown")
def commit(message, metric_sharpe, metric_drawdown):
    """Record changes to the repository"""
    repo_root = ensure_repo()
    idx = Index(repo_root)
    cfg = load_config(repo_root)
    client = VaultClient(cfg.get("remote_url", "http://localhost:8000"))
    
    staged = idx.get_staged_entries()
    if not staged:
        click.secho("Nothing to commit", fg='yellow')
        return
        
    tree = {}
    for rel_path, e in idx.entries.items():
        tree[rel_path] = {
            "hash": e["hash"], 
            "size": e["size"], 
            "type": e["type"],
            "layers": e.get("layers", [])
        }
            
    head_path = repo_root / '.av' / 'HEAD'
    parents = []
    ref_path = None
    if head_path.exists():
        head_content = head_path.read_text().strip()
        if head_content.startswith("ref: "):
            ref_path = repo_root / '.av' / head_content.split(": ", 1)[1]
            if ref_path.exists() and ref_path.read_text().strip():
                parents.append(ref_path.read_text().strip())
        else:
            parents.append(head_content)
            
    commit_data = {
        "parents": parents,
        "author": os.environ.get("AV_AUTHOR", "anonymous"),
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "message": message,
        "tree": tree,
        "metrics": {}
    }
    if metric_sharpe is not None: commit_data["metrics"]["sharpe"] = metric_sharpe
    if metric_drawdown is not None: commit_data["metrics"]["drawdown"] = metric_drawdown
    
    commit_str = json.dumps(commit_data, sort_keys=True)
    commit_hash = hashlib.sha256(commit_str.encode()).hexdigest()
    commit_data["hash"] = commit_hash
    
    try:
        with open(repo_root / '.av' / 'commits' / f"{commit_hash}.json", 'w') as f:
            json.dump(commit_data, f, indent=2)
            
        if ref_path:
            with open(ref_path, 'w') as f:
                f.write(commit_hash)
        else:
            with open(head_path, 'w') as f:
                f.write(commit_hash)
                
        idx.clear_staged()
        click.secho(f"[{commit_hash[:7]}] {message}", fg='green')
        
        if client.server_available():
            client.push_commit(commit_data)
            if ref_path:
                client.update_ref(ref_path.name, commit_hash)
                
            for rel_path, info in tree.items():
                if info["type"] == "artifact":
                    # Upload individual layers if they exist
                    if info.get("layers"):
                        for layer in info["layers"]:
                            l_hash = layer["hash"]
                            l_obj_file = repo_root / '.av' / 'objects' / l_hash[:2] / l_hash[2:]
                            if l_obj_file.exists():
                                client.upload_object(l_obj_file, l_hash)
                    
                    # Also upload the full file object (or pointer reference)
                    obj_file = repo_root / '.av' / 'objects' / info["hash"][:2] / info["hash"][2:]
                    if obj_file.exists():
                        client.upload_object(obj_file, info["hash"])
    except Exception as e:
        raise StorageError(f"Commit failed: {e}")

if __name__ == '__main__':
    cli()
