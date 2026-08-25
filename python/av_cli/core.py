"""Shared multi-consumer helpers for the av_cli command modules.

Extracted from main.py verbatim (Point-13 split): every helper here is used by more than
one command module. Command implementations live in `cmd_*.py`; `main.py` is the thin
compat shell (cli group construction, registration order, patch-target owners, re-exports).

Import-hub note: this module intentionally re-exports the stdlib/third-party names the
command bodies rely on (json/os/click/Path/Index/...), because cmd modules start with
`from .core import *` — keeps per-module headers tiny without eager heavy imports.
"""

from __future__ import annotations

import datetime
import hashlib
import fnmatch
import json
import logging
import os
import re as _re
import shutil
import subprocess
import sys
import tempfile
import uuid
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import click

from .exceptions import (
    AmbiguousCommitHash,
    AetherVaultException,
    AuthenticationError,
    NetworkError,
    StorageError,
    ValidationError,
)
from .fsutil import atomic_write_json, atomic_write_text, find_commit_file
from .index import Index
from .pointer import (
    create_pointer,
    get_pointer_path,
    is_pointer_file,
    parse_pointer,
)



# UnicodeEncodeError before the command logic even runs.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

from . import speedcheck
from . import __version__
from .exceptions import AetherVaultException, AmbiguousCommitHash, NetworkError, StorageError, ValidationError
from .index import Index
from .pointer import create_pointer, get_pointer_path, is_pointer_file, parse_pointer

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logger = logging.getLogger("av")


_aether_core = None
_aether_core_load_attempted = False


def _get_aether_core():
    """Lazily import the aether_core pybind11 extension.

    Loading the compiled extension costs real time (~90ms) that no-op commands like a
    fully-cached `add` never recoup, since they never reach a hash/split call. Deferred to
    first actual use instead of importing unconditionally at module load.
    """
    global _aether_core, _aether_core_load_attempted
    if not _aether_core_load_attempted:
        try:
            import aether_core as _ac

            _aether_core = _ac
        except ImportError:
            _aether_core = None
        _aether_core_load_attempted = True
    return _aether_core


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


def load_avignore_patterns(repo_root: Path) -> list[str]:
    """Reads `.avignore` from the repo root, if present.

    Gitignore-*lite*, not full gitignore semantics: plain glob patterns, one per line, `#`
    comments and blank lines skipped, matched via `fnmatch` against a path's filename or any of
    its path components. Deliberately doesn't implement negation (`!pattern`), anchoring
    (`/pattern`), or `**` double-glob — covers the stated use case (`venv`, `node_modules`,
    `*.log`) without the edge cases of a full gitignore parser.
    """
    avignore_path = repo_root / ".avignore"
    if not avignore_path.exists():
        return []
    patterns = []
    for line in avignore_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        patterns.append(line.rstrip("/"))
    return patterns


def _matches_avignore(name: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatch(name, pattern) for pattern in patterns)


def iter_working_files(root: Path):
    """Yield every working-tree file path under `root`, skipping ignored dirs/noise and
    anything matching a `.avignore` pattern.

    Prunes ignored/ignored-by-pattern directories in-place so the CAS object store (and e.g. a
    `.avignore`'d `venv/`) is never traversed in the first place, not just filtered after a full
    walk.
    """
    repo_root = find_repo_root() or root
    avignore_patterns = load_avignore_patterns(repo_root)
    for dirpath, dirnames, files in os.walk(root):
        dirnames[:] = [
            d for d in dirnames
            if d not in _IGNORED_DIRS and not _matches_avignore(d, avignore_patterns)
        ]
        for f in files:
            if f.endswith(".pyc") or f.endswith(".av-pointer"):
                continue
            if _matches_avignore(f, avignore_patterns):
                continue
            yield Path(dirpath) / f


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


