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

    # The C++ core's mtime epoch differs from Python's os.stat Unix-epoch mtime, so this
    # only asserts the `size` mismatch case.
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
    # 32 MB: with the default avg-2MB mask, the chance of zero cut points in random data
    # is ~e^-15 -- reliably asserts a lower bound of 2 cuts while staying sub-second.
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


def test_chunk_and_hash_file_max_chunk_is_a_hard_cap_deterministic(tmp_path):
    """Deterministic counterpart to the random-data test above: uniform-byte content
    never trips the gear-hash mask (each cut here is purely size-driven), so this pins
    down the exact max_chunk/min_chunk edge instead of relying on random data's ~e^-15
    chance of ever reaching it. Regression cover for two real bugs -- max_chunk silently
    becoming a soft cap near EOF, and the fix for that over-firing and splitting files
    nowhere near max_chunk."""
    p = tmp_path / "checkpoint.pt"

    # Below max_chunk + min_chunk: no forced cut should ever be needed.
    p.write_bytes(b"\x00" * (2 * 1024 * 1024))
    chunks = aether_core.chunk_and_hash_file(str(p))
    assert chunks == [{"hash": chunks[0]["hash"], "size": 2 * 1024 * 1024, "offset": 0}]

    # Comfortably past max_chunk + min_chunk: must be forced to split, and every
    # resulting chunk -- including the tail -- must respect [min_chunk, max_chunk].
    size = 20 * 1024 * 1024
    p.write_bytes(b"\x00" * size)
    chunks = aether_core.chunk_and_hash_file(str(p))
    assert len(chunks) >= 2
    covered = 0
    for c in chunks:
        assert 512 * 1024 <= c["size"] <= 8 * 1024 * 1024
        assert c["offset"] == covered
        covered += c["size"]
    assert covered == size


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
# stage_cdc -- V1.6.0 (WS4.1) fused single-read staging: one sequential pass computes the
# whole-file hash, cuts + hashes each chunk (via the SAME CdcCutter chunk_and_hash_file uses
# -- see core.cpp's own comment), and publishes each chunk to objects_dir directly, instead
# of chunk_and_hash_file's two read passes plus a separate Python-side write pass. Every
# test here checks against chunk_and_hash_file + hashlib as independent oracles, not just
# "stage_cdc agrees with itself" -- exactly the discipline this project applies to its other
# hardware/algorithm-dispatch surfaces (see src/sha256_backend.cpp's own correctness
# self-test for the same reasoning).
# ---------------------------------------------------------------------------

def _stage_and_verify(tmp_path, data: bytes, min_chunk=512 * 1024, avg_chunk=2 * 1024 * 1024,
                       max_chunk=8 * 1024 * 1024, suffix=""):
    """Runs stage_cdc, cross-checks it against hashlib and chunk_and_hash_file (the legacy
    two-pass oracle), verifies every published object's bytes reassemble the original file
    exactly, and returns the raw result dict for any additional assertions the caller wants."""
    p = tmp_path / f"f{suffix}.bin"
    p.write_bytes(data)
    objects_dir = tmp_path / f"objects{suffix}"

    result = aether_core.stage_cdc(str(p), str(objects_dir), min_chunk=min_chunk,
                                    avg_chunk=avg_chunk, max_chunk=max_chunk)
    assert result["whole_hash"] == hashlib.sha256(data).hexdigest()

    legacy = aether_core.chunk_and_hash_file(str(p), min_chunk=min_chunk, avg_chunk=avg_chunk,
                                              max_chunk=max_chunk)
    fused_boundaries = [(part["offset"], part["size"], part["hash"]) for part in result["parts"]]
    legacy_boundaries = [(c["offset"], c["size"], c["hash"]) for c in legacy]
    assert fused_boundaries == legacy_boundaries, (
        "stage_cdc's cut points/hashes must exactly match chunk_and_hash_file's -- they "
        "share the same CdcCutter, so any difference here is a real bug in one of them"
    )

    reassembled = bytearray()
    for part in result["parts"]:
        obj_path = objects_dir / part["hash"][:2] / part["hash"][2:]
        assert obj_path.exists(), f"published object missing for chunk {part['hash']}"
        obj_bytes = obj_path.read_bytes()
        assert len(obj_bytes) == part["size"]
        assert hashlib.sha256(obj_bytes).hexdigest() == part["hash"]
        reassembled.extend(obj_bytes)
    assert bytes(reassembled) == data, "reassembled chunk bytes must equal the original file exactly"

    leftovers = list(objects_dir.glob("**/*tmp*"))
    assert leftovers == [], f"leftover temp files after staging: {leftovers}"

    return result


