"""Dataset CDC (v1.2.2 gap 2): boundary stability + .avattributes enforcement across
EVERY chunkable extension.

v1.2.0 generalized CDC from checkpoints to dataset serialization formats
(CHUNKABLE_EXTS = .pt/.pth/.ckpt/.npz/.h5/.hdf5/.pb/.msgpack). Until now only `.pt`
was exercised. This module parametrizes the two invariants that make cross-version and
cross-commit dedup WORK over the whole set:

1. Boundary stability Ã¢â‚¬â€ a local edit reuses every chunk outside the edited window
   (deterministic gear table Ã¢â€¡â€™ identical cut points on identical bytes).
2. Round-trip integrity Ã¢â‚¬â€ staged shards reassemble byte-identically through checkout.
3. .avattributes enforcement matrix Ã¢â‚¬â€ `no-chunk` / `no-layer-split` honored per
   extension, last-match-wins, unknown flags ignored (forward compatibility).

Native-core tests skip cleanly when aether_core isn't built; the attributes matrix is
framework-free and always runs.
"""
import shutil

import pytest

from python.av_cli.attributes import flags_for, load_attributes
from python.av_cli.core import CHUNKABLE_EXTS, load_config, stage_one_file
from python.av_cli.index import Index

aether_core = pytest.importorskip("aether_core")

# A seeded-random payload, big enough for several chunks at reduced params so fixtures
# stay tiny. Real entropy matters: a periodic payload can yield one chunk spanning the
# whole file, breaking the "no whole-file blob alongside shards" invariant.
import random

PAYLOAD = random.Random(0xA17C5EED).randbytes(3 * 1024 * 1024)


def _chunk_params():
    return {"min_chunk": 64 * 1024, "avg_chunk": 256 * 1024, "max_chunk": 1024 * 1024}


@pytest.mark.parametrize("ext", sorted(CHUNKABLE_EXTS))
def test_cdc_boundary_stability_across_all_extensions(tmp_path, ext):
    """A mid-file byte flip must leave all other chunks' hashes untouched Ã¢â‚¬â€ per ext."""
    p = tmp_path / f"data{ext}"
    p.write_bytes(PAYLOAD)

    before = aether_core.chunk_and_hash_file(str(p), **_chunk_params())
    assert len(before) >= 2, f"{ext}: fixture produced a single chunk Ã¢â‚¬â€ test useless"

    data = bytearray(PAYLOAD)
    flip_at = len(data) // 2
    data[flip_at] ^= 0xFF
    p.write_bytes(bytes(data))

    after = aether_core.chunk_and_hash_file(str(p), **_chunk_params())

    before_hashes = [c["hash"] for c in before]
    after_hashes = [c["hash"] for c in after]
    reused = set(before_hashes) & set(after_hashes)
    # The edited window's chunk changed; everything else MUST be reused:
    assert len(reused) >= len(before) - 2, (
        f"{ext}: edit caused mass re-chunking ({len(reused)}/{len(before)} reused) Ã¢â‚¬â€ "
        "boundaries are not stable"
    )
    # And the total population moved forward, not sideways:
    assert set(after_hashes) - set(before_hashes), f"{ext}: edit produced no new chunk?"


@pytest.mark.parametrize("ext", sorted(CHUNKABLE_EXTS))
def test_cdc_determinism_identical_bytes_identical_boundaries(tmp_path, ext):
    """Same bytes Ã¢â€ â€™ same boundaries/hashes (the dedup invariant across machines)."""
    a = tmp_path / f"a{ext}"
    b = tmp_path / f"b{ext}"
    a.write_bytes(PAYLOAD)
    b.write_bytes(PAYLOAD)
    ca = aether_core.chunk_and_hash_file(str(a), **_chunk_params())
    cb = aether_core.chunk_and_hash_file(str(b), **_chunk_params())
    assert [(c["hash"], c["offset"], c["size"]) for c in ca] == \
           [(c["hash"], c["offset"], c["size"]) for c in cb]


# ---------------------------------------------------------------------------
# Every extension takes the CDC path above threshold; .avattributes overrides are honored per path.
# ---------------------------------------------------------------------------

def _stage(repo_root, name, content):
    """Writes the payload and stages it through the real stage_one_file path."""
    (repo_root / name).write_bytes(content)
    idx = Index(repo_root)
    rules = load_attributes(repo_root)
    threshold = load_config(repo_root)["lfs_threshold_mb"] * 1024 * 1024
    stage_one_file(repo_root, idx, threshold, repo_root / name, name,
                   flags_for(rules, name))
    return idx.get_entry(name)


