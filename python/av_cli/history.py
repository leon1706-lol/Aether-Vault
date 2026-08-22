"""Commit-history walking and rendering for `av log`.

Pure-local module: reads only `.av/commits/*.json` and `.av/refs/heads/*`, never touches
the network — so `av log` pays zero HTTP latency even when a registry is configured (and
keeps working fully offline). Cloned repositories carry every commit's metadata locally
(see `av clone`), so the walk sees the full upstream history there too.
"""

from __future__ import annotations

import json
from pathlib import Path

from .fsutil import find_commit_file


def load_commit_local(repo_root: Path, commit_hash: str) -> dict | None:
    """Reads `.av/commits/<hash>.json` (full or unique-prefix hash), or None when absent.

    Local-only by design: `av log` deliberately does not fall back to the registry, so a
    history view never blocks on the network or half-renders when offline.
    """
    from .exceptions import AmbiguousCommitHash

    try:
        commit_path = find_commit_file(repo_root, commit_hash)
    except (FileNotFoundError, AmbiguousCommitHash):
        return None
    with open(commit_path, "r", encoding="utf-8") as f:
        return json.load(f)


def resolve_branch_tip(repo_root: Path, branch: str) -> str | None:
    ref_path = repo_root / ".av" / "refs" / "heads" / branch
    if not ref_path.exists():
        return None
    tip = ref_path.read_text().strip()
    return tip or None


def resolve_start_hash(repo_root: Path, branch: str | None) -> tuple[str | None, str | None]:
    """Returns (start_hash, error_message) — exactly one is set.

    `branch=None` starts from HEAD (attached ref or detached hash, matching how every
    other command interprets HEAD).
    """
    if branch is not None:
        tip = resolve_branch_tip(repo_root, branch)
        if tip is None:
            return None, f"No such branch '{branch}'."
        return tip, None

    head_path = repo_root / ".av" / "HEAD"
    if not head_path.exists():
        return None, None
    head_content = head_path.read_text().strip()
    if head_content.startswith("ref: "):
        ref_path = repo_root / ".av" / head_content.split(": ", 1)[1]
        if not ref_path.exists():
            return None, None
        head_content = ref_path.read_text().strip()
    return (head_content or None), None


def collect_branch_decorations(repo_root: Path) -> dict[str, list[str]]:
    """Maps commit hash -> branch names pointing at it (for `(main)`-style annotations)."""
    heads_dir = repo_root / ".av" / "refs" / "heads"
    decorations: dict[str, list[str]] = {}
    if not heads_dir.exists():
        return decorations
    for ref_file in sorted(heads_dir.iterdir()):
        tip = ref_file.read_text().strip()
        if tip:
            decorations.setdefault(tip, []).append(ref_file.name)
    return decorations


def walk_history(repo_root: Path, start: str, limit: int) -> list[dict]:
    """Walks the first-parent chain backwards from `start`, newest first.

    Stops at the first parent that isn't stored locally (never-fetched upstream history),
    at a cycle, or once `limit` entries are collected — whichever comes first. First-parent
    walking keeps merge commits (Phase: av merge) linear in the default view instead of
    duplicating shared ancestors.
    """
    commits: list[dict] = []
    visited: set[str] = set()
    current: str | None = start
    while current and current not in visited and len(commits) < limit:
        visited.add(current)
        commit_data = load_commit_local(repo_root, current)
        if commit_data is None:
            break
        commits.append(commit_data)
        parents = commit_data.get("parents") or []
        current = parents[0] if parents else None
    return commits


def collect_all_commits(repo_root: Path, limit: int) -> list[dict]:
    """Every local commit across all branches, newest first (timestamp-descending)."""
    commits_dir = repo_root / ".av" / "commits"
    if not commits_dir.exists():
        return []
    commits: list[dict] = []
    for commit_file in commits_dir.glob("*.json"):
        try:
            with open(commit_file, "r", encoding="utf-8") as f:
                commits.append(json.load(f))
        except Exception:
            continue
    commits.sort(key=lambda c: c.get("timestamp", ""), reverse=True)
    return commits[:limit]


def head_branch(repo_root: Path) -> str | None:
    head_path = repo_root / ".av" / "HEAD"
    if not head_path.exists():
        return None
    content = head_path.read_text().strip()
    if content.startswith("ref: refs/heads/"):
        return content.split("refs/heads/", 1)[1]
    return None


def format_log_line(commit: dict, decorations: list[str], is_head: bool) -> str:
    """Renders one `[shorthash] (deco) message` line, av-style."""
    parts = [f"[{commit['hash'][:7]}]"]
    deco = list(decorations)
    if is_head:
        deco.insert(0, "HEAD")
    if deco:
        parts.append(f"({', '.join(deco)})")
    parts.append(commit.get("message", ""))
    return " ".join(parts)


def format_meta_line(commit: dict) -> str | None:
    """Optional indented detail line — omitted entirely when there's nothing to show."""
    bits: list[str] = []
    author = commit.get("author")
    if author and author != "anonymous":
        bits.append(author)
    ts = commit.get("timestamp", "")
    if ts:
        bits.append(ts[:16].replace("T", " "))
    tags = commit.get("tags") or []
    metrics = commit.get("metrics") or {}
    if tags:
        bits.append(f"tags: {', '.join(tags)}")
    if metrics:
        shown = ", ".join(list(metrics)[:3])
        more = len(metrics) - 3
        bits.append("metrics: " + shown + (f" (+{more} more)" if more > 0 else ""))
    if not bits:
        return None
    return "    " + " · ".join(bits)