def test_stage_cdc_matches_hashlib_and_chunk_and_hash_file_random_data(tmp_path):
    _stage_and_verify(tmp_path, os.urandom(3 * 1024 * 1024 + 17))


def test_stage_cdc_empty_file(tmp_path):
    result = _stage_and_verify(tmp_path, b"")
    assert len(result["parts"]) == 1
    assert result["parts"][0]["size"] == 0


def test_stage_cdc_single_byte(tmp_path):
    _stage_and_verify(tmp_path, b"x")


def test_stage_cdc_file_smaller_than_min_chunk_is_one_part(tmp_path):
    result = _stage_and_verify(tmp_path, os.urandom(100))
    assert len(result["parts"]) == 1


def test_stage_cdc_file_exactly_min_chunk(tmp_path):
    _stage_and_verify(tmp_path, os.urandom(512 * 1024))


@pytest.mark.parametrize("size", [1024 * 1024 - 1, 1024 * 1024, 1024 * 1024 + 1])
def test_stage_cdc_around_the_internal_read_buffer_boundary(tmp_path, size):
    """stage_cdc reads in fixed 1MiB buffers internally -- these sizes straddle that
    boundary exactly, the classic off-by-one surface for any buffered-read implementation."""
    _stage_and_verify(tmp_path, os.urandom(size), min_chunk=1024, avg_chunk=4096, max_chunk=16384)


def test_stage_cdc_max_chunk_is_a_hard_cap_deterministic(tmp_path):
    """Deterministic (uniform-byte) counterpart to the random-data test -- pins down the
    same max_chunk/min_chunk edge chunk_and_hash_file's own equivalent test does."""
    size = 20 * 1024 * 1024
    result = _stage_and_verify(tmp_path, b"\x00" * size)
    assert len(result["parts"]) >= 2
    covered = 0
    for part in result["parts"]:
        assert 512 * 1024 <= part["size"] <= 8 * 1024 * 1024
        assert part["offset"] == covered
        covered += part["size"]
    assert covered == size


@pytest.mark.parametrize("size", [50_000, 300_000, 1_000_000, 2_500_000, 4_999_999])
def test_stage_cdc_various_sizes_small_chunks(tmp_path, size):
    _stage_and_verify(tmp_path, os.urandom(size), min_chunk=4096, avg_chunk=65536,
                       max_chunk=262144, suffix=str(size))


def test_stage_cdc_skips_writing_objects_that_already_exist(tmp_path):
    """The actual dedup claim: staging the SAME file into the SAME objects_dir a second
    time must publish nothing new (mtimes untouched, `written` reports False for every
    part) -- proven by mtime, not just by re-reading correct content."""
    data = os.urandom(3 * 1024 * 1024)
    p = tmp_path / "f.bin"
    p.write_bytes(data)
    objects_dir = tmp_path / "objects"

    first = aether_core.stage_cdc(str(p), str(objects_dir))
    assert first["bytes_written"] == len(data)
    assert all(part["written"] for part in first["parts"])
    mtimes = {
        part["hash"]: (objects_dir / part["hash"][:2] / part["hash"][2:]).stat().st_mtime_ns
        for part in first["parts"]
    }

    second = aether_core.stage_cdc(str(p), str(objects_dir))
    assert second["bytes_written"] == 0
    assert all(not part["written"] for part in second["parts"])
    for part in second["parts"]:
        obj_path = objects_dir / part["hash"][:2] / part["hash"][2:]
        assert obj_path.stat().st_mtime_ns == mtimes[part["hash"]], (
            "an object that already existed must not be rewritten"
        )


