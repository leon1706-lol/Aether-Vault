import datetime
import fnmatch
import hashlib
import importlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TYPE_CHECKING

import click

if TYPE_CHECKING:
    from .client import VaultClient

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


def __getattr__(name: str):
    # PEP 562 module __getattr__: keeps `av_cli.main.VaultClient` resolvable (tests and
    # other callers monkeypatch it via this attribute) without paying for `import requests`
    # at module load time for commands that never touch the network — see local
    # `from .client import VaultClient` imports inside the command functions that need it.
    if name == "VaultClient":
        from .client import VaultClient

        return VaultClient
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


# Windows consoles default to a legacy codepage (e.g. cp1252) that can't
# encode the emoji/symbols used in CLI output below, crashing with a
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


# ---------------------------------------------------------------------------
# Atomic write helpers
# ---------------------------------------------------------------------------

from .fsutil import atomic_write_json, atomic_write_text, find_commit_file  # noqa: E402


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


# ---------------------------------------------------------------------------
# File hashing / metadata helpers (wraps aether_core with Python fallback)
# ---------------------------------------------------------------------------

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


# Opaque checkpoint formats that get content-defined chunking instead of whole-file blobs
# (safetensors gets precise layer-splitting above; everything else stays whole-file).
CHUNKABLE_EXTS = {".pt", ".pth", ".ckpt"}


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


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

@click.group(invoke_without_command=True, cls=_AuthRetryGroup)
@click.option("--verbose", is_flag=True, default=False, help="Enable debug logging.")
@click.option("--silent", is_flag=True, default=False, help="Suppress all output.")
@click.pass_context
def cli(ctx: click.Context, verbose: bool, silent: bool) -> None:
    """Aether-Vault: High-performance version control for ML models & datasets."""
    ctx.ensure_object(dict)
    setup_logging(verbose, silent)

    if ctx.invoked_subcommand is not None:
        return

    # Bare `av` with no subcommand: in an already-initialized project, reconnect and drop
    # straight into the interactive session; otherwise fall back to the normal help screen.
    repo_root = find_repo_root()
    if repo_root is None:
        click.echo(ctx.get_help())
        click.echo("\nRun `av init` to get started.")
        return

    cfg = load_config(repo_root)
    _reconnect_existing_repo(repo_root, cfg)
    from . import repl

    repl.run_repl(repo_root, login_mode=cfg.get("login_mode", "local"))


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

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


def _reconnect_existing_repo(repo_root: Path, cfg: dict) -> None:
    """Reconnect to an already-initialized repo's stored backend (no questions asked)."""
    login_mode = cfg.get("login_mode", "local")
    if login_mode == "enterprise":
        from . import enterprise

        enterprise.run_enterprise_login_flow()
        return

    from . import docker_runtime

    try:
        docker_runtime.ensure_local_backend_running(_find_source_root(), open_browser=False)
    except Exception as exc:
        click.secho(f"[WARN] Could not reach the local backend: {exc}", fg="yellow")


def _handle_init_protection_choice(
    repo_root: Path, yes: bool, protected_flag: bool, join_token: str | None
) -> None:
    """The Anonymous/Protected prompt `av init` shows after choosing Local mode, plus its
    "Generate a new token" vs "Enter an existing one" follow-up. Saves whatever was decided to
    this repo's config (and, for "generate," writes/applies it via `av auth set-token`'s same
    underlying helper) — never touched at all if the result is Anonymous, matching today's
    behavior exactly.
    """
    from . import ui
    from .client import AuthenticationError, VaultClient

    if join_token:
        choice, token_source = "protected", "existing"
        existing_token = join_token
    elif protected_flag:
        choice, token_source = "protected", "generate"
        existing_token = None
    elif yes or not ui.is_interactive():
        choice, token_source = "anonymous", None
        existing_token = None
    else:
        choice = ui.select_protection_mode()
        token_source = ui.select_token_source() if choice == "protected" else None
        existing_token = ui.prompt_for_existing_token() if token_source == "existing" else None

    if choice != "protected":
        return

    if token_source == "generate":
        token = _generate_and_apply_token(repo_root)
        click.secho(f"Token set: {token}", fg="green")
        click.secho("Save this — it won't be shown again. Share it with teammates who need access.", fg="yellow")
        return

    # "existing" — joining a registry someone else already protected. Validate before saving:
    # an unreachable server and a rejected token must not look the same to the user.
    if not existing_token:
        click.secho("No token entered — leaving this registry Anonymous.", fg="yellow")
        return

    cfg = load_config(repo_root)
    client = VaultClient(cfg.get("remote_url", "http://localhost:8000"), existing_token)
    if not client.server_available():
        click.secho(
            "Could not reach the server to verify this token right now — saved anyway; "
            "you'll find out if it's wrong on your next command (`av auth status` to check).",
            fg="yellow",
        )
    else:
        try:
            client.fetch_all_refs()
        except AuthenticationError:
            click.secho(
                "That token was rejected by the server — leaving this registry Anonymous. "
                "Run `av auth set-token <token>` once you have the correct one.",
                fg="red",
            )
            return

    cfg["remote_api_token"] = existing_token
    save_config(repo_root, cfg)
    click.secho("Token saved.", fg="green")


@cli.command()
@click.option(
    "--mode", "mode", type=click.Choice(["local", "enterprise"]), default=None,
    help="Skip the interactive prompt and use this login mode.",
)
@click.option("--yes", "-y", is_flag=True, default=False, help="Skip interactive prompts.")
@click.option(
    "--no-repl", is_flag=True, default=False,
    help="Don't enter the interactive session after init (used by scripts/CI).",
)
@click.option(
    "--protected", "protected_flag", is_flag=True, default=False,
    help="Non-interactive equivalent of choosing Protected + Generate a new token.",
)
@click.option(
    "--token", "join_token", default=None,
    help="Non-interactive equivalent of choosing Protected + Enter an existing token — for "
         "joining a registry someone else already protected.",
)
def init(mode: str | None, yes: bool, no_repl: bool, protected_flag: bool, join_token: str | None) -> None:
    """Initialize a new Aether-Vault repository in the current directory."""
    from . import ui

    repo_root = Path.cwd()
    av_dir = repo_root / ".av"

    if av_dir.exists():
        click.secho(f"Repository already initialized at {av_dir}", fg="yellow")
        cfg = load_config(repo_root)
        if not no_repl:
            _reconnect_existing_repo(repo_root, cfg)
            from . import repl

            repl.run_repl(repo_root, login_mode=cfg.get("login_mode", "local"))
        return

    ui.print_banner("Aether-Vault", "version control for ML models & datasets")

    # Enterprise mode is intentionally not offered interactively yet (the account-login flow
    # is unbuilt; selecting it today just falls back to Local) — the choice stays reachable
    # only via the explicit `--mode enterprise` flag so scripts keep working and the
    # enterprise.py seam stays wired for the real implementation.
    if mode is not None:
        login_mode = mode
    else:
        login_mode = "local"

    _init_repo_structure(repo_root)
    click.secho(f"Initialized empty Aether-Vault repository in {av_dir}", fg="green")

    if login_mode == "enterprise":
        from . import enterprise

        established = enterprise.run_enterprise_login_flow()
        if not established:
            login_mode = "local"

    cfg = load_config(repo_root)
    cfg["login_mode"] = login_mode
    save_config(repo_root, cfg)

    # Anonymous-vs-Protected only applies to Local mode — Enterprise has its own (separate,
    # not-yet-built) account-based auth system; this shared-secret token is the free/OSS-tier
    # mechanism, not something to layer underneath Enterprise login too.
    if login_mode == "local":
        _handle_init_protection_choice(repo_root, yes, protected_flag, join_token)
        cfg = load_config(repo_root)  # re-load: the protection-choice handler may have saved a token

    if login_mode == "local" and not no_repl:
        from . import docker_runtime

        try:
            docker_runtime.ensure_local_backend_running(
                _find_source_root(), open_browser=False, api_token=cfg.get("remote_api_token"),
            )
        except Exception as exc:
            click.secho(
                f"[WARN] Could not start the local backend ({exc}). Run `av webui` later once Docker is ready.",
                fg="yellow",
            )

    from . import update_check

    result = update_check.check_for_update()
    if result is not None and result.is_outdated:
        click.secho(
            f"\naether-vault {result.current} → {result.latest} available — run `av update`",
            fg="yellow",
        )

    if not no_repl:
        from . import repl

        repl.run_repl(repo_root, login_mode=login_mode)


