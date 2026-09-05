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
        # v1.2.5: routed through fail() so this honors the documented exit-code registry
        # (exit 10, not ClickException's default 1) and gets a proper JSON envelope under
        # --output json instead of styled text. get_current_context(silent=True) is safe
        # here — every real caller runs inside a live click command invocation.
        fail(click.get_current_context(silent=True), "not_a_repo",
             "Not an Aether-Vault repository (or any of the parent directories).")
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
            # v1.2.5: exit 12 (auth_failed) via fail() in all three outcomes below, not a
            # bare sys.exit(1) — honors the documented exit-code registry and, in the
            # non-interactive case, emits a proper JSON envelope under --output json.
            if not ui.is_interactive() or output_is_json(ctx):
                fail(ctx, "auth_failed",
                     "This registry is protected and needs a valid access token. Set "
                     "one with `av auth set-token <token>` (ask whoever manages this registry "
                     "for the current one), then retry.")

            click.secho("This registry is protected — enter the access token to continue.", fg="yellow")
            import questionary

            token = questionary.password("Access token:").ask()
            if not token:
                fail(ctx, "auth_failed", "No token entered — aborting.")

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
            # The info line above already told the human what happened; exit 12 (not 0)
            # because THIS invocation still did nothing — the caller must re-run it.
            sys.exit(EXIT_AUTH_FAILED)


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

# ---------------------------------------------------------------------------
# Env snapshot identity (v1.2.2 env snapshot/replay)
# ---------------------------------------------------------------------------

ENV_SNAPSHOT_RELPATH = ".av/env_snapshot.json"


def env_snapshot_file(repo_root: Path) -> Path:
    return repo_root / ".av" / "env_snapshot.json"


def canonical_env_bytes(snap: dict) -> bytes:
    """Canonical bytes of an env snapshot: sorted-keys JSON minus volatile fields.

    v1.2.5 (`snapshot_version: 2`): hashes ONLY `snap["env"]` (python, os family, pins,
    seeds, CUDA TOOLKIT version, critical env vars) — machine-specific `snap["observed"]`
    context (GPU model, driver version, hostname, conda env, interpreter path) is
    deliberately excluded, so two genuinely-equivalent environments on different
    machines/OSes produce the SAME id (see tests/test_env_snapshot.py's golden
    cross-machine fixtures). `captured_at` was already excluded for the same reason.

    Legacy (no `snapshot_version`, or version 1) snapshots hash exactly as before —
    minus `captured_at` only — so pre-1.2.5 objects already in a CAS/registry keep
    resolving to the same id; ids are only comparable WITHIN one snapshot_version.
    """
    if snap.get("snapshot_version") == 2 and isinstance(snap.get("env"), dict):
        canon = {"snapshot_version": 2, "env": snap["env"]}
    else:
        canon = {k: v for k, v in snap.items() if k not in ("captured_at",)}
    return json.dumps(canon, sort_keys=True, separators=(",", ":")).encode("utf-8")


def env_snapshot_id(snap: dict) -> str:
    """Content-addressed id of an env snapshot (sha256 over its canonical bytes)."""
    return hashlib.sha256(canonical_env_bytes(snap)).hexdigest()


def load_env_snapshot(repo_root: Path) -> tuple[str, dict] | None:
    """(id, snapshot) from .av/env_snapshot.json, or None when absent/corrupt."""
    path = env_snapshot_file(repo_root)
    if not path.exists():
        return None
    try:
        snap = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(snap, dict):
            return None
        return env_snapshot_id(snap), snap
    except (OSError, ValueError):
        return None


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