def test_stage_cdc_two_different_files_sharing_a_chunk_both_dedup_correctly(tmp_path):
    """Two unrelated files that share an identical PREFIX (a real scenario: two checkpoints
    saved from the same base weights, diverging only partway through) must publish the
    chunk(s) fully inside that shared prefix exactly once, each file's own unique tail
    chunks independently. The shared content must sit at the same relative position (here,
    the very start) in both files -- content-defined chunking's rolling hash carries context
    from whatever precedes a byte, so identical bytes at DIFFERENT preceding contexts are not
    guaranteed to cut identically (a property of the algorithm itself, not a stage_cdc-
    specific behavior: chunk_and_hash_file shares the exact same CdcCutter and would show
    the identical characteristic)."""
    shared_prefix = os.urandom(1024 * 1024)
    file_a = shared_prefix + os.urandom(600 * 1024)
    file_b = shared_prefix + os.urandom(600 * 1024)
    objects_dir = tmp_path / "objects"

    pa = tmp_path / "a.bin"
    pa.write_bytes(file_a)
    result_a = aether_core.stage_cdc(str(pa), str(objects_dir), min_chunk=256 * 1024,
                                      avg_chunk=512 * 1024, max_chunk=1024 * 1024)

    pb = tmp_path / "b.bin"
    pb.write_bytes(file_b)
    result_b = aether_core.stage_cdc(str(pb), str(objects_dir), min_chunk=256 * 1024,
                                      avg_chunk=512 * 1024, max_chunk=1024 * 1024)

    hashes_a = {part["hash"] for part in result_a["parts"]}
    hashes_b = {part["hash"] for part in result_b["parts"]}
    shared_hashes = hashes_a & hashes_b
    assert shared_hashes, "the two files should share at least one identical chunk hash"
    for h in shared_hashes:
        obj_path = objects_dir / h[:2] / h[2:]
        assert hashlib.sha256(obj_path.read_bytes()).hexdigest() == h


def test_stage_cdc_rejects_bad_params(tmp_path):
    p = tmp_path / "f.bin"
    p.write_bytes(b"x" * 1024)
    objects_dir = tmp_path / "objects"
    with pytest.raises(RuntimeError):
        aether_core.stage_cdc(str(p), str(objects_dir), min_chunk=0)
    with pytest.raises(RuntimeError):
        aether_core.stage_cdc(str(p), str(objects_dir), min_chunk=4 * 1024 * 1024, avg_chunk=1024)


def test_stage_cdc_raises_for_missing_file(tmp_path):
    with pytest.raises(RuntimeError):
        aether_core.stage_cdc(str(tmp_path / "does-not-exist.bin"), str(tmp_path / "objects"))


# ---------------------------------------------------------------------------
# stage_safetensors -- fused single-read layer-split staging (V1.6.0, WS4.1 second half)
# ---------------------------------------------------------------------------

def _stage_safetensors_and_verify(tmp_path, tensors: dict, buffer_cap_bytes=32 * 1024 * 1024,
                                   suffix=""):
    """Runs stage_safetensors on a file built by _make_safetensors, cross-checks it against
    hashlib (whole-file) and split_and_hash_safetensors (the legacy fully-parallel oracle,
    per-layer name/hash/size/offset), verifies every published object's bytes match its
    declared slice of the original file exactly, and returns the raw result dict for any
    additional assertions the caller wants."""
    data = _make_safetensors(tensors)
    p = tmp_path / f"model{suffix}.safetensors"
    p.write_bytes(data)
    objects_dir = tmp_path / f"objects{suffix}"

    result = aether_core.stage_safetensors(str(p), str(objects_dir),
                                            buffer_cap_bytes=buffer_cap_bytes)
    assert result["whole_hash"] == hashlib.sha256(data).hexdigest()

    legacy = aether_core.split_and_hash_safetensors(str(p))
    fused_layers = [(part["name"], part["offset"], part["size"], part["hash"]) for part in result["parts"]]
    legacy_layers = [(l["name"], l["offset"], l["size"], l["hash"]) for l in legacy]
    assert fused_layers == legacy_layers, (
        "stage_safetensors's per-layer hashes/offsets must exactly match "
        "split_and_hash_safetensors's -- any difference here is a real bug in one of them"
    )

    for part in result["parts"]:
        obj_path = objects_dir / part["hash"][:2] / part["hash"][2:]
        assert obj_path.exists(), f"published object missing for layer {part['name']!r}"
        obj_bytes = obj_path.read_bytes()
        assert len(obj_bytes) == part["size"]
        assert hashlib.sha256(obj_bytes).hexdigest() == part["hash"]
        assert obj_bytes == data[part["offset"]:part["offset"] + part["size"]], (
            f"published object for layer {part['name']!r} must equal that exact byte range "
            f"of the source file"
        )

    leftovers = list(objects_dir.glob("**/*stage-tmp*"))
    assert leftovers == [], f"leftover temp files after staging: {leftovers}"

    return result