@cli.command()
@click.option("--check", "check_only", is_flag=True, default=False, help="Only report; don't prompt to upgrade.")
@click.option("--list-versions", "list_versions_flag", is_flag=True, default=False, help="List every published version.")
@click.option("--enable-auto-update", is_flag=True, default=False, help="Turn on silent auto-update.")
@click.option("--disable-auto-update", is_flag=True, default=False, help="Turn off silent auto-update.")
@click.option("--docker", "docker_flag", is_flag=True, default=False,
              help="Pull the latest local Docker backend image and restart it if it changed. "
                   "Separate from the CLI update above — never bundled into plain `av update`.")
@click.option("--yes", "-y", is_flag=True, default=False, help="Skip the restart confirmation prompt (used with --docker).")
def update(check_only: bool, list_versions_flag: bool, enable_auto_update: bool, disable_auto_update: bool,
           docker_flag: bool, yes: bool) -> None:
    """Check for, and optionally install, the latest aether-vault release."""
    from . import update_check

    if docker_flag:
        from . import docker_runtime

        result = docker_runtime.check_for_docker_update(_find_source_root())
        if not result.checked:
            click.secho(result.message, fg="yellow")
            return
        click.secho(result.message, fg="green" if not result.updated else "yellow")
        if not result.updated:
            return

        if yes or click.confirm("Restart the local backend now to apply it?", default=True):
            compose_file, _ = docker_runtime.resolve_compose_file(_find_source_root())
            for service in docker_runtime.RELEASE_IMAGES:
                docker_runtime.restart_service(compose_file, service)
            # Only remove the old images once the new containers are confirmed up on the new
            # ones — never leave a window where neither image is safely runnable.
            docker_runtime.remove_old_images(result.old_image_ids)
            click.secho("Local backend restarted and old images cleaned up.", fg="green")
        return

    if enable_auto_update or disable_auto_update:
        cfg = update_check.load_user_config()
        cfg["auto_update"] = bool(enable_auto_update)
        update_check.save_user_config(cfg)
        state = "enabled" if enable_auto_update else "disabled"
        click.secho(f"Auto-update {state}.", fg="green")
        return

    if list_versions_flag:
        versions = update_check.list_versions()
        if versions is None:
            click.secho("Could not reach PyPI to list versions.", fg="red")
            return
        for v in versions:
            marker = " (installed)" if v == __version__ else ""
            click.echo(f"{v}{marker}")
        click.echo(f"\nRun `pip install {update_check.PACKAGE_NAME}==<version>` to switch.")
        return

    result = update_check.check_for_update(force=True)
    if result is None:
        click.secho("Could not reach PyPI to check for updates.", fg="red")
        return
    if not result.is_outdated:
        click.secho(f"aether-vault {result.current} is up to date.", fg="green")
        return

    click.secho(f"aether-vault {result.current} → {result.latest} available.", fg="yellow")
    if check_only:
        return

    if click.confirm("Upgrade now?", default=True):
        import subprocess

        subprocess.run([sys.executable, "-m", "pip", "install", "--upgrade", update_check.PACKAGE_NAME])


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

    from . import attributes

    attr_rules = attributes.load_attributes(repo_root)
    any_changed = False
    for fpath in files_to_process:
        rel_path = str(fpath.relative_to(repo_root)).replace("\\", "/")
        if is_pointer_file(fpath):
            continue
        if stage_one_file(repo_root, idx, threshold_bytes, fpath, rel_path,
                          attributes.flags_for(attr_rules, rel_path)):
            any_changed = True

    if any_changed:
        idx.save()


_AVIGNORE_TEMPLATE = """\
# Aether-Vault ignore patterns — one glob per line, # for comments.
# Examples (uncomment or add your own):
# venv/
# __pycache__/
# node_modules/
# *.log
"""


@cli.command()
@click.option("--avignore", "make_avignore", is_flag=True, default=False,
              help="Generate a .avignore template in the repo root.")
@click.option("--avattributes", "make_avattributes", is_flag=True, default=False,
              help="Generate a .avattributes template (per-path staging directives, "
                   "e.g. no-chunk / no-layer-split) in the repo root.")
def file(make_avignore: bool, make_avattributes: bool) -> None:
    """Generate scaffold files (.avignore, .avattributes) in the repo root.

    Each kind of generated file is its own flag, so more can be added later without
    restructuring this command.
    """
    from .attributes import ATTRIBUTES_TEMPLATE

    repo_root = ensure_repo()

    if not make_avignore and not make_avattributes:
        click.secho(
            "Nothing to do — pass a flag, e.g. `av file --avignore` or `av file --avattributes`.",
            fg="yellow",
        )
        return

    if make_avignore:
        avignore_path = repo_root / ".avignore"
        if avignore_path.exists():
            click.secho(f".avignore already exists at {avignore_path} — not overwriting.", fg="yellow")
        else:
            avignore_path.write_text(_AVIGNORE_TEMPLATE, encoding="utf-8")
            click.secho(f"Wrote {avignore_path}", fg="green")

    if make_avattributes:
        attrs_path = repo_root / ".avattributes"
        if attrs_path.exists():
            click.secho(f".avattributes already exists at {attrs_path} — not overwriting.", fg="yellow")
        else:
            attrs_path.write_text(ATTRIBUTES_TEMPLATE, encoding="utf-8")
            click.secho(f"Wrote {attrs_path}", fg="green")


