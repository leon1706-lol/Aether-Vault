import os
import json
import click
import hashlib
import shutil
import datetime
from pathlib import Path

try:
    import aether_core
except ImportError:
    aether_core = None

from .index import Index
from .pointer import create_pointer, is_pointer_file, get_pointer_path
from .client import VaultClient

def find_repo_root() -> Path | None:
    cwd = Path.cwd().resolve()
    for parent in [cwd] + list(cwd.parents):
        if (parent / '.av').is_dir():
            return parent
    return None

def ensure_repo() -> Path:
    repo_root = find_repo_root()
    if not repo_root:
        click.secho("Error: Not an Aether-Vault repository (or any of the parent directories).", fg='red')
        raise click.Abort()
    return repo_root

def load_config(repo_root: Path) -> dict:
    config_path = repo_root / '.av' / 'config'
    if config_path.exists():
        try:
            with open(config_path, 'r') as f:
                return json.load(f)
        except:
            pass
    return {"lfs_threshold_mb": 50, "remote_url": "http://localhost:8000"}

def save_config(repo_root: Path, config: dict) -> None:
    config_path = repo_root / '.av' / 'config'
    config_path.parent.mkdir(parents=True, exist_ok=True)
    with open(config_path, 'w') as f:
        json.dump(config, f, indent=2)

def hash_file_safe(path: str) -> str:
    if aether_core:
        try:
            return aether_core.hash_file(path)
        except:
            pass
    sha256 = hashlib.sha256()
    with open(path, 'rb') as f:
        while chunk := f.read(8 * 1024 * 1024):
            sha256.update(chunk)
    return sha256.hexdigest()

def get_file_meta_safe(path: str) -> dict:
    if aether_core:
        try:
            return aether_core.get_file_metadata(path)
        except:
            pass
    p = Path(path)
    if not p.exists():
        return {"exists": False, "size": 0, "mtime_ns": 0}
    stat = p.stat()
    return {"exists": True, "size": stat.st_size, "mtime_ns": stat.st_mtime_ns}

def compare_meta_safe(path: str, exp_size: int, exp_mtime: int) -> bool:
    if aether_core:
        try:
            return aether_core.compare_metadata(path, exp_size, exp_mtime)
        except:
            pass
    meta = get_file_meta_safe(path)
    return meta["exists"] and meta["size"] == exp_size and meta["mtime_ns"] == exp_mtime


@click.group()
def cli():
    """Aether-Vault CLI"""
    pass

@cli.command()
def init():
    """Initialize a new repository"""
    repo_root = Path.cwd()
    av_dir = repo_root / '.av'
    if av_dir.exists():
        click.secho(f"Repository already initialized at {av_dir}", fg='yellow')
        return

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
        rel_path = str(fpath.relative_to(repo_root)).replace('\\', '/')
        if is_pointer_file(fpath):
            continue
            
        meta = get_file_meta_safe(str(fpath))
        file_hash = hash_file_safe(str(fpath))
        file_type = idx.classify_file(rel_path)
        
        pointer_rel_path = None
        
        if file_type == 'artifact' and meta["size"] > threshold_bytes:
            obj_dir = repo_root / '.av' / 'objects' / file_hash[:2]
            obj_dir.mkdir(parents=True, exist_ok=True)
            obj_path = obj_dir / file_hash[2:]
            
            if not obj_path.exists():
                shutil.copy2(fpath, obj_path)
                
            ptr_path = get_pointer_path(fpath)
            ptr_content = create_pointer(fpath, file_hash, meta["size"])
            with open(ptr_path, 'w') as ptr_f:
                ptr_f.write(ptr_content)
                
            pointer_rel_path = rel_path + ".av-pointer"
            idx.add_entry(rel_path, file_hash, meta["size"], meta["mtime_ns"], file_type, pointer_rel_path)
            click.secho(f"Staged [ARTIFACT] {rel_path} (LFS)", fg='green')
        else:
            idx.add_entry(rel_path, file_hash, meta["size"], meta["mtime_ns"], file_type, None)
            click.secho(f"Staged [{file_type.upper()}] {rel_path}", fg='green')

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
            elif not compare_meta_safe(str(repo_root / rel_path), entry["size"], entry["mtime_ns"]):
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
        
    tree = {"code": {}, "artifacts": {}}
    for rel_path, e in idx.entries.items():
        if e["type"] == "code":
            tree["code"][rel_path] = e["hash"]
        else:
            tree["artifacts"][rel_path] = {"hash": e["hash"], "size": e["size"], "pointer": e.get("pointer")}
            
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
            
        for rel_path, e in tree["artifacts"].items():
            if e.get("pointer"):
                obj_file = repo_root / '.av' / 'objects' / e["hash"][:2] / e["hash"][2:]
                if obj_file.exists():
                    client.upload_object(obj_file, e["hash"])

