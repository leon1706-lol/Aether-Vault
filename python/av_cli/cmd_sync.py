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

    target = Path(directory).resolve() if directory else Path.cwd() / project
    if target.exists() and any(target.iterdir()):
        click.secho(f"Error: '{target}' already exists and is not empty.", fg="red")
        return

    url = remote_url or os.environ.get("AV_REMOTE_URL") or "http://localhost:8000"
    api_token = token or os.environ.get("AV_API_TOKEN")
    client = VaultClient(url, api_token)
    if not client.server_available():
        click.secho(f"Error: Registry unreachable at {url} — is the backend running?", fg="red")
        return

    try:
        proj = sync.resolve_project(client, project)
    except ValidationError as exc:
        click.secho(f"Error: {exc.message}", fg="red")
        return

    pid = proj["project_id"]
    refs = client.list_refs(project_id=pid)
    branch = sync.pick_default_branch(refs, pid)
    commits = sync.fetch_project_commits(client, pid)
    if not commits:
        click.secho(f"Error: Project '{proj.get('project_name')}' has no commits yet.", fg="red")
        return
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

    msg = f"Cloned '{proj.get('project_name')}' ({len(commits)} commit(s)) into {target}"
    click.secho(msg, fg="green")
    detail = f"  branch {branch} @ [{tip_hash[:7]}]"
    if downloaded:
        detail += f" — downloaded {downloaded} object(s)"
    click.echo(detail)


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

    repo_root = ensure_repo()
    cfg = load_config(repo_root)

    head_content = (repo_root / ".av" / "HEAD").read_text().strip()
    if not head_content.startswith("ref: refs/heads/"):
        click.secho("Error: HEAD is detached — check out a branch before pulling.", fg="red")
        return
    branch = head_content.split("refs/heads/", 1)[1]
    ref_path = repo_root / ".av" / "refs" / "heads" / branch
    local_tip = ref_path.read_text().strip() if ref_path.exists() else ""

    client = VaultClient(cfg.get("remote_url", "http://localhost:8000"), cfg.get("remote_api_token"))
    if not client.server_available():
        click.secho(f"Error: Registry unreachable at {cfg.get('remote_url')}.", fg="red")
        return

    remote_ref = f"{cfg['project_id']}/{branch}"
    remote_tip = client.get_ref(remote_ref)
    if not remote_tip:
        click.secho(f"No remote branch '{branch}' on the registry — nothing to pull.", fg="yellow")
        return
    if remote_tip == local_tip:
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
            click.secho(
                f"Error: Remote history is broken — commit {cursor[:7]}… is referenced but "
                "missing from the registry.",
                fg="red",
            )
            return
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
        click.secho(
            f"Local and remote '{branch}' have diverged.\n"
            f"The remote tip [{remote_tip[:7]}] and its history are now local — resolve with:\n"
            f"  av merge {remote_tip[:7]}",
            fg="yellow",
        )
        return

    idx = Index(repo_root)
    if not force:
        dirty = _collect_dirty_paths(repo_root, idx)
        if dirty:
            click.secho(
                "Error: You have uncommitted changes that would be overwritten by pull:",
                fg="red",
            )
            for d in dirty[:20]:
                click.echo(f"  {d}")
            click.secho("Commit them (or use --force to discard), then pull again.", fg="yellow")
            return

    tip_data = sync.load_local_commit(repo_root, remote_tip)
    tree = tip_data.get("tree", {}) if tip_data else {}
    sync.ensure_objects_local(repo_root, client, tree)
    _materialize_tree(repo_root, client, tree, idx)
    atomic_write_text(ref_path, remote_tip)

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
def merge(target: str, message: str | None, policy_ours: bool, policy_theirs: bool,
          no_ff: bool, force: bool) -> None:
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

    repo_root = ensure_repo()
    cfg = load_config(repo_root)
    idx = Index(repo_root)

    head_content = (repo_root / ".av" / "HEAD").read_text().strip()
    if not head_content.startswith("ref: refs/heads/"):
        click.secho("Error: HEAD is detached — check out a branch before merging.", fg="red")
        return
    branch = head_content.split("refs/heads/", 1)[1]
    our_ref_path = repo_root / ".av" / "refs" / "heads" / branch
    ours = our_ref_path.read_text().strip() if our_ref_path.exists() else ""
    if not ours:
        click.secho(f"Error: Branch '{branch}' has no commits yet — commit first.", fg="red")
        return
    if policy_ours and policy_theirs:
        click.secho("Error: --ours and --theirs are mutually exclusive.", fg="red")
        return

    # Promotion guardrail (v1.2.0): a policy armed for the CURRENT branch is evaluated
    # against OUR latest metrics before any merge lands on it. --force bypasses.
    if not force:
        from .cmd_policy import _latest_metrics_for_ref, enforce_policy

        enforce_policy(
            repo_root, branch,
            candidate_metrics=_latest_metrics_for_ref(repo_root, "HEAD"),
            baseline_metrics_fn=lambda ref: _latest_metrics_for_ref(repo_root, ref),
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
            click.secho(f"Error: {exc.message}", fg="red")
            return None

    theirs = _resolve_target()
    client = VaultClient(cfg.get("remote_url", "http://localhost:8000"), cfg.get("remote_api_token"))
    if theirs is None and client.server_available():
        row = client.get_commit(target)
        if row:
            data = sync.normalize_commit_row(row)
            sync.write_fetched_commit(repo_root, data)
            theirs = data["hash"]
    if not theirs:
        click.secho(f"Error: Branch or commit '{target}' not found.", fg="red")
        return
    if theirs == ours:
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
        click.secho(f"Fast-forwarded {branch}: {ours[:7]} → {theirs[:7]}", fg="green")
        return

    if dirty:
        click.secho(
            "Error: You have uncommitted changes that would be overwritten by merge:",
            fg="red",
        )
        for d in dirty[:20]:
            click.echo(f"  {d}")
        click.secho("Commit them (or stash them), then merge again.", fg="yellow")
        return

    base_tree = (load(base) or {}).get("tree", {}) if base else {}
    ours_tree = (load(ours) or {}).get("tree", {})
    theirs_data = load(theirs) or {}
    theirs_tree = theirs_data.get("tree", {})

    if not all(tree_is_flat(t) for t in (base_tree, ours_tree, theirs_tree)):
        click.secho(
            "Error: Merge targets a legacy-format commit ({code/artifacts} tree); "
            "only unified flat-tree commits (post-PR #8) can be merged.",
            fg="red",
        )
        return

    merged, conflicts = three_way_tree_merge(base_tree, ours_tree, theirs_tree)
    if conflicts and not (policy_ours or policy_theirs):
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
        return

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
    merge_hash = _finalize_commit(
        repo_root, cfg, client,
        commit_data=commit_data, tree=merged, ref_path=our_ref_path,
        head_path=head_path, idx=Index(repo_root),
    )

    added, removed, changed = summarize_changes(ours_tree, merged)
    note = f", {resolved_conflicts} conflict(s) auto-resolved via --{'ours' if policy_ours else 'theirs'}" \
        if resolved_conflicts else ""
    click.secho(
        f"Merged {target} into {branch}: +{added} -{removed} ~{changed} file(s)"
        f"{note} [{merge_hash[:7]}]",
        fg="green",
    )