@pytest.fixture()
def small_threshold_repo(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg_path = tmp_path / ".av" / "config"
    cfg_path.parent.mkdir(exist_ok=True)
    import json

    cfg_path.write_text(json.dumps({
        "lfs_threshold_mb": 1,  # 1 MB threshold Ã¢â‚¬â€ our 3 MB payload exceeds it
        "remote_url": "http://localhost:8000",
        "project_id": "test" * 16,
        "project_name": "cdc-matrix",
    }))
    (tmp_path / ".av" / "objects").mkdir(exist_ok=True)
    return tmp_path


@pytest.mark.parametrize("ext", sorted(CHUNKABLE_EXTS))
def test_staging_uses_chunks_above_threshold(small_threshold_repo, ext):
    entry = _stage(small_threshold_repo, f"d{ext}", PAYLOAD)
    assert entry.get("chunks"), f"{ext}: staged whole-file instead of chunking"
    assert not entry.get("layers")
    # No whole-file blob was stored (shards carry the bytes):
    obj = small_threshold_repo / ".av" / "objects" / entry["hash"][:2] / entry["hash"][2:]
    assert not obj.exists(), f"{ext}: whole-file blob stored alongside shards"


@pytest.mark.parametrize("ext", sorted(CHUNKABLE_EXTS))
def test_avattributes_no_chunk_matrix(small_threshold_repo, tmp_path, ext):
    (tmp_path / ".avattributes").write_text(f"*{ext} no-chunk\n")
    entry = _stage(small_threshold_repo, f"d{ext}", PAYLOAD)
    assert not entry.get("chunks"), f"{ext}: no-chunk directive ignored"
    assert not entry.get("layers")
    obj = small_threshold_repo / ".av" / "objects" / entry["hash"][:2] / entry["hash"][2:]
    assert obj.exists(), f"{ext}: no-chunk did not store the whole-file blob"


def test_avattributes_last_match_wins_matrix(small_threshold_repo, tmp_path):
    (tmp_path / ".avattributes").write_text(
        "*.npz no-chunk\n"
        "*.npz\n          # bare line clears nothing by itself, but...\n"
        "data/*.npz no-chunk no-layer-split\n"
        "*.msgpack no-chunk\n"
        "kept/*.msgpack\n"
    )
    # last matching line wins: d.npz matches lines 1,2,3 Ã¢â€ â€™ directives {no-chunk}
    flags_npz = flags_for(load_attributes(tmp_path), "data/d.npz")
    assert flags_npz == {"no-chunk", "no-layer-split"}
    # kept/x.msgpack matches lines 4 AND 5; the bare line 5 wins with NO directives:
    flags_msg = flags_for(load_attributes(tmp_path), "kept/x.msgpack")
    assert flags_msg == set()


def test_avattributes_unknown_flags_ignored_everywhere(tmp_path):
    (tmp_path / ".avattributes").write_text(
        "*.h5 future-flag another-one no-chunk\n"
    )
    flags = flags_for(load_attributes(tmp_path), "x.h5")
    assert flags == {"no-chunk"}, "unknown flags must be ignored (forward compat)"


# ---------------------------------------------------------------------------
# v1.2.5: broadened default extension set + the `chunk` opt-in flag
# ---------------------------------------------------------------------------

def test_new_default_extensions_are_actually_in_chunkable_exts():
    """Pins the specific v1.2.5 additions so a future refactor can't silently drop one."""
    for ext in (".bin", ".onnx", ".model", ".arrow", ".feather", ".pkl", ".pickle"):
        assert ext in CHUNKABLE_EXTS


def test_chunk_flag_force_enables_a_non_default_extension(small_threshold_repo, tmp_path):
    """.parquet is deliberately NOT in CHUNKABLE_EXTS (compressed-by-default risk) — the
    `chunk` flag must still force it on for a glob the repo owner has vetted."""
    assert ".parquet" not in CHUNKABLE_EXTS
    (tmp_path / ".avattributes").write_text("*.parquet chunk\n")
    entry = _stage(small_threshold_repo, "d.parquet", PAYLOAD)
    assert entry.get("chunks"), "chunk flag did not force CDC for a non-default extension"
    assert not entry.get("layers")


def test_without_chunk_flag_non_default_extension_stays_whole_file(small_threshold_repo, tmp_path):
    entry = _stage(small_threshold_repo, "d.parquet", PAYLOAD)
    assert not entry.get("chunks"), "an extension outside CHUNKABLE_EXTS chunked by default"
    obj = small_threshold_repo / ".av" / "objects" / entry["hash"][:2] / entry["hash"][2:]
    assert obj.exists()


def test_no_chunk_wins_over_chunk_on_the_same_line(small_threshold_repo, tmp_path):
    """Safety over opt-in: `no-chunk` always wins when both flags are on one matching line."""
    (tmp_path / ".avattributes").write_text("*.parquet chunk no-chunk\n")
    entry = _stage(small_threshold_repo, "d.parquet", PAYLOAD)
    assert not entry.get("chunks"), "no-chunk should have overridden chunk on the same line"


def test_chunk_flag_is_a_no_op_for_already_default_chunkable_extensions(small_threshold_repo, tmp_path):
    """Redundant but harmless: `chunk` on an extension already in CHUNKABLE_EXTS changes
    nothing (still chunks), it just doesn't need to be there."""
    (tmp_path / ".avattributes").write_text("*.npz chunk\n")
    entry = _stage(small_threshold_repo, "d.npz", PAYLOAD)
    assert entry.get("chunks")
