"""av diff — semantic change summary between HEAD and a ref/commit (v1.2.0)."""

from .core import *  # noqa: F401,F403 -- shared prelude (stdlib + helpers)
from .core import current_output_mode, emit_json


@click.command()
@click.argument("target", required=False, default=None)
@click.option("--from", "base_ref", default=None, help="Compare TARGET against BASE instead of HEAD.")
def diff(target: str | None, base_ref: str | None) -> None:
    """Semantic diff between two refs/commits (defaults: HEAD vs its parent).

    Reports layer-level movement for safetensors models, chunk reuse for CDC-chunked
    checkpoints, dataset changes, and byte totals — not just a file list.
    """
    from .handoff import load_commit, resolve_head
    from .semdiff import diff_trees, human_summary

    repo_root = ensure_repo()

    def _tree_of(ref_or_hash: str | None) -> tuple[str | None, dict]:
        if not ref_or_hash:
            return None, {}
        commit = load_commit(repo_root, ref_or_hash)
        if commit is None:
            # try short-hash resolution through history walking
            head_branch, head_hash = resolve_head(repo_root)
            candidates = []
            commits_dir = repo_root / ".av" / "commits"
            for p in commits_dir.glob("*.json"):
                h = p.stem
                if h.startswith(ref_or_hash):
                    candidates.append(h)
            if len(candidates) == 1:
                commit = load_commit(repo_root, candidates[0])
        if commit is None:
            fail(None, "validation", f"Unknown ref or commit: {ref_or_hash}")
        return commit.get("hash"), commit.get("tree", {})

    _, head_hash = resolve_head(repo_root)
    if base_ref:
        base_hash, old_tree = _tree_of(base_ref)
        new_hash, new_tree = _tree_of(target)
    else:
        head_commit = load_commit(repo_root, head_hash) if head_hash else None
        parent = (head_commit or {}).get("parent_hash")
        base_hash, old_tree = _tree_of(parent)
        new_hash = head_hash
        new_tree = (head_commit or {}).get("tree", {})

    sd = diff_trees(old_tree, new_tree)
    sd["base"] = base_hash
    sd["target"] = new_hash
    sd["summary"] = human_summary(sd)

    if current_output_mode() == "json":
        emit_json(None, "diff", data=sd)
        return

    click.secho(sd["summary"], bold=True)
    for m in sd["models"]:
        click.secho(f"  {m['path']}: {m['layers_changed']}/{m['layers_total']} layers moved "
                    f"({m['pct']:.1%})", fg="cyan")
        for lm in m["largest_moved"]:
            click.echo(f"     - {lm['name']} ({lm['size']} bytes)")
    c = sd["chunks"]
    if c["reused"] or c["new"]:
        click.secho(f"  chunks reused: {c['reused']}, new: {c['new']}", fg="cyan")
    if sd["datasets"]:
        click.secho(f"  datasets touched: {', '.join(sd['datasets'])}", fg="cyan")
