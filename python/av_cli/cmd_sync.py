"""Remote collaboration: clone/pull/merge.

Bodies moved verbatim from main.py (Point-13 split). Patch-target names owned by
main.py (`_find_source_root`, `_update_readme_test_badge`) are accessed late-bound via
`_root.<name>` so test monkeypatching on the main namespace stays effective.
"""

from .core import *  # noqa: F401,F403 -- shared prelude (stdlib + helpers)
from .core import (
    _collect_dirty_paths,
    _finalize_commit,
    _init_repo_structure,
    _materialize_tree,
)



@click.command("clone")
@click.argument("project")
@click.argument("directory", required=False)
@click.option("--remote-url", default=None, metavar="URL",
              help="Registry to clone from (default: $AV_REMOTE_URL, else http://localhost:8000).")
@click.option("--token", default=None, help="Access token for a Protected registry.")
def clone(project: str, directory: str | None, remote_url: str | None, token: str | None) -> None:
    """Clone an existing project from a registry into a new directory.

    Downloads the project's full commit history (metadata — cheap) and materializes the
    default branch's tip; older versions' large objects lazy-download on first checkout.
    The cloned repo inherits the source project's identity, so pushes from either copy
    land in the same project on the shared registry.
    """
    from .client import VaultClient
    from . import sync

    ctx = click.get_current_context(silent=True)
    target = Path(directory).resolve() if directory else Path.cwd() / project
    if target.exists() and any(target.iterdir()):
        fail(ctx, "validation", f"'{target}' already exists and is not empty.")

    url = remote_url or os.environ.get("AV_REMOTE_URL") or "http://localhost:8000"
    api_token = token or os.environ.get("AV_API_TOKEN")
    client = VaultClient(url, api_token)
    if not client.server_available():
        fail(ctx, "validation", f"Registry unreachable at {url} — is the backend running?")

    try:
        proj = sync.resolve_project(client, project)
    except ValidationError as exc:
        fail(ctx, "validation", exc.message)

    pid = proj["project_id"]
    refs = client.list_refs(project_id=pid)
    branch = sync.pick_default_branch(refs, pid)
    commits = sync.fetch_project_commits(client, pid)
    if not commits:
        fail(ctx, "validation", f"Project '{proj.get('project_name')}' has no commits yet.")
    if branch is None:
        # No refs pushed (e.g. only queued/offline commits): fall back to the newest commit.
        branch = "main"
        tip_hash = commits[0]["hash"]
    else:
        tip_hash = refs[f"{pid}/{branch}"]

    target.mkdir(parents=True, exist_ok=True)
    _init_repo_structure(target)
    cfg = load_config(target)
    cfg.update({
        "remote_url": url,
        "login_mode": "local",
        "project_id": pid,
        "project_name": proj.get("project_name") or target.name,
    })
    if api_token:
        cfg["remote_api_token"] = api_token
    save_config(target, cfg)

    for c in commits:
        sync.write_fetched_commit(target, c)

    heads_dir = target / ".av" / "refs" / "heads"
    atomic_write_text(heads_dir / branch, tip_hash)
    atomic_write_text(target / ".av" / "HEAD", f"ref: refs/heads/{branch}\n")
    for stale in heads_dir.iterdir():
        if stale.name != branch and not stale.read_text().strip():
            stale.unlink()

    tip_tree = next((c.get("tree", {}) for c in commits if c["hash"] == tip_hash), {})
    downloaded = sync.ensure_objects_local(target, client, tip_tree)
    _materialize_tree(target, client, tip_tree, Index(target))

    if output_is_json(ctx):
        emit_json(ctx, "clone", data={
            "project_id": pid, "project_name": proj.get("project_name"),
            "directory": str(target), "branch": branch, "tip": tip_hash,
            "commits": len(commits), "downloaded_objects": downloaded,
        })
        return
    msg = f"Cloned '{proj.get('project_name')}' ({len(commits)} commit(s)) into {target}"
    click.secho(msg, fg="green")
    detail = f"  branch {branch} @ [{tip_hash[:7]}]"
    if downloaded:
        detail += f" — downloaded {downloaded} object(s)"
    click.echo(detail)


# v1.3.0: moved to core.py::tip_run_id() so _finalize_commit's ref-race path can share
# it too — kept as a thin re-export so nothing here (or any external caller) breaks.
from .core import tip_run_id as _tip_run_id  # noqa: F401


@click.command()
@click.option("--force", "-f", is_flag=True, default=False,
              help="Discard uncommitted local changes instead of aborting.")
