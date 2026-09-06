"""av budget — compute/storage/step quotas per run or per lineage (v1.3.1, RSI R3:
todo.md D.17). Exit 17 (`budget_exhausted`) is raised when `av budget consume` reports
any dimension now exceeded.
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
             f"Registry unreachable at {client.server_url} — budgets are server-authoritative.")
    return client


@click.group()
def budget() -> None:
    """Compute/storage/step quotas, scoped to a run or a whole lineage."""


@budget.command("set")
@click.argument("scope_ref")
@click.option("--scope", type=click.Choice(["run", "lineage"]), default="run", show_default=True)
@click.option("--compute-seconds", "compute_seconds_limit", type=float, default=None)
@click.option("--storage-bytes", "storage_bytes_limit", type=int, default=None)
@click.option("--steps", "step_limit", type=int, default=None)
def budget_set(scope_ref: str, scope: str, compute_seconds_limit: float | None,
              storage_bytes_limit: int | None, step_limit: int | None) -> None:
    """Create a budget account for SCOPE_REF (a run id, or a lineage root run id)."""
    repo_root = ensure_repo()
    client = _require_online(repo_root)
    cfg = load_config(repo_root)
    if compute_seconds_limit is None and storage_bytes_limit is None and step_limit is None:
        fail(None, "validation",
             "Provide at least one of --compute-seconds/--storage-bytes/--steps.")
    resp = client.session.post(f"{client.server_url}/api/budgets", json={
        "project_id": cfg["project_id"], "scope": scope, "scope_ref": scope_ref,
        "compute_seconds_limit": compute_seconds_limit,
        "storage_bytes_limit": storage_bytes_limit, "step_limit": step_limit,
    })
    if resp.status_code not in (200, 201):
        fail(None, "validation", f"Registry rejected the budget: {resp.text[:200]}")
    body = resp.json()
    if current_output_mode() == "json":
        emit_json(None, "budget set", data=body)
        return
    click.secho(f"Budget {body['id']} armed for {scope} '{scope_ref}'.", fg="green")


@budget.command("show")
@click.argument("budget_id")
def budget_show(budget_id: str) -> None:
    repo_root = ensure_repo()
    client = _require_online(repo_root)
    resp = client.session.get(f"{client.server_url}/api/budgets/{budget_id}")
    if resp.status_code != 200:
        fail(None, "validation", f"Unknown budget: {budget_id}")
    row = resp.json()
    if current_output_mode() == "json":
        emit_json(None, "budget show", data=row)
        return
    click.secho(f"Budget {row['id']} ({row['scope']}: {row['scope_ref']})", bold=True)
    for dim, used_key, limit_key in (("compute (s)", "compute_seconds_used", "compute_seconds_limit"),
                                     ("storage (bytes)", "storage_bytes_used", "storage_bytes_limit"),
                                     ("steps", "steps_used", "step_limit")):
        limit = row.get(limit_key)
        used = row.get(used_key)
        suffix = f" / {limit}" if limit is not None else " (no limit)"
        click.echo(f"  {dim}: {used}{suffix}")


@budget.command("attach")
@click.argument("budget_id")
@click.option("--run", "run_id", required=True)
def budget_attach(budget_id: str, run_id: str) -> None:
    repo_root = ensure_repo()
    client = _require_online(repo_root)
    resp = client.session.post(f"{client.server_url}/api/runs/{run_id}/budget",
                               json={"budget_id": budget_id})
    if resp.status_code != 200:
        fail(None, "validation", f"Could not attach budget: {resp.text[:200]}")
    if current_output_mode() == "json":
        emit_json(None, "budget attach", data=resp.json())
        return
    click.secho(f"Budget {budget_id} attached to run {run_id}.", fg="green")


@budget.command("consume")
@click.argument("budget_id")
@click.option("--compute-seconds", type=float, default=0)
@click.option("--storage-bytes", type=int, default=0)
@click.option("--steps", type=int, default=0)
def budget_consume(budget_id: str, compute_seconds: float, storage_bytes: int, steps: int) -> None:
    """Record spend against BUDGET_ID. The spend is recorded server-side first either way
    (never lost); if now over any limit, this exits 17 (`budget_exhausted`) with the
    updated budget row in `error.data` so a caller sees what was spent without a second
    round trip."""
    repo_root = ensure_repo()
    client = _require_online(repo_root)
    resp = client.session.post(f"{client.server_url}/api/budgets/{budget_id}/consume", json={
        "compute_seconds": compute_seconds, "storage_bytes": storage_bytes, "steps": steps,
    })
    if resp.status_code != 200:
        fail(None, "validation", f"Could not record spend: {resp.text[:200]}")
    body = resp.json()
    if body["exhausted"]:
        fail(None, "budget_exhausted",
             f"Budget {budget_id} exhausted on: {', '.join(body['exceeded_dims'])} "
             "(the spend above was still recorded).",
             data=body)
    if current_output_mode() == "json":
        emit_json(None, "budget consume", data=body)
        return
    click.secho(f"Recorded. compute={body['compute_seconds_used']} "
               f"storage={body['storage_bytes_used']} steps={body['steps_used']}", fg="green")
