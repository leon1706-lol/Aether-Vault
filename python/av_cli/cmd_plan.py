"""av plan — experiment planner objects (v1.3.1, RSI R3: todo.md D.16).

A plan is a CAS object (hypotheses, ablations, budget, stop rules) — the agent's stated
intent for a run, attachable before OR after `av run start` since real planning happens
both ways. `av plan validate` is a purely local structural check (no network) so an agent
can sanity-check a plan document before ever registering it.
"""
import json

from .core import *  # noqa: F401,F403 -- shared prelude (stdlib + helpers)
from .core import current_output_mode, emit_json, resolve_remote

_EXPECTED_KEYS = ("hypotheses", "ablations", "budget", "stop_rules")


def _client(repo_root):
    from .client import VaultClient

    return VaultClient(*resolve_remote(repo_root))


def _require_online(repo_root):
    client = _client(repo_root)
    if not client.server_available():
        fail(None, "unreachable_queued",
             f"Registry unreachable at {client.server_url} — plans are server-authoritative.")
    return client


def _validate_plan_doc(doc) -> list[str]:
    problems = []
    if not isinstance(doc, dict):
        return ["plan document must be a JSON object"]
    for key in _EXPECTED_KEYS:
        if key not in doc:
            problems.append(f"missing recommended key: {key!r}")
    if "hypotheses" in doc and not isinstance(doc["hypotheses"], list):
        problems.append("'hypotheses' must be a list")
    if "stop_rules" in doc and not isinstance(doc["stop_rules"], list):
        problems.append("'stop_rules' must be a list")
    return problems


@click.group()
def plan() -> None:
    """Experiment plans: hypotheses, ablations, budget, stop rules."""


@plan.command("validate")
@click.argument("file", type=click.Path(exists=True, dir_okay=False))
def plan_validate(file: str) -> None:
    """Structural check only — no network. Missing recommended keys are warnings, not
    hard failures (a plan is a communication tool, not a rigid schema)."""
    try:
        doc = json.loads(Path(file).read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        fail(None, "validation", f"{file} is not valid JSON: {exc}")
    problems = _validate_plan_doc(doc)
    if current_output_mode() == "json":
        emit_json(None, "plan validate", data={"valid": not problems, "problems": problems})
        return
    if problems:
        click.secho("Issues:", fg="yellow")
        for p in problems:
            click.echo(f"  - {p}")
    else:
        click.secho("Plan document looks complete.", fg="green")


@plan.command("create")
@click.argument("file", type=click.Path(exists=True, dir_okay=False))
def plan_create(file: str) -> None:
    """Register FILE as a new plan object."""
    from . import casobj

    repo_root = ensure_repo()
    client = _require_online(repo_root)
    doc = json.loads(Path(file).read_text(encoding="utf-8"))
    object_id = casobj.write_object(repo_root, doc)
    if not client.upload_object(casobj.object_path(repo_root, object_id), object_id):
        fail(None, "unreachable_queued", "Failed to upload the plan object.")

    cfg = load_config(repo_root)
    resp = client.session.post(f"{client.server_url}/api/plans",
                               json={"project_id": cfg["project_id"], "object_id": object_id})
    if resp.status_code not in (200, 201):
        fail(None, "validation", f"Registry rejected the plan: {resp.text[:200]}")
    body = resp.json()
    if current_output_mode() == "json":
        emit_json(None, "plan create", data={**body, "object_id": object_id})
        return
    click.secho(f"Plan {body['id']} created.", fg="green")


@plan.command("show")
@click.argument("plan_id")
def plan_show(plan_id: str) -> None:
    from . import casobj

    repo_root = ensure_repo()
    client = _require_online(repo_root)
    resp = client.session.get(f"{client.server_url}/api/plans/{plan_id}")
    if resp.status_code != 200:
        fail(None, "validation", f"Unknown plan: {plan_id}")
    row = resp.json()
    doc = casobj.read_object(repo_root, row["object_id"])
    if doc is None and client.download_object(row["object_id"],
                                              casobj.object_path(repo_root, row["object_id"])):
        doc = casobj.read_object(repo_root, row["object_id"])
    if current_output_mode() == "json":
        emit_json(None, "plan show", data={**row, "document": doc})
        return
    click.secho(f"Plan {row['id']}", bold=True)
    click.echo(json.dumps(doc, indent=2))


@plan.command("attach")
@click.argument("plan_id")
@click.option("--run", "run_id", required=True)
def plan_attach(plan_id: str, run_id: str) -> None:
    repo_root = ensure_repo()
    client = _require_online(repo_root)
    resp = client.session.post(f"{client.server_url}/api/runs/{run_id}/plan",
                               json={"plan_id": plan_id})
    if resp.status_code != 200:
        fail(None, "validation", f"Could not attach plan: {resp.text[:200]}")
    if current_output_mode() == "json":
        emit_json(None, "plan attach", data=resp.json())
        return
    click.secho(f"Plan {plan_id} attached to run {run_id}.", fg="green")
