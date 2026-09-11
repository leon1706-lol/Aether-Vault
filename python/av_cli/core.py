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
from .fsutil import atomic_write_json, atomic_write_json_compact, atomic_write_text, find_commit_file
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


# ---------------------------------------------------------------------------
# V1.5.0: deterministic multithreading -- `--threads`/`AV_THREADS`/`.av/config "threads"`
# resolve to one thread count, shared by the Python-side worker pool (parallel `add`, see
# cmd_staging.py) and the C++ core's own shared pool (aether_core.set_max_threads) so both
# layers agree instead of the Python pool's N workers each spawning their own C++ pool.
# ---------------------------------------------------------------------------

_native_threads_configured = False


def cpu_count_for_threading() -> int:
    """CPU count for auto-sizing, cgroup/affinity-aware where Python exposes it -- so a
    CI container with a 2-CPU quota doesn't oversubscribe just because the host has 64."""
    getter = getattr(os, "process_cpu_count", None)  # 3.13+
    if getter is not None:
        n = getter()
        if n:
            return n
    affinity = getattr(os, "sched_getaffinity", None)  # POSIX only
    if affinity is not None:
        try:
            n = len(affinity(0))
            if n:
                return n
        except OSError:
            pass
    return os.cpu_count() or 1


def resolve_threads(repo_root: Path | None, cli_threads: int | None = None) -> int:
    """`--threads` > `AV_THREADS` > `.av/config "threads"` > auto. Returns 0 for "auto"
    (callers pass 0 straight through to `aether_core.set_max_threads`, which already
    treats 0 as hardware_concurrency()); a positive return is an explicit override.
    `AV_THREADS=1` is a real escape hatch, not just "a pool of one" -- callers should treat
    exactly 1 as "take the old single-threaded code path", useful for isolating whether a
    bug is threading-related.
    """
    if cli_threads is not None and cli_threads > 0:
        return cli_threads
    env_val = os.environ.get("AV_THREADS", "").strip()
    if env_val:
        try:
            n = int(env_val)
            if n > 0:
                return n
        except ValueError:
            pass
    if repo_root is not None:
        try:
            cfg_threads = load_config(repo_root).get("threads")
        except Exception:
            cfg_threads = None
        if isinstance(cfg_threads, int) and cfg_threads > 0:
            return cfg_threads
    return 0


def python_pool_size(threads: int) -> int:
    """Resolves `resolve_threads()`'s 0="auto" into a concrete Python ThreadPoolExecutor
    size. Capped at 8: SHA-256 hashing is memory-bandwidth-bound well before 8 threads on
    typical hardware, and each in-flight hash holds its own read buffer -- unlike the C++
    pool (capped higher, at 16, in shared_pool()), which does finer-grained per-chunk work."""
    n = threads if threads > 0 else cpu_count_for_threading()
    return max(1, min(n, 8))


def configure_native_threads(repo_root: Path | None, cli_threads: int | None = None) -> int:
    """Resolves the effective thread count and configures the C++ core's shared pool to
    match -- once per process (idempotent). Deliberately NOT called from every command's
    entry point: only call this from a path that's already loading `aether_core` anyway
    (currently: `hash_file_safe`, right before its own `aether_core.hash_file` call) so a
    command that never hashes anything still never pays the extension's import cost.
    Returns the resolved count (0=auto) for the caller to also size a Python-side pool via
    `python_pool_size()`."""
    global _native_threads_configured
    threads = resolve_threads(repo_root, cli_threads)
    if not _native_threads_configured:
        aether_core = _get_aether_core()
        if aether_core is not None and hasattr(aether_core, "set_max_threads"):
            aether_core.set_max_threads(threads)
        _native_threads_configured = True
    return threads


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

# V1.5.0: sane built-in defaults for the directories that show up in nearly every real ML
# repo but were never in _IGNORED_DIRS -- their absence here (not a competitor's algorithm)
# is what made `av status`/`av add .` walk an entire virtualenv or node_modules tree on
# every invocation. Distinct from _IGNORED_DIRS above: these are a documented, opt-out-able
# convenience default, not a hard repo-format invariant. Set AV_NO_DEFAULT_IGNORES=1 to track
# one of these directories anyway -- `.avignore` has no negation mechanism (deliberately, see
# load_avignore_patterns()'s docstring), so there is no per-directory override for this list.
_DEFAULT_IGNORED_DIR_NAMES = frozenset({
    "venv", ".venv", "env", "node_modules", "build", "dist", ".tox", ".nox",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", ".eggs", "site-packages",
    ".ipynb_checkpoints",
})


def _default_ignores_enabled() -> bool:
    return os.environ.get("AV_NO_DEFAULT_IGNORES", "").strip().lower() not in ("1", "true", "yes")


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


class _GitignoreRule:
    """One parsed `.gitignore` line. `anchored` = pattern contained a `/` before its last
    character (git: match only from the `.gitignore`'s own directory down, not at any
    depth). `dir_only` = pattern ended in `/` (only matches directories)."""

    __slots__ = ("pattern", "negate", "dir_only", "anchored")

    def __init__(self, pattern: str, negate: bool, dir_only: bool, anchored: bool):
        self.pattern = pattern
        self.negate = negate
        self.dir_only = dir_only
        self.anchored = anchored