def upload_commit_objects(repo_root: Path, client: "VaultClient", tree: dict) -> bool:
    """Upload every tracked file's object/layer shards referenced by a commit tree.

    Covers every type (`code` and `artifact` alike), not just artifacts — `add()` now writes
    a CAS object for every tracked file (see its comment), so a remote checkout/clone needs
    code's bytes uploaded too, or `av checkout` against the remote would restore artifacts but
    silently leave code files at whatever the puller's working tree already had.

    Must run BEFORE push_commit() — but NOT because the server enforces this at the DB
    level: `DBTree.object_hash` (av_server/models.py) is deliberately NOT a real foreign
    key (a layer-split/CDC-chunked artifact's whole-file hash never gets its own object
    row, only its shards do — enforcing the FK there broke every such commit). That means
    the server accepts a commit's tree unconditionally, even one referencing an object that
    was never actually stored — so THIS function's own return value is the only signal
    that an object genuinely failed to land. v1.3.0 (Probleme #126): it used to be silently
    discarded (`future.result()`'s bool return went nowhere), so a write failure here (a
    full/unwritable registry disk, mid-upload) let the caller push commit METADATA
    referencing bytes that were never stored — a "successful" push whose artifact content
    is unrecoverable. Returns True only when every upload genuinely succeeded (or there
    was nothing to upload); callers MUST queue rather than call push_commit() on False.

    Uploads are batch-checked then sent in parallel (small thread pool — these are
    network-bound HTTP calls, not CPU work) rather than one HEAD+POST round trip per
    object in sequence: a 60-object commit was previously ~120 serial round trips.

    v1.2.2 env snapshot/replay: when `.av/env_snapshot.json` exists it is ALSO uploaded
    through this exact object flow under its canonical content hash, so `av replay
    <run|commit>` can fetch the snapshot from any clone of the registry — no side channel.
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

    env_file = env_snapshot_file(repo_root)
    if env_file.exists():
        try:
            snap = json.loads(env_file.read_text(encoding="utf-8"))
            sid = env_snapshot_id(snap)
            # The CAS object must contain EXACTLY the canonical bytes the id hashes —
            # uploading the pretty-printed .av/env_snapshot.json instead makes the
            # server reject it (sha256 mismatch → 400) and clones can never replay
            # (found by the v1.2.2 manual wire pass).
            obj_path = repo_root / ".av" / "objects" / sid[:2] / sid[2:]
            if not obj_path.exists():
                obj_path.parent.mkdir(parents=True, exist_ok=True)
                atomic_write_text(obj_path,
                                  canonical_env_bytes(snap).decode("utf-8"))
                # atomic_write_text adds nothing; but keep bytes exact:
                obj_path.write_bytes(canonical_env_bytes(snap))
            candidates.setdefault(sid, obj_path)
        except (OSError, ValueError):
            pass  # a corrupt snapshot never blocks a push

    if not candidates:
        return True

    found = client.batch_check_objects(list(candidates.keys()))
    missing = {h: p for h, p in candidates.items() if h not in found}
    if not missing:
        return True

    with ThreadPoolExecutor(max_workers=min(8, len(missing))) as pool:
        futures = [
            pool.submit(client.upload_object, path, h, known_missing=True)
            for h, path in missing.items()
        ]
        # v1.3.0 (Probleme #126): a False return from upload_object() (a clean HTTP
        # failure — e.g. the server's storage write itself failed) used to be discarded
        # here entirely, so a caller could never tell "every object genuinely landed"
        # from "nothing raised". `.result()` for every future first (not a short-
        # circuiting `all()` over the generator) — an unexpected exception from a LATER
        # future must still surface, not get silently skipped because an earlier one
        # already turned out False.
        results = [future.result() for future in futures]
        return all(results)


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

    from .client import AuthenticationError, RefRaceError

    still_pending: list[dict] = []
    for i, entry in enumerate(pending):
        commit_path = repo_root / ".av" / "commits" / f"{entry['commit_hash']}.json"
        if not commit_path.exists():
            continue
        with open(commit_path, "r") as f:
            commit_data = json.load(f)
        try:
            # v1.3.0 (Probleme #126): a False return means an object genuinely failed to
            # upload again (the server accepts a commit's tree unconditionally — see
            # upload_commit_objects()'s own docstring — so this return value is the only
            # signal of that). Skip push_commit() entirely and fall through to
            # still_pending.append(entry) below, exactly as if push_commit() itself had
            # failed — never land commit metadata for bytes that still aren't stored.
            if upload_commit_objects(repo_root, client, commit_data.get("tree", {})) \
                    and client.push_commit(commit_data):
                ref_ok = True
                if entry.get("ref_name"):
                    _parents = commit_data.get("parents") or []
                    try:
                        ref_ok = client.update_ref(
                            entry["ref_name"], entry["commit_hash"],
                            expected_hash=_parents[0] if _parents else None,
                        )
                    except RefRaceError:
                        # v1.2.5: lost the compare-and-swap race again — someone else's
                        # commit is still ahead of this one on the ref. Keep it queued
                        # (never lose the commit) and keep draining the rest of the
                        # queue; unlike AuthenticationError this isn't systemic, so it
                        # shouldn't abort every other entry too.
                        ref_ok = False
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
#
# v1.2.5: broadened to every UNCOMPRESSED/block-structured format we could confirm
# survives content-defined boundaries across a small edit (a byte changed mid-file
# shifts at most the chunks touching it, not the whole stream) — .bin/.onnx/.model are
# the same "raw tensor/graph dump" shape as .pt/.ckpt; .arrow/.feather are Arrow's
# columnar IPC format (fixed-size record batches, block-aligned); .pkl/.pickle are
# Python's own serialization, uncompressed by default. Deliberately NOT default-chunked:
# COMPRESSED or otherwise non-block-aligned containers (.parquet often uses per-column
# compression, .zip/.gz/.tar/.7z always do) — a one-byte logical change there can rewrite
# the entire compressed stream, so CDC boundaries wouldn't survive an edit and chunking
# would just add overhead for no dedup benefit. Force-enable chunking for a specific glob
# regardless of extension (accepting that tradeoff deliberately) via the `chunk`
# .avattributes flag; `no-chunk` still wins over `chunk` on the same line (safety).
CHUNKABLE_EXTS = {
    ".pt", ".pth", ".ckpt", ".npz", ".h5", ".hdf5", ".pb", ".msgpack",
    ".bin", ".onnx", ".model", ".arrow", ".feather", ".pkl", ".pickle",
}


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
            # v1.2.5: `chunk` in .avattributes force-enables CDC for a glob regardless of
            # extension (e.g. a .parquet dataset the user has confirmed edits append-only
            # to); `no-chunk` still wins when both are set on the matching line — safety
            # over the opt-in, never the reverse.
            if (
                (suffix in CHUNKABLE_EXTS or "chunk" in attr_flags)
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
        if current_output_mode() != "json":
            click.secho(f"Staged [ARTIFACT] {rel_path} (LFS, {split_desc})", fg="green")
    else:
        obj_dir = repo_root / ".av" / "objects" / file_hash[:2]
        obj_dir.mkdir(parents=True, exist_ok=True)
        obj_path = obj_dir / file_hash[2:]
        if not obj_path.exists():
            shutil.copy2(fpath, obj_path)

        idx.add_entry(rel_path, file_hash, meta["size"], meta["mtime_ns"], file_type, None, auto_save=False)
        if current_output_mode() != "json":
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
    outcome_sink=None,
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

    # --- v1.2.2 signed commits: auto-sign when an ed25519 key is configured ---
    # The signature covers the canonical sorted-keys JSON of the payload INCLUDING the
    # hash just computed, EXCLUDING the signature itself (see signing.py). Best-effort:
    # no key configured → unsigned commit (always valid); [sign] extra missing → logged
    # and skipped. Signing never blocks or fails a commit — tamper evidence, not a gate.
    try:
        from .signing import sign_payload

        signature = sign_payload(commit_data, repo_root)
        if signature:
            commit_data["signature"] = signature
    except Exception as exc:  # pragma: no cover - defensive; sign_payload swallows its own
        logger.warning("commit signing skipped: %s", exc)

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

    if result_sink is None:
        click.secho(f"[{commit_hash[:7]}] {message}", fg="green")
        if tags:
            click.secho(f"  Tags: {', '.join(tags)}", fg="cyan")
        if metrics:
            click.secho(f"  Metrics: {metrics}", fg="cyan")
    # result_sink(result) itself is deferred to just before `return` (below) — v1.2.5 fix:
    # calling it HERE, before the push-or-queue section runs, froze queued/queued_reason
    # at their pre-push defaults (False/None) for every machine caller (JSON mode, the
    # SDK), so `av --output json commit` against an unreachable server always reported
    # queued:false — the exit-code 13 (unreachable_queued) contract had nothing correct
    # to key off of. See Probleme.md and tests/test_exit_codes.py.

    # --- Push to remote if available ---
    # Refs are namespaced as "<project_id>/<branch>" on the shared registry so two projects
    # can each have a branch named "main" without overwriting each other's ref.
    remote_ref_name = f"{cfg['project_id']}/{ref_path.name}" if ref_path else None
    # The parent this commit advances the ref FROM — None for a ref's first-ever commit.
    # Passed as expected_hash below so a losing compare-and-swap race is detectable
    # instead of silently overwriting a concurrent agent's ref update (v1.2.5).
    _parents = commit_data.get("parents") or []
    if len(_parents) == 2 and remote_ref_name:
        # Two-parent MERGE commit. parents[0] ("ours") is only a valid expected_hash if
        # OUR OWN prior commit on this ref actually landed on the server — normally true
        # (an ordinary merge of some other branch/commit into a not-diverged ref), so
        # "ours" stays the default. But if "ours" is still sitting in our OWN pending-push
        # queue for this exact ref, it lost its own CAS race earlier (exactly the case
        # `av merge <target>` resolving a genuine divergence reported by `av pull` is
        # for) — the server's ref is NOT "ours", it's parents[1] ("theirs", the target
        # being merged in), so use that instead. Otherwise this merge's own ref update
        # would spuriously race against a server state "ours" never actually reached,
        # even though the merge is precisely what reconciles that divergence. See
        # Probleme.md and scripts/e2e_scenario.sh's Phase A.
        _still_queued = {
            e.get("commit_hash") for e in load_pending_push(repo_root)
            if e.get("ref_name") == remote_ref_name
        }
        expected_parent = _parents[1] if _parents[0] in _still_queued else _parents[0]
    else:
        expected_parent = _parents[0] if _parents else None

    from .client import AuthenticationError, RefRaceError

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
            # Objects must reach the server before the commit — NOT because the server
            # enforces this at the DB level (it deliberately doesn't; see
            # upload_commit_objects()'s own docstring), but because that function's
            # return value is the ONLY signal a real object-write failure (a full/
            # unwritable registry disk, mid-upload) ever produces. Queue exactly like a
            # failed push_commit() rather than land commit metadata referencing bytes
            # that were never actually stored (v1.3.0, Probleme #126).
            if not upload_commit_objects(repo_root, client, tree):
                queue_pending_push(repo_root, commit_hash, remote_ref_name)
                _queued("object_upload_failed")
                if result_sink is None:
                    click.secho(
                        "  One or more objects failed to upload — commit queued for "
                        "retry (run `av push` later)", fg="yellow",
                    )
            elif client.push_commit(commit_data):
                ref_ok = True
                if remote_ref_name:
                    try:
                        ref_ok = client.update_ref(remote_ref_name, commit_hash,
                                                    expected_hash=expected_parent)
                    except RefRaceError as race:
                        # Another agent's commit landed on this ref first. The commit
                        # itself is already durable (pushed above, content-addressed —
                        # never lost); only the ref pointer lost the race. Queue it —
                        # non-negotiable #3 (offline resilience is sacred) applies to lost
                        # ref races exactly as it does to network failures. `av pull` on
                        # the next attempt will surface the divergence with run attribution.
                        ref_ok = False
                        # v1.3.0 (todo.md item 14): the pull-divergence and merge-conflict
                        # paths already attribute a race to the colliding run IDs and give
                        # identical remediation in human text and error.data — this is the
                        # one race path (the actual concurrent-write case) that didn't.
                        winner_run_id = tip_run_id(repo_root, race.current)
                        result["ref_race"] = {
                            "ref": race.ref_name, "current": race.current,
                            "expected": race.expected,
                            "current_run_id": winner_run_id,
                            "remediation": ["av pull", "av push"],
                        }
                if ref_ok and len(_parents) == 2 and remote_ref_name:
                    # v1.2.5: this merge just landed on the ref, directly superseding
                    # both parents as candidates for THIS ref's tip. A parent still
                    # sitting in pending_push (e.g. "ours" lost its own earlier ref race
                    # — see the expected_parent selection above) can never legitimately
                    # become the ref's value on its own again; its content already lives
                    # on as an ancestor of the merge that just succeeded. Drop it now,
                    # rather than have it retry-and-fail forever on every future
                    # flush_pending_push (its expected_hash can never match again once
                    # the ref has moved past it) — see Probleme.md and
                    # scripts/e2e_scenario.sh's Phase B, which caught this via a
                    # never-draining queue right after Phase A's merge landed.
                    still_pending = load_pending_push(repo_root)
                    remaining = [
                        e for e in still_pending
                        if not (e.get("ref_name") == remote_ref_name
                                and e.get("commit_hash") in _parents)
                    ]
                    if len(remaining) != len(still_pending):
                        save_pending_push(repo_root, remaining)
                if not ref_ok:
                    queue_pending_push(repo_root, commit_hash, remote_ref_name)
                    _queued("ref_race" if "ref_race" in result else "ref_update_failed")
                    if result_sink is None:
                        if "ref_race" in result:
                            who = f" (run {result['ref_race']['current_run_id']})" \
                                if result["ref_race"].get("current_run_id") else ""
                            click.secho(
                                f"  Another agent{who} updated "
                                f"'{ref_path.name if ref_path else remote_ref_name}' "
                                "first — commit queued for retry (run `av pull` then `av push`)",
                                fg="yellow",
                            )
                        else:
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

    if result_sink is not None:
        result["committed"] = True  # marker for the sink path; humans see the echo above
        result_sink(result)  # now reflects the FINAL queued/queued_reason/ref_race state
    # outcome_sink (v1.2.5) fires unconditionally, independent of result_sink/output mode —
    # it exists purely so callers can learn the final queued state to decide on an exit
    # code (EXIT_UNREACHABLE_QUEUED=13) WITHOUT also suppressing text-mode's human echoes,
    # which result_sink's "is not None" check is what controls.
    if outcome_sink is not None:
        outcome_sink(result)

    return commit_hash


def commit_staged(
    repo_root: Path,
    message: str,
    tags: tuple = (),
    metrics: dict | None = None,
    run_id: str | None = None,
    defer_upload: bool = False,
    result_sink=None,
    outcome_sink=None,
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
    client = VaultClient(*resolve_remote(repo_root, cfg))

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

    # v1.2.2 env snapshot/replay: when a snapshot exists, its content id rides the
    # hashed payload (so `av replay <commit>` can find it) and the linked run back-fills
    # env_snapshot_id server-side on first link. The snapshot OBJECT itself uploads via
    # the normal object flow inside upload_commit_objects().
    loaded_snapshot = load_env_snapshot(repo_root)
    if loaded_snapshot:
        commit_data["env_snapshot_id"] = loaded_snapshot[0]

    return _finalize_commit(
        repo_root, cfg, client,
        commit_data=commit_data, tree=tree, ref_path=ref_path, head_path=head_path,
        idx=idx, tags=tags, metrics=metrics or {},
        result_sink=result_sink, defer_upload=defer_upload, outcome_sink=outcome_sink,
    )


def parse_metric_args(raw_metrics: tuple) -> dict:
    """v1.3.1: THE shared `--metric key=value` parser — extracted verbatim from two
    call sites that had drifted into copies of the same logic (`cmd_history.py`'s
    `av commit --metric` and `cmd_run.py`'s `av run finish --metric`). Values with a
    literal `.` parse as float, else int, else fall back to the raw string unchanged;
    entries without an `=` are silently skipped (matches both callers' prior behavior).
    """
    metrics: dict = {}
    for raw in raw_metrics:
        if "=" in raw:
            k, v = raw.split("=", 1)
            try:
                metrics[k.strip()] = float(v) if "." in v else int(v)
            except ValueError:
                metrics[k.strip()] = v
    return metrics


def resolve_remote(repo_root: Path, cfg: dict | None = None) -> tuple[str, str | None]:
    """v1.3.1: THE shared `(remote_url, api_token)` resolution — extracted from the
    identical `cfg.get("remote_url", "http://localhost:8000")` /
    `cfg.get("remote_api_token")` idiom that had been copy-pasted at eight call sites
    (this module, `cmd_run.py`, `cmd_policy.py`, `cmd_registry.py`, `cmd_env.py`,
    `cmd_audit.py`, `av_sdk/repo.py`, `av_plugins/_shared.py`). No behavior change —
    same default URL, same config keys. Pass an already-loaded `cfg` (several callers
    need it for other fields too) to skip a redundant `load_config()` read.

    v1.3.3 (WP-14): a live `av login` session (`~/.aether-vault/session.json`) now takes
    priority over `cfg["remote_api_token"]` — but ONLY when the session was issued for
    the SAME registry this repo points at. A session from a login against a different
    server is silently ignored here (never sent cross-server), falling back to whatever
    this repo's own config already resolves to; a genuinely wrong-server session is
    exactly the case `av whoami` surfaces so a user notices and re-runs `av login`."""
    cfg = cfg if cfg is not None else load_config(repo_root)
    remote_url = cfg.get("remote_url", "http://localhost:8000")

    from . import session_store

    session = session_store.load_session()
    if session and session.get("url") == remote_url and session.get("token"):
        return remote_url, session["token"]

    return remote_url, cfg.get("remote_api_token")


def capture_code_pointer(repo_root: Path) -> dict | None:
    """v1.3.1: THE shared git code-provenance capture for `av run start` — extracted
    verbatim from `cmd_run.py::start()`'s inline subprocess calls so `av_sdk.Repo.run_start()`
    can capture the same `{git_remote, git_sha, dirty}` instead of always passing `None`
    (a documented divergence from the CLI — `av_sdk/repo.py`'s own comment on `run_start()`
    used to note it explicitly). `None` when this isn't a git checkout, or on any
    subprocess failure/timeout — code provenance is best-effort, never a hard requirement."""
    import subprocess

    try:
        out = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo_root,
                             capture_output=True, text=True, timeout=10)
        sha = out.stdout.strip() or None
        if not sha:
            return None
        remote = subprocess.run(["git", "remote", "get-url", "origin"], cwd=repo_root,
                                capture_output=True, text=True, timeout=10
                                ).stdout.strip() or None
        dirty = bool(subprocess.run(["git", "status", "--porcelain"], cwd=repo_root,
                                    capture_output=True, text=True,
                                    timeout=10).stdout.strip())
        return {"git_remote": remote, "git_sha": sha, "dirty": dirty}
    except (OSError, subprocess.TimeoutExpired):
        return None


def resolve_run_id(repo_root: Path, explicit: str | None = None) -> str | None:
    """v1.2.5: THE single run-id precedence rule — explicit argument > AV_RUN_ID env >
    .av/run.json state — used by every commit path (`av commit`, `av watch`,
    `commit_scoped_paths`/plugins) so `AV_RUN_ID=<id> <any av command>` behaves
    identically everywhere, as the docs already advertise ("joins ANY process' commits
    with zero integration").

    Before this existed, three call sites disagreed: `av commit` checked env before
    state, `cmd_run.current_run_id()` (used by the plugin/SDK seam) checked state before
    env, and `av watch` didn't resolve either at all — its auto-commits were silently
    never filed under the active run regardless of `av run start`/AV_RUN_ID (see
    Probleme.md). Env wins over state because it's the documented, deliberate
    per-process override; state is the ambient "someone ran `av run start` in this
    repo" default.
    """
    if explicit:
        return explicit
    env_run_id = os.environ.get("AV_RUN_ID")
    if env_run_id:
        return env_run_id
    state_path = repo_root / ".av" / "run.json"
    if state_path.exists():
        try:
            return json.loads(state_path.read_text(encoding="utf-8")).get("run_id")
        except (OSError, json.JSONDecodeError):
            return None
    return None


def tip_run_id(repo_root: Path, commit_hash: str | None) -> str | None:
    """The run:<id> tag of a (local) commit, or None.

    v1.3.0: moved here from cmd_sync.py's private `_tip_run_id` (still re-exported there
    for compat) so `_finalize_commit`'s own ref-race path can attribute the collision to
    a run exactly like `av pull`'s divergence message and `av merge`'s conflict message
    already do — todo.md item 14 ("every pull/merge/push race path includes run IDs when
    known") was true for those two but not for this one, the actual concurrent-write case.
    """
    if not commit_hash:
        return None
    from . import sync as _sync

    commit = _sync.load_local_commit(repo_root, commit_hash)
    for tag in (commit or {}).get("tags", []):
        if isinstance(tag, str) and tag.startswith("run:"):
            return tag.split(":", 1)[1]
    return None


def commit_scoped_paths(
    repo_root: Path,
    paths: list[str],
    message: str,
    tags: tuple = (),
    metrics: dict | None = None,
    run_id: str | None = None,
) -> str | None:
    """Stages exactly `paths` and commits ONLY them, leaving unrelated staged work alone.

    THE shared machine-driven-commit seam (v1.2.2): framework plugins
    (`av_plugins._shared.commit_scoped`) call this instead of chdir-ing and invoking
    the CLI, and agent tooling can too — while plain `av commit` keeps full-snapshot
    semantics through `commit_staged`. Both funnel into `_finalize_commit`, so there is
    still exactly one commit writer.

    Fixes Probleme.md #38: a naive commit sweeps unrelated staged files under a message
    they never agreed to. Isolation WITHOUT destroying the change-detection baseline
    (Probleme.md #71): staging runs against the untouched index so re-importing unchanged
    content stays a "Nothing to commit" no-op, then the index is scoped to exactly what
    THIS staging touched (new keys / changed content / staged transitions) before the
    single-code-path commit, and everything else merges back in `finally` with its staged
    flag untouched.

    Missing paths are skipped silently (Lightning legitimately announces checkpoints
    before writing them — Probleme.md #76); directory paths stage recursively via the
    same iter_working_files rules `av add .` uses (`.avignore` honored).

    Returns the new commit hash, or None when nothing changed (documented no-op).
    """
    import copy

    from .attributes import flags_for, load_attributes

    idx = Index(repo_root)
    saved = copy.deepcopy(idx.entries)
    baseline_keys = set(saved)
    # Staged-before-this-call set: lets the scoping step tell "this staging staged it"
    # apart from "the user had this staged long before" — both read staged=True after.
    pre_staged = {rel for rel, entry in saved.items() if entry.get("staged")}

    cfg = load_config(repo_root)
    threshold_bytes = cfg.get("lfs_threshold_mb", 50) * 1024 * 1024
    rules = load_attributes(repo_root)

    # v1.2.5: resolve_run_id() is now THE one precedence rule (explicit > env > state),
    # shared with av commit/av watch — see its docstring for why this used to disagree
    # across call sites.
    run_id = resolve_run_id(repo_root, run_id)

    try:
        for raw_path in paths:
            p = Path(raw_path)
            if not p.is_absolute():
                p = repo_root / p
            p = p.resolve()
            targets = list(iter_working_files(p)) if p.is_dir() else [p]
            for fpath in targets:
                if not fpath.exists():
                    continue
                rel = str(fpath.relative_to(repo_root)).replace(os.sep, "/")
                if rel.endswith(".av-pointer"):
                    continue
                stage_one_file(repo_root, idx, threshold_bytes, fpath, rel,
                               flags_for(rules, rel))

        # Scope to exactly what THIS staging touched: brand-new keys, keys whose content
        # changed under a known path (re-staged), and keys that transitioned into staged
        # because of it. Unchanged re-imports touch nothing → scoped index stays empty →
        # commit_staged returns None (the documented no-op).
        idx.entries = {
            rel_path: entry
            for rel_path, entry in idx.entries.items()
            if rel_path not in baseline_keys
            or entry.get("hash") != saved[rel_path].get("hash")
            or (entry.get("staged") and rel_path not in pre_staged)
        }
        idx.save()

        return commit_staged(
            repo_root, message, tags=tuple(tags), metrics=dict(metrics or {}),
            run_id=run_id,
        )
    finally:
        # Post-commit index: the scoped targets present with staged flags cleared by
        # _finalize_commit; everything the user had before comes back unchanged.
        fresh = Index(repo_root)
        for rel_path, entry in saved.items():
            if rel_path not in fresh.entries:
                fresh.entries[rel_path] = entry
        fresh.save()


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
# v1.3.1 (RSI control plane): every reserved code is now real. `av budget consume` exits
# 17 when any dimension is now exceeded; `av freeze on` blocks `av promote`/`av improver
# register|propose|apply`/`av policy pack publish` with 18; `av improver promote`'s
# `require_review` denies with 19 when no approving review is on file for the candidate
# (distinct from 16 — this is specifically "nobody has signed off yet", not "the metrics/
# signature don't qualify"); `av freeze on/off` maps the server's 403
# {"error":"scope_denied"} to 20.
EXIT_BUDGET_EXHAUSTED = 17        # a budget dimension is now exceeded (the spend still recorded)
EXIT_FROZEN = 18                  # project is frozen; promotions/self-edits are paused
EXIT_REVIEW_REQUIRED = 19         # improver promotion needs reviewer approval / has open critiques
EXIT_SCOPE_DENIED = 20            # token authenticated but lacks the required scope (server 403)
# v1.3.2 (hard multi-tenancy): mirrors `scope_denied`'s shape exactly — the caller
# authenticated fine, they just don't own the project_id they targeted
# (server.py::_enforce_project_tenant's 403, `AV_TENANCY_ENFORCE=1` only).
# v1.3.3: `login_required` is now real -- `av login`'s device-code flow (cmd_login.py)
# raises it when the flow times out with no approval, and `resolve_remote()`/whoami-style
# callers distinguish "never logged in / session expired" from `auth_failed` (12, a
# REJECTED credential) this way. Registering it here is what activates it in
# `_EXIT_CODES` below -- it sat reserved-but-unregistered since v1.3.2 specifically
# until a real caller existed, per this file's own "documented but never raised" drift
# discipline (test_contract_matrix.py's registry-parity test).
EXIT_LOGIN_REQUIRED = 21
EXIT_TENANT_DENIED = 22

_EXIT_CODES = {
    "not_a_repo": EXIT_NOT_A_REPO,
    "nothing_to_commit": EXIT_NOTHING_TO_COMMIT,
    "auth_failed": EXIT_AUTH_FAILED,
    "unreachable_queued": EXIT_UNREACHABLE_QUEUED,
    "merge_conflict": EXIT_CONFLICT,
    "validation": EXIT_VALIDATION,
    "policy_denied": EXIT_POLICY_DENIED,
    "budget_exhausted": EXIT_BUDGET_EXHAUSTED,
    "frozen": EXIT_FROZEN,
    "review_required": EXIT_REVIEW_REQUIRED,
    "scope_denied": EXIT_SCOPE_DENIED,
    "login_required": EXIT_LOGIN_REQUIRED,
    "tenant_denied": EXIT_TENANT_DENIED,
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
                  error_message: str | None = None, error_data: dict | None = None) -> dict:
    """Builds the one-and-only agent-facing response shape.

    `error_data` (v1.2.5, additive) is an optional dict of machine-readable failure
    context — conflict file lists, racing run/commit ids, remediation command lines —
    for the failure modes where a plain message string used to be the only signal
    (merge conflicts, ref races). Omitted entirely when empty/None so old clients that
    only look at `error.code`/`error.message` see no shape change.
    """
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
        if error_data:
            env["error"]["data"] = error_data
    return env


def emit_json(ctx, command: str, data=None) -> None:
    """Prints an ok-envelope for `command` (call instead of human output in JSON mode)."""
    click.echo(json.dumps(json_envelope(command, data=data)))


_CONTRACT_SCHEMA_NAMES = (
    "envelope-1.0", "event-1.0", "run-1.0", "webhook-payload-1.0", "semdiff-1.0", "avh-2.0",
    # v1.3.1 RSI additions
    "improver-1.0", "change-set-1.0", "policy-pack-1.0", "eval-suite-1.0",
    "tool-manifest-1.0", "action-log-1.0",
    # v1.3.2 enterprise readiness additions
    "backup-manifest-1.0",
)


def load_contract_schema(name: str) -> dict:
    """Loads and parses one of the published contracts under av_cli/schemas/<name>.schema.json.

    `name` is the file's stem without the `.schema.json` suffix, e.g. "envelope-1.0" or
    "avh-2.0" — see docs/contracts.md for the full list (`_CONTRACT_SCHEMA_NAMES`).
    Uses importlib.resources so this works from an installed wheel, not just a checkout —
    `setup.py`'s package_data ships `av_cli/schemas/*.schema.json` for exactly this reason.
    Raises FileNotFoundError with the attempted filename on a typo'd/missing name.
    """
    import importlib.resources as resources

    if name not in _CONTRACT_SCHEMA_NAMES:
        raise FileNotFoundError(
            f"unknown contract schema '{name}' — expected one of {_CONTRACT_SCHEMA_NAMES}"
        )
    ref = resources.files("av_cli").joinpath("schemas", f"{name}.schema.json")
    with resources.as_file(ref) as path:
        return json.loads(path.read_text(encoding="utf-8"))


def fail(ctx, code: str, message: str, command: str | None = None, data: dict | None = None,
         quiet_text: bool = False):
    """Uniform failure path: JSON envelope + documented exit code in one raise.

    In text mode the message prints plainly (no traceback); in JSON mode the envelope
    carries the machine-readable code plus, when given, `error.data` (v1.2.5). Always
    raises — call sites stop here.

    `quiet_text=True` (v1.2.5) skips the generic "Error: {message}" line in text mode —
    for call sites that already printed a richer, purpose-formatted explanation (a
    conflict file list, a divergence's run attribution) and would otherwise duplicate it.
    JSON mode is unaffected either way; the envelope is the only output there.
    """
    if ctx is None:
        # v1.2.5 fix: ~40 call sites across the CLI pass ctx=None here (no live context
        # handy at that point in the call chain) — output_is_json(None) is unconditionally
        # False, so EVERY one of those silently ignored `--output json` and always printed
        # plain text instead of an envelope. click.get_current_context(silent=True) finds
        # the REAL context of whichever command is actually running (always live during a
        # real invocation), so a bare `fail(None, ...)` now correctly honors JSON mode
        # instead of requiring every call site to thread ctx through by hand.
        ctx = click.get_current_context(silent=True)
    exit_code = _EXIT_CODES.get(code, EXIT_VALIDATION)
    cmd = command or (ctx.command.name if ctx and getattr(ctx, "command", None) else "av")
    if output_is_json(ctx):
        click.echo(json.dumps(json_envelope(cmd, error_code=code, error_message=message,
                                             error_data=data)))
    elif not quiet_text:
        click.secho(f"Error: {message}", fg="red", err=True)
    # Many call sites pass ctx=None (they run outside a click context or before one is
    # handy). Context.exit on None used to raise AttributeError AFTER the message printed,
    # so users saw a Python traceback under every clean validation failure (Probleme.md).
    # v1.2.5: always SystemExit, never ctx.exit() — empirically, Context.exit() raises
    # click.exceptions.Exit, which CliRunner.invoke(standalone_mode=False) (the pattern
    # this test suite uses throughout: test_signing.py, test_v122.py, this file's own
    # tests, ...) silently swallows, leaving result.exit_code at 0 regardless of the
    # code passed. A bare SystemExit propagates correctly under standalone_mode True
    # AND False, and in real (non-test) CLI usage — so it's the only mechanism used here
    # now, resolving ctx (above) purely for output_is_json() detection.
    raise SystemExit(exit_code)
