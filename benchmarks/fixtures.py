"""Fixture generators shared across bench_*.py scripts.

Wraps av_cli.speedcheck's existing fixture constants/builders (single source of truth for
those stays in speedcheck.py, which is scoped to av's own internal hot-path probes) and
adds constants specific to cross-tool benchmarking that don't belong there.
"""

import json
import struct
from pathlib import Path

from av_cli.speedcheck import (  # noqa: F401 — re-exported for bench_*.py scripts
    CLI_CODE_FILE_COUNT,
    CLI_CODE_FILE_SIZE,
    CLI_LARGE_FILE_COUNT,
    CLI_LARGE_FILE_SIZE,
    populate_cli_fixture,
)

# "Small tier" per user direction — not literal GB, to stay practical to generate/run in a
# dev sandbox. Headline framing is "Nx faster at every size tested," not a specific GB claim.
HASH_BENCH_FILE_SIZES_MB = [10, 50, 100, 200]

STORAGE_CURVE_COMMIT_COUNT = 6
SAFETENSORS_LAYER_COUNT = 4
SAFETENSORS_LAYER_SIZE_MB = 5


def make_large_file(path: Path, size_mb: int) -> None:
    chunk = b"\xab" * (1024 * 1024)
    with open(path, "wb") as f:
        for _ in range(size_mb):
            f.write(chunk)


def dir_size_bytes(path: Path) -> int:
    if not path.is_dir():
        return 0
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())


def make_safetensors(path: Path, layer_contents: dict[str, bytes]) -> None:
    """Builds a minimal, real, valid safetensors file: 8-byte LE header length + JSON
    header + raw tensor bytes. Same format as tests/test_core.py's `_make_safetensors`
    helper, so `aether_core.split_and_hash_safetensors` parses it exactly as it would a
    real checkpoint.
    """
    header = {}
    offset = 0
    blobs = []
    for name, data in layer_contents.items():
        header[name] = {"dtype": "U8", "shape": [len(data)], "data_offsets": [offset, offset + len(data)]}
        offset += len(data)
        blobs.append(data)
    header_bytes = json.dumps(header).encode("utf-8")
    path.write_bytes(struct.pack("<Q", len(header_bytes)) + header_bytes + b"".join(blobs))


def make_finetune_step_safetensors(path: Path, step: int) -> None:
    """Writes a checkpoint for fine-tune step `step`: every `backbone.layer_i` is
    byte-identical across steps; only `classifier_head`'s bytes change -- exactly the
    scenario layer-level dedup exists for.
    """
    layer_size = SAFETENSORS_LAYER_SIZE_MB * 1024 * 1024
    # Backbone and head use disjoint byte-value ranges so their content can never
    # coincidentally collide and get deduped into the same CAS object.
    layers = {f"backbone.layer_{i}": bytes([i]) * layer_size for i in range(SAFETENSORS_LAYER_COUNT - 1)}
    layers["classifier_head"] = bytes([200 + (step % 50)]) * layer_size
    make_safetensors(path, layers)
