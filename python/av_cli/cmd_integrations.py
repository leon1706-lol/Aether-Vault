"""Integrations: graph/handoff/webui/plugin-import commands.

Bodies moved verbatim from main.py (Point-13 split). Patch-target names owned by
main.py (`_find_source_root`, `_update_readme_test_badge`) are accessed late-bound via
`_root.<name>` so test monkeypatching on the main namespace stays effective.
"""

from .core import *  # noqa: F401,F403 -- shared prelude (stdlib + helpers)
from . import main as _root



@click.command()
@click.option("--update", is_flag=True, help="Regenerate and update the markdown vault.")
def graph(update: bool) -> None:
    """Generate or update a markdown vault of code dependencies for Obsidian."""
    repo_root = ensure_repo()
    vault_dir = repo_root / "Aether-Graph"
    
    if update or not vault_dir.exists():
        click.echo(f"Generating graph vault at {vault_dir}...")
        vault_dir.mkdir(exist_ok=True)
        from .graph import generate_full_graph
        generate_full_graph(repo_root, vault_dir)
        click.secho("Graph vault updated successfully.", fg="green")
    
    if not update:
        click.secho(f"To visualize, open the following folder as a vault in Obsidian:\n  {vault_dir.resolve()}", fg="cyan")
        import webbrowser
        import urllib.parse
        encoded_path = urllib.parse.quote(str(vault_dir.resolve()))
        obsidian_uri = f"obsidian://open?path={encoded_path}"
        try:
            webbrowser.open(obsidian_uri)
            click.echo("Attempted to launch Obsidian.")
        except Exception:
            pass


@click.group(invoke_without_command=True)
@click.option("--update", is_flag=True, help="Update the existing handoff.avh with the latest repo state.")
@click.option("--note", default=None, help="Freeform agent instruction text.")
@click.option("--instructions-file", type=click.Path(exists=True), default=None, help="Read agent instructions from a file.")
@click.option("--diff-weights", is_flag=True, help="Include a per-layer weight-diff against the parent commit.")
@click.option("--since", default=None, help="Diff weights/metrics against this commit hash instead of the direct parent.")
@click.option("--with-memory/--no-memory", default=True, show_default=True,
              help="Include the agent context-memory layer (notes + metric trend).")
@click.pass_context
def handoff(ctx: click.Context, update: bool, note: str | None, instructions_file: str | None, diff_weights: bool, since: str | None, with_memory: bool) -> None:
    """Generate (or update) a .avh agent handoff snapshot and a Markdown log entry in Aether-Handoff/."""
    if ctx.invoked_subcommand is not None:
        return

    repo_root = ensure_repo()
    from .handoff import generate_handoff
    note_text = Path(instructions_file).read_text() if instructions_file else note
    avh_path, md_path = generate_handoff(
        repo_root, update=update, agent_instructions=note_text, diff_weights=diff_weights,
        since=since, with_memory=with_memory,
    )
    click.secho(f"Handoff snapshot written: {avh_path.relative_to(repo_root)}", fg="green")
    click.secho(f"Markdown log entry: {md_path.relative_to(repo_root)}", fg="cyan")


@handoff.command("init")
def handoff_init() -> None:
    """Create the Aether-Handoff/ folder structure without generating a snapshot."""
    repo_root = ensure_repo()
    from .handoff import init_handoff_dir
    vault_dir = init_handoff_dir(repo_root)
    click.secho(f"{vault_dir.relative_to(repo_root)}/ initialized.", fg="green")


@handoff.command("log")
def handoff_log() -> None:
    """List all handoff snapshots in chronological order."""
    repo_root = ensure_repo()
    snapshots_dir = repo_root / "Aether-Handoff" / "snapshots"
    if not snapshots_dir.exists():
        click.secho("No handoff snapshots yet. Run `av handoff` first.", fg="yellow")
        return

    entries = sorted(snapshots_dir.glob("*.md"))
    if not entries:
        click.secho("No handoff snapshots yet. Run `av handoff` first.", fg="yellow")
        return

    for entry in entries:
        click.echo(entry.stem)


