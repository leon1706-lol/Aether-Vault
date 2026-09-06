"""History & local mutations: commit/branch/checkout/log/stash/list-meta/push.

Bodies moved verbatim from main.py (Point-13 split). Patch-target names owned by
main.py (`_find_source_root`, `_update_readme_test_badge`) are accessed late-bound via
`_root.<name>` so test monkeypatching on the main namespace stays effective.
"""

from .core import *  # noqa: F401,F403 -- shared prelude (stdlib + helpers)
from .core import _collect_dirty_paths, _finalize_commit, _materialize_tree



@click.command()
@click.option("-m", "--message", required=True, help="Commit message.")
@click.option("--tag", "tags", multiple=True, help="Free-form tag label (repeatable).")
@click.option(
    "--metric",
    "metrics_raw",
    multiple=True,
    help="Metric in key=value format (repeatable). E.g. --metric sharpe=2.45",
)
@click.option("--no-upload", is_flag=True, default=False,
              help="Skip the registry entirely: persist locally and queue for `av push`.")
@click.option("--metric-sharpe", type=float, default=None, help="Sharpe ratio (legacy shorthand).")
@click.option("--metric-drawdown", type=float, default=None, help="Max drawdown (legacy shorthand).")
def commit(
    message: str,
    tags: tuple,
    metrics_raw: tuple,
    no_upload: bool,
    metric_sharpe: float | None,
    metric_drawdown: float | None,
) -> None:
    """Record staged changes to the repository with optional tags and metrics."""
    from .client import VaultClient

    repo_root = ensure_repo()
    idx = Index(repo_root)
    cfg = load_config(repo_root)
    client = VaultClient(cfg.get("remote_url", "http://localhost:8000"), cfg.get("remote_api_token"))

    staged = idx.get_staged_entries()
    if not staged:
        # Exit 11 (nothing_to_commit) via fail(), matching the exit-code registry and
        # av_sdk.Repo.commit()'s SDKError("nothing_to_commit").
        fail(click.get_current_context(silent=True), "nothing_to_commit", "Nothing to commit",
             data={"reason": "nothing_to_commit"})

    # Run linkage: an active run (av run start / AV_RUN_ID) rides the commit payload so
    # the server can file it under the run without extra round trips.
    from .core import resolve_run_id

    run_id = resolve_run_id(repo_root)
    if run_id:
        tags = tuple(tags) + (f"run:{run_id}",) if f"run:{run_id}" not in tags else tags

    # --- Build metrics dict ---
    from .core import parse_metric_args

    metrics: dict = parse_metric_args(metrics_raw)
    if metric_sharpe is not None:
        metrics["sharpe"] = metric_sharpe
    if metric_drawdown is not None:
        metrics["drawdown"] = metric_drawdown

    # --- Update local metadata registry ---
    update_registry(repo_root, list(tags), metrics)

    sink_data: dict = {}
    json_sink = None
    if current_output_mode() == "json":
        def json_sink(result):
            sink_data.update(result)  # humans get echoes; JSON gets the recorded result

    # --no-upload (or AV_COMMIT_UPLOAD=0): high-frequency loops persist locally and let
    # `av push` drain later. The offline queue IS the mechanism — no separate store.
    defer_upload = no_upload or os.environ.get("AV_COMMIT_UPLOAD", "").strip().lower() in ("0", "false", "off")

    # THE shared commit path (also used by `av watch` and the av_sdk SDK).
    from .core import commit_staged

    # outcome_sink captures the final queued state unconditionally in both output modes,
    # independent of json_sink (JSON-mode only, also suppresses the human echo).
    head_hash = commit_staged(
        repo_root, message, tags=tags, metrics=metrics,
        run_id=run_id, defer_upload=defer_upload,
        result_sink=json_sink, outcome_sink=sink_data.update,
    )

    if current_output_mode() == "json":
        emit_json(None, "commit", data={
            "committed": True,
            "hash": head_hash,
            "short": (head_hash or "")[:7],
            "message": message,
            "tags": list(tags),
            "metrics": metrics,
            "run_id": run_id,
            "queued": sink_data.get("queued", False),
            "queued_reason": sink_data.get("queued_reason"),
            # Present only when queued_reason == "ref_race": the colliding run + remediation.
            "ref_race": sink_data.get("ref_race"),
        })
    # Exit 0 even when queued -- queued is a SAFE, complete local outcome by design
    # (AGENTS.md non-negotiable #3), not a partial failure.


