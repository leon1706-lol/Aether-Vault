import hashlib
import json
import os
import struct

import pytest

aether_core = pytest.importorskip("aether_core")


def _make_safetensors(tensors: dict) -> bytes:
    """Build a minimal valid safetensors blob: 8-byte LE header length + JSON header + data.

    `tensors` maps name -> raw bytes for that tensor's data.
    """
    header = {}
    offset = 0
    blobs = []
    for name, data in tensors.items():
        header[name] = {
            "dtype": "U8",
            "shape": [len(data)],
            "data_offsets": [offset, offset + len(data)],
        }
        offset += len(data)
        blobs.append(data)
    header_bytes = json.dumps(header).encode("utf-8")
    return struct.pack("<Q", len(header_bytes)) + header_bytes + b"".join(blobs)


def test_hash_file_matches_python_sha256(tmp_path):
    p = tmp_path / "f.bin"
    p.write_bytes(b"hello world" * 1000)
    assert aether_core.hash_file(str(p)) == hashlib.sha256(p.read_bytes()).hexdigest()


def test_hash_file_missing_file_raises(tmp_path):
    with pytest.raises(RuntimeError):
        aether_core.hash_file(str(tmp_path / "nope.bin"))


def test_compare_metadata_detects_size_change(tmp_path):
    p = tmp_path / "f.bin"
    p.write_bytes(b"data")
    st = os.stat(p)

    # The C++ core's mtime epoch (std::filesystem) is documented (development/Probleme.md) to
    # differ from Python's os.stat Unix-epoch mtime, so this only asserts the `size` mismatch
    # case — the CLI itself never compares the two across languages (see compare_meta_safe in
    # main.py, which uses os.stat exclusively for that reason).
    p.write_bytes(b"data, but longer now")
    assert aether_core.compare_metadata(str(p), st.st_size, st.st_mtime_ns) is False


def test_compare_metadata_missing_file_is_false(tmp_path):
    assert aether_core.compare_metadata(str(tmp_path / "nope.bin"), 0, 0) is False


def test_split_and_hash_safetensors_layers(tmp_path):
    p = tmp_path / "model.safetensors"
    p.write_bytes(_make_safetensors({
        "layer1.weight": b"A" * 16,
        "layer2.weight": b"A" * 16,  # identical bytes to layer1 -> identical hash
    }))

    layers = aether_core.split_and_hash_safetensors(str(p))
    names = {l["name"] for l in layers}
    assert "__header__" in names
    assert "layer1.weight" in names
    assert "layer2.weight" in names

    by_name = {l["name"]: l for l in layers}
    assert by_name["layer1.weight"]["size"] == 16
    assert by_name["layer2.weight"]["size"] == 16
    assert by_name["layer1.weight"]["hash"] == by_name["layer2.weight"]["hash"]


def test_split_and_hash_safetensors_rejects_oversized_header(tmp_path):
    p = tmp_path / "bad.safetensors"
    # Claim a header far larger than the file actually has.
    p.write_bytes(struct.pack("<Q", 10_000_000) + b"{}")
    with pytest.raises(RuntimeError):
        aether_core.split_and_hash_safetensors(str(p))
