"""av watch — filesystem watcher for continuous training loops (v1.2.0).

Pure-stdlib polling (no watchdog dependency): scans the repo on an interval, stages and
commits any file matching --glob that appeared or changed since the last scan. Commits
run through the SAME single code path (commit command semantics, upload deferred) so
offline resilience and run-tagging apply unchanged.
"""

import fnmatch
import os
import time

from .core import *  # noqa: F401,F403 -- shared prelude (stdlib + helpers)


@click.command()
@click.option("--glob", "pattern", default="*.ckpt", show_default=True,
              help="fnmatch pattern (relative paths) to watch.")
@click.option("--interval", default=10.0, show_default=True, help="Scan interval seconds.")
@click.option("--debounce", default=5.0, show_default=True,
              help="A file must be stable for this many seconds before committing.")
@click.option("--max-commits", default=0, type=int,
              help="Stop after N commits (0 = run until Ctrl+C).")
def watch(pattern: str, interval: float, debounce: float, max_commits: int) -> None:
    """Watch for new/changed checkpoints and commit them automatically.

    Built for high-frequency loops without framework plugins: point it at a directory
    pattern and every artifact matching GLOB gets staged + committed (upload deferred)
    as soon as its size/mtime stops changing for DEBOUNCE seconds. Ctrl+C exits; the
    offline queue keeps everything safe until `av push`.
    """
    from .index import Index

    repo_root = ensure_repo()
    click.secho(f"Watching '{pattern}' every {interval:.0f}s "
                f"(debounce {debounce:.0f}s) — Ctrl+C to stop.", fg="cyan")

    seen: dict[str, tuple[int, int]] = {}   # rel_path -> (mtime_ns, size)
    pending_since: dict[str, float] = {}
    commits_made = 0

    try:
        while True:
            now = time.monotonic()
            current: dict[str, tuple[int, int]] = {}
            for dirpath, _dirnames, filenames in os.walk(repo_root):
                if ".av" in dirpath.split(os.sep):
                    continue
                for fn in filenames:
                    full = os.path.join(dirpath, fn)
                    rel = os.path.relpath(full, repo_root).replace(os.sep, "/")
                    if not fnmatch.fnmatch(rel, pattern):
                        continue
                    try:
                        st = os.stat(full)
                    except OSError:
                        continue
                    current[rel] = (st.st_mtime_ns, st.st_size)

            for rel, sig in sorted(current.items()):
                prev = seen.get(rel)
                if prev is None or prev != sig:
                    pending_since[rel] = now          # (re)arm debounce timer
                elif rel in pending_since and now - pending_since[rel] >= debounce:
                    idx = Index(repo_root)
                    entry = idx.get_entry(rel)
                    if entry and entry.get("hash") == _hash_of(repo_root, rel):
                        pending_since.pop(rel, None)  # already committed this content
                        continue
                    click.secho(f"[watch] new content: {rel}", fg="yellow")
                    # Stage through the REAL staging path (hashing/pointers/CDC/attributes),
                    # then commit through THE shared path (offline-queue semantics apply).
                    from .core import commit_staged, get_file_meta_safe, hash_file_safe, stage_one_file
                    from . import attributes as attr_mod

                    fpath = repo_root / rel
                    cfg = load_config(repo_root)
                    threshold = cfg.get("lfs_threshold_mb", 50) * 1024 * 1024
                    rules = attr_mod.load_attributes(repo_root)
                    file_hash = hash_file_safe(str(fpath))
                    if not file_hash:
                        pending_since.pop(rel, None)
                        continue
                    stage_one_file(repo_root, idx, threshold, fpath, rel,
                                   attr_mod.flags_for(rules, rel))
                    idx.save()
                    from .core import commit_staged as _commit

                    _commit(repo_root, f"watch: {rel} @ {time.strftime('%H:%M:%S')}",
                            defer_upload=True)
                    commits_made += 1
                    pending_since.pop(rel, None)

            seen = current

            if max_commits and commits_made >= max_commits:
                click.secho(f"[watch] reached --max-commits={max_commits}; "
                            f"{commits_made} auto-commit(s) this session.", fg="cyan")
                break
            time.sleep(interval)
    except KeyboardInterrupt:
        click.secho(f"\n[watch] stopped — {commits_made} auto-commit(s) this session.", fg="cyan")


def _hash_of(repo_root, rel_path: str) -> str | None:
    """Content hash via the index when present, else None (forces first commit)."""
    from .index import Index

    return (Index(repo_root).get_entry(rel_path) or {}).get("hash")