def _parse_gitignore_lines(lines: list[str]) -> list[_GitignoreRule]:
    """Real (if partial) gitignore semantics: `#` comments, blank lines, `!` negation,
    leading-`/` anchoring, trailing-`/` directory-only. Deliberately doesn't implement `**`
    cross-directory globs or escaped `\\#`/`\\!` -- documented gap, same spirit as
    `.avignore`'s own "gitignore-lite" scope note. A pattern's own directory component (if
    any, e.g. `build/output`) is matched against the full relative path; a bare name (e.g.
    `*.log`) matches at any depth, exactly like real git.
    """
    rules: list[_GitignoreRule] = []
    for raw in lines:
        line = raw.rstrip("\n").rstrip("\r")
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        negate = line.startswith("!")
        if negate:
            line = line[1:]
        dir_only = line.endswith("/") and not line.endswith("\\/")
        if dir_only:
            line = line[:-1]
        if not line:
            continue
        anchored = "/" in line[:-1] or line.startswith("/")
        line = line.lstrip("/")
        if not line:
            continue
        rules.append(_GitignoreRule(line, negate, dir_only, anchored))
    return rules


def load_gitignore_rules(repo_root: Path) -> list[_GitignoreRule]:
    """Reads the repo-root `.gitignore`, if present. Nested per-directory `.gitignore`
    files (real git supports one per subdirectory) are out of scope here -- a single
    root-level file covers the overwhelming majority of real repos and keeps this a
    bounded, testable piece of surface rather than a full gitignore engine."""
    gi_path = repo_root / ".gitignore"
    if not gi_path.exists():
        return []
    try:
        return _parse_gitignore_lines(gi_path.read_text(encoding="utf-8").splitlines())
    except OSError:
        return []


def _gitignore_rule_matches(rule: _GitignoreRule, rel_posix: str, name: str, is_dir: bool) -> bool:
    if rule.dir_only and not is_dir:
        return False
    if rule.anchored:
        return fnmatch.fnmatch(rel_posix, rule.pattern)
    return fnmatch.fnmatch(name, rule.pattern) or fnmatch.fnmatch(rel_posix, rule.pattern)


def _is_gitignored(rel_posix: str, name: str, is_dir: bool, rules: list[_GitignoreRule]) -> bool:
    """Last matching rule wins (real git semantics) -- a later `!pattern` can un-ignore
    something an earlier broader pattern caught."""
    ignored = False
    for rule in rules:
        if _gitignore_rule_matches(rule, rel_posix, name, is_dir):
            ignored = not rule.negate
    return ignored


