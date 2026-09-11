"""Staging surface: config/add/file/unstage/status.

Bodies moved verbatim from main.py (Point-13 split). Patch-target names owned by
main.py (`_find_source_root`, `_update_readme_test_badge`) are accessed late-bound via
`_root.<name>` so test monkeypatching on the main namespace stays effective.
"""

from .core import *  # noqa: F401,F403 -- shared prelude (stdlib + helpers)
from .core import current_output_mode, emit_json  # noqa: E402
from .core import _compute_stage_result  # noqa: E402



@click.command()
@click.argument("value", type=int, required=False, default=None)
@click.option("--remote-url", default=None, help="Set the remote registry URL for this repo.")
@click.option("--name", "project_name", default=None, help="Rename this repo's project (display name only — does not change its project_id).")
def config(value: int | None, remote_url: str | None, project_name: str | None) -> None:
    """Set the LFS threshold in MB, the remote registry URL, and/or the project name.

    Run with no arguments to print the current configuration.
    """
    repo_root = ensure_repo()
    cfg = load_config(repo_root)
    json_mode = current_output_mode() == "json"

    if value is None and remote_url is None and project_name is None:
        if json_mode:
            emit_json(None, "config", data={
                "lfs_threshold_mb": cfg.get("lfs_threshold_mb"),
                "remote_url": cfg.get("remote_url"),
                "project_name": cfg.get("project_name"),
                "project_id": cfg.get("project_id"),
            })
            return
        click.echo(f"LFS threshold : {cfg.get('lfs_threshold_mb')} MB")
        click.echo(f"Remote URL    : {cfg.get('remote_url')}")
        click.echo(f"Project name  : {cfg.get('project_name')}")
        click.echo(f"Project ID    : {cfg.get('project_id')}")
        return

    changed = {}
    if value is not None:
        cfg["lfs_threshold_mb"] = value
        changed["lfs_threshold_mb"] = value
        if not json_mode:
            click.secho(f"Configured LFS threshold to {value} MB", fg="green")
    if remote_url is not None:
        cfg["remote_url"] = remote_url
        changed["remote_url"] = remote_url
        if not json_mode:
            click.secho(f"Configured remote URL to {remote_url}", fg="green")
    if project_name is not None:
        cfg["project_name"] = project_name
        changed["project_name"] = project_name
        if not json_mode:
            click.secho(f"Configured project name to {project_name}", fg="green")

    save_config(repo_root, cfg)
    if json_mode:
        emit_json(None, "config", data={"changed": changed})


@click.command()
@click.argument("paths", nargs=-1, type=click.Path(exists=True))
@click.option("--threads", type=int, default=None,
              help="Worker threads for hashing/staging multiple files (default: auto, "
                   "sized to CPU count; also settable via AV_THREADS or .av/config "
                   "\"threads\"). 1 forces the old single-threaded code path.")