def test_stage_safetensors_matches_split_and_hash_and_hashlib(tmp_path):
    result = _stage_safetensors_and_verify(tmp_path, {
        "layer1.weight": os.urandom(5000),
        "layer2.weight": os.urandom(12000),
        "layer3.bias": os.urandom(37),
    })
    names = {p["name"] for p in result["parts"]}
    assert names == {"__header__", "layer1.weight", "layer2.weight", "layer3.bias"}


def test_stage_safetensors_header_only_no_tensors(tmp_path):
    result = _stage_safetensors_and_verify(tmp_path, {})
    assert len(result["parts"]) == 1
    assert result["parts"][0]["name"] == "__header__"


def test_stage_safetensors_single_tiny_layer(tmp_path):
    _stage_safetensors_and_verify(tmp_path, {"w": b"x"})


@pytest.mark.parametrize("buffer_cap_bytes", [1, 100, 1024 * 1024])
def test_stage_safetensors_various_buffer_caps_forcing_disk_streaming(tmp_path, buffer_cap_bytes):
    """A buffer_cap far below every layer's actual size forces the streaming-to-temp-file
    path for every one of them (including __header__) -- must still produce byte-identical
    results to the in-memory path and the legacy oracle."""
    _stage_safetensors_and_verify(tmp_path, {
        "layer1.weight": os.urandom(20_000),
        "layer2.weight": os.urandom(5_000),
    }, buffer_cap_bytes=buffer_cap_bytes, suffix=str(buffer_cap_bytes))


def test_stage_safetensors_layer_larger_than_buffer_cap_streams_correctly(tmp_path):
    """One large layer forced onto the streaming-to-disk path while a small sibling layer
    stays in-memory in the very same staging call -- the two code paths coexist correctly
    within one file, not just in isolation."""
    result = _stage_safetensors_and_verify(tmp_path, {
        "big.weight": os.urandom(2 * 1024 * 1024),
        "small.bias": os.urandom(64),
    }, buffer_cap_bytes=64 * 1024)
    by_name = {p["name"]: p for p in result["parts"]}
    assert by_name["big.weight"]["size"] == 2 * 1024 * 1024


def test_stage_safetensors_identical_layers_dedup_within_one_file(tmp_path):
    """Two layers with byte-identical content hash the same -- the second one publishes
    nothing new (an actual dedup, not just "happens to look the same"), proven by mtime."""
    data = os.urandom(4096)
    p = tmp_path / "model.safetensors"
    p.write_bytes(_make_safetensors({"layer1.weight": data, "layer2.weight": data}))
    objects_dir = tmp_path / "objects"

    result = aether_core.stage_safetensors(str(p), str(objects_dir))
    by_name = {part["name"]: part for part in result["parts"]}
    assert by_name["layer1.weight"]["hash"] == by_name["layer2.weight"]["hash"]
    assert by_name["layer1.weight"]["written"] is True
    assert by_name["layer2.weight"]["written"] is False


def test_stage_safetensors_skips_writing_objects_that_already_exist(tmp_path):
    p = tmp_path / "model.safetensors"
    p.write_bytes(_make_safetensors({"layer1.weight": os.urandom(8192)}))
    objects_dir = tmp_path / "objects"

    first = aether_core.stage_safetensors(str(p), str(objects_dir))
    assert first["bytes_written"] > 0
    mtimes = {
        part["hash"]: (objects_dir / part["hash"][:2] / part["hash"][2:]).stat().st_mtime_ns
        for part in first["parts"]
    }

    second = aether_core.stage_safetensors(str(p), str(objects_dir))
    assert second["bytes_written"] == 0
    assert all(not part["written"] for part in second["parts"])
    for part in second["parts"]:
        obj_path = objects_dir / part["hash"][:2] / part["hash"][2:]
        assert obj_path.stat().st_mtime_ns == mtimes[part["hash"]], (
            "an object that already existed must not be rewritten"
        )