@cli.command()
@click.argument("paths", nargs=-1)
def unstage(paths: tuple) -> None:
    """Unstage files staged by `av add`, without touching the working tree.

    Reverts each staged index entry back to its last-committed state (so it correctly shows up
    as "modified" again, or as untracked if it was never committed) — like `git reset` / `git
    restore --staged`, this only ever touches the index, never the working-tree files.
    """
    repo_root = ensure_repo()
    idx = Index(repo_root)

    staged = idx.get_staged_entries()
    if not staged:
        click.secho("Nothing staged to unstage", fg="yellow")
        return

    if paths:
        rel_paths = []
        for p in paths:
            try:
                rel = str(Path(p).resolve().relative_to(repo_root)).replace("\\", "/")
            except ValueError:
                continue
            if rel in staged:
                rel_paths.append(rel)
        if not rel_paths:
            click.secho("None of the given paths are staged", fg="yellow")
            return
    else:
        rel_paths = list(staged.keys())

    head_tree = resolve_head_tree(repo_root)
    for rel_path in rel_paths:
        entry = idx.entries[rel_path]
        head_data = head_tree.get(rel_path)
        if head_data:
            # Was already tracked before this staging — revert the index entry to HEAD's
            # data (mtime_ns=0 deliberately never matches a real file's stat, so `av status`
            # correctly reports it as "modified" again rather than silently looking clean).
            new_entry = {
                "hash": head_data["hash"],
                "size": head_data["size"],
                "mtime_ns": 0,
                "type": head_data["type"],
                "staged": False,
                "pointer": rel_path + ".av-pointer" if head_data["type"] == "artifact" else None,
            }
            if head_data.get("layers"):
                new_entry["layers"] = head_data["layers"]
            idx.entries[rel_path] = new_entry
        else:
            # Never committed — unstaging makes it untracked again. The `.av-pointer` is pure
            # bookkeeping `add()` created, not user data, so it's removed; the real working-tree
            # file (if any) is never touched.
            pointer = entry.get("pointer")
            if pointer:
                ptr_path = repo_root / pointer
                if ptr_path.exists() and ptr_path.is_file():
                    ptr_path.unlink()
            del idx.entries[rel_path]

    idx.save()
    click.secho(f"Unstaged {len(rel_paths)} file(s):", fg="green")
    for rel_path in rel_paths:
        click.echo(f"  {rel_path}")


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

    staged, modified, deleted, untracked = compute_status(repo_root, idx)

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
    from .client import VaultClient

    repo_root = ensure_repo()
    idx = Index(repo_root)
    cfg = load_config(repo_root)
    client = VaultClient(cfg.get("remote_url", "http://localhost:8000"), cfg.get("remote_api_token"))

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
            "chunks": e.get("chunks", []),
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

    # Deterministic hash over sorted JSON (preserves DAG integrity), atomic local persist,
    # ref advance, and remote push/queue — shared with `av merge` via _finalize_commit.
    return _finalize_commit(
        repo_root, cfg, client,
        commit_data=commit_data, tree=tree, ref_path=ref_path, head_path=head_path,
        idx=idx, tags=tags, metrics=metrics,
    )


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

    if client.server_available():
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
                    click.secho("  Ref update failed — commit queued for retry (run `av push` later)", fg="yellow")
            else:
                queue_pending_push(repo_root, commit_hash, remote_ref_name)
                click.secho("  Push failed — commit queued for retry (run `av push` later)", fg="yellow")
        except AuthenticationError:
            # client.server_available() only proves the server is up (it's exempt from the
            # auth gate) — it does NOT prove this token is valid, so a bad/stale token surfaces
            # here as an exception instead of push_commit's normal False return. Queue exactly
            # like any other push failure — losing the commit because of a credential problem
            # specifically, vs. a network problem, would be an arbitrary distinction the user
            # shouldn't have to think about.
            queue_pending_push(repo_root, commit_hash, remote_ref_name)
            click.secho(
                "  Server rejected the access token — commit queued for retry "
                "(run `av auth set-token <token>` then `av push`)",
                fg="yellow",
            )
    else:
        queue_pending_push(repo_root, commit_hash, remote_ref_name)
        click.secho("  Server unreachable — commit queued for push (run `av push` later)", fg="yellow")

    return commit_hash


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
    from .client import VaultClient

    repo_root = ensure_repo()
    cfg = load_config(repo_root)
    client = VaultClient(cfg.get("remote_url", "http://localhost:8000"), cfg.get("remote_api_token"))

    heads_dir = repo_root / ".av" / "refs" / "heads"
    commit_hash = target
    ref_name = None

    if (heads_dir / target).exists():
        commit_hash = (heads_dir / target).read_text().strip()
        ref_name = target

    commit_file = None
    try:
        commit_file = find_commit_file(repo_root, commit_hash)
    except AmbiguousCommitHash as exc:
        click.secho(f"Error: {exc.message}", fg="red")
        return
    except FileNotFoundError:
        pass
    commit_data = None

    if commit_file is not None:
        commit_hash = commit_file.stem
        with open(commit_file, "r") as f:
            commit_data = json.load(f)
    elif client.server_available():
        commit_file = repo_root / ".av" / "commits" / f"{commit_hash}.json"
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
        dirty = _collect_dirty_paths(repo_root, idx)
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

    _materialize_tree(repo_root, client, commit_data.get("tree", {}), idx)

    head_path = repo_root / ".av" / "HEAD"
    with open(head_path, "w") as f:
        if ref_name:
            f.write(f"ref: refs/heads/{ref_name}\n")
        else:
            f.write(f"{commit_hash}\n")

    click.secho(f"Checked out '{target}'", fg="green")


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
# av log
# ---------------------------------------------------------------------------

@cli.command()
@click.option("--limit", default=30, show_default=True, help="Maximum commits to show.")
@click.option("--branch", default=None,
              help="Start from this branch's tip instead of HEAD.")
@click.option("--all", "show_all", is_flag=True,
              help="List every local commit across all branches (newest first).")
def log(limit: int, branch: str | None, show_all: bool) -> None:
    """Show local commit history, newest first."""
    from . import history

    repo_root = ensure_repo()

    if show_all:
        commits = history.collect_all_commits(repo_root, limit)
        if not commits:
            click.secho("No commits yet.", fg="yellow")
            return
        decorations = history.collect_branch_decorations(repo_root)
        head_hash = None
    else:
        start, err = history.resolve_start_hash(repo_root, branch)
        if err:
            click.secho(f"Error: {err}", fg="red")
            return
        if start is None:
            click.secho("No commits yet.", fg="yellow")
            return
        commits = history.walk_history(repo_root, start, limit)
        decorations = history.collect_branch_decorations(repo_root)
        head_hash = start

    for commit in commits:
        h = commit["hash"]
        is_head = bool(head_hash) and h == head_hash
        click.echo(history.format_log_line(commit, decorations.get(h, []), is_head))
        meta = history.format_meta_line(commit)
        if meta:
            click.echo(meta)


# ---------------------------------------------------------------------------
# av clone / av pull
# ---------------------------------------------------------------------------

@cli.command("clone")
@click.argument("project")
@click.argument("directory", required=False)
@click.option("--remote-url", default=None, metavar="URL",
              help="Registry to clone from (default: $AV_REMOTE_URL, else http://localhost:8000).")
@click.option("--token", default=None, help="Access token for a Protected registry.")
def clone(project: str, directory: str | None, remote_url: str | None, token: str | None) -> None:
    """Clone an existing project from a registry into a new directory.

    Downloads the project's full commit history (metadata — cheap) and materializes the
    default branch's tip; older versions' large objects lazy-download on first checkout.
    The cloned repo inherits the source project's identity, so pushes from either copy
    land in the same project on the shared registry.
    """
    from .client import VaultClient
    from . import sync

    target = Path(directory).resolve() if directory else Path.cwd() / project
    if target.exists() and any(target.iterdir()):
        click.secho(f"Error: '{target}' already exists and is not empty.", fg="red")
        return

    url = remote_url or os.environ.get("AV_REMOTE_URL") or "http://localhost:8000"
    api_token = token or os.environ.get("AV_API_TOKEN")
    client = VaultClient(url, api_token)
    if not client.server_available():
        click.secho(f"Error: Registry unreachable at {url} — is the backend running?", fg="red")
        return

    try:
        proj = sync.resolve_project(client, project)
    except ValidationError as exc:
        click.secho(f"Error: {exc.message}", fg="red")
        return

    pid = proj["project_id"]
    refs = client.list_refs(project_id=pid)
    branch = sync.pick_default_branch(refs, pid)
    commits = sync.fetch_project_commits(client, pid)
    if not commits:
        click.secho(f"Error: Project '{proj.get('project_name')}' has no commits yet.", fg="red")
        return
    if branch is None:
        # No refs pushed (e.g. only queued/offline commits): fall back to the newest commit.
        branch = "main"
        tip_hash = commits[0]["hash"]
    else:
        tip_hash = refs[f"{pid}/{branch}"]

    target.mkdir(parents=True, exist_ok=True)
    _init_repo_structure(target)
    cfg = load_config(target)
    cfg.update({
        "remote_url": url,
        "login_mode": "local",
        "project_id": pid,
        "project_name": proj.get("project_name") or target.name,
    })
    if api_token:
        cfg["remote_api_token"] = api_token
    save_config(target, cfg)

    for c in commits:
        sync.write_fetched_commit(target, c)

    heads_dir = target / ".av" / "refs" / "heads"
    atomic_write_text(heads_dir / branch, tip_hash)
    atomic_write_text(target / ".av" / "HEAD", f"ref: refs/heads/{branch}\n")
    for stale in heads_dir.iterdir():
        if stale.name != branch and not stale.read_text().strip():
            stale.unlink()

    tip_tree = next((c.get("tree", {}) for c in commits if c["hash"] == tip_hash), {})
    downloaded = sync.ensure_objects_local(target, client, tip_tree)
    _materialize_tree(target, client, tip_tree, Index(target))

    msg = f"Cloned '{proj.get('project_name')}' ({len(commits)} commit(s)) into {target}"
    click.secho(msg, fg="green")
    detail = f"  branch {branch} @ [{tip_hash[:7]}]"
    if downloaded:
        detail += f" — downloaded {downloaded} object(s)"
    click.echo(detail)


@cli.command()
@click.option("--force", "-f", is_flag=True, default=False,
              help="Discard uncommitted local changes instead of aborting.")