def pull(force: bool) -> None:
    """Fetch the current branch from the registry and fast-forward onto it.

    Pull is deliberately fast-forward-only: when local and remote histories have diverged
    it refuses instead of guessing a merge — the fetched commits are stored locally first,
    so `av merge <remote-tip>` resolves it explicitly.
    """
    from .client import VaultClient
    from . import sync

    ctx = click.get_current_context(silent=True)
    repo_root = ensure_repo()
    cfg = load_config(repo_root)

    head_content = (repo_root / ".av" / "HEAD").read_text().strip()
    if not head_content.startswith("ref: refs/heads/"):
        fail(ctx, "validation", "HEAD is detached — check out a branch before pulling.")
    branch = head_content.split("refs/heads/", 1)[1]
    ref_path = repo_root / ".av" / "refs" / "heads" / branch
    local_tip = ref_path.read_text().strip() if ref_path.exists() else ""

    client = VaultClient(cfg.get("remote_url", "http://localhost:8000"), cfg.get("remote_api_token"))
    if not client.server_available():
        fail(ctx, "validation", f"Registry unreachable at {cfg.get('remote_url')}.")

    remote_ref = f"{cfg['project_id']}/{branch}"
    remote_tip = client.get_ref(remote_ref)
    if not remote_tip:
        if output_is_json(ctx):
            emit_json(ctx, "pull", data={"pulled": False, "reason": "no_remote_branch", "branch": branch})
            return
        click.secho(f"No remote branch '{branch}' on the registry — nothing to pull.", fg="yellow")
        return
    if remote_tip == local_tip:
        if output_is_json(ctx):
            emit_json(ctx, "pull", data={"pulled": False, "reason": "up_to_date", "tip": local_tip})
            return
        click.secho("Already up to date.")
        return

    # Walk the remote chain back until it joins history we already have, storing every new
    # commit locally as we go — so even a diverged pull leaves the full picture on disk.
    fetched: list[dict] = []
    cursor: str | None = remote_tip
    join_found = False
    while cursor:
        if cursor == local_tip:
            join_found = True
            break
        existing = sync.load_local_commit(repo_root, cursor)
        if existing is not None:
            join_found = True
            break
        row = client.get_commit(cursor)
        if not row:
            fail(ctx, "validation",
                 f"Remote history is broken — commit {cursor[:7]}… is referenced but "
                 "missing from the registry.")
        data = sync.normalize_commit_row(row)
        sync.write_fetched_commit(repo_root, data)
        fetched.append(data)
        parents = data["parents"]
        cursor = parents[0] if parents else None

    # Fast-forwarding is only safe when the local tip is an ANCESTOR of the remote tip.
    # Joining the walked chain somewhere below the local tip isn't enough: a repo with its
    # own unpushed commits would otherwise have them silently overwritten by the remote
    # tree. Not-an-ancestor = diverged → hand off to `av merge`.
    ff_allowed = (not local_tip) or (
        join_found and sync.is_ancestor(lambda h: sync.load_local_commit(repo_root, h),
                                        local_tip, remote_tip)
    )
    if not ff_allowed:
        # v1.2.2: surface the runs the two tips belong to, so an agent orchestrating
        # multiple training efforts can tell WHICH experiments diverged without walking
        # commits by hand. Best-effort: untagged tips simply contribute no line/field.
        local_run = _tip_run_id(repo_root, local_tip)
        remote_run = _tip_run_id(repo_root, remote_tip)
        remediation = [f"av merge {remote_tip[:7]}"]
        if not output_is_json(ctx):
            click.secho(
                f"Local and remote '{branch}' have diverged.\n"
                f"The remote tip [{remote_tip[:7]}] and its history are now local — resolve with:\n"
                f"  av merge {remote_tip[:7]}",
                fg="yellow",
            )
            if local_run or remote_run:
                fmt = lambda rid: f"run:{rid[:8]}…" if rid else "(no run)"
                click.echo(f"  local  tip [{local_tip[:7]}] belongs to {fmt(local_run)}")
                click.echo(f"  remote tip [{remote_tip[:7]}] belongs to {fmt(remote_run)}")
        # v1.2.5: routed through fail() reusing "merge_conflict" (exit 14) — a divergence
        # is resolved by exactly the same command family as a conflicting merge, and the
        # exit-code registry is deliberately kept closed rather than growing a new code
        # per divergence flavor. error.data carries what the text-mode lines above said;
        # quiet_text=True avoids a redundant generic "Error: ..." line under them.
        fail(ctx, "merge_conflict", f"Local and remote '{branch}' have diverged.",
             data={
                 "reason": "diverged", "branch": branch,
                 "local_tip": local_tip, "remote_tip": remote_tip,
                 "local_run_id": local_run, "remote_run_id": remote_run,
                 "remediation": remediation,
             }, quiet_text=True)

    idx = Index(repo_root)
    if not force:
        dirty = _collect_dirty_paths(repo_root, idx)
        if dirty:
            if not output_is_json(ctx):
                click.secho(
                    "Error: You have uncommitted changes that would be overwritten by pull:",
                    fg="red",
                )
                for d in dirty[:20]:
                    click.echo(f"  {d}")
                click.secho("Commit them (or use --force to discard), then pull again.", fg="yellow")
            fail(ctx, "validation",
                 "You have uncommitted changes that would be overwritten by pull.",
                 data={"dirty": dirty[:20], "remediation": ["av commit -m ...", "av pull --force"]},
                 quiet_text=True)

    tip_data = sync.load_local_commit(repo_root, remote_tip)
    tree = tip_data.get("tree", {}) if tip_data else {}
    sync.ensure_objects_local(repo_root, client, tree)
    _materialize_tree(repo_root, client, tree, idx)
    atomic_write_text(ref_path, remote_tip)

    if output_is_json(ctx):
        emit_json(ctx, "pull", data={
            "pulled": True, "branch": branch, "from": local_tip or None, "to": remote_tip,
            "new_commits": len(fetched),
        })
        return
    click.secho(
        f"Fast-forwarded {branch}: {local_tip[:7] or '(empty)'} → {remote_tip[:7]} "
        f"({len(fetched)} new commit(s))",
        fg="green",
    )


