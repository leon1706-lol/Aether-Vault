"""av watch — filesystem watcher for continuous training loops (v1.2.0). Pure-stdlib
polling by default: scans the repo on an interval, stages and commits any file matching
--glob through the SAME single commit path (upload deferred). With the optional
`watchdog` extra installed, change DETECTION switches to real filesystem events instead
of a full os.walk() every tick -- the debounce/commit logic is identical either way.
"""

import fnmatch
import os
import threading
import time

from .core import *  # noqa: F401,F403 -- shared prelude (stdlib + helpers)


def _try_start_watchdog(repo_root, pattern: str):
    """Returns (observer, drain_fn) when the watchdog extra is installed, else None.
    `drain_fn()` returns (and clears) the set of rel_paths touched since the last call --
    a superset is fine (a cheap no-op re-stat), a false negative is not, so events are
    filtered only by the --glob pattern, never by event type."""
    try:
        from watchdog.events import FileSystemEventHandler
        from watchdog.observers import Observer
    except ImportError:
        return None

    touched: set[str] = set()
    lock = threading.Lock()

    def _rel(path: str) -> str | None:
        try:
            rel = os.path.relpath(path, repo_root).replace(os.sep, "/")
        except ValueError:
            return None
        if ".av" in rel.split("/"):
            return None
        return rel if fnmatch.fnmatch(rel, pattern) else None

    class _Handler(FileSystemEventHandler):
        def on_any_event(self, event) -> None:
            for raw in (getattr(event, "src_path", None), getattr(event, "dest_path", None)):
                if not raw:
                    continue
                rel = _rel(raw)
                if rel:
                    with lock:
                        touched.add(rel)

    observer = Observer()
    observer.schedule(_Handler(), str(repo_root), recursive=True)
    observer.start()

    def _drain() -> set[str]:
        with lock:
            paths = set(touched)
            touched.clear()
        return paths

    return observer, _drain


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
    json_mode = current_output_mode() == "json"
    # watch is the one documented streaming exception to "single clean envelope per
    # invocation" -- it runs indefinitely, so JSON mode emits one newline-delimited
    # envelope PER auto-commit plus a final summary envelope on exit.
    watchdog_handle = _try_start_watchdog(repo_root, pattern)
    using_watchdog = watchdog_handle is not None
    if not json_mode:
        mode_desc = "watchdog events" if using_watchdog else "polling"
        click.secho(f"Watching '{pattern}' every {interval:.0f}s "
                    f"(debounce {debounce:.0f}s, {mode_desc}) — Ctrl+C to stop.", fg="cyan")

    seen: dict[str, tuple[int, int]] = {}   # rel_path -> (mtime_ns, size)
    pending_since: dict[str, float] = {}
    commits_made = 0
    first_tick = True

    try:
        while True:
            now = time.monotonic()
            if using_watchdog and not first_tick:
                # Only re-stat paths a real fs event touched, or already mid-debounce --
                # everything else in `seen` carries forward unchanged.
                _, drain = watchdog_handle
                candidates = drain() | set(pending_since)
                current: dict[str, tuple[int, int]] = dict(seen)
                for rel in candidates:
                    try:
                        st = os.stat(repo_root / rel)
                    except OSError:
                        current.pop(rel, None)
                        continue
                    current[rel] = (st.st_mtime_ns, st.st_size)
            else:
                # Polling mode every tick, OR the watchdog path's very first tick: a real
                # fs-event watcher only sees CHANGES from the moment it starts, so it would
                # otherwise never discover files that already existed before `av watch` ran.
                if using_watchdog:
                    watchdog_handle[1]()  # drain events queued during the scan below
                current = {}
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
                first_tick = False

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
                    if not json_mode:
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
                    from .core import resolve_run_id

                    # Same run-id precedence as every other commit path (resolve_run_id:
                    # env > state). result_sink suppresses _finalize_commit's own human
                    # echoes in JSON mode, matching cmd_history.py's `commit` command.
                    sink_data: dict = {}
                    json_sink = (lambda result: sink_data.update(result)) if json_mode else None
                    commit_hash = _commit(repo_root, f"watch: {rel} @ {time.strftime('%H:%M:%S')}",
                            run_id=resolve_run_id(repo_root), defer_upload=True,
                            result_sink=json_sink, outcome_sink=sink_data.update)
                    commits_made += 1
                    pending_since.pop(rel, None)
                    if json_mode:
                        click.echo(json.dumps(json_envelope("watch", data={
                            "event": "auto_commit", "path": rel, "commit_hash": commit_hash,
                            **sink_data,
                        })))

            seen = current

            if max_commits and commits_made >= max_commits:
                if json_mode:
                    click.echo(json.dumps(json_envelope("watch", data={
                        "event": "stopped", "reason": "max_commits_reached",
                        "commits_made": commits_made,
                    })))
                else:
                    click.secho(f"[watch] reached --max-commits={max_commits}; "
                                f"{commits_made} auto-commit(s) this session.", fg="cyan")
                break
            time.sleep(interval)
    except KeyboardInterrupt:
        if json_mode:
            click.echo(json.dumps(json_envelope("watch", data={
                "event": "stopped", "reason": "keyboard_interrupt", "commits_made": commits_made,
            })))
        else:
            click.secho(f"\n[watch] stopped — {commits_made} auto-commit(s) this session.", fg="cyan")
    finally:
        if using_watchdog:
            observer, _ = watchdog_handle
            observer.stop()
            observer.join(timeout=5)


def _hash_of(repo_root, rel_path: str) -> str | None:
    """Content hash via the index when present, else None (forces first commit)."""
    from .index import Index

    return (Index(repo_root).get_entry(rel_path) or {}).get("hash")
