"""Atomic file-write helpers, shared between the per-repo config (`main.py`) and the
user-level config (`update_check.py`) — factored out so neither module needs to import
the other just to get a write helper.
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from pathlib import Path

from .exceptions import AmbiguousCommitHash, RepoLockedError

# User-level (not per-repo) config directory -- ~/.aether-vault. Lives here, not in
# update_check.py, specifically so session_store.py (and anything else that just needs
# this one path) doesn't have to import update_check.py's own `requests`/`packaging`
# dependencies just to resolve a directory name. V1.5.0 perf work: this one misplaced
# constant was the root cause of `requests` loading on every single `av` invocation.
USER_CONFIG_DIR = Path.home() / ".aether-vault"


def get_version() -> str:
    """Banner/`--version` string, resolved locally with zero network cost. setuptools-scm
    regenerates `av_cli/_version.py` on every build; metadata and a literal fallback cover
    source-checkouts without that file.

    Lives here (not in `ui.py`, where it originally sat next to the banner that also uses
    it) so `av --version` -- and anything else that only needs this one string -- doesn't
    drag in `ui.py`'s module-level `rich`/`questionary` imports. V1.5.0 perf work: measured
    at ~1.3s of that path's cost, same class of bug as `USER_CONFIG_DIR` above.
    """
    try:
        from ._version import __version__

        return __version__
    except Exception:
        pass
    try:
        from importlib.metadata import version

        return version("aether-vault")
    except Exception:
        return "dev"


def atomic_write_text(path: Path, text: str, *, durable: bool = True) -> None:
    """Write text to `path` atomically (write to a temp file in the same dir, then replace).

    Prevents a crash mid-write from leaving a truncated/corrupt file: readers always see
    either the old or the new complete content. os.replace is atomic on POSIX and Windows.

    `durable=False` (V1.6.0, WS4.8) skips the `fsync` -- still atomic (a crash mid-write
    never leaves a truncated/corrupt file, since `os.replace` only ever sees a fully-written
    temp file), just not guaranteed to survive a power loss between the write and the next
    fsync of *something* in this directory. Only for data a crash can always regenerate by
    redoing the operation, e.g. `av clone`'s commit-file writes (measured ~16 ms/file here,
    ~8 s for 500 commits) -- never for a local commit, whose fsync is the durability
    guarantee the whole product promises.
    """
    atomic_write_chunks(path, (text,), durable=durable)


def atomic_write_chunks(path: Path, chunks, *, durable: bool = True) -> None:
    """`atomic_write_text` over an iterable of text fragments, written as they are produced
    -- the whole document never has to exist as one string in memory (V1.6.3: `.av/index`
    streams its entries this way instead of building a multi-MB JSON string first). Same
    atomicity and `durable` semantics."""
    path.parent.mkdir(parents=True, exist_ok=True)
    # Short random suffix (not pid + full uuid4 hex): commit filenames are already a 64-char
    # hash, and on Windows the combined path can exceed the 260-char MAX_PATH once a long
    # temp suffix is appended, which makes the "atomic" write fail outright instead of just
    # being verbose.
    tmp = path.with_name(f"{path.name}.tmp.{uuid.uuid4().hex[:8]}")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            for chunk in chunks:
                f.write(chunk)
            if durable:
                f.flush()
                os.fsync(f.fileno())
        _replace_with_retry(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def _replace_with_retry(source: Path, target: Path, attempts: int = 40, wait_seconds: float = 0.05) -> None:
    """Windows refuses `os.replace` while another process has `target` open for reading, even
    for the instant a concurrent reader needs."""
    for attempt in range(attempts):
        try:
            os.replace(source, target)
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(wait_seconds)


def read_text_with_retry(path: Path, attempts: int = 40, wait_seconds: float = 0.05) -> str:
    """Same reason as `_replace_with_retry`, from the reader's side."""
    for attempt in range(attempts):
        try:
            return path.read_text(encoding="utf-8")
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(wait_seconds)
    raise AssertionError("unreachable")


class _LockState:
    __slots__ = ("guard", "depth", "handle")

    def __init__(self) -> None:
        self.guard = threading.RLock()
        self.depth = 0
        self.handle = None


