"""Atomic file-write helpers, shared between the per-repo config (`main.py`) and the
user-level config (`update_check.py`) — factored out so neither module needs to import
the other just to get a write helper.
"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path


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
