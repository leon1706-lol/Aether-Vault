"""Semantic diff engine tests — pure functions over synthetic trees (v1.2.0)."""
from python.av_cli.semdiff import diff_trees, human_summary


def _entry(h="a" * 64, size=100, type_="artifact", layers=None, chunks=None):
    e = {"hash": h, "size": size, "type": type_, "layers": layers or [], "chunks": chunks or []}
    return e


def test_added_removed_changed_classification():
    old = {"kept.py": _entry("k" * 64), "gone.pt": _entry("g" * 64)}
    new = {"kept.py": _entry("k" * 64), "new.pt": _entry("n" * 64)}
    sd = diff_trees(old, new)
    assert [f["path"] for f in sd["files"]["added"]] == ["new.pt"]
    assert [f["path"] for f in sd["files"]["removed"]] == ["gone.pt"]
    assert sd["files"]["changed"] == []
    assert sd["totals"]["bytes_after"] == 200  # kept(100) + new(100)


def test_layer_movement_counts_and_largest_movers():
    layers_old = [{"name": f"L{i}", "hash": f"{i}" * 64, "size": (i + 1) * 10} for i in range(10)]
    layers_new = [dict(l) for l in layers_old]
    # change L3 and L7; make L7 the biggest mover:
    layers_new[3]["hash"] = "c" * 64
    layers_new[7]["hash"] = "d" * 64
    layers_new[7]["size"] = 9999

    old = {"model.safetensors": _entry(layers=layers_old, size=0)}
    new = {"model.safetensors": _entry("b" * 64, layers=layers_new, size=0)}

    sd = diff_trees(old, new)
    m = sd["models"][0]
    assert m["layers_changed"] == 2
    assert m["layers_total"] == 10
    assert abs(m["pct"] - 0.2) < 1e-9
    assert m["largest_moved"][0] == {"name": "L7", "size": 9999}
    # identical layer hashes are NOT movement:
    same = diff_trees(old, {"model.safetensors": _entry("z" * 64, layers=layers_old)})
    assert same["models"][0]["layers_changed"] == 0


def test_chunk_reuse_ratio_and_dedup_efficiency():
    ch_old = [{"hash": f"{i}" * 64, "size": 1, "offset": i} for i in range(8)]
    ch_new = [{"hash": f"{i}" * 64, "size": 1, "offset": i} for i in range(6)] + [
        {"hash": "f" * 64, "size": 1, "offset": 99},
        {"hash": "e" * 64, "size": 1, "offset": 100},
    ]
    old = {"ckpt.pt": _entry(chunks=ch_old)}
    new = {"ckpt.pt": _entry("9" * 64, chunks=ch_new)}
    sd = diff_trees(old, new)
    # v1.2.2: dedup_efficiency = reused / (reused + new); None when no chunks exist.
    # v1.2.5: `status` is additive and ALWAYS present ("measured"/"no_chunks") — a
    # stable field .avh/agent consumers can branch on without null-checking the float.
    assert sd["chunks"] == {"reused": 6, "new": 2, "dedup_efficiency": 0.75, "status": "measured"}
    empty = diff_trees(None, None)
    assert empty["chunks"]["dedup_efficiency"] is None
    assert empty["chunks"]["status"] == "no_chunks"


def test_dedup_efficiency_math_edges():
    # all-new population → 0.0; single reused-only file → 1.0
    ch_a = [{"hash": "a" * 64, "size": 1, "offset": 0}]
    ch_b = [{"hash": "b" * 64, "size": 1, "offset": 0}]
    all_new = diff_trees({"m.pt": _entry(chunks=ch_b)}, {"m.pt": _entry("x" * 64, chunks=ch_a)})
    assert all_new["chunks"]["dedup_efficiency"] == 0.0
    unchanged = diff_trees({"m.pt": _entry(chunks=ch_a)}, {"m.pt": _entry("y" * 64, chunks=list(ch_a))})
    assert unchanged["chunks"]["dedup_efficiency"] == 1.0


def test_dataset_detection_by_extension_and_name():
    old = {"data/train.parquet": _entry("1" * 64), "readme.md": _entry("2" * 64),
           "weights.ckpt": _entry("3" * 64, chunks=[{"hash": "4" * 64, "size": 1, "offset": 0}])}
    new = {"data/train.parquet": _entry("5" * 64), "readme.md": _entry("2" * 64),
           "weights.ckpt": _entry("6" * 64, chunks=[{"hash": "4" * 64, "size": 1, "offset": 0}]),
           "my_dataset/file.h5": _entry("7" * 64)}
    sd = diff_trees(old, new)
    assert "data/train.parquet" in sd["datasets"]
    assert "my_dataset/file.h5" not in sd["datasets"]  # brand-new → counted in added only
    assert "readme.md" not in sd["datasets"]
    assert all(d != "weights.ckpt" for d in sd["datasets"])


def test_empty_sides_and_human_summary():
    sd = diff_trees(None, None)
    assert human_summary(sd) == "no changes"

    old = {"m.safetensors": _entry(
        layers=[{"name": "L", "hash": "1" * 64, "size": 5}], size=5),
        "x.parquet": _entry("3" * 64)}
    new = {"m.safetensors": _entry("2" * 64,
        layers=[{"name": "L", "hash": "9" * 64, "size": 5}], size=5),
        "x.parquet": _entry("4" * 64)}
    text = human_summary(diff_trees(old, new))
    assert "1/1 layers moved" in text and "datasets touched: 1" in text