class _AuthRetryGroup(click.Group):
    """`cli`'s class (see `cli = click.group(..., cls=_AuthRetryGroup)` below) — catches
    AuthenticationError from *any* subcommand in one place, rather than wrapping each of the
    ~7 commands that talk to the server individually (push, commit, checkout, gc, doctor --fix,
    stash push/pop). Click's MultiCommand.invoke() lets exceptions raised inside a subcommand's
    callback propagate up through this call, which is the standard way to add a shared
    error-handling layer across an entire Click command tree.

    Re-runs the command from scratch after saving a token rather than silently retrying
    mid-operation — several of the affected commands (push, commit, gc) make multiple
    sequential server calls, and resuming a partially-completed multi-step operation with a
    freshly swapped credential is riskier than just asking the user to re-invoke it.
    """

    def invoke(self, ctx: click.Context):
        from .client import AuthenticationError
        from . import ui

        try:
            return super().invoke(ctx)
        except AuthenticationError:
            if not ui.is_interactive():
                click.secho(
                    "Error: this registry is protected and needs a valid access token. Set "
                    "one with `av auth set-token <token>` (ask whoever manages this registry "
                    "for the current one), then retry.",
                    fg="red",
                )
                sys.exit(1)

            click.secho("This registry is protected — enter the access token to continue.", fg="yellow")
            import questionary

            token = questionary.password("Access token:").ask()
            if not token:
                click.secho("No token entered — aborting.", fg="red")
                sys.exit(1)

            repo_root = find_repo_root()
            if repo_root is not None:
                cfg = load_config(repo_root)
                cfg["remote_api_token"] = token
                save_config(repo_root, cfg)
                click.secho("Token saved. Please re-run the command.", fg="green")
            else:
                click.secho(
                    "Token entered, but no .av repository found here to save it in — "
                    "re-run `av auth set-token` from inside the repo.",
                    fg="yellow",
                )
            sys.exit(1)


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

    Uploads are batch-checked then sent in parallel (small thread pool — these are
    network-bound HTTP calls, not CPU work) rather than one HEAD+POST round trip per
    object in sequence: a 60-object commit was previously ~120 serial round trips.
    """
    candidates: dict[str, Path] = {}  # hash -> object file on disk, dedup'd
    for info in tree.values():
        parts = list(info.get("layers", [])) + list(info.get("chunks", []))
        for part in parts:
            p_hash = part["hash"]
            p_obj = repo_root / ".av" / "objects" / p_hash[:2] / p_hash[2:]
            if p_obj.exists():
                candidates.setdefault(p_hash, p_obj)
        # Layer-split safetensors and CDC-chunked checkpoints deliberately never upload a
        # whole-file blob (the shards carry all the bytes); only unsplit files do.
        if not parts:
            obj_file = repo_root / ".av" / "objects" / info["hash"][:2] / info["hash"][2:]
            if obj_file.exists():
                candidates.setdefault(info["hash"], obj_file)

    if not candidates:
        return

    found = client.batch_check_objects(list(candidates.keys()))
    missing = {h: p for h, p in candidates.items() if h not in found}
    if not missing:
        return

    with ThreadPoolExecutor(max_workers=min(8, len(missing))) as pool:
        futures = [
            pool.submit(client.upload_object, path, h, known_missing=True)
            for h, path in missing.items()
        ]
        for future in futures:
            future.result()


def flush_pending_push(repo_root: Path, client: "VaultClient") -> list[dict]:
    """Retry pushing queued commits to the remote server. Returns the entries still pending.

    `client.server_available()` only proves the server process is up — it's exempt from the
    auth gate (see server.py) specifically so it stays answerable with no credentials, which
    means it does NOT prove this client's token is valid. A bad/stale token surfaces as
    AuthenticationError from the calls below, not a False/None return — caught here and
    treated exactly like "server unreachable" for queueing purposes (queue and retry later,
    never lose the commit), since from a data-safety standpoint that's exactly what it is.
    Stops retrying the rest of the queue on the first such failure, since the same bad token
    would just fail identically for every remaining entry.
    """
    pending = load_pending_push(repo_root)
    if not pending or not client.server_available():
        return pending

    from .client import AuthenticationError

    still_pending: list[dict] = []
    for i, entry in enumerate(pending):
        commit_path = repo_root / ".av" / "commits" / f"{entry['commit_hash']}.json"
        if not commit_path.exists():
            continue
        with open(commit_path, "r") as f:
            commit_data = json.load(f)
        try:
            upload_commit_objects(repo_root, client, commit_data.get("tree", {}))
            if client.push_commit(commit_data):
                ref_ok = True
                if entry.get("ref_name"):
                    ref_ok = client.update_ref(entry["ref_name"], entry["commit_hash"])
                if ref_ok:
                    continue
        except AuthenticationError:
            still_pending.extend(pending[i:])  # this entry + everything not yet attempted
            save_pending_push(repo_root, still_pending)
            raise
        still_pending.append(entry)

    save_pending_push(repo_root, still_pending)
    return still_pending


def hash_file_safe(path: str) -> str:
    aether_core = _get_aether_core()
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


def materialize_file(
    repo_root: Path,
    client: "VaultClient",
    rel_path: str,
    h: str,
    layers: list | None = None,
    chunks: list | None = None,
) -> None:
    """Writes a tracked path's content to the working tree from the CAS — whole-object,
    reassembled from safetensors layers (`layers`), or reassembled from CDC chunks
    (`chunks`, see the .pt/.ckpt dedup), downloading missing pieces from the remote.

    Extracted from `checkout()`'s per-entry restore logic so `av stash pop`/`apply`,
    clone/pull, and merge can materialize a file the exact same way checkout restores a
    commit's — reusing this instead of reimplementing it avoids re-introducing the
    safetensors reconstruction bug this exact code path already had fixed once
    (development/Probleme.md).
    """
    layers = layers or []
    chunks = chunks or []
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
                            f"Missing layer {lh} for {rel_path}; aborted to avoid a corrupt artifact"
                        )
                    with open(l_obj, "rb") as f_in:
                        shutil.copyfileobj(f_in, f_out)
        except click.ClickException:
            dest.unlink(missing_ok=True)
            raise

        obj_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(dest, obj_path)
    elif chunks and not obj_path.exists():
        ordered = sorted(chunks, key=lambda c: c.get("offset", 0))
        click.echo(f"Reassembling {rel_path} from {len(ordered)} chunks...")
        try:
            with open(dest, "wb") as f_out:
                for chunk in ordered:
                    ch = chunk["hash"]
                    c_obj = repo_root / ".av" / "objects" / ch[:2] / ch[2:]
                    if not c_obj.exists() and client.server_available():
                        client.download_object(ch, c_obj)
                    if not c_obj.exists():
                        raise click.ClickException(
                            f"Missing chunk {ch} for {rel_path}; aborted to avoid a corrupt artifact"
                        )
                    with open(c_obj, "rb") as f_in:
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


def remove_file_and_pointer(repo_root: Path, rel_path: str) -> None:
    """Deletes a working-tree file (and its `.av-pointer` sibling, if any), pruning now-empty
    parent directories — extracted from `checkout()`'s "this path no longer exists in the
    target commit" cleanup so `av stash push`/`av unstage` can remove a file the same way.
    """
    file_path = repo_root / rel_path
    if file_path.exists() and file_path.is_file():
        file_path.unlink()
        try:
            for parent in file_path.parents:
                if parent == repo_root or parent.name == ".av":
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


def resolve_head_tree(repo_root: Path) -> dict:
    """Reads the current HEAD commit's tree (rel_path -> {hash, size, type, layers}), or {}
    if there are no commits yet. Normalizes the legacy {"code":..., "artifacts":...} shape
    (see `checkout()`) into the unified flat shape so callers only handle one format.
    """
    head_path = repo_root / ".av" / "HEAD"
    if not head_path.exists():
        return {}
    head_content = head_path.read_text().strip()
    if head_content.startswith("ref: "):
        ref_path = repo_root / ".av" / head_content.split(": ", 1)[1]
        commit_hash = ref_path.read_text().strip() if ref_path.exists() else ""
    else:
        commit_hash = head_content
    if not commit_hash:
        return {}

    commit_file = repo_root / ".av" / "commits" / f"{commit_hash}.json"
    if not commit_file.exists():
        return {}
    with open(commit_file, "r") as f:
        commit_data = json.load(f)

    tree = commit_data.get("tree", {})
    if "code" in tree or "artifacts" in tree:
        normalized = {}
        for rel_path, h in tree.get("code", {}).items():
            normalized[rel_path] = {"hash": h, "size": 0, "type": "code", "layers": []}
        for rel_path, artifact in tree.get("artifacts", {}).items():
            normalized[rel_path] = {
                "hash": artifact["hash"], "size": artifact["size"],
                "type": "artifact", "layers": artifact.get("layers", []),
            }
        return normalized
    return tree


# v1.2.0: generalized dataset chunking - serialization formats that benefit from
# CDC dedup on re-exports. Per-file override stays with .avattributes.
CHUNKABLE_EXTS = {".pt", ".pth", ".ckpt", ".npz", ".h5", ".hdf5", ".pb", ".msgpack"}


def stage_one_file(
    repo_root: Path,
    idx: Index,
    threshold_bytes: int,
    fpath: Path,
    rel_path: str,
    attr_flags: set | None = None,
) -> bool:
    """Hashes and stores a single file's current content (LFS threshold check, safetensors
    layer-split if applicable, CDC chunking for opaque checkpoint formats, pointer creation)
    and records it in the index. Returns whether anything actually changed (False = already
    up to date in the index).

    `attr_flags` carries this path's `.avattributes` directives (`no-chunk`,
    `no-layer-split`) — resolved once per invocation by the caller via attributes.flags_for.

    Extracted from `add()`'s per-file loop body so `av stash push` can get a modified-but-not-
    yet-staged file's content safely into the CAS before reverting the working copy, using
    exactly the same logic `add()` already uses — not a reimplementation of it.
    """
    attr_flags = attr_flags or set()
    meta = get_file_meta_safe(str(fpath))

    existing = idx.get_entry(rel_path)
    if (
        existing
        and meta["exists"]
        and meta["size"] == existing["size"]
        and meta["mtime_ns"] == existing["mtime_ns"]
    ):
        return False

    file_hash = hash_file_safe(str(fpath))
    file_type = idx.classify_file(rel_path)

    if file_type == "artifact" and meta["size"] > threshold_bytes:
        layers: list[dict] = []
        chunks: list[dict] = []

        aether_core = _get_aether_core()
        if (
            rel_path.endswith(".safetensors")
            and "no-layer-split" not in attr_flags
            and aether_core
            and hasattr(aether_core, "split_and_hash_safetensors")
        ):
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

        if not layers:
            suffix = Path(rel_path).suffix.lower()
            core_cdc = _get_aether_core()
            if (
                suffix in CHUNKABLE_EXTS
                and "no-chunk" not in attr_flags
                and core_cdc is not None
                and hasattr(core_cdc, "chunk_and_hash_file")
            ):
                logger.info(f"Content-defined chunking for {rel_path}...")
                try:
                    chunk_results = core_cdc.chunk_and_hash_file(str(fpath))
                    for cr in chunk_results:
                        c_hash = cr["hash"]
                        c_size = cr["size"]
                        c_offset = cr["offset"]
                        c_obj_dir = repo_root / ".av" / "objects" / c_hash[:2]
                        c_obj_dir.mkdir(parents=True, exist_ok=True)
                        c_obj_path = c_obj_dir / c_hash[2:]
                        if not c_obj_path.exists():
                            with open(fpath, "rb") as src_f:
                                src_f.seek(c_offset)
                                with open(c_obj_path, "wb") as dst_f:
                                    remaining = c_size
                                    while remaining > 0:
                                        block = src_f.read(min(8 * 1024 * 1024, remaining))
                                        if not block:
                                            break
                                        dst_f.write(block)
                                        remaining -= len(block)
                        chunks.append({"hash": c_hash, "size": c_size, "offset": c_offset})
                except Exception as exc:
                    logger.warning(f"Chunking failed for {rel_path}, falling back to whole-file: {exc}")
                    chunks = []

        if not layers and not chunks:
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
        idx.add_entry(rel_path, file_hash, meta["size"], meta["mtime_ns"], file_type, pointer_rel_path, auto_save=False)
        if layers:
            idx.entries[rel_path]["layers"] = layers
        if chunks:
            idx.entries[rel_path]["chunks"] = chunks
        split_desc = (
            f"{len(layers)} layers" if layers
            else (f"{len(chunks)} chunks" if chunks else "whole-file")
        )
        click.secho(f"Staged [ARTIFACT] {rel_path} (LFS, {split_desc})", fg="green")
    else:
        obj_dir = repo_root / ".av" / "objects" / file_hash[:2]
        obj_dir.mkdir(parents=True, exist_ok=True)
        obj_path = obj_dir / file_hash[2:]
        if not obj_path.exists():
            shutil.copy2(fpath, obj_path)

        idx.add_entry(rel_path, file_hash, meta["size"], meta["mtime_ns"], file_type, None, auto_save=False)
        click.secho(f"Staged [{file_type.upper()}] {rel_path}", fg="green")

    return True


def _init_repo_structure(repo_root: Path) -> None:
    """Bootstrap the .av/ directory layout. Behavior-preserving extraction from `init`."""
    av_dir = repo_root / ".av"
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


def compute_status(repo_root: Path, idx: Index) -> tuple[list[str], list[str], list[str], list[str]]:
    """Returns (staged, modified, deleted, untracked) rel_paths — the same dirty-state
    classification `status()` displays, factored out so `av stash` can compute exactly the same
    dirty set instead of re-deriving its own (slightly different) notion of "dirty"."""
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

    return staged, modified, deleted, untracked


def _finalize_commit(
    repo_root: Path,
    cfg: dict,
    client: "VaultClient",
    *,
    commit_data: dict,
    tree: dict,
    ref_path: Path | None,
    head_path: Path,
    idx: Index,
    tags: tuple = (),
    metrics: dict | None = None,
    result_sink=None,
    defer_upload: bool = False,
) -> str:
    """Everything `av commit` does after its tree snapshot and parents are resolved: hash
    the payload deterministically over sorted JSON (preserves DAG integrity — two projects
    can never collide on the same hash even with byte-identical trees/messages/timestamps),
    persist atomically (commit object before ref move; temp+replace writes so a crash can't
    leave a half-written commit behind a moved ref), advance the branch ref (or HEAD when
    detached), clear the staged flags, print the summary, and push to the registry with the
    standard offline-queue fallbacks.

    Extracted verbatim from commit()'s tail so `av merge` creates its two-parent commits
    through exactly the same code path instead of duplicating it.
    """
    metrics = metrics or {}
    message = commit_data.get("message", "")

    commit_str = json.dumps(commit_data, sort_keys=True)
    commit_hash = hashlib.sha256(commit_str.encode()).hexdigest()
    commit_data["hash"] = commit_hash

    # --- Persist locally ---
    atomic_write_json(repo_root / ".av" / "commits" / f"{commit_hash}.json", commit_data)

    if ref_path:
        atomic_write_text(ref_path, commit_hash)
    else:
        atomic_write_text(head_path, commit_hash)

    idx.clear_staged()
    result = {
        "hash": commit_hash,
        "short": commit_hash[:7],
        "message": message,
        "tags": list(tags),
        "metrics": dict(metrics),
        "queued": False,
        "queued_reason": None,
    }

    def _queued(reason: str) -> None:
        result["queued"] = True
        result["queued_reason"] = reason

    if result_sink is not None:
        result_sink(result)
        result["committed"] = True  # marker for the sink path; humans see the echo below
    else:
        click.secho(f"[{commit_hash[:7]}] {message}", fg="green")
        if tags:
            click.secho(f"  Tags: {', '.join(tags)}", fg="cyan")
        if metrics:
            click.secho(f"  Metrics: {metrics}", fg="cyan")

    # --- Push to remote if available ---
    # Refs are namespaced as "<project_id>/<branch>" on the shared registry so two projects
    # can each have a branch named "main" without overwriting each other's ref.
    remote_ref_name = f"{cfg['project_id']}/{ref_path.name}" if ref_path else None

    from .client import AuthenticationError

    try:
        flush_pending_push(repo_root, client)
    except AuthenticationError:
        pass  # already re-queued by flush_pending_push itself; this commit's own push attempt below still needs to happen

    if defer_upload:
        # High-frequency mode: skip every network attempt, queue directly. The commit is
        # fully durable locally; `av push` (or the next online commit) drains the queue.
        queue_pending_push(repo_root, commit_hash, remote_ref_name)
        _queued("upload_deferred")
        if result_sink is None:
            click.secho("  Upload deferred — queued for `av push`", fg="yellow")
    elif client.server_available():
        try:
            # Objects must reach the server before the commit: the server's tree rows store
            # each entry's object hash as a foreign key, so pushing the commit first makes
            # that insert fail (previously misreported as a successful 409 "already exists" —
            # see upload_commit_objects()'s docstring / development/Probleme.md).
            upload_commit_objects(repo_root, client, tree)
            if client.push_commit(commit_data):
                ref_ok = True
                if remote_ref_name:
                    ref_ok = client.update_ref(remote_ref_name, commit_hash)
                if not ref_ok:
                    queue_pending_push(repo_root, commit_hash, remote_ref_name)
                    _queued("ref_update_failed")
                    if result_sink is None:
                        click.secho("  Ref update failed — commit queued for retry (run `av push` later)", fg="yellow")
            else:
                queue_pending_push(repo_root, commit_hash, remote_ref_name)
                _queued("push_failed")
                if result_sink is None:
                    click.secho("  Push failed — commit queued for retry (run `av push` later)", fg="yellow")
        except AuthenticationError:
            # client.server_available() only proves the server is up (it's exempt from the
            # auth gate) — it does NOT prove this token is valid, so a bad/stale token surfaces
            # here as an exception instead of push_commit's normal False return. Queue exactly
            # like any other push failure — losing the commit because of a credential problem
            # specifically, vs. a network problem, would be an arbitrary distinction the user
            # shouldn't have to think about.
            queue_pending_push(repo_root, commit_hash, remote_ref_name)
            _queued("auth_rejected")
            if result_sink is None:
                click.secho(
                    "  Server rejected the access token — commit queued for retry "
                    "(run `av auth set-token <token>` then `av push`)",
                    fg="yellow",
                )
    else:
        queue_pending_push(repo_root, commit_hash, remote_ref_name)
        _queued("server_unreachable")
        if result_sink is None:
            click.secho("  Server unreachable — commit queued for push (run `av push` later)", fg="yellow")

    return commit_hash


def commit_staged(
    repo_root: Path,
    message: str,
    tags: tuple = (),
    metrics: dict | None = None,
    run_id: str | None = None,
    defer_upload: bool = False,
    result_sink=None,
) -> str | None:
    """Commit whatever is currently staged — THE shared entry point.

    Callers: `av commit` (after flag parsing), `av watch` (auto-commits), and the
    av_sdk.Repo SDK. All of them get identical semantics because this is the only place
    that builds the payload and calls _finalize_commit (the historical single writer):
    deterministic hash over sorted JSON, atomic local persist, ref advance, and
    push-or-queue with offline resilience.

    Returns the new commit hash, or None when nothing was staged.
    """
    from .client import VaultClient

    idx = Index(repo_root)
    if not idx.get_staged_entries():
        return None
    cfg = load_config(repo_root)
    client = VaultClient(cfg.get("remote_url", "http://localhost:8000"),
                         cfg.get("remote_api_token"))

    tree: dict = {}
    for rel_path, e in idx.entries.items():
        tree[rel_path] = {
            "hash": e["hash"],
            "size": e["size"],
            "type": e["type"],
            "layers": e.get("layers", []),
            "chunks": e.get("chunks", []),
        }

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

    import datetime as _dt

    commit_data: dict = {
        "parents": parents,
        "author": os.environ.get("AV_AUTHOR", "anonymous"),
        "timestamp": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "message": message,
        "tree": tree,
        "tags": list(tags),
        "metrics": metrics or {},
        # In the hashed payload so two projects can never collide on byte-identical
        # trees/messages/timestamps (registry keys commits by hash alone).
        "project_id": cfg["project_id"],
        "project_name": cfg["project_name"],
    }
    if run_id:
        commit_data["run_id"] = run_id
        tagged = f"run:{run_id}"
        if tagged not in commit_data["tags"]:
            commit_data["tags"] = commit_data["tags"] + [tagged]
            tags = tuple(commit_data["tags"])

    return _finalize_commit(
        repo_root, cfg, client,
        commit_data=commit_data, tree=tree, ref_path=ref_path, head_path=head_path,
        idx=idx, tags=tags, metrics=metrics or {},
        result_sink=result_sink, defer_upload=defer_upload,
    )


def _collect_dirty_paths(repo_root: Path, idx: Index) -> list[str]:
    """Tracked paths whose working-tree state would be lost by a tree switch — deleted from
    disk, staged-but-uncommitted, or stat-different from the index. Shared by `checkout`,
    `av pull`, and `av merge` so all three refuse destructive switches under exactly the
    same conditions.
    """
    dirty: list[str] = []
    for rel_path, entry in idx.entries.items():
        fpath = repo_root / rel_path
        if not fpath.exists():
            dirty.append(rel_path)
        elif entry.get("staged") or not compare_meta_safe(
            str(fpath), entry["size"], entry["mtime_ns"]
        ):
            dirty.append(rel_path)
    return dirty


def _materialize_tree(repo_root: Path, client: "VaultClient", tree: dict, idx: Index) -> None:
    """Makes the index and the working tree match a flat commit tree.

    The one shared restore path behind `checkout`, `av clone`, and `av pull`: replaces
    idx.entries with the tree's entries (downloading any object the remote has but this
    machine doesn't), deletes working files the tree no longer contains, then re-stats
    every entry and clears its staged flag so `av status` reads clean immediately after.
    Extracted verbatim from checkout's body — behavior-preserving, verified by the existing
    checkout/stash suites.
    """
    old_entries = dict(idx.entries)
    idx.entries.clear()

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
            chunks = info.get("chunks", [])
            pointer = rel_path + ".av-pointer" if file_type == "artifact" else None

            idx.add_entry(rel_path, h, size, 0, file_type, pointer, auto_save=False)
            if layers:
                idx.entries[rel_path]["layers"] = layers
            if chunks:
                idx.entries[rel_path]["chunks"] = chunks

            # Restore every tracked file's content from the CAS, not just artifacts — `code`
            # files are written to .av/objects by `add()` too (see its comment there), so an
            # older commit's code must be materialized here the same way, or `av checkout`
            # would silently leave the working tree's code untouched while still claiming
            # success (development/Probleme.md).
            materialize_file(repo_root, client, rel_path, h, layers, chunks)

    for rel_path in old_entries:
        if rel_path not in idx.entries:
            remove_file_and_pointer(repo_root, rel_path)

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


# ---------------------------------------------------------------------------
# Agent surface: structured output envelope + stable exit-code contract (v1.2.0)
# ---------------------------------------------------------------------------
# Commands reachable by agents emit either human text or a single JSON envelope,
# selected by the root group's --output flag. The envelope shape is a compatibility
# surface: {"ok": bool, "data": ..., "error": {"code","message"}|null, "meta": {...}}.
# See docs/for-agents.md; the exit-code table below is part of that contract.

EXIT_OK = 0
EXIT_USAGE = 2                    # click's own usage-error code
EXIT_NOT_A_REPO = 10
EXIT_NOTHING_TO_COMMIT = 11
EXIT_AUTH_FAILED = 12
EXIT_UNREACHABLE_QUEUED = 13      # work is SAFE (queued), registry unreachable
EXIT_CONFLICT = 14                # merge conflicts present, nothing touched
EXIT_VALIDATION = 15              # bad input values
EXIT_POLICY_DENIED = 16           # promotion/branch policy rejected the action

_EXIT_CODES = {
    "not_a_repo": EXIT_NOT_A_REPO,
    "nothing_to_commit": EXIT_NOTHING_TO_COMMIT,
    "auth_failed": EXIT_AUTH_FAILED,
    "unreachable_queued": EXIT_UNREACHABLE_QUEUED,
    "merge_conflict": EXIT_CONFLICT,
    "validation": EXIT_VALIDATION,
    "policy_denied": EXIT_POLICY_DENIED,
}


_OUTPUT_MODE = "text"


def set_output_mode(mode: str) -> None:
    """Called once by the root group; process-lifetime output selection."""
    global _OUTPUT_MODE
    _OUTPUT_MODE = mode if mode in ("text", "json") else "text"


def current_output_mode() -> str:
    return _OUTPUT_MODE


def output_is_json(ctx) -> bool:
    """True when the root group was invoked with --output json."""
    return bool(ctx and isinstance(ctx.obj, dict) and ctx.obj.get("output") == "json")


def json_envelope(command: str, data=None, error_code: str | None = None,
                  error_message: str | None = None) -> dict:
    """Builds the one-and-only agent-facing response shape."""
    from . import _version

    try:
        version = _version.__version__
    except Exception:
        version = "dev"
    env: dict = {
        "ok": error_code is None,
        "data": data if data is not None else {},
        "error": None,
        "meta": {"command": command, "version": version},
    }
    if error_code is not None:
        env["error"] = {"code": error_code, "message": error_message or ""}
    return env


def emit_json(ctx, command: str, data=None) -> None:
    """Prints an ok-envelope for `command` (call instead of human output in JSON mode)."""
    click.echo(json.dumps(json_envelope(command, data=data)))


def fail(ctx, code: str, message: str, command: str | None = None):
    """Uniform failure path: JSON envelope + documented exit code in one raise.

    In text mode the message prints plainly (no traceback); in JSON mode the envelope
    carries the machine-readable code. Always raises — call sites stop here.
    """
    exit_code = _EXIT_CODES.get(code, EXIT_VALIDATION)
    cmd = command or (ctx.command.name if ctx and getattr(ctx, "command", None) else "av")
    if output_is_json(ctx):
        click.echo(json.dumps(json_envelope(cmd, error_code=code, error_message=message)))
    else:
        click.secho(f"Error: {message}", fg="red", err=True)
    ctx.exit(exit_code)
