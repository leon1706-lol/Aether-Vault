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


def test_chunk_and_hash_file_produces_valid_chunks(tmp_path):
    # 32 MB: with the default avg-2MB mask the chance of ZERO cut points in random data
    # is ~e^-15 (vs ~7% at the 6 MB this test used before, which flaked once in CI-style
    # full runs when a blob happened to produce a single chunk). Deterministic enough to
    # assert a lower bound of 2 while staying sub-second.
    p = tmp_path / "checkpoint.pt"
    data = os.urandom(32 * 1024 * 1024)
    p.write_bytes(data)

    chunks = aether_core.chunk_and_hash_file(str(p))
    assert 2 <= len(chunks) <= 64
    covered = 0
    for c in chunks:
        assert 512 * 1024 <= c["size"] <= 8 * 1024 * 1024
        assert c["offset"] == covered          # consecutive, no gaps/overlaps
        covered += c["size"]
    assert covered == len(data)


def test_chunk_and_hash_file_boundaries_stable_under_local_edit(tmp_path):
    """The actual dedup claim: an edit inside one region must leave every chunk entirely
    before the edit point byte-identical (same boundary offset AND hash).

    Small explicit chunk params guarantee multiple cuts inside the first half of the file,
    so the survivor assertion is deterministic instead of distribution-dependent.
    """
    p = tmp_path / "checkpoint.pt"
    data = os.urandom(8 * 1024 * 1024)
    p.write_bytes(data)
    original = aether_core.chunk_and_hash_file(str(p), min_chunk=256 * 1024,
                                               avg_chunk=512 * 1024, max_chunk=1024 * 1024)

    mutated = bytearray(data)
    edit_pos = len(data) // 2
    mutated[edit_pos] ^= 0xFF           # flip one byte mid-file
    p.write_bytes(bytes(mutated))
    after = aether_core.chunk_and_hash_file(str(p), min_chunk=256 * 1024,
                                            avg_chunk=512 * 1024, max_chunk=1024 * 1024)

    # every chunk ending strictly before the edited byte must be untouched
    survivors = [
        b for b in after
        if any(a["hash"] == b["hash"] and a["offset"] == b["offset"]
               and b["offset"] + b["size"] <= edit_pos for a in original)
    ]
    assert len(survivors) >= 3, "chunks fully before the edit point must survive unchanged"


def test_chunk_and_hash_file_rejects_bad_params(tmp_path):
    p = tmp_path / "f.bin"
    p.write_bytes(b"x" * 1024)
    with pytest.raises(RuntimeError):
        aether_core.chunk_and_hash_file(str(p), min_chunk=0)
    with pytest.raises(RuntimeError):
        aether_core.chunk_and_hash_file(str(p), min_chunk=4 * 1024 * 1024, avg_chunk=1024)


# ---------------------------------------------------------------------------
# v1.3.0 (todo.md item 1/17): cross-OS golden fixture. Every other CDC test in this
# suite proves determinism WITHIN one process/machine (same-run reuse, boundary
# stability under a local edit) — none of them ever pinned an actual expected hash that
# a genuinely different OS/architecture could be compared against. The gear table is
# generated from a fixed splitmix64 seed (src/core.cpp) specifically so chunk boundaries
# (and therefore shard hashes) are architecture-independent — this is the test that
# actually proves it, by hardcoding the exact expected output and running it on every
# CI leg: Windows (`test` job), Linux (`nightly`'s `compat` job), and macOS
# (`nightly`'s dedicated `golden-fixtures-macos` job, -k "golden").
#
# Input is `random.Random(42).getrandbits(8)` — CPython's Mersenne Twister stream for a
# fixed seed is a long-stable, documented property (not OS/architecture-dependent), so
# this is exactly reproducible input without needing a checked-in binary fixture file.
# ---------------------------------------------------------------------------

def _golden_cdc_input(size_bytes: int) -> bytes:
    import random

    rng = random.Random(42)
    return bytes(rng.getrandbits(8) for _ in range(size_bytes))


def test_golden_cdc_input_bytes_are_stable():
    """Pins the INPUT itself first — if this ever fails, Python's random module changed
    its stream generation (which would also silently invalidate the boundary/hash golden
    fixture below without this catching it first, more legibly)."""
    data = _golden_cdc_input(4 * 1024 * 1024)
    assert len(data) == 4 * 1024 * 1024
    assert hashlib.sha256(data).hexdigest() == (
        "5ac5ccdde350c54d2ebf9e39f33cdd29721cefa16955c5c214ec59427c107ed1"
    )


def test_golden_cdc_chunk_boundaries_and_hashes(tmp_path):
    """The actual cross-OS golden fixture: exact expected (offset, size, hash) triples
    for a fixed 4 MiB input under fixed chunk-size parameters. ANY change to the gear
    table, the cut-point rule, or the hashing itself changes these numbers — that's
    precisely what this test exists to catch, on every OS in CI."""
    p = tmp_path / "golden.bin"
    p.write_bytes(_golden_cdc_input(4 * 1024 * 1024))

    chunks = aether_core.chunk_and_hash_file(
        str(p), min_chunk=256 * 1024, avg_chunk=512 * 1024, max_chunk=1024 * 1024)

    expected = [
        (0, 599408, "23a7c1a2341c899d837a7908127078691bf156f40e4e8d208a845a3bcc1035b9"),
        (599408, 1003226, "eef07a0842594a6fe379cca0d8a016ab53e9652546cc5353cb4800d19b3a68b5"),
        (1602634, 385964, "d20c45241b20aed8767b6eceff93f9a906f9a877911fbd273f50f271f73afc1d"),
        (1988598, 936099, "2c6eafc606b73772252d8d87fc8a1379019dee49a24c60cf842b45f638f81a43"),
        (2924697, 862770, "cc14a21631f5a66edafbdf17d8129dc371c6fa79dc71cc404688d50ca5f060b4"),
        (3787467, 406837, "45faba08f727218cfdb33f06d9a410bf9236bc1a7dee8dd7b286c222bbf34734"),
    ]
    actual = [(c["offset"], c["size"], c["hash"]) for c in chunks]
    assert actual == expected, (
        "CDC chunk boundaries/hashes drifted from the pinned golden fixture — if this "
        "is an intentional algorithm change, dedup silently breaks for every existing "
        "chunked object in every deployed repo (see src/core.cpp's gear-table comment); "
        "update this fixture only alongside a deliberate, documented breaking change."
    )