@click.command()
@click.argument("name", required=False)
def branch(name: str | None) -> None:
    """List existing branches, or create a new one."""
    repo_root = ensure_repo()
    heads_dir = repo_root / ".av" / "refs" / "heads"
    json_mode = current_output_mode() == "json"

    if not name:
        head_path = repo_root / ".av" / "HEAD"
        current = ""
        if head_path.exists():
            head_content = head_path.read_text().strip()
            if head_content.startswith("ref: refs/heads/"):
                current = head_content.split("/")[-1]

        if json_mode:
            emit_json(None, "branch", data={
                "branches": [{"name": br.name, "current": br.name == current}
                            for br in sorted(heads_dir.iterdir(), key=lambda p: p.name)],
            })
            return
        for br in heads_dir.iterdir():
            if br.name == current:
                click.secho(f"* {br.name}", fg="green")
            else:
                click.echo(f"  {br.name}")
    else:
        head_path = repo_root / ".av" / "HEAD"
        commit_hash = ""
        if head_path.exists():
            head_content = head_path.read_text().strip()
            if head_content.startswith("ref: "):
                ref_path = repo_root / ".av" / head_content.split(": ")[1]
                if ref_path.exists():
                    commit_hash = ref_path.read_text().strip()
            else:
                commit_hash = head_content

        with open(heads_dir / name, "w") as f:
            f.write(commit_hash)
        if json_mode:
            emit_json(None, "branch", data={"created": name, "at": commit_hash or None})
            return
        click.secho(f"Created branch '{name}'", fg="green")


@click.command()
@click.argument("target")
@click.option("--force", "-f", is_flag=True, default=False,
              help="Discard uncommitted local changes instead of aborting.")
def checkout(target: str, force: bool) -> None:
    """Checkout a branch or a specific commit hash."""
    from .client import VaultClient

    repo_root = ensure_repo()
    cfg = load_config(repo_root)
    client = VaultClient(cfg.get("remote_url", "http://localhost:8000"), cfg.get("remote_api_token"))
    json_mode = current_output_mode() == "json"

    heads_dir = repo_root / ".av" / "refs" / "heads"
    commit_hash = target
    ref_name = None

    if (heads_dir / target).exists():
        commit_hash = (heads_dir / target).read_text().strip()
        ref_name = target

    commit_file = None
    try:
        commit_file = find_commit_file(repo_root, commit_hash)
    except AmbiguousCommitHash as exc:
        if json_mode:
            fail(None, "validation", exc.message, command="checkout")
        click.secho(f"Error: {exc.message}", fg="red")
        return
    except FileNotFoundError:
        pass
    commit_data = None

    if commit_file is not None:
        commit_hash = commit_file.stem
        with open(commit_file, "r") as f:
            commit_data = json.load(f)
    elif client.server_available():
        commit_file = repo_root / ".av" / "commits" / f"{commit_hash}.json"
        commit_data = client.get_commit(commit_hash)
        if commit_data:
            with open(commit_file, "w") as f:
                json.dump(commit_data, f)

    if not commit_data:
        if json_mode:
            fail(None, "validation", f"Commit '{target}' not found.", command="checkout")
        click.secho(f"Error: Commit '{target}' not found.", fg="red")
        return

    idx = Index(repo_root)

    # Guard against silent data loss: checkout overwrites/deletes tracked working files.
    # Refuse to proceed if there are uncommitted changes (modified/deleted tracked files
    # or staged-but-uncommitted edits) unless the user explicitly passes --force.
    if not force:
        dirty = _collect_dirty_paths(repo_root, idx)
        if dirty:
            if json_mode:
                fail(None, "validation",
                     "You have uncommitted changes that would be overwritten by checkout.",
                     command="checkout", data={"dirty_paths": dirty[:20],
                                               "dirty_count": len(dirty)})
            click.secho(
                "Error: You have uncommitted changes that would be overwritten by checkout:",
                fg="red",
            )
            for d in dirty[:20]:
                click.echo(f"  {d}")
            if len(dirty) > 20:
                click.echo(f"  … and {len(dirty) - 20} more")
            click.secho("Commit them, or re-run with --force to discard.", fg="yellow")
            return

    _materialize_tree(repo_root, client, commit_data.get("tree", {}), idx)

    head_path = repo_root / ".av" / "HEAD"
    with open(head_path, "w") as f:
        if ref_name:
            f.write(f"ref: refs/heads/{ref_name}\n")
        else:
            f.write(f"{commit_hash}\n")

    if json_mode:
        emit_json(None, "checkout", data={"target": target, "commit_hash": commit_hash,
                                          "branch": ref_name})
        return
    click.secho(f"Checked out '{target}'", fg="green")