@cli.command()
@click.argument('name', required=False)
def branch(name):
    """List or create branches"""
    repo_root = ensure_repo()
    heads_dir = repo_root / '.av' / 'refs' / 'heads'
    
    if not name:
        head_path = repo_root / '.av' / 'HEAD'
        current = ""
        if head_path.exists():
            head_content = head_path.read_text().strip()
            if head_content.startswith("ref: refs/heads/"):
                current = head_content.split("/")[-1]
                
        for br in heads_dir.iterdir():
            if br.name == current:
                click.secho(f"* {br.name}", fg='green')
            else:
                click.echo(f"  {br.name}")
    else:
        head_path = repo_root / '.av' / 'HEAD'
        commit_hash = ""
        if head_path.exists():
            head_content = head_path.read_text().strip()
            if head_content.startswith("ref: "):
                ref_path = repo_root / '.av' / head_content.split(": ")[1]
                if ref_path.exists():
                    commit_hash = ref_path.read_text().strip()
            else:
                commit_hash = head_content
                
        with open(heads_dir / name, 'w') as f:
            f.write(commit_hash)
        click.secho(f"Created branch {name}", fg='green')

@cli.command()
@click.argument('target')
def checkout(target):
    """Checkout a branch or commit"""
    repo_root = ensure_repo()
    cfg = load_config(repo_root)
    client = VaultClient(cfg.get("remote_url", "http://localhost:8000"))
    
    heads_dir = repo_root / '.av' / 'refs' / 'heads'
    commit_hash = target
    ref_name = None
    
    if (heads_dir / target).exists():
        commit_hash = (heads_dir / target).read_text().strip()
        ref_name = target
        
    commit_file = repo_root / '.av' / 'commits' / f"{commit_hash}.json"
    commit_data = None
    if commit_file.exists():
        with open(commit_file, 'r') as f:
            commit_data = json.load(f)
    elif client.server_available():
        commit_data = client.get_commit(commit_hash)
        if commit_data:
            with open(commit_file, 'w') as f:
                json.dump(commit_data, f)
                
    if not commit_data:
        click.secho(f"Error: Commit {target} not found.", fg='red')
        return
        
    idx = Index(repo_root)
    idx.entries.clear()
    
    tree = commit_data.get("tree", {"code": {}, "artifacts": {}})
    for rel_path, h in tree.get("code", {}).items():
        idx.add_entry(rel_path, h, 0, 0, "code")
        
    for rel_path, artifact in tree.get("artifacts", {}).items():
        h = artifact["hash"]
        size = artifact["size"]
        pointer = artifact.get("pointer")
        idx.add_entry(rel_path, h, size, 0, "artifact", pointer)
        
        if pointer:
            obj_path = repo_root / '.av' / 'objects' / h[:2] / h[2:]
            dest = repo_root / rel_path
            dest.parent.mkdir(parents=True, exist_ok=True)
            if obj_path.exists():
                shutil.copy2(obj_path, dest)
            elif client.server_available():
                click.echo(f"Downloading {rel_path}...")
                if client.download_object(h, dest):
                    obj_path.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(dest, obj_path)
            
    head_path = repo_root / '.av' / 'HEAD'
    with open(head_path, 'w') as f:
        if ref_name:
            f.write(f"ref: refs/heads/{ref_name}\n")
        else:
            f.write(f"{commit_hash}\n")
            
    click.secho(f"Checked out {target}", fg='green')

if __name__ == '__main__':
    cli()