def test_stage_safetensors_gap_between_layers_hashed_into_whole_only(tmp_path):
    """A declared data_offsets gap (padding a real safetensors writer might insert) must be
    folded into the whole-file hash but must NOT become its own part -- only __header__ plus
    the one declared tensor layer."""
    header = {
        "layer1.weight": {"dtype": "U8", "shape": [16], "data_offsets": [100, 116]},
    }
    header_bytes = json.dumps(header).encode("utf-8")
    base_offset = 8 + len(header_bytes)
    padding = os.urandom(100)  # the gap: bytes [0, 100) of the data section
    tensor_data = os.urandom(16)
    data = struct.pack("<Q", len(header_bytes)) + header_bytes + padding + tensor_data
    p = tmp_path / "model.safetensors"
    p.write_bytes(data)
    objects_dir = tmp_path / "objects"

    result = aether_core.stage_safetensors(str(p), str(objects_dir))
    assert result["whole_hash"] == hashlib.sha256(data).hexdigest()
    names = [part["name"] for part in result["parts"]]
    assert names == ["__header__", "layer1.weight"], (
        "the padding gap must not appear as its own part"
    )
    layer_part = result["parts"][1]
    assert layer_part["offset"] == base_offset + 100
    assert layer_part["size"] == 16
    obj_path = objects_dir / layer_part["hash"][:2] / layer_part["hash"][2:]
    assert obj_path.read_bytes() == tensor_data


def test_stage_safetensors_overlapping_layers_raise(tmp_path):
    """The fused path cannot represent overlapping declared ranges (see core.cpp's own
    comment) -- it must throw so the Python caller falls back to the legacy per-layer
    re-read, which handles overlap fine (each layer independently re-reads its own range)."""
    header = {
        "layer1.weight": {"dtype": "U8", "shape": [16], "data_offsets": [0, 16]},
        "layer2.weight": {"dtype": "U8", "shape": [16], "data_offsets": [8, 24]},  # overlaps layer1
    }
    header_bytes = json.dumps(header).encode("utf-8")
    data = struct.pack("<Q", len(header_bytes)) + header_bytes + os.urandom(24)
    p = tmp_path / "model.safetensors"
    p.write_bytes(data)
    objects_dir = tmp_path / "objects"

    with pytest.raises(RuntimeError):
        aether_core.stage_safetensors(str(p), str(objects_dir))
    # The legacy oracle has no such restriction -- confirms the fallback path stays viable.
    legacy = aether_core.split_and_hash_safetensors(str(p))
    assert {l["name"] for l in legacy} == {"__header__", "layer1.weight", "layer2.weight"}


def test_stage_safetensors_rejects_oversized_header(tmp_path):
    p = tmp_path / "bad.safetensors"
    p.write_bytes(struct.pack("<Q", 10_000_000) + b"{}")
    with pytest.raises(RuntimeError):
        aether_core.stage_safetensors(str(p), str(tmp_path / "objects"))


def test_stage_safetensors_raises_for_missing_file(tmp_path):
    with pytest.raises(RuntimeError):
        aether_core.stage_safetensors(str(tmp_path / "does-not-exist.safetensors"),
                                       str(tmp_path / "objects"))


# ---------------------------------------------------------------------------
# Cross-OS golden fixture: the gear table is generated from a fixed splitmix64 seed so
# chunk boundaries (and shard hashes) are architecture-independent -- this hardcodes the
# exact expected output and runs it on every CI leg (Windows/Linux/macOS). Input is
# `random.Random(42).getrandbits(8)`, a long-stable reproducible stream with no need
# for a checked-in binary fixture file.
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


# ---------------------------------------------------------------------------
# V1.5.0 perf work: SHA-256 bulk-update rewrite, GIL release, shared thread pool
# ---------------------------------------------------------------------------