_lock_states: dict[str, _LockState] = {}
_lock_states_guard = threading.Lock()
DEFAULT_LOCK_TIMEOUT_SECONDS = 30.0


def _try_os_lock(handle) -> bool:
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def _os_unlock(handle) -> None:
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass


class RepoLock:
    """One writer at a time for a repository's local state. An OS file lock, so a killed
    holder can never leave it stuck; re-entrant within a thread, so a caller already holding
    it can reach `commit_staged`. `release()` is idempotent per instance, which lets
    `commit_staged` drop the lock before its network step and still clean up in `finally`."""

    def __init__(self, repo_root: Path, name: str = "lock") -> None:
        self.path = Path(repo_root) / ".av" / name
        self._held = False

    def _state(self) -> _LockState:
        key = str(self.path)
        with _lock_states_guard:
            return _lock_states.setdefault(key, _LockState())

    def acquire(self) -> "RepoLock":
        raw = os.environ.get("AV_LOCK_TIMEOUT", "").strip()
        try:
            timeout = float(raw) if raw else DEFAULT_LOCK_TIMEOUT_SECONDS
        except ValueError:
            timeout = DEFAULT_LOCK_TIMEOUT_SECONDS
        deadline = time.monotonic() + timeout
        state = self._state()
        if not state.guard.acquire(timeout=timeout):
            raise RepoLockedError(f"Repository is locked by another writer in this process ({self.path.name}).")
        try:
            if state.depth == 0:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                handle = open(self.path, "a+b")
                while not _try_os_lock(handle):
                    if time.monotonic() >= deadline:
                        handle.close()
                        raise RepoLockedError(
                            f"Repository is locked by another writer ({self.path.name}); gave up after {timeout:g} s."
                        )
                    time.sleep(0.02)
                state.handle = handle
            state.depth += 1
        except BaseException:
            state.guard.release()
            raise
        self._held = True
        return self

    def release(self) -> None:
        if not self._held:
            return
        self._held = False
        state = self._state()
        state.depth -= 1
        if state.depth == 0 and state.handle is not None:
            _os_unlock(state.handle)
            state.handle.close()
            state.handle = None
        state.guard.release()

    def __enter__(self) -> "RepoLock":
        return self.acquire()

    def __exit__(self, *_exc) -> None:
        self.release()


def atomic_write_json(path: Path, data, *, durable: bool = True) -> None:
    atomic_write_text(path, json.dumps(data, indent=2), durable=durable)


def atomic_write_json_compact(path: Path, data) -> None:
    """Same atomicity guarantee as `atomic_write_json`, without the `indent=2` pretty-
    printing -- for machine-only files nobody hand-reads (`.av/index`, `.av/pending_push`).
    V1.5.0 perf work: `indent=2` costs real bytes (and the `json` module's own formatting
    work) on files rewritten wholesale on every `add`/`commit`; `.av/commits/<hash>.json`
    deliberately keeps `atomic_write_json`'s pretty form since a human does sometimes read
    one directly, and it isn't the hashed/signed byte form regardless (that's a separate,
    explicit `json.dumps(..., sort_keys=True)` call in core.py/casobj.py -- see the
    V1.5.0 CHANGELOG entry's invariant note; this helper must never be used for that call).
    """
    atomic_write_text(path, json.dumps(data, separators=(",", ":")))


def find_commit_file(repo_root: Path, commit_hash: str) -> Path:
    """Resolve a commit identifier to its `.av/commits/<hash>.json` file.

    Accepts the full 64-character hash or any unique hex prefix of one (the short
    form `av commit` itself prints). Raises FileNotFoundError when nothing matches
    and AmbiguousCommitHash when several commits share the given prefix.
    """
    commits_dir = repo_root / ".av" / "commits"
    exact = commits_dir / f"{commit_hash}.json"
    if exact.exists():
        return exact
    if 4 <= len(commit_hash) < 64 and all(c in "0123456789abcdef" for c in commit_hash.lower()):
        matches = sorted(commits_dir.glob(f"{commit_hash.lower()}*.json"))
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise AmbiguousCommitHash(
                f"Commit '{commit_hash}' is ambiguous — {len(matches)} commits share this "
                "prefix. Use more characters."
            )
    raise FileNotFoundError(f"Commit '{commit_hash}' not found.")
