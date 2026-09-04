"""av lineage / av search — causal run graphs + cross-run structured search (v1.3.1,
RSI R4: todo.md E.21/E.24).
"""
from .core import *  # noqa: F401,F403 -- shared prelude (stdlib + helpers)
from .core import current_output_mode, emit_json, resolve_remote


def _client(repo_root):
    from .client import VaultClient

    return VaultClient(*resolve_remote(repo_root))


def _require_online(repo_root):
    client = _client(repo_root)
    if not client.server_available():
        fail(None, "unreachable_queued",
             f"Registry unreachable at {client.server_url} — the causal graph is server-authoritative.")
    return client


@click.group()
def lineage() -> None:
    """Causal run graphs: explicit "this change caused that metric delta" links."""


@lineage.command("link")
@click.option("--cause-type", type=click.Choice(["change_set", "commit"]), required=True)
@click.option("--cause", "cause_ref", required=True, help="A change_set id or commit hash.")
@click.option("--metric", "effect_metric", required=True)
@click.option("--delta", "effect_delta", type=float, default=None)
@click.option("--verified", is_flag=True, default=False,
              help="Mark this link as independently verified, not just agent-claimed.")
def lineage_link(cause_type: str, cause_ref: str, effect_metric: str, effect_delta: float | None,
                 verified: bool) -> None:
    repo_root = ensure_repo()
    cfg = load_config(repo_root)
    client = _require_online(repo_root)
    resp = client.session.post(f"{client.server_url}/api/causal-links", json={
        "project_id": cfg["project_id"], "cause_type": cause_type, "cause_ref": cause_ref,
        "effect_metric": effect_metric, "effect_delta": effect_delta, "verified": verified,
    })
    if resp.status_code not in (200, 201):
        fail(None, "validation", f"Registry rejected the causal link: {resp.text[:200]}")
    body = resp.json()
    if current_output_mode() == "json":
        emit_json(None, "lineage link", data=body)
        return
    click.secho(f"Causal link {body['id']} recorded: {cause_ref[:8]} -> {effect_metric}"
               + (f" ({effect_delta:+g})" if effect_delta is not None else ""), fg="green")


@lineage.command("show")
@click.option("--cause", "cause_ref", default=None)
@click.option("--project", "project_id", default=None)
def lineage_show(cause_ref: str | None, project_id: str | None) -> None:
    repo_root = ensure_repo()
    cfg = load_config(repo_root)
    client = _require_online(repo_root)
    params = {"project_id": project_id or cfg.get("project_id")}
    if cause_ref:
        params["cause_ref"] = cause_ref
    resp = client.session.get(f"{client.server_url}/api/causal-links", params=params)
    rows = resp.json().get("causal_links", []) if resp.status_code == 200 else []
    if current_output_mode() == "json":
        emit_json(None, "lineage show", data={"causal_links": rows})
        return
    for r in rows:
        verified_mark = "✓" if r["verified"] else " "
        click.echo(f"  [{verified_mark}] {r['cause_ref'][:8]} -> {r['effect_metric']}"
                  + (f" ({r['effect_delta']:+g})" if r["effect_delta"] is not None else ""))


@click.group()
def search() -> None:
    """Structured cross-run/lineage queries."""


@search.command("runs")
@click.option("--metric", required=True)
@click.option("--direction", type=click.Choice(["up", "down"]), default="up", show_default=True)
@click.option("--min-delta", type=float, default=0.0, show_default=True)
@click.option("--project", "project_id", default=None)
def search_runs(metric: str, direction: str, min_delta: float, project_id: str | None) -> None:
    """e.g. `av search runs --metric eval_acc --direction up` — every run whose METRIC
    moved DIRECTION relative to its parent run's latest value for that metric."""
    repo_root = ensure_repo()
    cfg = load_config(repo_root)
    client = _require_online(repo_root)
    resp = client.session.get(f"{client.server_url}/api/search/runs", params={
        "project_id": project_id or cfg.get("project_id"), "metric": metric,
        "direction": direction, "min_delta": min_delta,
    })
    if resp.status_code != 200:
        fail(None, "validation", f"Search failed: {resp.text[:200]}")
    matches = resp.json().get("matches", [])
    if current_output_mode() == "json":
        emit_json(None, "search runs", data={"matches": matches})
        return
    if not matches:
        click.secho("No matching runs.", fg="yellow")
        return
    for m in matches:
        click.echo(f"  {m['run_id'][:8]}  {m['metric']}: {m['parent_value']} -> {m['value']} "
                  f"({m['delta']:+g})")