# ---------------------------------------------------------------------------
# av log
# ---------------------------------------------------------------------------

@click.command()
@click.option("--limit", default=30, show_default=True, help="Maximum commits to show.")
@click.option("--branch", default=None,
              help="Start from this branch's tip instead of HEAD.")
@click.option("--all", "show_all", is_flag=True,
              help="List every local commit across all branches (newest first).")
def log(limit: int, branch: str | None, show_all: bool) -> None:
    """Show local commit history, newest first."""
    from . import history

    repo_root = ensure_repo()
    json_mode = current_output_mode() == "json"

    if show_all:
        commits = history.collect_all_commits(repo_root, limit)
        if not commits:
            if json_mode:
                emit_json(None, "log", data={"commits": []})
                return
            click.secho("No commits yet.", fg="yellow")
            return
        decorations = history.collect_branch_decorations(repo_root)
        head_hash = None
    else:
        start, err = history.resolve_start_hash(repo_root, branch)
        if err:
            if json_mode:
                fail(None, "validation", err, command="log")
            click.secho(f"Error: {err}", fg="red")
            return
        if start is None:
            if json_mode:
                emit_json(None, "log", data={"commits": []})
                return
            click.secho("No commits yet.", fg="yellow")
            return
        commits = history.walk_history(repo_root, start, limit)
        decorations = history.collect_branch_decorations(repo_root)
        head_hash = start

    if json_mode:
        # `tree` omitted (can be large); every other field rides through as-is. `short`
        # is synthesized to match the `commit`/`checkout` envelopes' own convention.
        emit_json(None, "log", data={"commits": [
            {**{k: v for k, v in commit.items() if k != "tree"},
             "short": commit["hash"][:7],
             "decorations": decorations.get(commit["hash"], []),
             "is_head": bool(head_hash) and commit["hash"] == head_hash}
            for commit in commits
        ]})
        return

    for commit in commits:
        h = commit["hash"]
        is_head = bool(head_hash) and h == head_hash
        click.echo(history.format_log_line(commit, decorations.get(h, []), is_head))
        meta = history.format_meta_line(commit)
        if meta:
            click.echo(meta)


def _stash_dir(repo_root: Path) -> Path:
    return repo_root / ".av" / "stash"


def _list_stash_files(repo_root: Path) -> list[Path]:
    stash_dir = _stash_dir(repo_root)
    if not stash_dir.exists():
        return []
    # Filenames are timestamp-prefixed (YYYYMMDDTHHMMSSZ-<shortid>.json), so a reverse
    # lexicographic sort is already newest-first.
    return sorted(stash_dir.glob("*.json"), reverse=True)


def _resolve_stash_file(repo_root: Path, stash_id: str | None) -> Path | None:
    files = _list_stash_files(repo_root)
    if not files:
        return None
    if stash_id is None:
        return files[0]
    for f in files:
        if f.stem == stash_id or f.name == stash_id:
            return f
    return None