@click.command()
@click.argument("target")
@click.option("-m", "--message", default=None, help="Override the default merge commit message.")
@click.option("--ours", "policy_ours", is_flag=True, default=False,
              help="Resolve conflicting files by keeping THIS branch's version.")
@click.option("--theirs", "policy_theirs", is_flag=True, default=False,
              help="Resolve conflicting files by taking TARGET's version.")
@click.option("--no-ff", is_flag=True, default=False,
              help="Create a merge commit even when a fast-forward would do.")
@click.option("--force", "-f", is_flag=True, default=False,
              help="Bypass an armed branch policy for this merge (recorded in output).")
@click.option("--conflict-report", "conflict_report_path", type=click.Path(), default=None,
              help="On conflict, also write the structured conflict report to this path "
                   "(always written to .av/last_conflict.json regardless of this flag).")
def merge(target: str, message: str | None, policy_ours: bool, policy_theirs: bool,
          no_ff: bool, force: bool, conflict_report_path: str | None) -> None:
    """Merge another branch or commit into the current branch.

    Tree-level three-way merge against the nearest common ancestor: per file, whichever
    side changed wins; if BOTH sides changed the same file differently the merge aborts
    cleanly (nothing touched) and lists the conflicts — resolve with --ours/--theirs.
    Successful non-fast-forward merges create a two-parent merge commit that syncs to the
    registry (v1.1.1 servers store both parents).
    """
    from .client import VaultClient
    from . import sync
    from .merge import find_merge_base, three_way_tree_merge, tree_is_flat, summarize_changes

    ctx = click.get_current_context(silent=True)
    repo_root = ensure_repo()
    cfg = load_config(repo_root)
    idx = Index(repo_root)

    head_content = (repo_root / ".av" / "HEAD").read_text().strip()
    if not head_content.startswith("ref: refs/heads/"):
        fail(ctx, "validation", "HEAD is detached — check out a branch before merging.")
    branch = head_content.split("refs/heads/", 1)[1]
    our_ref_path = repo_root / ".av" / "refs" / "heads" / branch
    ours = our_ref_path.read_text().strip() if our_ref_path.exists() else ""
    if not ours:
        fail(ctx, "validation", f"Branch '{branch}' has no commits yet — commit first.")
    if policy_ours and policy_theirs:
        fail(ctx, "validation", "--ours and --theirs are mutually exclusive.")

    # Promotion guardrail (v1.2.0): a policy armed for the CURRENT branch is evaluated
    # against OUR latest metrics before any merge lands on it. --force bypasses.
    if not force:
        from .cmd_policy import _latest_metrics_for_ref, enforce_policy

        enforce_policy(
            repo_root, branch,
            candidate_metrics=_latest_metrics_for_ref(repo_root, "HEAD"),
            baseline_metrics_fn=lambda ref: _latest_metrics_for_ref(repo_root, ref),
            candidate_ref="HEAD",  # v1.2.5: require_signature, same "ours" semantics as above
        )

    heads_dir = repo_root / ".av" / "refs" / "heads"

    def _resolve_target() -> str | None:
        if (heads_dir / target).exists():
            return (heads_dir / target).read_text().strip()
        try:
            resolved = find_commit_file(repo_root, target)
            return resolved.stem
        except FileNotFoundError:
            return None
        except AmbiguousCommitHash as exc:
            fail(ctx, "validation", exc.message)

    theirs = _resolve_target()
    client = VaultClient(cfg.get("remote_url", "http://localhost:8000"), cfg.get("remote_api_token"))
    if theirs is None and client.server_available():
        row = client.get_commit(target)
        if row:
            data = sync.normalize_commit_row(row)
            sync.write_fetched_commit(repo_root, data)
            theirs = data["hash"]
    if not theirs:
        fail(ctx, "validation", f"Branch or commit '{target}' not found.")
    if theirs == ours:
        if output_is_json(ctx):
            emit_json(ctx, "merge", data={"merged": False, "reason": "up_to_date", "tip": ours})
            return
        click.secho("Already up to date.")
        return

    load = lambda h: sync.load_local_commit(repo_root, h)

    # Make sure both sides' full trees are readable locally before computing anything;
    # fetched remote history (via av pull) already lands here, but a hand-given hash may not.
    for h in {theirs}:
        if load(h) is None and client.server_available():
            row = client.get_commit(h)
            if row:
                sync.write_fetched_commit(repo_root, sync.normalize_commit_row(row))

    base = find_merge_base(load, ours, theirs)
    if base == theirs:
        if output_is_json(ctx):
            emit_json(ctx, "merge", data={"merged": False, "reason": "up_to_date", "tip": ours})
            return
        click.secho("Already up to date.")
        return

    dirty = _collect_dirty_paths(repo_root, idx)
    fast_forward = base == ours
    if fast_forward and not no_ff and not dirty:
        tip_data = load(theirs) or {}
        tree = tip_data.get("tree", {})
        sync.ensure_objects_local(repo_root, client, tree)
        _materialize_tree(repo_root, client, tree, idx)
        atomic_write_text(our_ref_path, theirs)
        if output_is_json(ctx):
            emit_json(ctx, "merge", data={
                "merged": True, "fast_forward": True, "branch": branch,
                "from": ours, "to": theirs,
            })
            return
        click.secho(f"Fast-forwarded {branch}: {ours[:7]} → {theirs[:7]}", fg="green")
        return

    if dirty:
        if not output_is_json(ctx):
            click.secho(
                "Error: You have uncommitted changes that would be overwritten by merge:",
                fg="red",
            )
            for d in dirty[:20]:
                click.echo(f"  {d}")
            click.secho("Commit them (or stash them), then merge again.", fg="yellow")
        fail(ctx, "validation",
             "You have uncommitted changes that would be overwritten by merge.",
             data={"dirty": dirty[:20], "remediation": ["av commit -m ...", "av stash"]},
             quiet_text=True)

    base_tree = (load(base) or {}).get("tree", {}) if base else {}
    ours_tree = (load(ours) or {}).get("tree", {})
    theirs_data = load(theirs) or {}
    theirs_tree = theirs_data.get("tree", {})

    if not all(tree_is_flat(t) for t in (base_tree, ours_tree, theirs_tree)):
        fail(ctx, "validation",
             "Merge targets a legacy-format commit ({code/artifacts} tree); "
             "only unified flat-tree commits (post-PR #8) can be merged.")

    merged, conflicts = three_way_tree_merge(base_tree, ours_tree, theirs_tree)
    if conflicts and not (policy_ours or policy_theirs):
        # v1.2.5: run attribution on the conflict path too, matching pull's divergence
        # message — reuses _tip_run_id so an agent sees WHICH runs collided, not just
        # which files. See the "Three real bugs" note in the V1.2.5 plan: this path used
        # to return None (exit 0) despite EXIT_CONFLICT=14 being documented for it.
        ours_run = _tip_run_id(repo_root, ours)
        theirs_run = _tip_run_id(repo_root, theirs)
        remediation = [
            f"av merge {target} --ours     # keep this branch's versions",
            f"av merge {target} --theirs   # take the target's versions",
        ]
        if not output_is_json(ctx):
            click.secho(
                f"Merge conflicts in {len(conflicts)} file(s) — both branches changed them "
                "differently. Nothing was modified. Resolve with:",
                fg="red",
            )
            for p in conflicts[:20]:
                click.echo(f"  {p}")
            if len(conflicts) > 20:
                click.echo(f"  … and {len(conflicts) - 20} more")
            click.secho(
                '  av merge <target> --ours     keep this branch\'s versions\n'
                '  av merge <target> --theirs   take the target\'s versions',
                fg="yellow",
            )
            if ours_run or theirs_run:
                fmt = lambda rid: f"run:{rid[:8]}…" if rid else "(no run)"
                click.echo(f"  ours   [{ours[:7]}] belongs to {fmt(ours_run)}")
                click.echo(f"  theirs [{theirs[:7]}] belongs to {fmt(theirs_run)}")

        # v1.3.0 (todo.md item 2): structured conflict report, always written locally
        # (never lost even if this exact stdout is never re-read) plus optionally to a
        # caller-chosen path — same fields as error.data below, so JSON and file agree.
        import datetime as _dt

        report = {
            "conflicts": conflicts, "conflict_count": len(conflicts),
            "ours": ours, "theirs": theirs, "target": target, "branch": branch,
            "ours_run_id": ours_run, "theirs_run_id": theirs_run,
            "remediation": remediation,
            "generated_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        }
        default_report_path = repo_root / ".av" / "last_conflict.json"
        atomic_write_text(default_report_path, json.dumps(report, indent=2))
        report_paths = [str(default_report_path)]
        if conflict_report_path:
            Path(conflict_report_path).write_text(json.dumps(report, indent=2), encoding="utf-8")
            report_paths.append(str(conflict_report_path))
        if not output_is_json(ctx):
            click.secho(f"  Conflict report written: {default_report_path}", fg="cyan")

        fail(ctx, "merge_conflict",
             f"Merge conflicts in {len(conflicts)} file(s) — both branches changed them "
             "differently. Nothing was modified.",
             data={
                 "conflicts": conflicts[:20], "conflict_count": len(conflicts),
                 "ours": ours, "theirs": theirs,
                 "ours_run_id": ours_run, "theirs_run_id": theirs_run,
                 "remediation": remediation,
                 "report_paths": report_paths,
             }, quiet_text=True)

    policy_side = ours_tree if policy_ours else theirs_tree
    resolved_conflicts = 0
    if conflicts:
        for p in conflicts:
            entry = policy_side.get(p)
            if entry is None:
                merged.pop(p, None)
            else:
                merged[p] = entry
        resolved_conflicts = len(conflicts)

    sync.ensure_objects_local(repo_root, client, merged)
    _materialize_tree(repo_root, client, merged, Index(repo_root))

    head_path = repo_root / ".av" / "HEAD"
    commit_data: dict = {
        "parents": [ours, theirs],
        "author": os.environ.get("AV_AUTHOR", "anonymous"),
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "message": message or f"Merge {target} into {branch}",
        "tree": merged,
        "tags": [],
        "metrics": {},
        "project_id": cfg["project_id"],
        "project_name": cfg["project_name"],
    }
    finalize_result: dict = {}
    merge_hash = _finalize_commit(
        repo_root, cfg, client,
        commit_data=commit_data, tree=merged, ref_path=our_ref_path,
        head_path=head_path, idx=Index(repo_root),
        # In JSON mode, suppress _finalize_commit's own human echo — this command emits
        # ONE envelope covering both the merge and the resulting commit's queue outcome.
        result_sink=(finalize_result.update if output_is_json(ctx) else None),
    )

    added, removed, changed = summarize_changes(ours_tree, merged)
    if output_is_json(ctx):
        emit_json(ctx, "merge", data={
            "merged": True, "fast_forward": False, "branch": branch,
            "hash": merge_hash, "parents": [ours, theirs],
            "added": added, "removed": removed, "changed": changed,
            "conflicts_resolved": resolved_conflicts,
            "resolution": ("ours" if policy_ours else "theirs") if resolved_conflicts else None,
            "queued": finalize_result.get("queued", False),
            "queued_reason": finalize_result.get("queued_reason"),
        })
        return
    note = f", {resolved_conflicts} conflict(s) auto-resolved via --{'ours' if policy_ours else 'theirs'}" \
        if resolved_conflicts else ""
    click.secho(
        f"Merged {target} into {branch}: +{added} -{removed} ~{changed} file(s)"
        f"{note} [{merge_hash[:7]}]",
        fg="green",
    )