def iter_working_files(root: Path):
    """Yield every working-tree file path under `root`, skipping ignored dirs/noise,
    anything matching a `.avignore` pattern or the repo's `.gitignore`, and (V1.5.0) a
    built-in default ignore list of common heavy directories (`venv`, `node_modules`, ...) --
    see `_DEFAULT_IGNORED_DIR_NAMES` and `AV_NO_DEFAULT_IGNORES`.

    Prunes ignored/ignored-by-pattern directories in-place so the CAS object store (and e.g. a
    `.avignore`'d `venv/`) is never traversed in the first place, not just filtered after a full
    walk.
    """
    repo_root = find_repo_root() or root
    avignore_patterns = load_avignore_patterns(repo_root)
    gitignore_rules = load_gitignore_rules(repo_root)
    default_ignores = _default_ignores_enabled()

    def _rel_posix(p: Path) -> str | None:
        # `root` isn't always under repo_root (a caller can pass an unrelated path) --
        # gitignore matching is simply skipped for a path outside repo_root rather than
        # crashing the whole walk over an edge case .avignore/default-dir matching never
        # needed to care about (they only ever look at the bare name).
        try:
            return p.relative_to(repo_root).as_posix()
        except ValueError:
            return None

    for dirpath, dirnames, files in os.walk(root):
        dpath = Path(dirpath)
        kept_dirnames = []
        for d in dirnames:
            if d in _IGNORED_DIRS:
                continue
            if default_ignores and d in _DEFAULT_IGNORED_DIR_NAMES:
                continue
            if _matches_avignore(d, avignore_patterns):
                continue
            if gitignore_rules:
                rel = _rel_posix(dpath / d)
                if rel is not None and _is_gitignored(rel, d, True, gitignore_rules):
                    continue
            kept_dirnames.append(d)
        # Sorted, not insertion order -- os.walk's own order is filesystem-dependent (NTFS
        # and ext4 don't agree), which made `.av/index` key order vary by machine even
        # before V1.5.0's threading work. Sorting here (both the walk's own descent order
        # via dirnames[:], and the yielded file order) is what makes the "same repo ->
        # byte-identical index/commit hash, any machine, any thread count" guarantee real
        # rather than incidental -- see tests/test_threads_determinism.py.
        dirnames[:] = sorted(kept_dirnames)
        for f in sorted(files):
            if f.endswith(".pyc") or f.endswith(".av-pointer"):
                continue
            if _matches_avignore(f, avignore_patterns):
                continue
            if gitignore_rules:
                rel = _rel_posix(dpath / f)
                if rel is not None and _is_gitignored(rel, f, False, gitignore_rules):
                    continue
            yield dpath / f


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
    """`cli`'s class — catches AuthenticationError from *any* subcommand in one place
    rather than wrapping each server-talking command individually. Re-runs the command
    from scratch after saving a token rather than silently retrying mid-operation, since
    resuming a partially-completed multi-step operation with a freshly swapped credential
    is riskier than asking the user to re-invoke it.

    Also carries V1.5.0's lazy command registration: `main.py` used to `from .cmd_X import
    ...` all ~45 command modules unconditionally at import time, which is most of why every
    `av` invocation (including a plain `--version`/`--help`) paid for every command module's
    entire dependency tree. `main.py` now registers one small loader function per module
    against the command name(s) it produces (`_LAZY_LOADERS`) instead of importing eagerly;
    `get_command` imports+registers a module's commands only the first time one of its names
    is actually resolved. `list_commands` (used for `--help`/completion) needs no import at
    all -- the loader dict's keys ARE the command names, known upfront without running any
    loader.
    """

    _LAZY_LOADERS: dict = {}

    def list_commands(self, ctx: click.Context | None) -> list[str]:
        return sorted(set(self.commands) | set(self._LAZY_LOADERS))

    def get_command(self, ctx: click.Context | None, name: str):
        cmd = self.commands.get(name)
        if cmd is not None:
            return cmd
        loader = self._LAZY_LOADERS.get(name)
        if loader is None:
            return None
        loader(self)  # calls self.add_command(...) for every name this module produces
        return self.commands.get(name)

    def invoke(self, ctx: click.Context):
        # V1.5.0 perf fix: both imports below used to sit at the top of this method,
        # unconditionally, on EVERY single command dispatch (this override wraps the whole
        # group, so it ran even for `av --version`/`--help`) — `ui`'s module-level
        # `questionary`/`rich` imports alone measured ~1.3-1.4s, silently defeating the P2
        # lazy-command-registration work for literally every invocation. Deferred to inside
        # the except block, which only runs on an actual 401 (rare, interactive-auth path).
        #
        # `except Exception` here (needed to catch *anything*, not just AuthenticationError,
        # without importing `.client` up front) also catches `click.exceptions.Exit` -- which
        # `--version`'s own handler raises on every single call -- so a plain
        # `from .client import AuthenticationError` + isinstance check right here would
        # import `.client` (and its `requests` dependency) on literally every invocation,
        # reintroducing the exact cost this fix removes. Instead: peek at `sys.modules`
        # first. Whatever raised a real AuthenticationError must have already imported
        # `.client` to construct one, so "not imported yet" cheaply proves "not this
        # exception" without ever triggering the import ourselves.
        try:
            return super().invoke(ctx)
        except Exception as exc:
            client_mod = sys.modules.get(f"{__name__.rsplit('.', 1)[0]}.client")
            auth_error_cls = getattr(client_mod, "AuthenticationError", None) if client_mod else None
            if auth_error_cls is None or not isinstance(exc, auth_error_cls):
                raise
            from . import ui

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
    """Merge new tags and metric keys into the local registry.

    A no-op commit (no new tag/metric names) skips the write+fsync entirely -- this ran
    unconditionally on every single commit before, for a set that usually never changes.
    """
    reg = load_registry(repo_root)
    new_tags = sorted(set(reg["tags"]) | set(tags))
    new_metrics = sorted(set(reg["metrics"]) | set(metrics.keys()))
    if new_tags == reg["tags"] and new_metrics == reg["metrics"]:
        return
    reg["tags"] = new_tags
    reg["metrics"] = new_metrics
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
    `snapshot_version: 2` hashes ONLY `snap["env"]` (python, pins, seeds, ...) --
    machine-specific `snap["observed"]` context is deliberately excluded, so two
    genuinely-equivalent environments on different machines produce the SAME id. Legacy
    (no version, or version 1) snapshots hash minus `captured_at` only, for backward compat."""
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
    atomic_write_json_compact(path, pending)


def queue_pending_push(repo_root: Path, commit_hash: str, ref_name: str | None) -> None:
    """Append a commit/ref pair to the pending-push queue."""
    pending = load_pending_push(repo_root)
    pending.append({"commit_hash": commit_hash, "ref_name": ref_name})
    save_pending_push(repo_root, pending)


def upload_commit_objects(
    repo_root: Path, client: "VaultClient", tree: dict, only_paths: set[str] | None = None
) -> bool:
    """Upload every tracked file's object/layer shards referenced by a commit tree,
    covering `code` and `artifact` alike (a remote checkout needs code's bytes too).

    MUST run BEFORE push_commit(): the server accepts a commit's tree unconditionally
    (its object_hash column is deliberately not a real FK), so this function's return
    value is the ONLY signal that an object genuinely failed to land. Returns True only
    when every upload succeeded; callers MUST queue rather than call push_commit() on
    False -- never land commit metadata referencing bytes that were never stored.

    `only_paths`, when given, scopes the scan to just those tree entries -- O(files
    changed in this commit) instead of O(every tracked file), since an unchanged file's
    object was already confirmed present server-side when IT was committed. V1.5.0:
    `commit_staged`'s live path passes the staged set here; the offline-queue RETRY path
    (`flush_pending_push`, below) deliberately does NOT -- a queued commit is exactly the
    "something already went wrong" case where re-scanning that commit's full historical
    tree as a self-healing pass is worth the extra cost.

    Uploads are batch-checked then sent in parallel (small thread pool). When
    `.av/env_snapshot.json` exists it is uploaded through this same object flow.
    """
    candidates: dict[str, Path] = {}  # hash -> object file on disk, dedup'd
    scan_items = tree.items() if only_paths is None else (
        (rel_path, info) for rel_path, info in tree.items() if rel_path in only_paths
    )
    for _rel_path, info in scan_items:
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
            # The CAS object must contain EXACTLY the canonical bytes the id hashes --
            # uploading the pretty-printed .av/env_snapshot.json instead makes the server
            # reject it (sha256 mismatch -> 400).
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
        # `.result()` for every future first (not a short-circuiting `all()` over the
        # generator) -- an exception from a LATER future must still surface.
        results = [future.result() for future in futures]
        return all(results)


def flush_pending_push(repo_root: Path, client: "VaultClient") -> list[dict]:
    """Retry pushing queued commits to the remote server. Returns the entries still
    pending. `server_available()` only proves the server is up, not that this client's
    token is valid -- a bad token surfaces as AuthenticationError, caught here and treated
    like "server unreachable" (queue and retry later), stopping the rest of the queue
    since the same bad token would fail identically for every remaining entry."""
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
            # A False return means an object genuinely failed to upload again -- skip
            # push_commit() and fall through to still_pending.append(entry) below.
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
                        # Lost the compare-and-swap race again -- keep it queued and keep
                        # draining the rest, since unlike AuthenticationError this isn't systemic.
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
        if not _native_threads_configured:
            # First real use of the extension in this process -- configure its shared
            # thread pool here rather than at every command's entry point, so a command
            # that never hashes anything never pays aether_core's import cost. Idempotent.
            configure_native_threads(find_repo_root())
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


def hash_and_publish_whole_file(repo_root: Path, fpath: Path) -> str:
    """Hashes `fpath` and, if its content isn't already in the CAS, publishes it there --
    in one read where possible, instead of hash_file_safe() reading the whole file once to
    learn its hash and then a second full read via shutil.copy2 to actually store it (the
    dominant I/O cost for a repo of many small files, which is exactly the shape of the
    commit/add benchmark's own fixture). Returns the whole-file SHA-256.

    Trade-off, stated plainly: since the destination name isn't known until the file is
    hashed, this always writes to a temp file first and only keeps it if the object didn't
    already exist -- a genuinely new/changed file (the common case once the mtime/size
    no-op check above has already returned False) pays exactly one read + one write; a file
    whose content turns out to duplicate an already-stored object pays one wasted temp
    write it immediately discards. Bounded, and never slower than the old always-two-reads
    path for the common case.
    """
    aether_core = _get_aether_core()
    if aether_core is not None and hasattr(aether_core, "hash_and_copy"):
        if not _native_threads_configured:
            configure_native_threads(repo_root)
        obj_dir = repo_root / ".av" / "objects"
        # Hash straight into a scratch temp name first -- its final shard directory isn't
        # known until the hash comes back.
        scratch = obj_dir / f".stage-tmp.{uuid.uuid4().hex[:12]}"
        obj_dir.mkdir(parents=True, exist_ok=True)
        try:
            file_hash = aether_core.hash_and_copy(str(fpath), str(scratch))
        except Exception as exc:
            print(
                f"Warning: aether_core.hash_and_copy failed, using Python fallback: {exc}",
                file=sys.stderr,
            )
            scratch.unlink(missing_ok=True)
        else:
            obj_path = repo_root / ".av" / "objects" / file_hash[:2] / file_hash[2:]
            if obj_path.exists():
                scratch.unlink(missing_ok=True)
            else:
                obj_path.parent.mkdir(parents=True, exist_ok=True)
                try:
                    os.replace(scratch, obj_path)
                except OSError:
                    scratch.unlink(missing_ok=True)
                    raise
            return file_hash

    # Pure-Python fallback (no aether_core, or it failed above): still one read, via
    # hashlib updated incrementally as each block is both hashed and written.
    file_hash_obj = hashlib.sha256()
    obj_dir = repo_root / ".av" / "objects"
    obj_dir.mkdir(parents=True, exist_ok=True)
    scratch = obj_dir / f".stage-tmp.{uuid.uuid4().hex[:12]}"
    try:
        with open(fpath, "rb") as src, open(scratch, "wb") as dst:
            while chunk := src.read(8 * 1024 * 1024):
                file_hash_obj.update(chunk)
                dst.write(chunk)
        file_hash = file_hash_obj.hexdigest()
        obj_path = repo_root / ".av" / "objects" / file_hash[:2] / file_hash[2:]
        if obj_path.exists():
            scratch.unlink(missing_ok=True)
        else:
            obj_path.parent.mkdir(parents=True, exist_ok=True)
            os.replace(scratch, obj_path)
        return file_hash
    finally:
        if scratch.exists():
            scratch.unlink(missing_ok=True)


# IMPORTANT — single source of truth for file metadata (Unix epoch).
# These deliberately do NOT use the C++ core: std::filesystem::last_write_time has an
# implementation-defined clock epoch (e.g. 1601 on Windows / 100ns ticks) that does not
# match Python's Unix-epoch st_mtime_ns. Routing some calls through C++ and others through
# Python (e.g. after an aether_core fallback) would store one epoch and compare against the
# other, exact-equality change detection would then flag unchanged files as "modified".
# os.stat is a single cheap syscall, so there is no meaningful speed loss in keeping all
# size/mtime handling in Python and reserving the C++ core for hashing only.
def get_file_meta_safe(path: str) -> dict:
    # One stat() call, not exists()+stat() (two syscalls for the common case where the
    # file exists, which is nearly every call on a real repo) -- V1.5.0 perf work.
    try:
        stat = os.stat(path)
    except OSError:
        return {"exists": False, "size": 0, "mtime_ns": 0}
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
    """Writes a tracked path's content to the working tree from the CAS -- whole-object,
    reassembled from safetensors layers, or reassembled from CDC chunks, downloading
    missing pieces from the remote. Shared by `checkout`, `av stash pop`/`apply`,
    clone/pull, and merge, so all of them restore a file identically."""
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


# Extensions that benefit from CDC dedup: uncompressed/block-structured formats where a
# small edit only shifts the chunks touching it, not the whole stream. Deliberately NOT
# default-chunked: compressed containers (.parquet, .zip/.gz/.tar/.7z) can rewrite their
# whole stream on any logical edit, so CDC boundaries wouldn't survive. Per-file override
# via the `chunk`/`no-chunk` .avattributes flags (no-chunk always wins).
CHUNKABLE_EXTS = {
    ".pt", ".pth", ".ckpt", ".npz", ".h5", ".hdf5", ".pb", ".msgpack",
    ".bin", ".onnx", ".model", ".arrow", ".feather", ".pkl", ".pickle",
}


def _atomic_publish_object(obj_path: Path, write_fn) -> None:
    """Publishes a CAS object at `obj_path` by writing to a temp file in the same shard
    directory first, then `os.replace` -- never write straight to the final content-
    addressed name. `write_fn(tmp_path)` does the actual write (a plain copy, or a streamed
    reassembly from a source offset).

    V1.5.0: this closes a race that existed even before threading -- two files with
    identical content (a common case for ML checkpoints: an unchanged encoder re-saved
    alongside a changed head) hash to the same `obj_path`, and a `Ctrl-C` mid-`shutil.copy2`
    could already leave a torn object under that name for a single-threaded `av add`.
    Parallel staging (see cmd_staging.py) makes the *concurrent*-write version of this race
    real too: two worker threads racing the exact same destination name.
    """
    if obj_path.exists():
        return
    obj_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = obj_path.with_name(f"{obj_path.name}.tmp.{uuid.uuid4().hex[:8]}")
    try:
        write_fn(tmp)
        # Another thread may have published the same content-addressed object while this
        # one was writing its own temp copy -- that's fine, os.replace still lands
        # atomically; the loser's temp file just becomes the (byte-identical) final file.
        os.replace(tmp, obj_path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def _compute_stage_result(
    repo_root: Path,
    threshold_bytes: int,
    fpath: Path,
    rel_path: str,
    file_type: str,
    existing_entry: dict | None,
    attr_flags: set | None = None,
) -> dict | None:
    """Pure per-file work for `av add`: hashing, safetensors layer-split, CDC chunking, CAS
    object writes, and pointer-file creation for one path. Touches nothing shared across
    files (every write is namespaced by either `file_hash` via `_atomic_publish_object`'s
    temp+replace, or by `fpath`'s own unique name for the pointer file) and never touches
    `Index` or `click` -- safe to run on a worker thread. Returns None if nothing changed,
    else a result dict for the caller to apply to the index and print, in whatever order
    the caller chooses (V1.5.0: always the original sorted-input order, regardless of which
    worker finished first -- see cmd_staging.py's `add`).
    """
    attr_flags = attr_flags or set()
    meta = get_file_meta_safe(str(fpath))
    if (
        existing_entry
        and meta["exists"]
        and meta["size"] == existing_entry["size"]
        and meta["mtime_ns"] == existing_entry["mtime_ns"]
    ):
        return None

    if file_type == "artifact" and meta["size"] > threshold_bytes:
        # Needed regardless of whether layer-split/chunking below actually fires -- kept as
        # a separate read here (unlike the plain-file branch at the bottom of this
        # function) because a successful split reads the file again anyway for its own
        # per-layer/per-chunk hashing; folding the whole-file hash into that pass too is a
        # real further optimization, just a separate/riskier one than this function takes on.
        file_hash = hash_file_safe(str(fpath))
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
                    l_obj_path = repo_root / ".av" / "objects" / l_hash[:2] / l_hash[2:]

                    def _write_layer(tmp_path, _offset=l_offset, _size=l_size):
                        with open(fpath, "rb") as src_f:
                            src_f.seek(_offset)
                            with open(tmp_path, "wb") as dst_f:
                                remaining = _size
                                while remaining > 0:
                                    chunk = src_f.read(min(8 * 1024 * 1024, remaining))
                                    if not chunk:
                                        break
                                    dst_f.write(chunk)
                                    remaining -= len(chunk)

                    _atomic_publish_object(l_obj_path, _write_layer)
                    layers.append({"name": lr["name"], "hash": l_hash, "size": l_size})
            except Exception as exc:
                logger.warning(f"Layer splitting failed for {rel_path}, falling back to whole-file: {exc}")

        if not layers:
            suffix = Path(rel_path).suffix.lower()
            core_cdc = _get_aether_core()
            # `chunk` in .avattributes force-enables CDC for a glob regardless of extension;
            # `no-chunk` still wins when both are set -- safety over the opt-in.
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
                        c_obj_path = repo_root / ".av" / "objects" / c_hash[:2] / c_hash[2:]

                        def _write_chunk(tmp_path, _offset=c_offset, _size=c_size):
                            with open(fpath, "rb") as src_f:
                                src_f.seek(_offset)
                                with open(tmp_path, "wb") as dst_f:
                                    remaining = _size
                                    while remaining > 0:
                                        block = src_f.read(min(8 * 1024 * 1024, remaining))
                                        if not block:
                                            break
                                        dst_f.write(block)
                                        remaining -= len(block)

                        _atomic_publish_object(c_obj_path, _write_chunk)
                        chunks.append({"hash": c_hash, "size": c_size, "offset": c_offset})
                except Exception as exc:
                    logger.warning(f"Chunking failed for {rel_path}, falling back to whole-file: {exc}")
                    chunks = []

        if not layers and not chunks:
            obj_path = repo_root / ".av" / "objects" / file_hash[:2] / file_hash[2:]
            _atomic_publish_object(obj_path, lambda tmp: shutil.copy2(fpath, tmp))

        ptr_path = get_pointer_path(fpath)
        ptr_content = create_pointer(fpath, file_hash, meta["size"])
        with open(ptr_path, "w") as ptr_f:
            ptr_f.write(ptr_content)

        split_desc = (
            f"{len(layers)} layers" if layers
            else (f"{len(chunks)} chunks" if chunks else "whole-file")
        )
        return {
            "rel_path": rel_path, "hash": file_hash, "size": meta["size"],
            "mtime_ns": meta["mtime_ns"], "file_type": file_type,
            "pointer": rel_path + ".av-pointer", "layers": layers, "chunks": chunks,
            "message": f"Staged [ARTIFACT] {rel_path} (LFS, {split_desc})",
        }

    # Plain code/small-artifact file: no split candidate, so this is exactly the case
    # hash_and_publish_whole_file exists for -- one read instead of hash_file_safe's read
    # followed by a second full read via shutil.copy2.
    file_hash = hash_and_publish_whole_file(repo_root, fpath)
    return {
        "rel_path": rel_path, "hash": file_hash, "size": meta["size"],
        "mtime_ns": meta["mtime_ns"], "file_type": file_type, "pointer": None,
        "layers": [], "chunks": [],
        "message": f"Staged [{file_type.upper()}] {rel_path}",
    }


def apply_stage_result(idx: Index, result: dict) -> None:
    """Applies one `_compute_stage_result()` result to the index and prints its message --
    the only part of staging that touches shared state, so callers (sequential or the
    parallel `add` in cmd_staging.py) always run this on the main thread, one result at a
    time, in whatever order they've chosen (V1.5.0: original sorted-input order)."""
    idx.add_entry(
        result["rel_path"], result["hash"], result["size"], result["mtime_ns"],
        result["file_type"], result["pointer"], auto_save=False,
    )
    if result["layers"]:
        idx.entries[result["rel_path"]]["layers"] = result["layers"]
    if result["chunks"]:
        idx.entries[result["rel_path"]]["chunks"] = result["chunks"]
    if current_output_mode() != "json":
        click.secho(result["message"], fg="green")


def stage_one_file(
    repo_root: Path,
    idx: Index,
    threshold_bytes: int,
    fpath: Path,
    rel_path: str,
    attr_flags: set | None = None,
) -> bool:
    """Hashes and stores a single file's current content and records it in the index.
    Returns whether anything actually changed. Thin sequential wrapper around
    `_compute_stage_result`/`apply_stage_result` -- kept as the one entry point every
    existing caller (plugins, `av watch`, `av stash push`, tests) already uses; the
    parallel `add` path in cmd_staging.py calls the two halves directly instead."""
    file_type = idx.classify_file(rel_path)
    result = _compute_stage_result(
        repo_root, threshold_bytes, fpath, rel_path, file_type, idx.get_entry(rel_path), attr_flags
    )
    if result is None:
        return False
    apply_stage_result(idx, result)
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
    changed_paths: set[str] | None = None,
) -> str:
    """Everything `av commit` does after its tree snapshot and parents are resolved: hash
    the payload deterministically over sorted JSON, persist atomically (commit object
    before ref move), advance the branch ref, clear staged flags, and push to the
    registry with the standard offline-queue fallbacks. Shared by `av merge` so its
    two-parent commits go through the exact same code path.

    `changed_paths`, if given, scopes upload_commit_objects to just those tree entries
    instead of the whole tree (V1.5.0 perf work) -- an O(commit size) upload instead of
    O(repo size). ONLY `commit_staged` passes this (the staged set it captures itself,
    BEFORE `idx.clear_staged()` below wipes every entry's staged flag -- capturing it here
    from `idx` would be wrong, since `idx.clear_staged()` runs unconditionally below and,
    for a caller like `cmd_sync.py`'s merge, the passed-in `idx` was already re-loaded
    fresh from disk with nothing staged by the time it reaches this function). Leave this
    None for any caller that can't state its own changed set with full confidence --
    `upload_commit_objects` then falls back to its safe, if slower, full-tree scan.
    """
    metrics = metrics or {}
    message = commit_data.get("message", "")

    commit_str = json.dumps(commit_data, sort_keys=True)
    commit_hash = hashlib.sha256(commit_str.encode()).hexdigest()
    commit_data["hash"] = commit_hash

    # --- Signed commits: auto-sign when an ed25519 key is configured ---
    # Signature covers the canonical sorted-keys JSON including the hash just computed,
    # excluding the signature itself. Best-effort: never blocks or fails a commit.
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
    # result_sink(result) itself is deferred to just before `return` (below) -- calling it
    # here, before push-or-queue runs, would freeze queued/queued_reason at their pre-push
    # defaults for every machine caller.

    # --- Push to remote if available ---
    # Refs are namespaced as "<project_id>/<branch>" on the shared registry so two projects
    # can each have a branch named "main" without overwriting each other's ref.
    remote_ref_name = f"{cfg['project_id']}/{ref_path.name}" if ref_path else None
    # The parent this commit advances the ref FROM -- None for a ref's first-ever commit.
    # Passed as expected_hash below so a losing compare-and-swap race is detectable
    # instead of silently overwriting a concurrent agent's ref update.
    _parents = commit_data.get("parents") or []
    if len(_parents) == 2 and remote_ref_name:
        # Two-parent MERGE commit. parents[0] ("ours") is the default expected_hash, but
        # if it's still sitting in our OWN pending-push queue for this ref, it already
        # lost its own CAS race -- the server's ref is parents[1] ("theirs") instead, or
        # this merge's ref update would spuriously race against a state "ours" never reached.
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
            # Objects must reach the server before the commit -- upload_commit_objects()'s
            # return value is the only signal a real object-write failure ever produces.
            if not upload_commit_objects(repo_root, client, tree, only_paths=changed_paths):
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
                        # itself is already durable (content-addressed, never lost); only
                        # the ref pointer lost the race, so queue it like any network
                        # failure -- `av pull` on the next attempt surfaces the divergence.
                        ref_ok = False
                        winner_run_id = tip_run_id(repo_root, race.current)
                        result["ref_race"] = {
                            "ref": race.ref_name, "current": race.current,
                            "expected": race.expected,
                            "current_run_id": winner_run_id,
                            "remediation": ["av pull", "av push"],
                        }
                if ref_ok and len(_parents) == 2 and remote_ref_name:
                    # This merge just landed on the ref, superseding both parents as
                    # candidates for the ref's tip. A parent still sitting in pending_push
                    # can never legitimately become the ref's value again, so drop it now
                    # rather than have it retry-and-fail forever.
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
    idx: "Index | None" = None,
) -> str | None:
    """Commit whatever is currently staged — THE shared entry point.

    Callers: `av commit` (after flag parsing), `av watch` (auto-commits), and the
    av_sdk.Repo SDK. All of them get identical semantics because this is the only place
    that builds the payload and calls _finalize_commit (the historical single writer):
    deterministic hash over sorted JSON, atomic local persist, ref advance, and
    push-or-queue with offline resilience.

    `idx`, if given, is used as-is instead of a fresh `Index(repo_root)` load -- V1.5.0:
    lets `commit_scoped_paths` (the plugin seam) pass the exact in-memory `Index` it already
    scoped, saving a redundant read+reparse of the whole index file it just wrote seconds
    earlier. Every other caller omits it and gets the original always-fresh-load behavior.

    Returns the new commit hash, or None when nothing was staged.
    """
    from .client import VaultClient

    if idx is None:
        idx = Index(repo_root)
    staged_entries = idx.get_staged_entries()
    if not staged_entries:
        return None
    # Captured here, not inside _finalize_commit -- see its changed_paths docstring for why
    # that would be unsafe for other callers (e.g. merge) sharing the same function.
    changed_paths = set(staged_entries.keys())
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
        changed_paths=changed_paths,
    )


def parse_metric_args(raw_metrics: tuple) -> dict:
    """THE shared `--metric key=value` parser. Values with a literal `.` parse as float,
    else int, else fall back to the raw string; entries without an `=` are skipped."""
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
    """THE shared `(remote_url, api_token)` resolution, used across every call site that
    needs a client. A live `av login` session takes priority over `cfg["remote_api_token"]`
    but ONLY when issued for the SAME registry this repo points at -- a session from a
    different server is silently ignored, never sent cross-server."""
    cfg = cfg if cfg is not None else load_config(repo_root)
    remote_url = cfg.get("remote_url", "http://localhost:8000")

    from . import session_store

    session = session_store.load_session()
    if session and session.get("url") == remote_url and session.get("token"):
        return remote_url, session["token"]

    return remote_url, cfg.get("remote_api_token")


def capture_code_pointer(repo_root: Path) -> dict | None:
    """THE shared git code-provenance capture for `av run start`: {git_remote, git_sha,
    dirty}, or None when this isn't a git checkout or on any subprocess failure -- code
    provenance is best-effort, never a hard requirement."""
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
    """THE single run-id precedence rule — explicit argument > AV_RUN_ID env >
    .av/run.json state — used by every commit path so `AV_RUN_ID=<id> <any av command>`
    behaves identically everywhere. Env wins over state because it's the deliberate
    per-process override; state is the ambient "someone ran `av run start`" default.
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
    """The run:<id> tag of a (local) commit, or None. Lets `_finalize_commit`'s ref-race
    path attribute a collision to a run, like `av pull`'s divergence message and
    `av merge`'s conflict message already do."""
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
    THE shared machine-driven-commit seam: framework plugins call this instead of
    chdir-ing and invoking the CLI. Both this and plain `av commit` funnel into
    `_finalize_commit`, so there is still exactly one commit writer.

    Staging runs against the untouched index (so an unchanged re-import stays a no-op),
    then the index is scoped to exactly what THIS staging touched before committing, and
    everything else merges back in `finally` with its staged flag untouched. Missing
    paths are skipped silently (some frameworks announce checkpoints before writing them);
    directories stage recursively via the same rules `av add .` uses.

    Returns the new commit hash, or None when nothing changed.
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

    # resolve_run_id() is THE one precedence rule (explicit > env > state), shared with
    # av commit/av watch -- see its docstring.
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
        #
        # V1.5.0: no idx.save() here (unlike before) -- the scoped dict stays in memory and
        # is handed straight to commit_staged(idx=idx) below, which writes it exactly once
        # via _finalize_commit's idx.clear_staged(). Writing it here just to have
        # commit_staged immediately re-read + overwrite it was a fully redundant read+write
        # cycle: 3 index reads + 3 writes + a deepcopy per call, down to 1 read + up to 2
        # writes (clear_staged's, and the merge-back below only if this staging actually
        # committed something).
        idx.entries = {
            rel_path: entry
            for rel_path, entry in idx.entries.items()
            if rel_path not in baseline_keys
            or entry.get("hash") != saved[rel_path].get("hash")
            or (entry.get("staged") and rel_path not in pre_staged)
        }

        return commit_staged(
            repo_root, message, tags=tuple(tags), metrics=dict(metrics or {}),
            run_id=run_id, idx=idx,
        )
    finally:
        # Post-commit index: the scoped targets present (idx already reflects their
        # post-commit state -- clear_staged() ran on this exact object) merged with
        # everything the user had staged/tracked before, untouched. Reuses `idx` in memory
        # instead of a fresh Index(repo_root) reload -- it's already exactly the right
        # object, whether commit_staged committed something or returned None early.
        for rel_path, entry in saved.items():
            if rel_path not in idx.entries:
                idx.entries[rel_path] = entry
        idx.save()


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
    """Makes the index and the working tree match a flat commit tree. The one shared
    restore path behind `checkout`, `av clone`, and `av pull`: replaces idx.entries,
    deletes working files the tree no longer contains, then re-stats every entry and
    clears its staged flag so `av status` reads clean immediately after."""
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

            # Restore every tracked file's content from the CAS, not just artifacts --
            # `code` files are written to .av/objects by `add()` too, so an older commit's
            # code must be materialized here the same way.
            materialize_file(repo_root, client, rel_path, h, layers, chunks)

    for rel_path in old_entries:
        if rel_path not in idx.entries:
            remove_file_and_pointer(repo_root, rel_path)

    # Record the real on-disk size/mtime for every materialized file and clear the staged
    # flag, so `av status` reports a clean tree right after checkout.
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
EXIT_BUDGET_EXHAUSTED = 17        # a budget dimension is now exceeded (the spend still recorded)
EXIT_FROZEN = 18                  # project is frozen; promotions/self-edits are paused
EXIT_REVIEW_REQUIRED = 19         # improver promotion needs reviewer approval / has open critiques
EXIT_SCOPE_DENIED = 20            # token authenticated but lacks the required scope (server 403)
# Mirrors scope_denied's shape -- the caller authenticated fine, they just don't own the
# project_id they targeted (AV_TENANCY_ENFORCE=1 only).
EXIT_LOGIN_REQUIRED = 21          # av login's device-code flow timed out with no approval
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
    """Builds the one-and-only agent-facing response shape. `error_data` is an optional
    dict of machine-readable failure context (conflict file lists, racing run/commit ids,
    remediation lines), omitted entirely when empty so old clients see no shape change."""
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
    """Loads and parses one of the published contracts under av_cli/schemas/<name>.schema.json
    (see docs/contracts.md for the full list). Uses importlib.resources so this works from
    an installed wheel, not just a checkout."""
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
    """Uniform failure path: JSON envelope + documented exit code in one raise. Always
    raises -- call sites stop here. `quiet_text=True` skips the generic "Error: {message}"
    line in text mode, for call sites that already printed a richer explanation."""
    if ctx is None:
        # Most call sites pass ctx=None (no live context handy at that point in the call
        # chain). click.get_current_context(silent=True) finds the REAL context of
        # whichever command is running, so this still correctly honors JSON mode.
        ctx = click.get_current_context(silent=True)
    exit_code = _EXIT_CODES.get(code, EXIT_VALIDATION)
    cmd = command or (ctx.command.name if ctx and getattr(ctx, "command", None) else "av")
    if output_is_json(ctx):
        click.echo(json.dumps(json_envelope(cmd, error_code=code, error_message=message,
                                             error_data=data)))
    elif not quiet_text:
        click.secho(f"Error: {message}", fg="red", err=True)
    # Always SystemExit, never ctx.exit(): Context.exit() raises click.exceptions.Exit,
    # which CliRunner.invoke(standalone_mode=False) silently swallows, leaving
    # result.exit_code at 0 regardless of the code passed. A bare SystemExit propagates
    # correctly either way, so it's the only mechanism used here.
    raise SystemExit(exit_code)