def pull(force: bool) -> None:
    """Fetch the current branch from the registry and fast-forward onto it.

    Pull is deliberately fast-forward-only: when local and remote histories have diverged
    it refuses instead of guessing a merge — the fetched commits are stored locally first,
    so `av merge <remote-tip>` resolves it explicitly.
    """
    from .client import VaultClient
    from . import sync

    repo_root = ensure_repo()
    cfg = load_config(repo_root)

    head_content = (repo_root / ".av" / "HEAD").read_text().strip()
    if not head_content.startswith("ref: refs/heads/"):
        click.secho("Error: HEAD is detached — check out a branch before pulling.", fg="red")
        return
    branch = head_content.split("refs/heads/", 1)[1]
    ref_path = repo_root / ".av" / "refs" / "heads" / branch
    local_tip = ref_path.read_text().strip() if ref_path.exists() else ""

    client = VaultClient(cfg.get("remote_url", "http://localhost:8000"), cfg.get("remote_api_token"))
    if not client.server_available():
        click.secho(f"Error: Registry unreachable at {cfg.get('remote_url')}.", fg="red")
        return

    remote_ref = f"{cfg['project_id']}/{branch}"
    remote_tip = client.get_ref(remote_ref)
    if not remote_tip:
        click.secho(f"No remote branch '{branch}' on the registry — nothing to pull.", fg="yellow")
        return
    if remote_tip == local_tip:
        click.secho("Already up to date.")
        return

    # Walk the remote chain back until it joins history we already have, storing every new
    # commit locally as we go — so even a diverged pull leaves the full picture on disk.
    fetched: list[dict] = []
    cursor: str | None = remote_tip
    join_found = False
    while cursor:
        if cursor == local_tip:
            join_found = True
            break
        existing = sync.load_local_commit(repo_root, cursor)
        if existing is not None:
            join_found = True
            break
        row = client.get_commit(cursor)
        if not row:
            click.secho(
                f"Error: Remote history is broken — commit {cursor[:7]}… is referenced but "
                "missing from the registry.",
                fg="red",
            )
            return
        data = sync.normalize_commit_row(row)
        sync.write_fetched_commit(repo_root, data)
        fetched.append(data)
        parents = data["parents"]
        cursor = parents[0] if parents else None

    # Fast-forwarding is only safe when the local tip is an ANCESTOR of the remote tip.
    # Joining the walked chain somewhere below the local tip isn't enough: a repo with its
    # own unpushed commits would otherwise have them silently overwritten by the remote
    # tree. Not-an-ancestor = diverged → hand off to `av merge`.
    ff_allowed = (not local_tip) or (
        join_found and sync.is_ancestor(lambda h: sync.load_local_commit(repo_root, h),
                                        local_tip, remote_tip)
    )
    if not ff_allowed:
        click.secho(
            f"Local and remote '{branch}' have diverged.\n"
            f"The remote tip [{remote_tip[:7]}] and its history are now local — resolve with:\n"
            f"  av merge {remote_tip[:7]}",
            fg="yellow",
        )
        return

    idx = Index(repo_root)
    if not force:
        dirty = _collect_dirty_paths(repo_root, idx)
        if dirty:
            click.secho(
                "Error: You have uncommitted changes that would be overwritten by pull:",
                fg="red",
            )
            for d in dirty[:20]:
                click.echo(f"  {d}")
            click.secho("Commit them (or use --force to discard), then pull again.", fg="yellow")
            return

    tip_data = sync.load_local_commit(repo_root, remote_tip)
    tree = tip_data.get("tree", {}) if tip_data else {}
    sync.ensure_objects_local(repo_root, client, tree)
    _materialize_tree(repo_root, client, tree, idx)
    atomic_write_text(ref_path, remote_tip)

    click.secho(
        f"Fast-forwarded {branch}: {local_tip[:7] or '(empty)'} → {remote_tip[:7]} "
        f"({len(fetched)} new commit(s))",
        fg="green",
    )


@cli.command()
@click.argument("target")
@click.option("-m", "--message", default=None, help="Override the default merge commit message.")
@click.option("--ours", "policy_ours", is_flag=True, default=False,
              help="Resolve conflicting files by keeping THIS branch's version.")
@click.option("--theirs", "policy_theirs", is_flag=True, default=False,
              help="Resolve conflicting files by taking TARGET's version.")
@click.option("--no-ff", is_flag=True, default=False,
              help="Create a merge commit even when a fast-forward would do.")
def merge(target: str, message: str | None, policy_ours: bool, policy_theirs: bool,
          no_ff: bool) -> None:
    """Merge another branch or commit into the current branch.

    Tree-level three-way merge against the nearest common ancestor: per file, whichever
    side changed wins; if BOTH sides changed the same file differently the merge aborts
    cleanly (nothing touched) and lists the conflicts — resolve with --ours/--theirs.
    Successful non-fast-forward merges create a two-parent merge commit that syncs to the
    registry (v1.1.1 servers store both parents).
    """
    from .client import VaultClient
    from . import sync
    from .merge import find_merge_base, three_way_tree_merge, tree_is_flat, summarize_changes

    repo_root = ensure_repo()
    cfg = load_config(repo_root)
    idx = Index(repo_root)

    head_content = (repo_root / ".av" / "HEAD").read_text().strip()
    if not head_content.startswith("ref: refs/heads/"):
        click.secho("Error: HEAD is detached — check out a branch before merging.", fg="red")
        return
    branch = head_content.split("refs/heads/", 1)[1]
    our_ref_path = repo_root / ".av" / "refs" / "heads" / branch
    ours = our_ref_path.read_text().strip() if our_ref_path.exists() else ""
    if not ours:
        click.secho(f"Error: Branch '{branch}' has no commits yet — commit first.", fg="red")
        return
    if policy_ours and policy_theirs:
        click.secho("Error: --ours and --theirs are mutually exclusive.", fg="red")
        return

    heads_dir = repo_root / ".av" / "refs" / "heads"

    def _resolve_target() -> str | None:
        if (heads_dir / target).exists():
            return (heads_dir / target).read_text().strip()
        try:
            resolved = find_commit_file(repo_root, target)
            return resolved.stem
        except FileNotFoundError:
            return None
        except AmbiguousCommitHash as exc:
            click.secho(f"Error: {exc.message}", fg="red")
            return None

    theirs = _resolve_target()
    client = VaultClient(cfg.get("remote_url", "http://localhost:8000"), cfg.get("remote_api_token"))
    if theirs is None and client.server_available():
        row = client.get_commit(target)
        if row:
            data = sync.normalize_commit_row(row)
            sync.write_fetched_commit(repo_root, data)
            theirs = data["hash"]
    if not theirs:
        click.secho(f"Error: Branch or commit '{target}' not found.", fg="red")
        return
    if theirs == ours:
        click.secho("Already up to date.")
        return

    load = lambda h: sync.load_local_commit(repo_root, h)

    # Make sure both sides' full trees are readable locally before computing anything;
    # fetched remote history (via av pull) already lands here, but a hand-given hash may not.
    for h in {theirs}:
        if load(h) is None and client.server_available():
            row = client.get_commit(h)
            if row:
                sync.write_fetched_commit(repo_root, sync.normalize_commit_row(row))

    base = find_merge_base(load, ours, theirs)
    if base == theirs:
        click.secho("Already up to date.")
        return

    dirty = _collect_dirty_paths(repo_root, idx)
    fast_forward = base == ours
    if fast_forward and not no_ff and not dirty:
        tip_data = load(theirs) or {}
        tree = tip_data.get("tree", {})
        sync.ensure_objects_local(repo_root, client, tree)
        _materialize_tree(repo_root, client, tree, idx)
        atomic_write_text(our_ref_path, theirs)
        click.secho(f"Fast-forwarded {branch}: {ours[:7]} → {theirs[:7]}", fg="green")
        return

    if dirty:
        click.secho(
            "Error: You have uncommitted changes that would be overwritten by merge:",
            fg="red",
        )
        for d in dirty[:20]:
            click.echo(f"  {d}")
        click.secho("Commit them (or stash them), then merge again.", fg="yellow")
        return

    base_tree = (load(base) or {}).get("tree", {}) if base else {}
    ours_tree = (load(ours) or {}).get("tree", {})
    theirs_data = load(theirs) or {}
    theirs_tree = theirs_data.get("tree", {})

    if not all(tree_is_flat(t) for t in (base_tree, ours_tree, theirs_tree)):
        click.secho(
            "Error: Merge targets a legacy-format commit ({code/artifacts} tree); "
            "only unified flat-tree commits (post-PR #8) can be merged.",
            fg="red",
        )
        return

    merged, conflicts = three_way_tree_merge(base_tree, ours_tree, theirs_tree)
    if conflicts and not (policy_ours or policy_theirs):
        click.secho(
            f"Merge conflicts in {len(conflicts)} file(s) — both branches changed them "
            "differently. Nothing was modified. Resolve with:",
            fg="red",
        )
        for p in conflicts[:20]:
            click.echo(f"  {p}")
        if len(conflicts) > 20:
            click.echo(f"  … and {len(conflicts) - 20} more")
        click.secho(
            '  av merge <target> --ours     keep this branch\'s versions\n'
            '  av merge <target> --theirs   take the target\'s versions',
            fg="yellow",
        )
        return

    policy_side = ours_tree if policy_ours else theirs_tree
    resolved_conflicts = 0
    if conflicts:
        for p in conflicts:
            entry = policy_side.get(p)
            if entry is None:
                merged.pop(p, None)
            else:
                merged[p] = entry
        resolved_conflicts = len(conflicts)

    sync.ensure_objects_local(repo_root, client, merged)
    _materialize_tree(repo_root, client, merged, Index(repo_root))

    head_path = repo_root / ".av" / "HEAD"
    commit_data: dict = {
        "parents": [ours, theirs],
        "author": os.environ.get("AV_AUTHOR", "anonymous"),
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "message": message or f"Merge {target} into {branch}",
        "tree": merged,
        "tags": [],
        "metrics": {},
        "project_id": cfg["project_id"],
        "project_name": cfg["project_name"],
    }
    merge_hash = _finalize_commit(
        repo_root, cfg, client,
        commit_data=commit_data, tree=merged, ref_path=our_ref_path,
        head_path=head_path, idx=Index(repo_root),
    )

    added, removed, changed = summarize_changes(ours_tree, merged)
    note = f", {resolved_conflicts} conflict(s) auto-resolved via --{'ours' if policy_ours else 'theirs'}" \
        if resolved_conflicts else ""
    click.secho(
        f"Merged {target} into {branch}: +{added} -{removed} ~{changed} file(s)"
        f"{note} [{merge_hash[:7]}]",
        fg="green",
    )


