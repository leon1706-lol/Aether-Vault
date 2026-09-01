"""semdiff.py — semantic change summaries over commit trees (v1.2.0).

Pure functions, no I/O: input are two flat trees in the standard commit format
({rel_path: {hash,size,type,layers,chunks}}). The output feeds three consumers:

* `av diff <ref>` (human + --json)
* `.avh` v2's `semantic_summary` section (agent context memory) — including
  `chunks.dedup_efficiency` (v1.2.2): reused / (reused+new), None when no chunks
* the WebUI's expanded-commit view (later milestone)

The whole point is answering an agent's / reviewer's real question — *what actually
moved and by how much* — using the layer-level and chunk-level hashes the hashing core
already produces, instead of a file list.
"""
from __future__ import annotations

Tree = dict


def _layer_map(entry: dict) -> dict[str, str]:
    return {l["name"]: l["hash"] for l in (entry.get("layers") or [])}


def _chunk_hashes(entry: dict) -> set[str]:
    return {c["hash"] for c in (entry.get("chunks") or [])}


def diff_trees(old_tree: Tree | None, new_tree: Tree | None) -> dict:
    """Semantic diff of two flat trees. Never raises on missing sides (None = empty).

    Returns a machine dict with per-file entries plus rollups:
      files.added/removed/changed  [{path, kind}]
      models: per model path → layers_changed/layers_total/pct, largest_moved[]
      chunks: reused/new counts across all chunked files (dedup efficiency realized)
      datasets: changed dataset paths (classification by extension heuristics)
      totals: bytes_before/bytes_after
    """
    old_tree = old_tree or {}
    new_tree = new_tree or {}

    added = sorted(set(new_tree) - set(old_tree))
    removed = sorted(set(old_tree) - set(new_tree))
    changed = sorted(p for p in set(old_tree) & set(new_tree)
                     if old_tree[p].get("hash") != new_tree[p].get("hash"))

    models: list[dict] = []
    chunks_reused = chunks_new = 0
    for path in sorted(set(new_tree) | set(old_tree)):
        entry = new_tree.get(path) or {}
        if not entry.get("layers"):
            continue
        parent_entry = old_tree.get(path) or {}
        pmap, nmap = _layer_map(parent_entry), _layer_map(entry)
        moved = [name for name, h in nmap.items() if pmap.get(name) != h]
        total = len(nmap) or 1
        # Largest movers need sizes; layer sizes live in the new entry's layers list.
        size_by_name = {l["name"]: l.get("size", 0) for l in (entry.get("layers") or [])}
        largest = sorted(
            ({"name": m, "size": size_by_name.get(m, 0)} for m in moved),
            key=lambda d: d["size"], reverse=True,
        )[:5]
        models.append({
            "path": path,
            "layers_changed": len(moved),
            "layers_total": len(nmap),
            "pct": round(len(moved) / total, 4),
            "moved": moved[:20],
            "largest_moved": largest,
        })

    for path in set(new_tree):
        entry = new_tree[path]
        chs = _chunk_hashes(entry)
        if not chs:
            continue
        parent_chs = _chunk_hashes(old_tree.get(path) or {})
        chunks_new += len(chs - parent_chs)
        chunks_reused += len(chs & parent_chs)

    # v1.2.2 dataset-CDC visibility: how much of the new chunk population was reused
    # rather than re-stored. None when no chunked files exist (no signal ≠ zero).
    chunk_total = chunks_reused + chunks_new
    dedup_efficiency = round(chunks_reused / chunk_total, 4) if chunk_total else None

    DATASET_EXTS = {".parquet", ".csv", ".h5", ".hdf5", ".npz", ".npy", ".arrow",
                    ".jsonl", ".tfrecord", ".wav", ".flac"}

    def _is_dataset(path: str, entry: dict) -> bool:
        low = path.lower()
        return any(low.endswith(e) for e in DATASET_EXTS) or "dataset" in low

    datasets = sorted(
        p for p in set(new_tree) | set(old_tree)
        if (_is_dataset(p, new_tree.get(p) or {}) or _is_dataset(p, old_tree.get(p) or {}))
        and (old_tree.get(p) or {}).get("hash") != (new_tree.get(p) or {}).get("hash")
        and p not in added  # brand-new datasets count as added, not "changed"
    )

    def _bytes(tree: Tree) -> int:
        return sum((e or {}).get("size") or 0 for e in tree.values())

    return {
        "files": {
            "added": [{"path": p, "kind": (new_tree.get(p) or {}).get("type")} for p in added],
            "removed": [{"path": p, "kind": (old_tree.get(p) or {}).get("type")} for p in removed],
            "changed": [{"path": p, "kind": (new_tree.get(p) or {}).get("type",
                        (old_tree.get(p) or {}).get("type"))} for p in changed],
        },
        "models": models,
        "chunks": {"reused": chunks_reused, "new": chunks_new,
                   "dedup_efficiency": dedup_efficiency,
                   # v1.2.5: dedup_efficiency stays None when there's no signal (that's
                   # real information — "no chunked files changed" isn't "0% reuse"), but
                   # `status` is ALWAYS one of these two strings, so .avh/agent consumers
                   # get a stable field to branch on without a null-check on the float.
                   "status": "measured" if chunk_total else "no_chunks"},
        "datasets": datasets,
        "totals": {
            "bytes_before": _bytes(old_tree),
            "bytes_after": _bytes(new_tree),
        },
    }


def human_summary(sd: dict) -> str:
    """One-sentence plain-language rendering of diff_trees output."""
    f = sd["files"]
    parts: list[str] = []
    na, nr, nc = len(f["added"]), len(f["removed"]), len(f["changed"])
    if na or nr or nc:
        bits = []
        if na:
            bits.append(f"{na} added")
        if nc:
            bits.append(f"{nc} changed")
        if nr:
            bits.append(f"{nr} removed")
        parts.append("files: " + ", ".join(bits))
    if sd["models"]:
        m = sd["models"][0]
        parts.append(
            f"model {m['path']}: {m['layers_changed']}/{m['layers_total']} layers moved "
            f"({m['pct']:.1%})"
        )
    c = sd["chunks"]
    if c["reused"] or c["new"]:
        total = c["reused"] + c["new"] or 1
        parts.append(f"chunks reused {c['reused']}/{total}")
    if sd["datasets"]:
        parts.append(f"datasets touched: {len(sd['datasets'])}")
    return "; ".join(parts) if parts else "no changes"
