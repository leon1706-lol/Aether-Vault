"""Remote-sync primitives behind `av clone` and `av pull`. Deliberately a separate module:
everything here is pure logic over an injected `VaultClient`, so the CLI layer stays thin
and tests can drive clone/pull against fakes without any HTTP. History comes down as
paginated batches (one stream for the whole project); object pre-fetch batch-checks every
hash in one call, then downloads only what's missing, in parallel.
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .exceptions import NetworkError, ValidationError

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


def iter_project_commits(client, project_id: str, page_size: int = 500):
    """Every commit of a project (metadata + resolved trees), newest first, yielded one at
    a time as pages arrive -- `av clone` writes each to disk immediately instead of holding
    the whole history (every tree included) in one list (V1.6.3).

    V1.6.0 (WS4.8): page N+1's request is submitted before page N's rows are normalized, so
    the network round trip for the next page overlaps with this page's (pure-CPU)
    `normalize_commit_row` work instead of happening strictly after it -- at most one page
    in flight ahead of the one being processed (two total), since the server is
    single-worker and a deeper pipeline would just queue without helping.
    """
    with ThreadPoolExecutor(max_workers=1) as pool:
        next_future = pool.submit(client.list_commits, project_id, limit=page_size, offset=0,
                                   include_layers=True)
        while next_future is not None:
            page = next_future.result()
            next_future = None
            if not page:
                break
            rows = page.get("commits", [])
            next_offset = page.get("next_offset")
            if next_offset is not None and rows:
                next_future = pool.submit(client.list_commits, project_id, limit=page_size,
                                           offset=next_offset, include_layers=True)
            for row in rows:
                yield normalize_commit_row(row)
            if next_offset is None or not rows:
                break


def fetch_project_commits(client, project_id: str) -> list[dict]:
    """`iter_project_commits` materialized -- for callers that genuinely need the list."""
    return list(iter_project_commits(client, project_id))


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


def resolve_fetch_targets(
    tree: dict,
    rel_paths: list[str] | None = None,
    *,
    fetch_all: bool = False,
    layer_names: list[str] | None = None,
) -> dict:
    """Pure resolution logic behind `av fetch`/`Repo.fetch()`: which entries of a flat HEAD
    tree the caller actually wants, applying `--layer`'s narrowing when given. Shared by
    the CLI (cmd_sync.py) and the SDK (av_sdk/repo.py) so path validation/layer-filtering
    logic exists exactly once. Raises ValidationError for bad input (an untracked path, an
    unknown layer name, or `--layer` combined with more than one path) rather than
    returning a sentinel -- both callers already have a ValidationError -> clean failure
    path (`fail()`/`SDKError`)."""
    if not fetch_all and not rel_paths:
        raise ValidationError("Specify one or more paths, or fetch_all=True.")
    if layer_names and (fetch_all or (rel_paths and len(rel_paths) != 1)):
        raise ValidationError(
            "layer_names requires exactly one path (not fetch_all, not multiple paths)."
        )

    if fetch_all:
        selected = dict(tree)
    else:
        selected = {}
        not_tracked = []
        for rel in rel_paths:
            if rel not in tree:
                not_tracked.append(rel)
                continue
            selected[rel] = tree[rel]
        if not_tracked:
            raise ValidationError(f"Not tracked at HEAD: {', '.join(not_tracked)}")

    if layer_names:
        (rel, info), = selected.items()
        available = {layer["name"] for layer in (info.get("layers") or [])}
        wanted = set(layer_names)
        unknown = wanted - available
        if unknown:
            raise ValidationError(
                f"Unknown layer name(s) for {rel}: {', '.join(sorted(unknown))}. "
                f"Available: {', '.join(sorted(available)) or '(none — not layer-split)'}"
            )
        restricted = dict(info)
        restricted["layers"] = [l for l in info.get("layers") or [] if l["name"] in wanted]
        selected = {rel: restricted}

    return selected


def download_selected_objects(repo_root: Path, client, selected: dict, pool_size: int = 8) -> dict:
    """Downloads whatever `resolve_fetch_targets()` selected into `.av/objects`, without
    touching the working tree. One batch-check round trip for every part across every
    selected path, then parallel downloads of only what's genuinely missing -- the same
    shape as `ensure_objects_local()`, but returning per-(path, object) detail
    (`{"fetched": [{"path","hash","bytes"}], "already_local": n, "bytes": n}`) instead of
    just a count, since `av fetch`'s whole point is reporting exactly what moved. Raises
    ValidationError if something is missing both locally and on the server, or if a
    download that batch-check promised was available fails anyway."""
    wanted_parts: list[tuple[str, str, int]] = []  # (rel_path, hash, size)
    for rel, info in selected.items():
        parts = list(info.get("layers") or []) + list(info.get("chunks") or [])
        if parts:
            for part in parts:
                wanted_parts.append((rel, part["hash"], part.get("size", 0)))
        else:
            wanted_parts.append((rel, info["hash"], info.get("size", 0)))

    already_local = 0
    to_download: list[tuple[str, str, int]] = []
    for rel, h, size in wanted_parts:
        obj_path = repo_root / ".av" / "objects" / h[:2] / h[2:]
        if obj_path.exists():
            already_local += 1
        else:
            to_download.append((rel, h, size))

    fetched: list[dict] = []
    total_bytes = 0
    if to_download:
        if not client.server_available():
            # NetworkError, not ValidationError: this maps to unreachable_queued (exit 13,
            # "safe, retry later") in both the CLI (fail()) and the SDK (error_from_code()),
            # distinct from a genuine validation failure (exit 15/20) -- nothing is actually
            # wrong with the request, the registry is just not reachable right now.
            raise NetworkError(
                f"Registry unreachable at {client.server_url} — nothing to fetch from."
            )

        missing_hashes = list(dict.fromkeys(h for _, h, _ in to_download))
        found = client.batch_check_objects(missing_hashes)
        unrecoverable = sorted(set(missing_hashes) - found)
        if unrecoverable:
            raise ValidationError(
                f"{len(unrecoverable)} object(s) neither local nor on the server "
                f"(e.g. {unrecoverable[0][:12]}…) — repo state may be corrupt; try `av doctor`."
            )

        with ThreadPoolExecutor(max_workers=min(pool_size, len(to_download))) as pool:
            futures = {
                pool.submit(client.download_object, h,
                           repo_root / ".av" / "objects" / h[:2] / h[2:]): (rel, h, size)
                for rel, h, size in to_download
            }
            for future in futures:
                rel, h, size = futures[future]
                if future.result():
                    fetched.append({"path": rel, "hash": h, "bytes": size})
                    total_bytes += size
                else:
                    raise ValidationError(f"Failed to download object {h[:12]}… for {rel}.")

    return {"fetched": fetched, "already_local": already_local, "bytes": total_bytes}


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


def write_fetched_commit(repo_root: Path, commit_data: dict, *, durable: bool = True) -> None:
    """Persists a commit fetched from the registry. `durable=False` (V1.6.0, WS4.8) skips
    the fsync -- for `av clone`, where every commit file can always be re-fetched by
    redoing the clone, unlike `av pull`/`av merge`'s writes (kept durable: those integrate
    into a repo whose OWN un-pushed commits already made stronger promises)."""
    from .fsutil import atomic_write_json

    atomic_write_json(repo_root / ".av" / "commits" / f"{commit_data['hash']}.json", commit_data,
                       durable=durable)


def load_local_commit(repo_root: Path, commit_hash: str) -> dict | None:
    try:
        with open(repo_root / ".av" / "commits" / f"{commit_hash}.json", "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return None
