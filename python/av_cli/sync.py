"""Remote-sync primitives behind `av clone` and `av pull`.

Deliberately a separate module (not more main.py): everything here is pure logic over an
injected `VaultClient`, so the CLI layer stays thin and tests can drive clone/pull against
fakes without any HTTP. Latency notes baked into the design:

- Project + ref discovery are single round trips (`/api/projects`, `/api/refs?project_id=`).
- History comes down as paginated `/api/commits?include_layers=true` batches (500/page) —
  one request stream for the whole project instead of one call per commit, so clones are
  fully self-sufficient offline afterwards.
- Object pre-fetch batch-checks every referenced hash in ONE `batch-objects` call, then
  downloads only what's actually missing, in parallel (network-bound work → small thread
  pool, mirroring upload_commit_objects' 8-worker pattern).
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .exceptions import ValidationError

DEFAULT_BRANCH_CANDIDATES = ("main", "master")
_FETCH_WORKERS = 8


def resolve_project(client, name_or_id: str) -> dict:
    """Finds a registry project by exact id, exact name, or unique name prefix.

    Raises ValidationError listing the candidates when ambiguous, or every known project
    when nothing matches — so a typo'd clone target tells you what IS available.
    """
    projects = client.list_projects()
    if not projects:
        raise ValidationError(f"No projects found on {client.server_url} — push something first.")

    for p in projects:
        if p.get("project_id") == name_or_id:
            return p

    def _label(p: dict) -> str:
        return f"  {p.get('project_name', '?')}  ({p.get('project_id', '?')[:8]}…)"

    exact = [p for p in projects if p.get("project_name") == name_or_id]
    if len(exact) == 1:
        return exact[0]

    prefix = [p for p in projects if str(p.get("project_name", "")).startswith(name_or_id)]
    candidates = exact or prefix
    if len(candidates) > 1:
        shown = "\n".join(_label(p) for p in candidates[:10])
        more = len(candidates) - 10
        raise ValidationError(
            f"'{name_or_id}' is ambiguous — {len(candidates)} projects match:\n{shown}"
            + (f"\n  … and {more} more" if more > 0 else "")
        )
    if len(candidates) == 1:
        return candidates[0]

    available = "\n".join(_label(p) for p in projects[:10])
    raise ValidationError(
        f"No project '{name_or_id}' on {client.server_url}.\nAvailable projects:\n{available}"
    )


def normalize_commit_row(row: dict) -> dict:
    """Server commit row -> local `.av/commits/<hash>.json` shape.

    The server persists `parent_hash` (plus `extra_parents` for merge commits); local commits
    store a full `parents` list — this is where the two shapes meet.

    v1.2.2: `signature` and `env_snapshot_id` ride through verbatim — dropping either
    would make cloned repos unable to verify commit signatures or resolve replay
    snapshots (both were silently lost in the first manual-debug pass of this feature).
    """
    parents = list(row.get("parents") or [])
    if not parents and row.get("parent_hash"):
        parents = [row["parent_hash"]]
    normalized = {
        "hash": row["hash"],
        "parents": parents,
        "author": row.get("author") or "anonymous",
        "timestamp": row.get("timestamp"),
        "message": row.get("message") or "",
        "tree": row.get("tree") or {},
        "tags": row.get("tags") or [],
        "metrics": row.get("metrics") or {},
        "project_id": row.get("project_id"),
        "project_name": row.get("project_name"),
    }
    if row.get("signature"):
        normalized["signature"] = row["signature"]
    if row.get("env_snapshot_id"):
        normalized["env_snapshot_id"] = row["env_snapshot_id"]
    return normalized


def fetch_project_commits(client, project_id: str) -> list[dict]:
    """Every commit of a project (metadata + resolved trees), newest first."""
    commits: list[dict] = []
    offset = 0
    while True:
        page = client.list_commits(project_id, limit=500, offset=offset, include_layers=True)
        if not page:
            break
        rows = page.get("commits", [])
        commits.extend(normalize_commit_row(r) for r in rows)
        next_offset = page.get("next_offset")
        if next_offset is None or not rows:
            break
        offset = next_offset
    return commits


def pick_default_branch(project_refs: dict[str, str], project_id: str) -> str | None:
    """Chooses the branch a fresh clone should start on.

    `project_refs` maps remote ref names ("<project_id>/<branch>") to hashes; preference
    order is main, master, then alphabetical first — deterministic when a project has no
    conventional default.
    """
    branches = sorted(
        name[len(project_id) + 1:] for name in project_refs
        if name.startswith(f"{project_id}/") and name[len(project_id) + 1:]
    )
    for candidate in DEFAULT_BRANCH_CANDIDATES:
        if candidate in branches:
            return candidate
    return branches[0] if branches else None


def collect_tree_hashes(tree: dict) -> list[str]:
    """Every content hash a flat tree references: whole-file objects plus layer/chunk shards."""
    hashes: list[str] = []
    for info in tree.values():
        if not isinstance(info, dict):
            continue
        parts = list(info.get("layers") or []) + list(info.get("chunks") or [])
        if parts:
            hashes.extend(part["hash"] for part in parts)
        else:
            hashes.append(info["hash"])
    return hashes


def ensure_objects_local(repo_root: Path, client, tree: dict) -> int:
    """Makes every object the tree references present under `.av/objects/`.

    One batch-check round trip for the whole tree, then parallel downloads of only the
    genuinely missing pieces. Returns how many were downloaded. Raises ValidationError if
    neither this machine nor the server can supply a referenced hash — a partial working
    copy would be worse than a failed one.
    """
    needed = collect_tree_hashes(tree)
    missing = [
        h for h in dict.fromkeys(needed)
        if not (repo_root / ".av" / "objects" / h[:2] / h[2:]).exists()
    ]
    if not missing:
        return 0

    found = client.batch_check_objects(missing)
    downloadable = [h for h in missing if h in found]
    downloaded = 0
    if downloadable:
        with ThreadPoolExecutor(max_workers=min(_FETCH_WORKERS, len(downloadable))) as pool:
            futures = {
                h: pool.submit(client.download_object, h,
                               repo_root / ".av" / "objects" / h[:2] / h[2:])
                for h in downloadable
            }
            for h, future in futures.items():
                if future.result():
                    downloaded += 1

    unrecoverable = sorted(set(missing) - {h for h in downloadable})
    # Anything batch-check reported as on-server but whose download failed also counts.
    unrecoverable += sorted(h for h in downloadable
                            if not (repo_root / ".av" / "objects" / h[:2] / h[2:]).exists())
    if unrecoverable:
        shown = ", ".join(h[:12] + "…" for h in unrecoverable[:5])
        raise ValidationError(
            f"{len(unrecoverable)} object(s) are unavailable locally and on the server "
            f"(e.g. {shown}) — refusing to materialize a partial tree."
        )
    return downloaded


def is_ancestor(load_commit, ancestor_hash: str, descendant_hash: str) -> bool:
    """True when `ancestor_hash` is reachable from `descendant_hash` via parent links.

    BFS over parents (merge-aware from day one). Both hashes must be resolvable by the
    caller-supplied loader; equality counts as ancestry.
    """
    if ancestor_hash == descendant_hash:
        return True
    visited: set[str] = set()
    queue = [descendant_hash]
    while queue:
        current = queue.pop()
        if current in visited or current is None:
            continue
        visited.add(current)
        commit = load_commit(current)
        if commit is None:
            continue
        for parent in commit.get("parents") or []:
            if parent == ancestor_hash:
                return True
            queue.append(parent)
    return False


def write_fetched_commit(repo_root: Path, commit_data: dict) -> None:
    from .fsutil import atomic_write_json

    atomic_write_json(repo_root / ".av" / "commits" / f"{commit_data['hash']}.json", commit_data)


def load_local_commit(repo_root: Path, commit_hash: str) -> dict | None:
    try:
        with open(repo_root / ".av" / "commits" / f"{commit_hash}.json", "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return None