def _stash_push(message: str | None) -> None:
    from .client import VaultClient

    repo_root = ensure_repo()
    idx = Index(repo_root)
    cfg = load_config(repo_root)
    client = VaultClient(cfg.get("remote_url", "http://localhost:8000"), cfg.get("remote_api_token"))
    threshold_bytes = cfg.get("lfs_threshold_mb", 50) * 1024 * 1024
    json_mode = current_output_mode() == "json"

    staged, modified, deleted, _untracked = compute_status(repo_root, idx)
    if deleted and not json_mode:
        click.secho(
            f"Skipping {len(deleted)} deleted file(s) — not yet supported by `av stash`.",
            fg="yellow",
        )

    dirty_paths = staged + modified  # compute_status's branches are mutually exclusive
    if not dirty_paths:
        if json_mode:
            emit_json(None, "stash", data={"stashed": False, "reason": "nothing_to_stash",
                                           "skipped_deleted": deleted})
            return
        click.secho("No local changes to stash", fg="yellow")
        return

    head_tree = resolve_head_tree(repo_root)
    stash_entries = []

    from . import attributes

    attr_rules = attributes.load_attributes(repo_root)
    for rel_path in dirty_paths:
        was_staged = rel_path in staged
        if not was_staged:
            # Modified-but-unstaged: get its current content safely into the CAS first,
            # exactly the way `av add` would — so reverting the working copy below doesn't
            # lose it.
            stage_one_file(repo_root, idx, threshold_bytes, repo_root / rel_path, rel_path,
                           attributes.flags_for(attr_rules, rel_path))

        entry = idx.entries[rel_path]
        stash_entries.append({
            "rel_path": rel_path,
            "hash": entry["hash"],
            "size": entry["size"],
            "type": entry["type"],
            "layers": entry.get("layers", []),
            "pointer": entry.get("pointer"),
            "was_staged": was_staged,
        })

        head_data = head_tree.get(rel_path)
        if head_data:
            materialize_file(repo_root, client, rel_path, head_data["hash"], head_data.get("layers", []))
            new_entry = {
                "hash": head_data["hash"],
                "size": head_data["size"],
                "mtime_ns": 0,
                "type": head_data["type"],
                "staged": False,
                "pointer": rel_path + ".av-pointer" if head_data["type"] == "artifact" else None,
            }
            if head_data.get("layers"):
                new_entry["layers"] = head_data["layers"]
            idx.entries[rel_path] = new_entry
            # Re-stat now that HEAD's content has actually been written to disk, so the index
            # matches the real (clean) file instead of the 0 placeholder above — otherwise
            # `av status` would immediately call every reverted file "modified" again.
            fpath = repo_root / rel_path
            if fpath.exists():
                m = get_file_meta_safe(str(fpath))
                idx.entries[rel_path]["size"] = m["size"]
                idx.entries[rel_path]["mtime_ns"] = m["mtime_ns"]
        else:
            # Never committed — same as `av unstage` for a new file: it disappears until
            # popped, since there's no HEAD baseline to revert to.
            remove_file_and_pointer(repo_root, rel_path)
            del idx.entries[rel_path]

    idx.save()

    stash_dir = _stash_dir(repo_root)
    stash_dir.mkdir(parents=True, exist_ok=True)
    # Microsecond resolution, not just seconds -- two stashes created in quick succession
    # would otherwise share the same prefix and sort arbitrarily instead of newest-first.
    stash_id = f"{datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%S%f')}-{uuid.uuid4().hex[:6]}"

    branch = "detached"
    head_path = repo_root / ".av" / "HEAD"
    if head_path.exists():
        head_content = head_path.read_text().strip()
        if head_content.startswith("ref: refs/heads/"):
            branch = head_content.split("/")[-1]

    atomic_write_json(stash_dir / f"{stash_id}.json", {
        "id": stash_id,
        "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "branch": branch,
        "message": message,
        "entries": stash_entries,
    })

    if json_mode:
        emit_json(None, "stash", data={"stashed": True, "id": stash_id,
                                       "file_count": len(stash_entries),
                                       "skipped_deleted": deleted})
        return
    label = f": {message}" if message else ""
    click.secho(f"Saved working directory state: stash@{{0}}{label}", fg="green")


