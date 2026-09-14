"""`av_server.gc_mark.mark_alive` -- the GC mark phase over compact tuples (V1.6.3). The
sweep's alive/dead decisions depend entirely on these sets, so the graph cases the old
ORM walk handled are pinned here without a database: nesting, layers + chunks, a hash
shared across tenants, a dangling child, a cycle, and per-tenant separation."""
from python.av_server.gc_mark import TreeRow, mark_alive


def _row(tenant, tree, child=None, obj=None, layers=None, chunks=None):
    return TreeRow(tenant, tree, child, obj, layers, chunks)


def test_nested_trees_layers_and_chunks_are_all_marked():
    rows = [
        _row("t1", "root", child="sub"),
        _row("t1", "root", obj="o-root"),
        _row("t1", "sub", obj="o-sub", layers=[{"hash": "L1"}, {"hash": "L2"}, {"nohash": 1}],
             chunks=[{"hash": "C1"}, "garbage"]),
    ]
    alive, visited, all_trees = mark_alive(rows, [("t1", "root")])
    assert alive == {"t1": {"o-root", "o-sub", "L1", "L2", "C1"}}
    assert visited == {"t1": {"root", "sub"}}
    assert all_trees == {"root", "sub"}


def test_unreferenced_tree_is_not_visited_but_still_counted():
    rows = [_row("t1", "root", obj="a"), _row("t1", "orphan", obj="b")]
    alive, visited, all_trees = mark_alive(rows, [("t1", "root")])
    assert alive["t1"] == {"a"}
    assert visited["t1"] == {"root"}
    assert all_trees == {"root", "orphan"}  # the sweep deletes all_trees - visited


def test_dangling_child_and_cycle_terminate():
    rows = [
        _row("t1", "a", child="b"),
        _row("t1", "b", child="a"),          # cycle back to a
        _row("t1", "b", child="missing"),    # dangling child: no rows
        _row("t1", "b", obj="x"),
    ]
    alive, visited, _ = mark_alive(rows, [("t1", "a")])
    assert alive["t1"] == {"x"}
    assert visited["t1"] == {"a", "b", "missing"}


def test_tenants_are_marked_separately_and_a_root_with_no_trees_is_harmless():
    rows = [
        _row("t1", "r1", obj="shared"),
        _row("t2", "r2", obj="shared"),
        _row("t2", "r2", obj="only-t2"),
    ]
    alive, visited, _ = mark_alive(rows, [("t1", "r1"), ("t2", "r2"), ("t3", "nothing"), ("t1", None)])
    assert alive == {"t1": {"shared"}, "t2": {"shared", "only-t2"}, "t3": set()}
    assert visited == {"t1": {"r1"}, "t2": {"r2"}, "t3": {"nothing"}}
    # A root from tenant t1 never reaches t2's trees, even with the same hash.
    rows2 = [_row("t1", "same", obj="a"), _row("t2", "same", obj="b")]
    alive2, _, _ = mark_alive(rows2, [("t1", "same")])
    assert alive2 == {"t1": {"a"}}


def test_accepts_single_pass_iterators():
    rows = iter([_row("t1", "root", obj="o")])
    roots = iter([("t1", "root")])
    alive, visited, all_trees = mark_alive(rows, roots)
    assert alive == {"t1": {"o"}} and visited == {"t1": {"root"}} and all_trees == {"root"}


def test_matches_the_pre_v163_orm_walk_on_a_random_graph():
    """Differential check against a faithful port of the old `_collect_alive_in_memory`
    over objects with the same attribute names (what the ORM instances exposed)."""
    import random
    from types import SimpleNamespace

    rng = random.Random(42)
    trees = [f"t{i}" for i in range(40)]
    rows = []
    for tree in trees:
        for _ in range(rng.randint(1, 4)):
            kind = rng.random()
            if kind < 0.4:
                rows.append(_row("t", tree, child=rng.choice(trees)))
            elif kind < 0.8:
                rows.append(_row("t", tree, obj=f"o{rng.randint(0, 200)}",
                                 layers=[{"hash": f"l{rng.randint(0, 50)}"}] if rng.random() < 0.5 else None))
            else:
                rows.append(_row("t", tree, obj=None, chunks=[{"hash": f"c{rng.randint(0, 50)}"}]))
    roots = [("t", rng.choice(trees)) for _ in range(6)]

    def legacy(root_hash, tree_map, visited, alive):
        stack = [root_hash]
        while stack:
            th = stack.pop()
            if not th or th in visited:
                continue
            visited.add(th)
            for entry in tree_map.get(th, []):
                if entry.child_tree_hash:
                    stack.append(entry.child_tree_hash)
                if entry.object_hash:
                    alive.add(entry.object_hash)
                if entry.layers:
                    for layer in entry.layers:
                        if isinstance(layer, dict) and "hash" in layer:
                            alive.add(layer["hash"])
                for chunk in getattr(entry, "chunks", None) or []:
                    if isinstance(chunk, dict) and "hash" in chunk:
                        alive.add(chunk["hash"])

    tree_map = {}
    for r in rows:
        tree_map.setdefault(r.tree_hash, []).append(SimpleNamespace(**r._asdict()))
    exp_alive, exp_visited = set(), set()
    for _, root in roots:
        legacy(root, tree_map, exp_visited, exp_alive)

    alive, visited, _ = mark_alive(rows, roots)
    assert alive["t"] == exp_alive and visited["t"] == exp_visited