# ---------------------------------------------------------------------------
# av stash
# ---------------------------------------------------------------------------

def _stash_dir(repo_root: Path) -> Path:
    return repo_root / ".av" / "stash"


def _list_stash_files(repo_root: Path) -> list[Path]:
    stash_dir = _stash_dir(repo_root)
    if not stash_dir.exists():
        return []
    # Filenames are timestamp-prefixed (YYYYMMDDTHHMMSSZ-<shortid>.json), so a reverse
    # lexicographic sort is already newest-first.
    return sorted(stash_dir.glob("*.json"), reverse=True)


def _resolve_stash_file(repo_root: Path, stash_id: str | None) -> Path | None:
    files = _list_stash_files(repo_root)
    if not files:
        return None
    if stash_id is None:
        return files[0]
    for f in files:
        if f.stem == stash_id or f.name == stash_id:
            return f
    return None


def _stash_push(message: str | None) -> None:
    from .client import VaultClient

    repo_root = ensure_repo()
    idx = Index(repo_root)
    cfg = load_config(repo_root)
    client = VaultClient(cfg.get("remote_url", "http://localhost:8000"), cfg.get("remote_api_token"))
    threshold_bytes = cfg.get("lfs_threshold_mb", 50) * 1024 * 1024

    staged, modified, deleted, _untracked = compute_status(repo_root, idx)
    if deleted:
        click.secho(
            f"Skipping {len(deleted)} deleted file(s) — not yet supported by `av stash`.",
            fg="yellow",
        )

    dirty_paths = staged + modified  # compute_status's branches are mutually exclusive
    if not dirty_paths:
        click.secho("No local changes to stash", fg="yellow")
        return

    head_tree = resolve_head_tree(repo_root)
    stash_entries = []

    from . import attributes

    attr_rules = attributes.load_attributes(repo_root)
    for rel_path in dirty_paths:
        was_staged = rel_path in staged
        if not was_staged:
            # Modified-but-unstaged: get its current content safely into the CAS first,
            # exactly the way `av add` would — so reverting the working copy below doesn't
            # lose it.
            stage_one_file(repo_root, idx, threshold_bytes, repo_root / rel_path, rel_path,
                           attributes.flags_for(attr_rules, rel_path))

        entry = idx.entries[rel_path]
        stash_entries.append({
            "rel_path": rel_path,
            "hash": entry["hash"],
            "size": entry["size"],
            "type": entry["type"],
            "layers": entry.get("layers", []),
            "pointer": entry.get("pointer"),
            "was_staged": was_staged,
        })

        head_data = head_tree.get(rel_path)
        if head_data:
            materialize_file(repo_root, client, rel_path, head_data["hash"], head_data.get("layers", []))
            new_entry = {
                "hash": head_data["hash"],
                "size": head_data["size"],
                "mtime_ns": 0,
                "type": head_data["type"],
                "staged": False,
                "pointer": rel_path + ".av-pointer" if head_data["type"] == "artifact" else None,
            }
            if head_data.get("layers"):
                new_entry["layers"] = head_data["layers"]
            idx.entries[rel_path] = new_entry
            # Re-stat now that HEAD's content has actually been written to disk, so the index
            # matches the real (clean) file instead of the 0 placeholder above — otherwise
            # `av status` would immediately call every reverted file "modified" again.
            fpath = repo_root / rel_path
            if fpath.exists():
                m = get_file_meta_safe(str(fpath))
                idx.entries[rel_path]["size"] = m["size"]
                idx.entries[rel_path]["mtime_ns"] = m["mtime_ns"]
        else:
            # Never committed — same as `av unstage` for a new file: it disappears until
            # popped, since there's no HEAD baseline to revert to.
            remove_file_and_pointer(repo_root, rel_path)
            del idx.entries[rel_path]

    idx.save()

    stash_dir = _stash_dir(repo_root)
    stash_dir.mkdir(parents=True, exist_ok=True)
    # Microsecond resolution, not just seconds — two stashes created in quick succession (e.g.
    # back-to-back in a test, or a fast manual `av stash` / `av stash` retry) would otherwise
    # share the same second-resolution prefix and sort arbitrarily (by the random shortid)
    # instead of newest-first (found via the test suite, not just inferred).
    stash_id = f"{datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%S%f')}-{uuid.uuid4().hex[:6]}"

    branch = "detached"
    head_path = repo_root / ".av" / "HEAD"
    if head_path.exists():
        head_content = head_path.read_text().strip()
        if head_content.startswith("ref: refs/heads/"):
            branch = head_content.split("/")[-1]

    atomic_write_json(stash_dir / f"{stash_id}.json", {
        "id": stash_id,
        "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "branch": branch,
        "message": message,
        "entries": stash_entries,
    })

    label = f": {message}" if message else ""
    click.secho(f"Saved working directory state: stash@{{0}}{label}", fg="green")


def _stash_apply_or_pop(stash_id: str | None, delete_after: bool) -> None:
    from .client import VaultClient

    repo_root = ensure_repo()
    stash_file = _resolve_stash_file(repo_root, stash_id)
    if stash_file is None:
        msg = f"No stash found matching '{stash_id}'" if stash_id else "No stashes to apply"
        click.secho(msg, fg="yellow")
        return

    with open(stash_file, "r") as f:
        record = json.load(f)

    cfg = load_config(repo_root)
    client = VaultClient(cfg.get("remote_url", "http://localhost:8000"), cfg.get("remote_api_token"))
    idx = Index(repo_root)
    head_tree = resolve_head_tree(repo_root)  # only needed for was_staged=False entries below

    for entry in record["entries"]:
        rel_path = entry["rel_path"]
        layers = entry.get("layers", [])
        # v1 doesn't attempt conflict detection against a dirty tree — this overwrites
        # whatever's currently at rel_path, same caveat as the plan this was built from.
        materialize_file(repo_root, client, rel_path, entry["hash"], layers)

        if entry["was_staged"]:
            # Was staged before the stash: restore it staged again, with the real (dirty)
            # content's hash/stat — `status()` shows staged entries as "to be committed"
            # purely from the `staged` flag, regardless of stat matching.
            meta = get_file_meta_safe(str(repo_root / rel_path))
            new_entry = {
                "hash": entry["hash"], "size": meta["size"], "mtime_ns": meta["mtime_ns"],
                "type": entry["type"], "staged": True, "pointer": entry.get("pointer"),
            }
        else:
            # Was modified-but-unstaged before the stash: the working-tree file is restored
            # to that dirty content above, but the index entry must go back to HEAD's
            # baseline (with a deliberately non-matching mtime) so `status()`'s stat-mismatch
            # check reports "modified" again instead of looking clean — mirroring exactly how
            # `_stash_push` represents an unstaged modification in the first place.
            head_data = head_tree.get(rel_path, {})
            new_entry = {
                "hash": head_data.get("hash", entry["hash"]),
                "size": head_data.get("size", entry["size"]),
                "mtime_ns": 0,
                "type": entry["type"], "staged": False, "pointer": entry.get("pointer"),
            }
        if layers:
            new_entry["layers"] = layers
        idx.entries[rel_path] = new_entry

    idx.save()

    if delete_after:
        stash_file.unlink()

    verb = "Popped" if delete_after else "Applied"
    click.secho(f"{verb} stash {stash_file.stem} ({len(record['entries'])} file(s) restored)", fg="green")