def _stash_apply_or_pop(stash_id: str | None, delete_after: bool) -> None:
    from .client import VaultClient

    repo_root = ensure_repo()
    stash_file = _resolve_stash_file(repo_root, stash_id)
    json_mode = current_output_mode() == "json"
    if stash_file is None:
        msg = f"No stash found matching '{stash_id}'" if stash_id else "No stashes to apply"
        if json_mode:
            emit_json(None, "stash pop" if delete_after else "stash apply",
                      data={"applied": False, "reason": "not_found"})
            return
        click.secho(msg, fg="yellow")
        return

    with open(stash_file, "r") as f:
        record = json.load(f)

    cfg = load_config(repo_root)
    client = VaultClient(cfg.get("remote_url", "http://localhost:8000"), cfg.get("remote_api_token"))
    idx = Index(repo_root)
    head_tree = resolve_head_tree(repo_root)  # only needed for was_staged=False entries below

    for entry in record["entries"]:
        rel_path = entry["rel_path"]
        layers = entry.get("layers", [])
        # v1 doesn't attempt conflict detection against a dirty tree — this overwrites
        # whatever's currently at rel_path, same caveat as the plan this was built from.
        materialize_file(repo_root, client, rel_path, entry["hash"], layers)

        if entry["was_staged"]:
            # Was staged before the stash: restore it staged again, with the real (dirty)
            # content's hash/stat — `status()` shows staged entries as "to be committed"
            # purely from the `staged` flag, regardless of stat matching.
            meta = get_file_meta_safe(str(repo_root / rel_path))
            new_entry = {
                "hash": entry["hash"], "size": meta["size"], "mtime_ns": meta["mtime_ns"],
                "type": entry["type"], "staged": True, "pointer": entry.get("pointer"),
            }
        else:
            # Was modified-but-unstaged: working tree is restored to the dirty content
            # above, but the index entry goes back to HEAD's baseline with a deliberately
            # non-matching mtime, so `status()` reports "modified" again instead of clean.
            head_data = head_tree.get(rel_path, {})
            new_entry = {
                "hash": head_data.get("hash", entry["hash"]),
                "size": head_data.get("size", entry["size"]),
                "mtime_ns": 0,
                "type": entry["type"], "staged": False, "pointer": entry.get("pointer"),
            }
        if layers:
            new_entry["layers"] = layers
        idx.entries[rel_path] = new_entry

    idx.save()

    if delete_after:
        stash_file.unlink()

    if json_mode:
        emit_json(None, "stash pop" if delete_after else "stash apply", data={
            "applied": True, "id": stash_file.stem, "restored_count": len(record["entries"]),
            "deleted": delete_after,
        })
        return
    verb = "Popped" if delete_after else "Applied"
    click.secho(f"{verb} stash {stash_file.stem} ({len(record['entries'])} file(s) restored)", fg="green")


@click.group(invoke_without_command=True, name="stash")
@click.option("-m", "--message", default=None, help="Optional label for this stash.")
@click.pass_context
def stash(ctx: click.Context, message: str | None) -> None:
    """Temporarily shelve uncommitted changes (staged + modified tracked files).

    Reverts the working tree to match HEAD — same scope as `git stash`: staged and modified
    tracked files, not untracked or deleted ones — so `checkout`/`branch` can proceed without
    --force. `av stash pop` brings everything back exactly as it was, staged or not.
    """
    if ctx.invoked_subcommand is None:
        _stash_push(message)


@stash.command("push")
@click.option("-m", "--message", default=None, help="Optional label for this stash.")
def stash_push_cmd(message: str | None) -> None:
    """Shelve uncommitted changes (same as bare `av stash`)."""
    _stash_push(message)


@stash.command("list")
def stash_list_cmd() -> None:
    """List stashes, newest first."""
    repo_root = ensure_repo()
    files = _list_stash_files(repo_root)
    json_mode = current_output_mode() == "json"
    if not files:
        if json_mode:
            emit_json(None, "stash list", data={"stashes": []})
            return
        click.secho("No stashes", fg="yellow")
        return
    if json_mode:
        stashes = []
        for f in files:
            with open(f, "r") as fh:
                record = json.load(fh)
            stashes.append({"id": f.stem, "created_at": record["created_at"],
                            "branch": record.get("branch"), "message": record.get("message"),
                            "file_count": len(record["entries"])})
        emit_json(None, "stash list", data={"stashes": stashes})
        return
    for i, f in enumerate(files):
        with open(f, "r") as fh:
            record = json.load(fh)
        label = f": {record['message']}" if record.get("message") else ""
        click.echo(f"stash@{{{i}}}  {record['created_at']}  ({len(record['entries'])} file(s)){label}  [{f.stem}]")


