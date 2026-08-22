""".avattributes — per-path staging directives, gitattributes-style.

Lets a repo pin how specific files are stored at `av add` time without CLI flags:
disable CDC chunking for opaque checkpoints, or disable safetensors layer-splitting,
via glob patterns. Parsed once per CLI invocation and consulted inside stage_one_file,
so the cost is one small file read per command regardless of file count.
"""

from __future__ import annotations

import fnmatch
from pathlib import Path

# Flags this version actually understands. Unknown flags on a matching line are ignored
# (forward compatibility — older CLIs won't choke on newer files).
KNOWN_FLAGS = frozenset({"no-chunk", "no-layer-split"})

ATTRIBUTES_TEMPLATE = """\
# .avattributes — per-path staging directives for Aether-Vault
#
# Format:    <glob-pattern> <flag> [<flag> ...]
# Matching:  fnmatch globs against repo-relative paths; the LAST matching line wins.
#
# Supported flags:
#   no-chunk         Store as a single whole-file blob instead of content-defined chunks
#                    (applies to opaque checkpoints: .pt / .pth / .ckpt above the LFS threshold)
#   no-layer-split   Never split safetensors into per-layer shards; store the whole file
#
# Examples:
#   *.pt no-chunk
#   models/frozen/** no-chunk no-layer-split
#   experiments/*.safetensors no-layer-split
"""


def load_attributes(repo_root: Path) -> list[tuple[str, set[str]]]:
    """Parses `.avattributes` from the repo root → [(pattern, known_flags)].

    Blank lines and #-comments are skipped. Returns [] when the file doesn't exist or is
    unreadable — attributes must never make `av add` fail.
    """
    path = repo_root / ".avattributes"
    if not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    rules: list[tuple[str, set[str]]] = []
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        pattern, flags = parts[0], set(parts[1:])
        rules.append((pattern, set(flags) & KNOWN_FLAGS))
    return rules


def flags_for(rules: list[tuple[str, set[str]]], rel_path: str) -> set[str]:
    """Effective flags for one repo-relative path (last matching rule wins)."""
    effective: set[str] = set()
    for pattern, flags in rules:
        if fnmatch.fnmatch(rel_path, pattern):
            effective = flags
    return effective