def test_sha256_update_bulk_rewrite_matches_hashlib_under_random_splits():
    """`SHA256::update()` used to copy one byte at a time; V1.5.0 rewrote it to a
    memcpy-based bulk path (fill partial block, transform whole 64-byte blocks, buffer the
    tail). `_hash_bytes_split` exercises that exact rewrite across randomized call-size
    splits real usage never produces (hash_file_sequential always reads in fixed 1MB
    buffers) -- every split pattern below must still match hashlib bit-for-bit."""
    import random

    rng = random.Random(20260908)
    for trial in range(40):
        size = rng.choice([0, 1, 2, 63, 64, 65, 127, 128, 129, 191, 192, 193,
                            4095, 4096, 4097, 1000, 100000])
        data = bytes(rng.getrandbits(8) for _ in range(size))
        expected = hashlib.sha256(data).hexdigest()

        # A handful of pathological split patterns, plus fully randomized ones.
        split_patterns = [
            [1] * size,                      # every byte its own update() call
            [size],                          # single call, whole buffer
            [63, 1, 64, 65, size],           # exact boundary-crossing sizes
            [1, 63, 1, 1, 65, 4096, 1],       # small then large then small
        ]
        # A few fully random partitions of `size`.
        for _ in range(3):
            remaining = size
            parts = []
            while remaining > 0:
                take = rng.randint(1, remaining)
                parts.append(take)
                remaining -= take
            split_patterns.append(parts or [0])

        for splits in split_patterns:
            actual = aether_core._hash_bytes_split(data, splits)
            assert actual == expected, (
                f"trial={trial} size={size} splits={splits}: got {actual}, want {expected}"
            )


def test_hash_file_releases_the_gil(tmp_path):
    """Regression guard for V1.5.0's py::call_guard<gil_scoped_release> on hash_file: a
    background Python thread must be able to make measurable progress *during* a hash_file
    call on a large-enough file. Asserts the counter advanced past a low threshold (that
    real work interleaved), never a speedup ratio -- a ratio assertion on a shared CI
    runner is exactly the kind of thing that becomes a flake within a week.
    """
    import threading

    # pytest's own tmp_path (not a hand-rolled RUNNER_TEMP/TEMP env-var fallback chain that
    # silently lands in the repo checkout's own cwd on POSIX CI, where neither is set) --
    # also auto-cleaned, no manual os.remove needed.
    path = tmp_path / "av_gil_release_test.bin"
    # Large enough that hashing takes a real, if brief, amount of wall time even on a fast
    # disk/CPU -- 128MB of zeros compresses to nothing on disk on most filesystems that
    # support sparse files, but is written here as real bytes to also exercise real I/O.
    size = 128 * 1024 * 1024
    path.write_bytes(b"\x00" * size)

    counter = {"n": 0}
    stop = threading.Event()

    def spin():
        while not stop.is_set():
            counter["n"] += 1

    t = threading.Thread(target=spin, daemon=True)
    t.start()
    try:
        aether_core.hash_file(str(path))
    finally:
        stop.set()
        t.join(timeout=5)

    # If the GIL were held for the whole call, the spin thread would get essentially no
    # time slices during it -- a few hundred increments at most from scheduling noise
    # before/after. A released GIL lets it run freely and rack up a much larger count.
    assert counter["n"] > 1000, (
        f"background thread only advanced {counter['n']} during hash_file() -- "
        "looks like the GIL was held for the call's duration (call_guard regression?)"
    )


@pytest.mark.parametrize("num_threads", [1, 2, 4, 8])
def test_chunk_and_hash_file_thread_count_does_not_change_golden_result(tmp_path, num_threads):
    """The CDC golden fixture's (offset, size, hash) triples must be identical no matter
    how many threads set_max_threads() configures for pass 2's per-chunk hashing --
    threading changes WHEN chunks get hashed, never WHAT gets produced."""
    p = tmp_path / "golden.bin"
    p.write_bytes(_golden_cdc_input(4 * 1024 * 1024))
    aether_core.set_max_threads(num_threads)
    try:
        chunks = aether_core.chunk_and_hash_file(
            str(p), min_chunk=256 * 1024, avg_chunk=512 * 1024, max_chunk=1024 * 1024)
    finally:
        aether_core.set_max_threads(0)  # back to auto for any test that runs after this one

    expected = [
        (0, 599408, "23a7c1a2341c899d837a7908127078691bf156f40e4e8d208a845a3bcc1035b9"),
        (599408, 1003226, "eef07a0842594a6fe379cca0d8a016ab53e9652546cc5353cb4800d19b3a68b5"),
        (1602634, 385964, "d20c45241b20aed8767b6eceff93f9a906f9a877911fbd273f50f271f73afc1d"),
        (1988598, 936099, "2c6eafc606b73772252d8d87fc8a1379019dee49a24c60cf842b45f638f81a43"),
        (2924697, 862770, "cc14a21631f5a66edafbdf17d8129dc371c6fa79dc71cc404688d50ca5f060b4"),
        (3787467, 406837, "45faba08f727218cfdb33f06d9a410bf9236bc1a7dee8dd7b286c222bbf34734"),
    ]
    actual = [(c["offset"], c["size"], c["hash"]) for c in chunks]
    assert actual == expected


