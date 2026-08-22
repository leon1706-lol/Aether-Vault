"""Pure merge algorithms behind `av merge`: nearest-common-ancestor resolution and
three-way tree merging.

Kept free of I/O, click, and client code so the merge semantics are unit-testable in
isolation (tests/test_merge.py) and reusable if a future UI wants to preview a merge.
Conflict policy is decided by the caller (`av merge` aborts unless --ours/--theirs was
given); these functions only compute.
"""

from __future__ import annotations

from collections import deque


def find_merge_base(load_commit, ours_hash: str, theirs_hash: str) -> str | None:
    """Nearest common ancestor of two commits, or None when the histories are unrelated.

    Two-phase walk: collect ALL of ours' ancestors into a set (stack DFS), then BFS theirs'
    ancestors in generation order — the first node that appears in ours' ancestor set is the
    common ancestor closest to theirs, which is the conventional merge base for this purpose.
    Merge-aware from day one: every parent edge is followed, not just parents[0].
    """
    ours_ancestors: set[str] = set()
    stack = [ours_hash]
    while stack:
        h = stack.pop()
        if h in ours_ancestors:
            continue
        ours_ancestors.add(h)
        commit = load_commit(h)
        if commit:
            stack.extend(commit.get("parents") or [])

    queue: deque[str] = deque([theirs_hash])
    visited: set[str] = set()
    while queue:
        h = queue.popleft()
        if h in visited:
            continue
        visited.add(h)
        if h in ours_ancestors:
            return h
        commit = load_commit(h)
        if commit:
            queue.extend(commit.get("parents") or [])
    return None


def three_way_tree_merge(
    base_tree: dict, ours_tree: dict, theirs_tree: dict
) -> tuple[dict, list[str]]:
    """Per-path three-way merge over flat trees ({rel_path: entry-dict}).

    Semantics per path (entry absence = "deleted on this side"):
      - ours == theirs                → take that (both agree, incl. both deleted)
      - base == ours                  → theirs changed it → take theirs (add/change/delete)
      - base == theirs                → ours changed it  → keep ours
      - otherwise                     → both changed differently → CONFLICT

    Returns (merged_tree, sorted_conflict_paths). Entries are compared by full dict equality,
    so a layer/chunk re-split that leaves content identical still counts as unchanged.
    """
    merged: dict = {}
    conflicts: list[str] = []
    for path in set(base_tree) | set(ours_tree) | set(theirs_tree):
        b = base_tree.get(path)
        o = ours_tree.get(path)
        t = theirs_tree.get(path)
        if o == t:
            winner = o
        elif b == o:
            winner = t
        elif b == t:
            winner = o
        else:
            conflicts.append(path)
            continue
        if winner is not None:
            merged[path] = winner
    return merged, sorted(conflicts)


def tree_is_flat(tree: dict) -> bool:
    """True for the unified flat format ({rel_path: {...}}); False for the legacy
    {"code": {...}, "artifacts": {...}} shape, which merge doesn't support."""
    return not ("code" in tree or "artifacts" in tree)


def summarize_changes(before: dict, after: dict) -> tuple[int, int, int]:
    """(added, removed, changed) counts between two trees — for the merge result line."""
    added = len(set(after) - set(before))
    removed = len(set(before) - set(after))
    changed = sum(1 for p in set(before) & set(after) if before[p] != after[p])
    return added, removed, changed