@cli.group(invoke_without_command=True, name="stash")
@click.option("-m", "--message", default=None, help="Optional label for this stash.")
@click.pass_context
def stash(ctx: click.Context, message: str | None) -> None:
    """Temporarily shelve uncommitted changes (staged + modified tracked files).

    Reverts the working tree to match HEAD — same scope as `git stash`: staged and modified
    tracked files, not untracked or deleted ones — so `checkout`/`branch` can proceed without
    --force. `av stash pop` brings everything back exactly as it was, staged or not.
    """
    if ctx.invoked_subcommand is None:
        _stash_push(message)


@stash.command("push")
@click.option("-m", "--message", default=None, help="Optional label for this stash.")
def stash_push_cmd(message: str | None) -> None:
    """Shelve uncommitted changes (same as bare `av stash`)."""
    _stash_push(message)


@stash.command("list")
def stash_list_cmd() -> None:
    """List stashes, newest first."""
    repo_root = ensure_repo()
    files = _list_stash_files(repo_root)
    if not files:
        click.secho("No stashes", fg="yellow")
        return
    for i, f in enumerate(files):
        with open(f, "r") as fh:
            record = json.load(fh)
        label = f": {record['message']}" if record.get("message") else ""
        click.echo(f"stash@{{{i}}}  {record['created_at']}  ({len(record['entries'])} file(s)){label}  [{f.stem}]")


@stash.command("pop")
@click.argument("stash_id", required=False)
def stash_pop_cmd(stash_id: str | None) -> None:
    """Apply the most recent (or a given) stash, then delete it."""
    _stash_apply_or_pop(stash_id, delete_after=True)


@stash.command("apply")
@click.argument("stash_id", required=False)
def stash_apply_cmd(stash_id: str | None) -> None:
    """Apply the most recent (or a given) stash, keeping it for reuse."""
    _stash_apply_or_pop(stash_id, delete_after=False)


@stash.command("drop")
@click.argument("stash_id", required=False)
def stash_drop_cmd(stash_id: str | None) -> None:
    """Delete a stash without applying it."""
    repo_root = ensure_repo()
    stash_file = _resolve_stash_file(repo_root, stash_id)
    if stash_file is None:
        msg = f"No stash found matching '{stash_id}'" if stash_id else "No stashes to drop"
        click.secho(msg, fg="yellow")
        return
    stash_file.unlink()
    click.secho(f"Dropped stash {stash_file.stem}", fg="green")


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
    from .client import VaultClient

    repo_root = ensure_repo()
    cfg = load_config(repo_root)
    client = VaultClient(cfg.get("remote_url", "http://localhost:8000"), cfg.get("remote_api_token"))

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
        click.secho(f"[OK] Pushed {pushed} commit(s) to the remote server", fg="green")
    if still_pending:
        click.secho(f"  {len(still_pending)} commit(s) still pending", fg="yellow")


@cli.command()
def gc() -> None:
    """Trigger garbage collection on the remote CAS server."""
    from .client import VaultClient

    repo_root = ensure_repo()
    cfg = load_config(repo_root)
    client = VaultClient(cfg.get("remote_url", "http://localhost:8000"), cfg.get("remote_api_token"))

    if not client.server_available():
        click.secho("Error: Remote server is not reachable.", fg="red")
        return

    result = client.run_gc()
    if result:
        click.secho("[OK] Garbage collection complete", fg="green")
        click.echo(
            f"  Alive objects : {result.get('alive_objects', '?')}\n"
            f"  Deleted objects: {result.get('deleted_objects', '?')}\n"
            f"  Reused trees  : {result.get('reused_trees', '?')}"
        )
    else:
        click.secho("GC request failed. Check server logs.", fg="red")


@cli.group()
def auth() -> None:
    """Manage the optional shared-secret access token ("Protected" mode).

    Unset/empty (the default) means the registry is "Anonymous" — every route behaves exactly
    as it always has, no credentials needed. Setting a token switches the server to
    "Protected" — every route (reads included) then requires it. See `av init`'s
    Anonymous/Protected prompt for the same choice at setup time.
    """


def _restart_server_for_token_change(repo_root: Path) -> bool:
    """Best-effort restart of the running server after `.env`'s AV_API_TOKEN changes, so the
    new value takes effect immediately instead of only on the next manual restart. Returns
    False (with a clear message already printed) if Docker isn't reachable or the restart
    itself fails — `write_env_token` has already succeeded by the time this runs, so a failed
    restart here just means "takes effect next time the stack starts," not data loss.
    """
    from . import docker_runtime

    if docker_runtime.check_docker_running() != docker_runtime.DockerCheckResult.RUNNING:
        click.secho(
            "Docker isn't running — saved, but it'll take effect next time the server starts.",
            fg="yellow",
        )
        return False
    compose_file, _ = docker_runtime.resolve_compose_file(_find_source_root())
    if not docker_runtime.restart_service(compose_file, "aether-vault-server"):
        click.secho(
            "Saved, but restarting the server automatically failed — restart it manually "
            "(`docker compose up -d aether-vault-server`) for the change to take effect.",
            fg="yellow",
        )
        return False
    return True


def _generate_and_apply_token(repo_root: Path, token: str | None = None) -> str:
    """Generates (if not given) and applies a token: writes it to .env next to whichever
    compose file is in play, saves it to this repo's config, and restarts the running server
    so it takes effect immediately. Shared by `av auth set-token` and `av init`'s "Generate a
    new token" choice so the two don't duplicate this logic."""
    import secrets as secrets_module

    from . import docker_runtime

    token = token or secrets_module.token_urlsafe(32)

    compose_file, _ = docker_runtime.resolve_compose_file(_find_source_root())
    docker_runtime.write_env_token(compose_file, token)

    cfg = load_config(repo_root)
    cfg["remote_api_token"] = token
    save_config(repo_root, cfg)

    _restart_server_for_token_change(repo_root)
    return token


@auth.command(name="set-token")
@click.argument("token", required=False, default=None)
def auth_set_token(token: str | None) -> None:
    """Set (or rotate) the access token, generating one if TOKEN is omitted.

    Writes it to the .env file next to whichever docker-compose file is in play, saves the
    same value to this repo's local config so the CLI starts sending it immediately, and
    restarts the running server so the change takes effect right away. Re-running this with a
    new value (or none, to generate a fresh one) is also the "I forgot it" path — there's no
    password-reset flow since this is a shared secret, not a real account: recovery is simply
    "you have shell access to the machine running the server."
    """
    repo_root = ensure_repo()
    token = _generate_and_apply_token(repo_root, token)
    click.secho(f"Token set: {token}", fg="green")
    click.secho("Save this — it won't be shown again. Share it with teammates who need access.", fg="yellow")


@auth.command(name="clear")
def auth_clear() -> None:
    """Remove the access token everywhere — back to "Anonymous" mode."""
    from . import docker_runtime

    repo_root = ensure_repo()
    compose_file, _ = docker_runtime.resolve_compose_file(_find_source_root())
    docker_runtime.write_env_token(compose_file, None)

    cfg = load_config(repo_root)
    cfg.pop("remote_api_token", None)
    save_config(repo_root, cfg)

    _restart_server_for_token_change(repo_root)
    click.secho("Token cleared — this registry is now Anonymous (no token required).", fg="green")


@auth.command(name="status")
def auth_status() -> None:
    """Report whether an access token is currently configured, without printing it."""
    repo_root = ensure_repo()
    cfg = load_config(repo_root)
    token = cfg.get("remote_api_token")
    if not token:
        click.secho("Anonymous — no token configured for this repo.", fg="yellow")
        return
    masked = f"{'*' * max(len(token) - 4, 0)}{token[-4:]}"
    click.secho(f"Protected — token configured for this repo: {masked}", fg="green")