def add(paths: tuple, threads: int | None) -> None:
    """Add files (or directories) to the staging index."""
    repo_root = ensure_repo()
    idx = Index(repo_root)
    cfg = load_config(repo_root)
    threshold_bytes = cfg.get("lfs_threshold_mb", 50) * 1024 * 1024

    files_to_process: list[Path] = []
    for p in paths:
        path_obj = Path(p).resolve()
        if path_obj.is_file():
            files_to_process.append(path_obj)
        elif path_obj.is_dir():
            files_to_process.extend(iter_working_files(path_obj))

    from . import attributes

    attr_rules = attributes.load_attributes(repo_root)

    # (rel_path, fpath, file_type, existing_entry, attr_flags) for every path actually
    # worth considering -- pointer files never get staged. Built once, in the exact
    # deterministic order files_to_process already has (user's arg order, sorted within
    # each expanded directory -- see iter_working_files), so applying results in this same
    # order later is independent of how many threads computed them or which finished first.
    work: list[tuple[str, Path, str, dict | None, set]] = []
    for fpath in files_to_process:
        if is_pointer_file(fpath):
            continue
        rel_path = str(fpath.relative_to(repo_root)).replace("\\", "/")
        work.append((
            rel_path, fpath, idx.classify_file(rel_path), idx.get_entry(rel_path),
            attributes.flags_for(attr_rules, rel_path),
        ))

    resolved_threads = configure_native_threads(repo_root, threads)
    pool_size = python_pool_size(resolved_threads)

    results: list[dict | None]
    if resolved_threads == 1 or len(work) < 2 or pool_size <= 1:
        # AV_THREADS=1 (or a single/no file) takes the literal old sequential path -- not
        # just "a pool of one" -- so a threading-suspected bug can be isolated by comparing
        # against this exact code path.
        results = [
            _compute_stage_result(repo_root, threshold_bytes, fpath, rel_path, file_type, existing, flags)
            for rel_path, fpath, file_type, existing, flags in work
        ]
    else:
        # ThreadPoolExecutor.map returns results in INPUT order regardless of completion
        # order -- exactly what makes the apply loop below deterministic across thread
        # counts and runs. Workers only do _compute_stage_result's pure per-file work
        # (hash/split/chunk/CAS-write); nothing here touches `idx` or prints until the
        # serial apply loop below.
        with ThreadPoolExecutor(max_workers=pool_size) as pool:
            results = list(pool.map(
                lambda item: _compute_stage_result(
                    repo_root, threshold_bytes, item[1], item[0], item[2], item[3], item[4]
                ),
                work,
            ))

    any_changed = False
    json_staged: list[dict] = []
    for result in results:
        if result is None:
            continue
        any_changed = True
        apply_stage_result(idx, result)
        json_staged.append({
            "path": result["rel_path"],
            "type": result["file_type"],
            "hash": result["hash"],
            "size": result["size"],
        })

    if any_changed:
        idx.save()

    if current_output_mode() == "json":
        emit_json(None, "add", data={"staged": json_staged, "count": len(json_staged)})


_AVIGNORE_TEMPLATE = """\
# Aether-Vault ignore patterns — one glob per line, # for comments.
# Examples (uncomment or add your own):
# venv/
# __pycache__/
# node_modules/
# *.log
"""


@click.command()
@click.option("--avignore", "make_avignore", is_flag=True, default=False,
              help="Generate a .avignore template in the repo root.")
@click.option("--avattributes", "make_avattributes", is_flag=True, default=False,
              help="Generate a .avattributes template (per-path staging directives, "
                   "e.g. no-chunk / no-layer-split) in the repo root.")
def file(make_avignore: bool, make_avattributes: bool) -> None:
    """Generate scaffold files (.avignore, .avattributes) in the repo root.

    Each kind of generated file is its own flag, so more can be added later without
    restructuring this command.
    """
    from .attributes import ATTRIBUTES_TEMPLATE

    repo_root = ensure_repo()
    json_mode = current_output_mode() == "json"
    result: dict = {}

    if not make_avignore and not make_avattributes:
        if json_mode:
            emit_json(None, "file", data={"generated": {}})
            return
        click.secho(
            "Nothing to do — pass a flag, e.g. `av file --avignore` or `av file --avattributes`.",
            fg="yellow",
        )
        return

    if make_avignore:
        avignore_path = repo_root / ".avignore"
        if avignore_path.exists():
            result["avignore"] = "already_exists"
            if not json_mode:
                click.secho(f".avignore already exists at {avignore_path} — not overwriting.", fg="yellow")
        else:
            avignore_path.write_text(_AVIGNORE_TEMPLATE, encoding="utf-8")
            result["avignore"] = "written"
            if not json_mode:
                click.secho(f"Wrote {avignore_path}", fg="green")

    if make_avattributes:
        attrs_path = repo_root / ".avattributes"
        if attrs_path.exists():
            result["avattributes"] = "already_exists"
            if not json_mode:
                click.secho(f".avattributes already exists at {attrs_path} — not overwriting.", fg="yellow")
        else:
            attrs_path.write_text(ATTRIBUTES_TEMPLATE, encoding="utf-8")
            result["avattributes"] = "written"
            if not json_mode:
                click.secho(f"Wrote {attrs_path}", fg="green")

    if json_mode:
        emit_json(None, "file", data={"generated": result})


