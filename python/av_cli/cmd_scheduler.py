"""av scheduler — external scheduler hooks (v1.3.1, RSI R3: todo.md D.20).

`GET /api/scheduler/queue` + `POST /api/runs/{id}/stop` (see `cmd_run.py::run_stop`) are
the two primitives an external bandit/scheduler needs: see what's running, stop what it
decides to stop. No new event kinds beyond the existing `run` kind (a `stop` action rides
the same `run` event `av watch`/an agent already polls via `GET /api/events?kinds=run`).
"""
from .core import *  # noqa: F401,F403 -- shared prelude (stdlib + helpers)
from .core import current_output_mode, emit_json, resolve_remote


def _client(repo_root):
    from .client import VaultClient

    return VaultClient(*resolve_remote(repo_root))


@click.group()
def scheduler() -> None:
    """External scheduler hooks: what's running, right now."""


@scheduler.command("queue")
@click.option("--project", "project_id", default=None)
def scheduler_queue(project_id: str | None) -> None:
    """List runs currently in flight (status == running) for a scheduler to act on."""
    repo_root = ensure_repo()
    cfg = load_config(repo_root)
    client = _client(repo_root)
    if not client.server_available():
        fail(None, "unreachable_queued", "Registry unreachable — cannot read the scheduler queue.")
    resp = client.session.get(f"{client.server_url}/api/scheduler/queue",
                              params={"project_id": project_id or cfg.get("project_id")})
    rows = resp.json().get("queue", []) if resp.status_code == 200 else []
    if current_output_mode() == "json":
        emit_json(None, "scheduler queue", data={"queue": rows})
        return
    if not rows:
        click.secho("Nothing currently running.", fg="yellow")
        return
    for r in rows:
        ms = r.get("metrics_summary") or {}
        tail = ", ".join(f"{k}={v}" for k, v in list(ms.items())[:3])
        click.echo(f"  {r['id'][:8]}  {(r.get('name') or '-'):<20} {tail}")