def test_split_and_hash_safetensors_layer_order_stable_across_thread_counts(tmp_path):
    """Layer results must come back sorted by offset (file order) regardless of how many
    worker threads hash them concurrently -- callers rely on this ordering."""
    tensors = {f"layer_{i}": bytes([i]) * 4096 for i in range(12)}
    p = tmp_path / "model.safetensors"
    p.write_bytes(_make_safetensors(tensors))

    results_by_threads = {}
    for n in (1, 2, 4, 8):
        aether_core.set_max_threads(n)
        try:
            layers = aether_core.split_and_hash_safetensors(str(p))
        finally:
            aether_core.set_max_threads(0)
        results_by_threads[n] = [(layer["name"], layer["hash"], layer["offset"]) for layer in layers]

    baseline = results_by_threads[1]
    for n, result in results_by_threads.items():
        assert result == baseline, f"thread count {n} produced a different layer ordering/hashes"
        offsets = [offset for _, _, offset in result]
        assert offsets == sorted(offsets), f"thread count {n}: layers not in file order"


def test_set_max_threads_get_max_threads_roundtrip():
    aether_core.set_max_threads(3)
    try:
        assert aether_core.get_max_threads() == 3
    finally:
        aether_core.set_max_threads(0)
    assert aether_core.get_max_threads() == 0


def test_hash_and_copy_matches_hash_file_and_copies_bytes(tmp_path):
    src = tmp_path / "src.bin"
    src.write_bytes(b"some content" * 5000)
    dest = tmp_path / "dest.bin"
    h = aether_core.hash_and_copy(str(src), str(dest))
    assert h == aether_core.hash_file(str(src))
    assert h == hashlib.sha256(src.read_bytes()).hexdigest()
    assert dest.read_bytes() == src.read_bytes()


def test_hash_and_copy_empty_file(tmp_path):
    src = tmp_path / "empty.bin"
    src.write_bytes(b"")
    dest = tmp_path / "dest.bin"
    h = aether_core.hash_and_copy(str(src), str(dest))
    assert h == hashlib.sha256(b"").hexdigest()
    assert dest.read_bytes() == b""


def test_hash_and_copy_missing_source_raises(tmp_path):
    with pytest.raises(RuntimeError):
        aether_core.hash_and_copy(str(tmp_path / "nope.bin"), str(tmp_path / "dest.bin"))


# ---------------------------------------------------------------------------
# V1.6.0: SHA-256 hardware-backend dispatch (sha256_backend.cpp) -- hash_backend()/
# set_hash_backend() and the correctness invariant every backend must uphold: bit-identical
# output to hashlib regardless of which one actually ran. On this project's own reference
# machine (no SHA-NI/ARM crypto) only "scalar" is ever selectable; the sha-ni/arm-sha2
# assertions below intentionally allow for either outcome so this file passes unmodified on
# a capable machine too (see src/README.md's note on execution-verification status).
# ---------------------------------------------------------------------------

_KNOWN_BACKENDS = {"scalar", "sha-ni", "arm-sha2"}


def test_hash_backend_is_a_known_value():
    assert aether_core.hash_backend() in _KNOWN_BACKENDS


def test_set_hash_backend_scalar_always_succeeds():
    try:
        assert aether_core.set_hash_backend("scalar") is True
        assert aether_core.hash_backend() == "scalar"
    finally:
        aether_core.set_hash_backend("auto")


def test_set_hash_backend_unknown_name_fails_and_leaves_backend_unchanged():
    before = aether_core.hash_backend()
    assert aether_core.set_hash_backend("not-a-real-backend") is False
    assert aether_core.hash_backend() == before


def test_set_hash_backend_auto_always_succeeds():
    assert aether_core.set_hash_backend("auto") is True
    assert aether_core.hash_backend() in _KNOWN_BACKENDS