@stash.command("pop")
@click.argument("stash_id", required=False)
def stash_pop_cmd(stash_id: str | None) -> None:
    """Apply the most recent (or a given) stash, then delete it."""
    _stash_apply_or_pop(stash_id, delete_after=True)


@stash.command("apply")
@click.argument("stash_id", required=False)
def stash_apply_cmd(stash_id: str | None) -> None:
    """Apply the most recent (or a given) stash, keeping it for reuse."""
    _stash_apply_or_pop(stash_id, delete_after=False)


@stash.command("drop")
@click.argument("stash_id", required=False)
def stash_drop_cmd(stash_id: str | None) -> None:
    """Delete a stash without applying it."""
    repo_root = ensure_repo()
    stash_file = _resolve_stash_file(repo_root, stash_id)
    json_mode = current_output_mode() == "json"
    if stash_file is None:
        if json_mode:
            emit_json(None, "stash drop", data={"dropped": False, "reason": "not_found"})
            return
        msg = f"No stash found matching '{stash_id}'" if stash_id else "No stashes to drop"
        click.secho(msg, fg="yellow")
        return
    stem = stash_file.stem
    stash_file.unlink()
    if json_mode:
        emit_json(None, "stash drop", data={"dropped": True, "id": stem})
        return
    click.secho(f"Dropped stash {stem}", fg="green")


@click.command("list-meta")
def list_meta() -> None:
    """Display all registered tag labels and metric keys for this repository."""
    repo_root = ensure_repo()
    reg = load_registry(repo_root)

    if current_output_mode() == "json":
        emit_json(None, "list-meta", data={
            "tags": sorted(reg["tags"]), "metrics": sorted(reg["metrics"]),
        })
        return

    click.secho("\n─── Registered Metadata ───", bold=True)

    if reg["tags"]:
        click.secho("\n  Tags:", fg="cyan")
        for t in sorted(reg["tags"]):
            click.echo(f"    • {t}")
    else:
        click.echo("\n  Tags: (none)")

    if reg["metrics"]:
        click.secho("\n  Metric Keys:", fg="cyan")
        for m in sorted(reg["metrics"]):
            click.echo(f"    • {m}")
    else:
        click.echo("\n  Metric Keys: (none)")


@click.command()
def push() -> None:
    """Retry pushing locally committed but not-yet-synced commits to the remote server."""
    from .client import VaultClient

    repo_root = ensure_repo()
    cfg = load_config(repo_root)
    client = VaultClient(cfg.get("remote_url", "http://localhost:8000"), cfg.get("remote_api_token"))

    pending_before = load_pending_push(repo_root)
    if not pending_before:
        if current_output_mode() == "json":
            emit_json(None, "push", data={"drained": 0, "still_queued": 0, "reachable": None})
            return
        click.secho("Nothing pending — all commits are synced.", fg="green")
        return

    if not client.server_available():
        if current_output_mode() == "json":
            emit_json(None, "push", data={"drained": 0,
                                          "still_queued": len(pending_before),
                                          "reachable": False})
            return
        click.secho("Error: Remote server is not reachable.", fg="red")
        return

    still_pending = flush_pending_push(repo_root, client)
    pushed = len(pending_before) - len(still_pending)
    if current_output_mode() == "json":
        emit_json(None, "push", data={
            "drained": pushed,
            "still_queued": len(still_pending),
            "reachable": True,
        })
        return
    if pushed:
        click.secho(f"[OK] Pushed {pushed} commit(s) to the remote server", fg="green")
    if still_pending:
        click.secho(f"  {len(still_pending)} commit(s) still pending", fg="yellow")
