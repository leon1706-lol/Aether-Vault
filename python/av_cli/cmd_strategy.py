"""av strategy — searchable cross-lineage record of what worked/failed (v1.3.1, RSI R4:
todo.md E.22). Beyond `.avh` context-memory notes (per-repo, not cross-run-queryable).
"""
import json

from .core import *  # noqa: F401,F403 -- shared prelude (stdlib + helpers)
from .core import current_output_mode, emit_json, resolve_remote


def _client(repo_root):
    from .client import VaultClient

    return VaultClient(*resolve_remote(repo_root))


def _require_online(repo_root):
    client = _client(repo_root)
    if not client.server_available():
        fail(None, "unreachable_queued",
             f"Registry unreachable at {client.server_url} — strategy memory is server-authoritative.")
    return client


@click.group()
def strategy() -> None:
    """Searchable cross-lineage record: technique, hyperparameters, data mix, outcome."""


@strategy.command("add")
@click.argument("technique")
@click.option("--outcome", type=click.Choice(["worked", "failed", "inconclusive"]), required=True)
@click.option("--hyperparameters", default=None, help="JSON object string.")
@click.option("--data-mix", default=None, help="JSON object string.")
@click.option("--run", "run_ids", multiple=True, help="Run id this entry is evidenced by (repeatable).")
def strategy_add(technique: str, outcome: str, hyperparameters: str | None, data_mix: str | None,
                 run_ids: tuple) -> None:
    repo_root = ensure_repo()
    cfg = load_config(repo_root)
    client = _require_online(repo_root)
    try:
        hp = json.loads(hyperparameters) if hyperparameters else None
        dm = json.loads(data_mix) if data_mix else None
    except json.JSONDecodeError as exc:
        fail(None, "validation", f"--hyperparameters/--data-mix must be valid JSON: {exc}")
    resp = client.session.post(f"{client.server_url}/api/strategy", json={
        "project_id": cfg["project_id"], "technique": technique, "outcome": outcome,
        "hyperparameters": hp, "data_mix": dm, "run_ids": list(run_ids),
    })
    if resp.status_code not in (200, 201):
        fail(None, "validation", f"Registry rejected the strategy entry: {resp.text[:200]}")
    body = resp.json()
    if current_output_mode() == "json":
        emit_json(None, "strategy add", data=body)
        return
    click.secho(f"Strategy entry {body['id'][:8]} recorded ({outcome}: {technique}).", fg="green")


@strategy.command("search")
@click.option("--technique", default=None)
@click.option("--outcome", type=click.Choice(["worked", "failed", "inconclusive"]), default=None)
@click.option("--q", "query", default=None, help="Substring match over technique names.")
@click.option("--project", "project_id", default=None)
def strategy_search(technique: str | None, outcome: str | None, query: str | None,
                    project_id: str | None) -> None:
    repo_root = ensure_repo()
    cfg = load_config(repo_root)
    client = _require_online(repo_root)
    params = {"project_id": project_id or cfg.get("project_id")}
    if technique:
        params["technique"] = technique
    if outcome:
        params["outcome"] = outcome
    if query:
        params["q"] = query
    resp = client.session.get(f"{client.server_url}/api/strategy", params=params)
    rows = resp.json().get("entries", []) if resp.status_code == 200 else []
    if current_output_mode() == "json":
        emit_json(None, "strategy search", data={"entries": rows})
        return
    for r in rows:
        click.echo(f"  [{r['outcome']:>12}] {r['technique']} ({len(r['run_ids'])} run(s))")


@strategy.command("show")
@click.argument("entry_id")
@click.option("--project", "project_id", default=None)
def strategy_show(entry_id: str, project_id: str | None) -> None:
    """No dedicated GET-by-id endpoint — searches with no filter and picks the match, since
    the table is small enough that a full scan is cheap and this keeps the server surface
    minimal (one query endpoint, not two)."""
    repo_root = ensure_repo()
    cfg = load_config(repo_root)
    client = _require_online(repo_root)
    resp = client.session.get(f"{client.server_url}/api/strategy",
                              params={"project_id": project_id or cfg.get("project_id")})
    rows = resp.json().get("entries", []) if resp.status_code == 200 else []
    match = next((r for r in rows if r["id"] == entry_id), None)
    if not match:
        fail(None, "validation", f"Unknown strategy entry: {entry_id}")
    if current_output_mode() == "json":
        emit_json(None, "strategy show", data=match)
        return
    click.secho(f"{match['technique']} — {match['outcome']}", bold=True)
    click.echo(json.dumps(match, indent=2))