@handoff.command("show")
@click.argument("snapshot_id")
def handoff_show(snapshot_id: str) -> None:
    """Print a previously generated handoff Markdown note."""
    repo_root = ensure_repo()
    snapshots_dir = repo_root / "Aether-Handoff" / "snapshots"
    matches = sorted(snapshots_dir.glob(f"{snapshot_id}*.md")) if snapshots_dir.exists() else []

    if not matches:
        click.secho(f"No snapshot found matching '{snapshot_id}'.", fg="red")
        return

    click.echo(matches[-1].read_text(encoding="utf-8"))


@click.command("webui")
@click.option(
    "--rebuild", is_flag=True, default=False,
    help="Force a fresh Docker image build even if the container is already running.",
)
def webui_cmd(rebuild: bool) -> None:
    """Start the Aether-Vault Web UI dashboard and open it in the browser.

    \b
    1. Checks that Docker is running
    2. Starts the aether-vault-webui container (+ deps) via docker-compose
    3. Waits for the UI to become ready
    4. Opens http://localhost:3000 in the default browser

    If the container is already running and healthy, this skips straight to opening the
    browser instead of re-running compose (use --rebuild to force a fresh image build after
    changing webui source code).
    """
    from . import docker_runtime

    result = docker_runtime.ensure_local_backend_running(
        _root._find_source_root(), open_browser=True, rebuild=rebuild,
    )
    if result.success:
        suffix = (
            "" if result.already_running
            else "\n   (Use `av webui --rebuild` if you changed webui source and need a fresh image.)"
        )
        click.secho(
            f"\n[OK] Aether-Vault Web UI is running at {result.backend_url}\n"
            "   Press Ctrl+C or run 'docker compose down' to stop." + suffix,
            fg="green",
            bold=True,
        )


@click.command("import-lightning")
@click.argument("checkpoint_path", type=click.Path(exists=True))
@click.option("--tag", default=None, help="Additional tag to attach to the imported commit.")
def import_lightning(checkpoint_path: str, tag: str | None) -> None:
    """Backfill a pre-existing PyTorch Lightning checkpoint not captured live by the callback."""
    repo_root = ensure_repo()
    from av_plugins.lightning import import_checkpoint
    import_checkpoint(checkpoint_path, repo_root=repo_root, tag=tag)
    click.secho(f"Imported Lightning checkpoint: {checkpoint_path}", fg="green")


@click.command("import-transformers")
@click.argument("checkpoint_dir", type=click.Path(exists=True, file_okay=False))
@click.option("--tag", default=None, help="Additional tag to attach to the imported commit.")
def import_transformers(checkpoint_dir: str, tag: str | None) -> None:
    """Backfill a pre-existing HuggingFace Transformers checkpoint directory."""
    repo_root = ensure_repo()
    from av_plugins.transformers import import_checkpoint
    import_checkpoint(checkpoint_dir, repo_root=repo_root, tag=tag)
    click.secho(f"Imported Transformers checkpoint: {checkpoint_dir}", fg="green")


@click.command("import-mlflow")
@click.argument("run_id")
@click.option("--tracking-uri", default=None, help="MLflow tracking URI (defaults to MLflow's own resolution).")
@click.option("--tag", default=None, help="Additional tag to attach to the imported commit.")
def import_mlflow(run_id: str, tracking_uri: str | None, tag: str | None) -> None:
    """Import an existing MLflow run's artifacts and metrics as a new Aether-Vault commit."""
    repo_root = ensure_repo()
    from av_plugins.mlflow import import_run
    import_run(run_id, repo_root=repo_root, tracking_uri=tracking_uri, tag=tag)
    click.secho(f"Imported MLflow run: {run_id}", fg="green")