@click.command()
@click.argument("paths", nargs=-1)
def unstage(paths: tuple) -> None:
    """Unstage files staged by `av add`, without touching the working tree.

    Reverts each staged index entry back to its last-committed state (so it correctly shows up
    as "modified" again, or as untracked if it was never committed) — like `git reset` / `git
    restore --staged`, this only ever touches the index, never the working-tree files.
    """
    repo_root = ensure_repo()
    idx = Index(repo_root)
    json_mode = current_output_mode() == "json"

    staged = idx.get_staged_entries()
    if not staged:
        if json_mode:
            emit_json(None, "unstage", data={"unstaged": [], "reason": "nothing_staged"})
            return
        click.secho("Nothing staged to unstage", fg="yellow")
        return

    if paths:
        rel_paths = []
        for p in paths:
            try:
                rel = str(Path(p).resolve().relative_to(repo_root)).replace("\\", "/")
            except ValueError:
                continue
            if rel in staged:
                rel_paths.append(rel)
        if not rel_paths:
            if json_mode:
                emit_json(None, "unstage", data={"unstaged": [], "reason": "none_matched"})
                return
            click.secho("None of the given paths are staged", fg="yellow")
            return
    else:
        rel_paths = list(staged.keys())

    head_tree = resolve_head_tree(repo_root)
    for rel_path in rel_paths:
        entry = idx.entries[rel_path]
        head_data = head_tree.get(rel_path)
        if head_data:
            # Was already tracked before this staging — revert the index entry to HEAD's
            # data (mtime_ns=0 deliberately never matches a real file's stat, so `av status`
            # correctly reports it as "modified" again rather than silently looking clean).
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
        else:
            # Never committed — unstaging makes it untracked again. The `.av-pointer` is pure
            # bookkeeping `add()` created, not user data, so it's removed; the real working-tree
            # file (if any) is never touched.
            pointer = entry.get("pointer")
            if pointer:
                ptr_path = repo_root / pointer
                if ptr_path.exists() and ptr_path.is_file():
                    ptr_path.unlink()
            del idx.entries[rel_path]

    idx.save()
    if json_mode:
        emit_json(None, "unstage", data={"unstaged": rel_paths})
        return
    click.secho(f"Unstaged {len(rel_paths)} file(s):", fg="green")
    for rel_path in rel_paths:
        click.echo(f"  {rel_path}")


@click.command()
def status() -> None:
    """Show the working tree status."""
    repo_root = ensure_repo()
    idx = Index(repo_root)

    head_path = repo_root / ".av" / "HEAD"
    branch = "detached"
    if head_path.exists():
        head_content = head_path.read_text().strip()
        if head_content.startswith("ref: refs/heads/"):
            branch = head_content.split("/")[-1]

    staged, modified, deleted, untracked = compute_status(repo_root, idx)

    if current_output_mode() == "json":
        emit_json(
            None,
            "status",
            data={
                "branch": branch,
                "staged": sorted(staged),
                "modified": sorted(modified),
                "deleted": sorted(deleted),
                "untracked": sorted(untracked),
            },
        )
        return

    click.secho(f"On branch {branch}\n", bold=True)

    if staged:
        click.secho("Changes to be committed:", fg="green")
        for f in staged:
            click.echo(f"  modified: {f}")
        click.echo("")
    if modified:
        click.secho("Changes not staged for commit:", fg="yellow")
        for f in modified:
            click.echo(f"  modified: {f}")
        click.echo("")
    if deleted:
        click.secho("Deleted files:", fg="red")
        for f in deleted:
            click.echo(f"  deleted:  {f}")
        click.echo("")
    if untracked:
        click.secho("Untracked files:", fg="red")
        for f in untracked:
            click.echo(f"  {f}")
        click.echo("")

    if not staged and not modified and not deleted and not untracked:
        click.secho("Nothing to commit, working tree clean", fg="green")