def _print_real_repo_speed_diagnostics(repo_root: Path) -> None:
    """`av doctor --speed` — read-only timing snapshot of the current repo."""
    probes = speedcheck.run_real_repo_probes(repo_root, load_config, iter_working_files)
    click.echo("")
    click.secho("=== Speed diagnostics (this repo) ===", bold=True, fg="cyan")
    click.echo(f"{'Probe':<42} {'Time':>10}")
    click.echo("-" * 53)
    for label, elapsed_ms in probes:
        click.echo(f"{label:<42} {elapsed_ms:>8.1f} ms")


@cli.command()
@click.option("--fix", is_flag=True, default=False,
              help="Repair fixable issues: re-link pointers, clear tmp leftovers, clear/retry pending-push entries.")
@click.option("--dry-run", "dry_run", is_flag=True, default=False,
              help="With --fix, preview what would be repaired without changing anything.")
@click.option("--speed", "speed", is_flag=True, default=False,
              help="Also print a read-only timing snapshot of this repo's hot paths (index load, config load, file scan, storage stats).")
def doctor(fix: bool, dry_run: bool, speed: bool) -> None:
    """Diagnose common repo and environment problems.

    Read-only by default: reports issues (native core availability, server reachability,
    index/pointer consistency, pending-push queue, leftover temp files) without modifying
    anything. Pass --fix to repair what's safely recoverable, or --fix --dry-run to preview
    what --fix would do without changing anything.
    """
    from .client import VaultClient

    repo_root = ensure_repo()
    cfg = load_config(repo_root)
    client = VaultClient(cfg.get("remote_url", "http://localhost:8000"), cfg.get("remote_api_token"))
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

    if _get_aether_core():
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
    # fail to materialize that file's real content. Split artifacts don't store a whole-file
    # blob at all (see add()'s comment) — for those, "missing content" means a missing
    # *layer* (safetensors) or *chunk* (.pt/.pth/.ckpt CDC), not the absent-by-design
    # whole-file object.
    def _missing_parts(entry: dict, key: str) -> list[dict]:
        return [
            part for part in entry.get(key) or []
            if not (av_dir / "objects" / part["hash"][:2] / part["hash"][2:]).exists()
        ]

    def _artifact_content_missing(entry: dict) -> bool:
        if entry.get("layers"):
            return bool(_missing_parts(entry, "layers"))
        if entry.get("chunks"):
            return bool(_missing_parts(entry, "chunks"))
        return not (av_dir / "objects" / entry["hash"][:2] / entry["hash"][2:]).exists()

    orphaned = [
        rel_path
        for rel_path, entry in idx.entries.items()
        if entry.get("pointer") and _artifact_content_missing(entry)
    ]
    recovered_orphans = []
    unrecovered_orphans = []
    for rel_path in orphaned:
        entry = idx.entries[rel_path]
        can_recover = False
        parts_key = "layers" if entry.get("layers") else ("chunks" if entry.get("chunks") else None)
        if parts_key:
            missing = _missing_parts(entry, parts_key)
            if fix and preview:
                can_recover = all(client.object_exists(p["hash"]) for p in missing)
            elif fix:
                can_recover = client.server_available() and all(
                    client.download_object(p["hash"], av_dir / "objects" / p["hash"][:2] / p["hash"][2:])
                    for p in missing
                )
        else:
            h = entry["hash"]
            obj_path = av_dir / "objects" / h[:2] / h[2:]
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

    if speed:
        _print_real_repo_speed_diagnostics(repo_root)


def _print_synthetic_speed_check() -> None:
    """`av test --speed` — synthetic, repeatable benchmark of av's own hot paths."""
    with tempfile.TemporaryDirectory(prefix="av-speedcheck-") as tmp:
        probes = speedcheck.run_synthetic_probes(load_config, iter_working_files, Path(tmp))

    click.secho("\n=== Speed check (synthetic fixtures) ===", bold=True, fg="cyan")
    click.echo(f"{'Probe':<40} {'Time':>10} {'Budget':>10}  Status")
    click.echo("-" * 75)
    slow_count = 0
    for label, elapsed_ms, budget_ms in probes:
        if budget_ms is not None and elapsed_ms > budget_ms:
            slow_count += 1
            status, color = "SLOW", "yellow"
        else:
            status, color = "OK", "green"
        budget_str = f"{budget_ms:.0f} ms" if budget_ms is not None else "-"
        click.secho(f"{label:<40} {elapsed_ms:>7.1f} ms {budget_str:>10}  {status}", fg=color)

    if slow_count:
        click.echo(f"\n{slow_count} of {len(probes)} probes exceeded their budget — see python/av_cli/speedcheck.py to adjust thresholds.")

    av_path = shutil.which("av")
    if av_path is None:
        click.echo("\n(av CLI not found on PATH — skipping end-to-end CLI timing.)")
        return

    with tempfile.TemporaryDirectory(prefix="av-speedcheck-cli-") as tmp:
        cli_probes = speedcheck.run_av_cli_probes(av_path, Path(tmp))

    click.secho("\n=== Speed check (av CLI, end-to-end) ===", bold=True, fg="cyan")
    click.echo(f"{'Probe':<28} {'Time':>10}")
    click.echo("-" * 39)
    for label, elapsed_ms in cli_probes:
        click.echo(f"{label:<28} {elapsed_ms:>7.1f} ms")


def _update_readme_test_badge(passed: int, failed: int) -> None:
    """Keep README.md's `tests-N%2FM passing` badge in sync with the real pytest results.

    Only called after a full, unfiltered `av test` run (no `-k`) — a scoped subset would
    overwrite the badge with a misleadingly small total otherwise.
    """
    total = passed + failed
    if total == 0:
        return  # parse failed or nothing collected — leave the badge alone rather than zero it out
    source_root = _find_source_root()
    readme_path = source_root / "README.md"
    if not readme_path.is_file():
        return
    text = readme_path.read_text(encoding="utf-8")
    color = "brightgreen" if failed == 0 else "red"
    pattern = re.compile(
        r'https://img\.shields\.io/badge/tests-\d+%2F\d+%20passing-[a-z]+(\?[^"]*)"\s+alt="\d+ of \d+ tests passing"'
    )

    def _replace(m: "re.Match[str]") -> str:
        return (
            f'https://img.shields.io/badge/tests-{passed}%2F{total}%20passing-{color}{m.group(1)}"'
            f' alt="{passed} of {total} tests passing"'
        )

    updated = pattern.sub(_replace, text, count=1)
    if updated != text:
        atomic_write_text(readme_path, updated)
        click.secho(f"Updated README.md test badge: {passed}/{total} passing", fg="cyan")


@cli.command(name="test")
@click.option("-k", "test_filter", default=None, help="Only run tests matching this substring (forwarded to pytest -k).")
@click.option("--cov", is_flag=True, default=False, help="Run with a coverage report (--cov=python --cov-report=term-missing).")
@click.option("--webui", "run_webui", is_flag=True, default=False, help="Also run the webui/ Vitest suite (npm test) after the Python suite.")
@click.option("--speed", "speed", is_flag=True, default=False,
              help="Also run a synthetic speed benchmark of av's hot paths (and the webui/ bench suite, with --webui).")
