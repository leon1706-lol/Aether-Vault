"""Atomic file-write helpers, shared between the per-repo config (`main.py`) and the
user-level config (`update_check.py`) — factored out so neither module needs to import
the other just to get a write helper.
"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

from .exceptions import AmbiguousCommitHash

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
