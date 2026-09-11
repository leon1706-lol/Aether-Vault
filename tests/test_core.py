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
