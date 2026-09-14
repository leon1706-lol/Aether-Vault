"""The GC mark phase as a pure function over compact rows (V1.6.3).

`run_garbage_collection` used to materialize every `DBTree`/`DBCommit` ORM instance to
walk the reachability graph -- on a large registry that is the single biggest RSS spike
the server has, and none of the ORM machinery (identity map, per-instance state, unused
columns) was needed for it. This module takes plain tuples streamed from the database
and produces exactly the same alive/visited sets the ORM walk did, so the sweep that
follows makes identical decisions. `tests/test_gc_mark.py` pins the algorithm without a
database.
"""
from __future__ import annotations

from typing import Dict, Iterable, NamedTuple, Optional, Set, Tuple


class TreeRow(NamedTuple):
    tenant_id: str
    tree_hash: str
    child_tree_hash: Optional[str]
    object_hash: Optional[str]
    layers: Optional[list]
    chunks: Optional[list]


def _mark_from_root(root_hash: Optional[str], tree_map: Dict[str, list], visited: Set[str],
                    alive: Set[str]) -> None:
    """Iteratively mark every object/layer/chunk hash reachable from a root tree as alive.
    Same traversal as the pre-V1.6.3 `_collect_alive_in_memory`, over TreeRow tuples."""
    stack = [root_hash]
    while stack:
        th = stack.pop()
        if not th or th in visited:
            continue
        visited.add(th)
        for entry in tree_map.get(th, ()):
            if entry.child_tree_hash:
                stack.append(entry.child_tree_hash)
            if entry.object_hash:
                alive.add(entry.object_hash)
            for layer in entry.layers or ():
                if isinstance(layer, dict) and "hash" in layer:
                    alive.add(layer["hash"])
            # CDC chunk shards live as their own objects, like safetensors layer shards --
            # unmarked here, GC would reap the pieces a chunked checkpoint needs to reassemble.
            for chunk in entry.chunks or ():
                if isinstance(chunk, dict) and "hash" in chunk:
                    alive.add(chunk["hash"])


def mark_alive(
    tree_rows: Iterable[TreeRow], roots: Iterable[Tuple[str, Optional[str]]],
) -> Tuple[Dict[str, Set[str]], Dict[str, Set[str]], Set[str]]:
    """Returns `(alive_by_tenant, visited_by_tenant, all_tree_hashes)`.

    PER-TENANT trees/marks always -- one shape serves both CAS isolation modes; the flat
    union the caller takes across tenants is identical to a flat computation, since each
    tenant's commits only ever reference trees that tenant fully wrote. `roots` are
    `(tenant_id, root_tree_hash)` pairs, one per commit. Both iterables may be
    single-pass streams; `tree_rows` is consumed fully before any root is walked
    (the graph has to exist before it can be traversed)."""
    trees_by_tenant: Dict[str, Dict[str, list]] = {}
    for row in tree_rows:
        trees_by_tenant.setdefault(row.tenant_id, {}).setdefault(row.tree_hash, []).append(row)

    alive_by_tenant: Dict[str, Set[str]] = {}
    visited_by_tenant: Dict[str, Set[str]] = {}
    for tenant_id, root_hash in roots:
        t_alive = alive_by_tenant.setdefault(tenant_id, set())
        t_visited = visited_by_tenant.setdefault(tenant_id, set())
        _mark_from_root(root_hash, trees_by_tenant.get(tenant_id, {}), t_visited, t_alive)

    all_tree_hashes = {th for tenant_map in trees_by_tenant.values() for th in tenant_map}
    return alive_by_tenant, visited_by_tenant, all_tree_hashes