@pytest.mark.parametrize("name", ["sha-ni", "arm-sha2"])
def test_set_hash_backend_hardware_name_either_unsupported_or_correct(name):
    """Requesting a specific hardware backend either fails cleanly (this CPU/build doesn't
    support it -- the expected outcome on the reference machine) or succeeds and then
    produces hashlib-identical output (the expected outcome on capable hardware/CI) --
    never a third, silently-wrong-digest outcome."""
    try:
        ok = aether_core.set_hash_backend(name)
        if ok:
            assert aether_core.hash_backend() == name
            data = os.urandom(70000)  # spans many blocks, well past one 64-byte block
            assert aether_core.hash_bytes(data) == hashlib.sha256(data).hexdigest()
        else:
            assert aether_core.hash_backend() != name
    finally:
        aether_core.set_hash_backend("auto")


def test_sha256_nist_golden_vectors_via_hash_bytes():
    cases = [
        b"",
        b"abc",
        b"abcdbcdecdefdefgefghfghighijhijkijkljklmklmnlmnomnopnopq",  # NIST 2-block vector
        b"a" * 1_000_000,
    ]
    for msg in cases:
        assert aether_core.hash_bytes(msg) == hashlib.sha256(msg).hexdigest()


def test_sha256_randomized_lengths_zero_to_thousand_match_hashlib():
    import random
    rng = random.Random(20260912)
    for length in range(0, 1000):
        data = bytes(rng.getrandbits(8) for _ in range(length))
        assert aether_core.hash_bytes(data) == hashlib.sha256(data).hexdigest(), length


@pytest.mark.parametrize("size", [
    4 * 1024 * 1024 - 1, 4 * 1024 * 1024, 4 * 1024 * 1024 + 1,
    8 * 1024 * 1024 + 63, 1024 * 1024 - 1,
])
def test_sha256_multi_mb_boundary_sizes_match_hashlib(size):
    data = os.urandom(size)
    assert aether_core.hash_bytes(data) == hashlib.sha256(data).hexdigest()


def test_hash_file_non_ascii_path(tmp_path):
    p = tmp_path / "модель_ü.bin"
    p.write_bytes(b"some content, non-ascii filename")
    assert aether_core.hash_file(str(p)) == hashlib.sha256(p.read_bytes()).hexdigest()


def test_scalar_and_currently_selected_backend_agree_on_random_data():
    """Whatever backend auto-detection picked on this machine, its output must match the
    scalar reference on the same bytes -- the direct backend-equality check the plan calls
    for, phrased so it's meaningful even on a machine (like the CI/dev boxes used to write
    this) where auto-detection can only ever pick scalar (in which case this trivially
    passes, matching itself) and still a real check anywhere sha-ni/arm-sha2 is selectable."""
    data = os.urandom(200_000)
    try:
        assert aether_core.set_hash_backend("scalar") is True
        scalar_digest = aether_core.hash_bytes(data)
    finally:
        aether_core.set_hash_backend("auto")
    auto_digest = aether_core.hash_bytes(data)
    assert auto_digest == scalar_digest == hashlib.sha256(data).hexdigest()


def test_av_sha256_backend_env_forces_scalar(tmp_path):
    import subprocess
    import sys

    result = subprocess.run(
        [sys.executable, "-c", "import aether_core; print(aether_core.hash_backend())"],
        capture_output=True, text=True,
        env={**os.environ, "AV_SHA256_BACKEND": "scalar"},
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "scalar"


def test_release_pool_does_not_break_subsequent_hashing(tmp_path):
    p = tmp_path / "f.bin"
    p.write_bytes(b"x" * (2 * 1024 * 1024))
    before = aether_core.hash_file(str(p))
    aether_core.release_pool()
    after = aether_core.hash_file(str(p))
    assert before == after == hashlib.sha256(p.read_bytes()).hexdigest()
    # And the pool-using paths (safetensors/CDC) still work after a release.
    aether_core.chunk_and_hash_file(str(p), min_chunk=256 * 1024, avg_chunk=512 * 1024, max_chunk=1024 * 1024)


def test_set_max_threads_caps_at_sixteen():
    try:
        aether_core.set_max_threads(999)
        assert aether_core.get_max_threads() == 16
    finally:
        aether_core.set_max_threads(0)
