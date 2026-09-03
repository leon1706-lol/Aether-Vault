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
KNOWN_FLAGS = frozenset({"no-chunk", "no-layer-split", "chunk"})

ATTRIBUTES_TEMPLATE = """\
# .avattributes — per-path staging directives for Aether-Vault
#
# Format:    <glob-pattern> <flag> [<flag> ...]
# Matching:  fnmatch globs against repo-relative paths; the LAST matching line wins
#            (a later line's flag set REPLACES an earlier one for the same path — flags
#            don't merge across lines).
#
# Supported flags:
#   no-chunk         Store as a single whole-file blob instead of content-defined chunks
#                    (applies above the LFS threshold to: .pt .pth .ckpt .npz .h5 .hdf5
#                    .pb .msgpack .bin .onnx .model .arrow .feather .pkl .pickle)
#   chunk            Force-enable content-defined chunking for a glob that wouldn't
#                    otherwise qualify (any extension not in the default list above) —
#                    e.g. a dataset dump you've confirmed is edited append-only. `no-chunk`
#                    on the same matching line always wins over `chunk` (safety first).
#                    Best for uncompressed / block-aligned formats; COMPRESSED containers
#                    (.parquet with per-column compression, .zip/.gz/.tar/.7z) usually
#                    rewrite their whole stream on any logical edit, so CDC boundaries
#                    won't survive and chunking just adds overhead with no dedup payoff —
#                    only opt them in if you've verified otherwise for your export path.
#   no-layer-split   Never split safetensors into per-layer shards; store the whole file
#
# Examples:
#   *.pt no-chunk
#   models/frozen/** no-chunk no-layer-split
#   experiments/*.safetensors no-layer-split
#   datasets/exports/*.parquet chunk        # opted in: this pipeline only appends rows
#   *.ckpt no-chunk                         # a checkpoint format you know rewrites whole
#   raw/*.wav chunk                         # uncompressed audio, block-aligned — safe to chunk
#   archives/*.zip                          # deliberately NOT opted in (compressed, see above)
#   scratch/** no-chunk no-layer-split      # last line wins: everything under scratch/ stored whole
#
# See docs/avattributes.md for the full "risky formats are opt-in by default" rationale.
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