def test_cmd(test_filter: str | None, cov: bool, run_webui: bool, speed: bool) -> None:
    """(Development only) Run Aether-Vault's own pytest suite from source, and optionally the
    webui/ Vitest suite too.

    Requires an editable/dev install (`pip install -e .[dev]`) — and, for --webui, `npm install`
    already run inside webui/. This is not a tool for inspecting an end user's .av/ repository;
    see `av doctor` for that.
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
    # Force color even though stdout is about to be piped (not a real tty) for output capture
    # below — otherwise pytest auto-detects the pipe and silently drops all colorization.
    args += ["--color=yes"]
    if test_filter:
        args += ["-k", test_filter]
    if cov:
        args += ["--cov=python", "--cov-report=term-missing"]
    if speed:
        args += ["--durations=20"]

    click.secho("=== Python test suite ===", bold=True, fg="cyan")
    click.secho(f"Running Aether-Vault's test suite (pytest {' '.join(args[3:])})...", fg="cyan")
    # Stream pytest's output live (line by line, as it would print unbuffered) while also
    # collecting it, so the final "N passed, M failed" summary can be parsed afterward to keep
    # README.md's test-count badge honest without a second, redundant pytest run.
    process = subprocess.Popen(
        args, cwd=source_root, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1
    )
    output_lines: list[str] = []
    assert process.stdout is not None
    for line in process.stdout:
        click.echo(line, nl=False)
        output_lines.append(line)
    process.wait()
    exit_code = process.returncode

    if test_filter is None:
        # Strip ANSI escapes (forced on above via --color=yes) before parsing — color codes can
        # otherwise sit between a number and "passed"/"failed" and break the regex match.
        captured = re.sub(r"\x1b\[[0-9;]*m", "", "".join(output_lines))
        passed_match = re.search(r"(\d+) passed", captured)
        failed_match = re.search(r"(\d+) failed", captured)
        error_match = re.search(r"(\d+) error", captured)
        if passed_match:
            passed_n = int(passed_match.group(1))
            failed_n = (int(failed_match.group(1)) if failed_match else 0) + (
                int(error_match.group(1)) if error_match else 0
            )
            _update_readme_test_badge(passed_n, failed_n)

    if run_webui:
        webui_dir = source_root / "webui"
        if not webui_dir.is_dir() or not (webui_dir / "package.json").exists():
            click.secho(
                "av test --webui requires a development install with the webui/ source present "
                "(run from a git clone, not a built wheel).",
                fg="red",
            )
            sys.exit(1)

        click.secho("\n=== Web UI test suite (webui/) ===", bold=True, fg="cyan")
        # shutil.which (not a bare "npm" argv) — on Windows, `npm` resolves to `npm.cmd`, which
        # subprocess.run(["npm", ...]) frequently fails to locate/execute even when npm is
        # genuinely installed and on PATH; resolving the full path first (as `which` does, via
        # PATHEXT) avoids a false "npm not found" on a machine that actually has it.
        npm_path = shutil.which("npm")
        if npm_path is None:
            click.secho(
                "npm not found on PATH — install Node.js to run the webui/ Vitest suite, "
                "or omit --webui.",
                fg="red",
            )
            sys.exit(1)
        webui_result = subprocess.run([npm_path, "test"], cwd=webui_dir)
        if webui_result.returncode != 0:
            exit_code = webui_result.returncode

        if speed:
            click.secho("\n=== Web UI speed bench (webui/) ===", bold=True, fg="cyan")
            bench_result = subprocess.run([npm_path, "run", "bench"], cwd=webui_dir)
            if bench_result.returncode != 0:
                exit_code = bench_result.returncode

    if speed:
        _print_synthetic_speed_check()

    sys.exit(exit_code)


BENCHMARK_NAMES = [
    "hashing_throughput",
    "safetensors_dedup",
    "commit_push_latency",
    "noop_status_speed",
    "cold_clone",
    "partial_checkpoint_fetch",
    "storage_footprint_curve",
    "concurrent_push",
    "gc_throughput",
]


@cli.command()
@click.option("--only", "only", multiple=True,
              help=f"Only run these benchmarks by name (repeatable). Default: run all {len(BENCHMARK_NAMES)}. Names: {', '.join(BENCHMARK_NAMES)}.")
@click.option("--vs", "vs_tools", multiple=True, default=("git-lfs", "dvc", "mlflow"),
              help="Competitor tools to include (repeatable). Default: all three. Aether-Vault itself always runs.")
@click.option("--markdown", "markdown_out", type=click.Path(), default=None,
              help="Write a complete, ready-to-commit Markdown report (header/legend/methodology notes + every benchmark's table) to this path — for regenerating BENCHMARKS.md.")
@click.option("--save-json", "save_json_out", type=click.Path(), default=None,
              help="Save this run's av-only numbers as a JSON snapshot, for a future --baseline comparison.")
@click.option("--baseline", "baseline_path", type=click.Path(exists=True), default=None,
              help="Compare this run's av numbers against a prior --save-json snapshot and report any row that regressed past the 1.5x verdict threshold. Exits non-zero if any regression is found.")
def benchmark(only: tuple, vs_tools: tuple, markdown_out: str | None, save_json_out: str | None, baseline_path: str | None) -> None:
    """(Development only) Run cross-tool benchmark comparisons against DVC, Git LFS, and MLflow.

    Requires an editable/dev install (`pip install -e .[dev,benchmarks]`) — see benchmarks/README.md
    for installing DVC/MLflow as comparison targets. A tool not found on PATH is skipped and
    labeled "not installed" in the output, never given a fabricated number.
    """
    source_root = _find_source_root()
    benchmarks_dir = source_root / "benchmarks"
    if not benchmarks_dir.is_dir():
        click.secho(
            "av benchmark requires a development install; run from a git clone with `pip install -e .[dev]`",
            fg="red",
        )
        sys.exit(1)

    names = list(only) if only else BENCHMARK_NAMES
    invalid = [n for n in names if n not in BENCHMARK_NAMES]
    if invalid:
        click.secho(f"Unknown benchmark name(s): {', '.join(invalid)}. Valid names: {', '.join(BENCHMARK_NAMES)}", fg="red")
        sys.exit(1)

    if str(source_root) not in sys.path:
        sys.path.insert(0, str(source_root))
    from benchmarks.tool_runner import (
        compare_to_baseline,
        print_regression_report,
        print_table,
        render_doc_header,
        result_to_markdown,
        results_to_json,
        METHODOLOGY_NOTES,
    )

    valid_competitors = {"git-lfs", "dvc", "mlflow"}
    invalid_tools = [t for t in vs_tools if t not in valid_competitors]
    if invalid_tools:
        click.secho(f"Unknown --vs tool(s): {', '.join(invalid_tools)}. Valid: {', '.join(sorted(valid_competitors))}", fg="red")
        sys.exit(1)
    tool_order = ["av", *[t for t in vs_tools]]

    results = []
    markdown_chunks = []
    for name in names:
        module = importlib.import_module(f"benchmarks.bench_{name}")
        result = module.run(tool_order=tool_order)
        print_table(result)
        results.append(result)
        markdown_chunks.append(result_to_markdown(result))

    if markdown_out:
        doc = render_doc_header(source_root) + METHODOLOGY_NOTES + "\n".join(markdown_chunks)
        Path(markdown_out).write_text(doc, encoding="utf-8")
        click.echo(f"\nWrote {markdown_out}")

    if save_json_out:
        Path(save_json_out).write_text(json.dumps(results_to_json(results), indent=2), encoding="utf-8")
        click.echo(f"Saved benchmark snapshot to {save_json_out}")

    if baseline_path:
        baseline = json.loads(Path(baseline_path).read_text(encoding="utf-8"))
        findings = compare_to_baseline(results, baseline)
        regressed = print_regression_report(findings)
        if regressed:
            sys.exit(1)


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
    from . import docker_runtime

    result = docker_runtime.ensure_local_backend_running(
        _find_source_root(), open_browser=True, rebuild=rebuild,
    )
    if result.success:
        suffix = (
            "" if result.already_running
            else "\n   (Use `av webui --rebuild` if you changed webui source and need a fresh image.)"
        )
        click.secho(
            f"\n[OK] Aether-Vault Web UI is running at {result.backend_url}\n"
            "   Press Ctrl+C or run 'docker compose down' to stop." + suffix,
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


def run() -> None:
    """Console-script entry point (`av = "av_cli.main:run"` in pyproject.toml).

    Wraps `cli()` so the opt-in auto-update check (`av update --enable-auto-update`) runs
    exactly once per OS process, right as it's about to exit — including after any REPL
    session `cli()` may have run internally. Can't hook this into `_AuthRetryGroup.invoke()`
    instead: that fires once per `cli.main()` call, which is once per line typed inside the
    REPL too, not once per process. `cli()` itself calls `sys.exit(...)` (Click's
    standalone_mode=True default) — Python still runs `finally` before that exit completes, so
    the update check reliably gets a turn either way. Any failure in the update check itself is
    swallowed here so it can never mask the real command's exit code or crash on the way out.
    """
    try:
        cli()
    finally:
        from . import update_check

        try:
            update_check.maybe_auto_update()
        except Exception:
            pass


if __name__ == "__main__":
    run()

