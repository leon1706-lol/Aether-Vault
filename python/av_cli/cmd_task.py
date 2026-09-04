"""av task — curriculum task/difficulty-ramp proposals (v1.3.1, RSI R2: todo.md B.8)."""
from .core import *  # noqa: F401,F403 -- shared prelude (stdlib + helpers)
from .core import current_output_mode, emit_json, resolve_remote


def _client(repo_root):
    from .client import VaultClient

    return VaultClient(*resolve_remote(repo_root))


def _require_online(repo_root):
    client = _client(repo_root)
    if not client.server_available():
        fail(None, "unreachable_queued",
             f"Registry unreachable at {client.server_url} — tasks are server-authoritative.")
    return client


@click.group()
def task() -> None:
    """Propose and track curriculum tasks / difficulty ramps / held-out probes."""


@task.command("propose")
@click.argument("title")
@click.option("--description", default=None)
@click.option("--difficulty", default=None)
def task_propose(title: str, description: str | None, difficulty: str | None) -> None:
    repo_root = ensure_repo()
    cfg = load_config(repo_root)
    client = _require_online(repo_root)
    resp = client.session.post(f"{client.server_url}/api/tasks", json={
        "project_id": cfg["project_id"], "title": title, "description": description,
        "difficulty": difficulty,
    })
    if resp.status_code not in (200, 201):
        fail(None, "validation", f"Registry rejected the task: {resp.text[:200]}")
    body = resp.json()
    if current_output_mode() == "json":
        emit_json(None, "task propose", data=body)
        return
    click.secho(f"Task '{title}' proposed ({body['id'][:8]}).", fg="green")


@task.command("list")
@click.option("--project", "project_id", default=None)
@click.option("--status", "status_filter", type=click.Choice(["proposed", "accepted", "rejected"]),
              default=None)
def task_list(project_id: str | None, status_filter: str | None) -> None:
    repo_root = ensure_repo()
    cfg = load_config(repo_root)
    client = _require_online(repo_root)
    params = {"project_id": project_id or cfg.get("project_id")}
    if status_filter:
        params["status"] = status_filter
    resp = client.session.get(f"{client.server_url}/api/tasks", params=params)
    rows = resp.json().get("tasks", []) if resp.status_code == 200 else []
    if current_output_mode() == "json":
        emit_json(None, "task list", data={"tasks": rows})
        return
    for r in rows:
        click.echo(f"  [{r['status']:>8}] {r['id'][:8]}  {r['title']}"
                  + (f" (difficulty: {r['difficulty']})" if r.get("difficulty") else ""))


def _set_status(task_id: str, status: str) -> dict:
    repo_root = ensure_repo()
    client = _require_online(repo_root)
    resp = client.session.post(f"{client.server_url}/api/tasks/{task_id}/status",
                               json={"status": status})
    if resp.status_code != 200:
        fail(None, "validation", f"Could not set task {task_id} to {status!r}: {resp.text[:200]}")
    return resp.json()


@task.command("accept")
@click.argument("task_id")
def task_accept(task_id: str) -> None:
    body = _set_status(task_id, "accepted")
    if current_output_mode() == "json":
        emit_json(None, "task accept", data=body)
        return
    click.secho(f"Task {task_id} accepted.", fg="green")


@task.command("reject")
@click.argument("task_id")
def task_reject(task_id: str) -> None:
    body = _set_status(task_id, "rejected")
    if current_output_mode() == "json":
        emit_json(None, "task reject", data=body)
        return
    click.secho(f"Task {task_id} rejected.", fg="yellow")
