"""av audit — read-side CLI for the registry's audit trail (v1.2.2 audit depth).

The trail itself is server-side (DBAuditLog, written on every mutating API call with
the resolved identity and the HTTP outcome). This command is its human/agent-facing
query surface: filter by action / project / time window and page through results.
Read-only — it never mutates anything (pruning lives server-side via
DELETE /api/admin/audit and the AV_AUDIT_RETENTION_DAYS GC sweep).
"""

import datetime

from .core import *  # noqa: F401,F403 -- shared prelude (stdlib + helpers)
from .core import current_output_mode, emit_json


def _client(repo_root):
    from .client import VaultClient

    cfg = load_config(repo_root)
    return VaultClient(cfg.get("remote_url", "http://localhost:8000"),
                       cfg.get("remote_api_token"))


@click.group()
def audit() -> None:
    """Registry audit trail: who did what, when, with what outcome."""


@audit.command("list")
@click.option("--action", default=None,
              help="Exact action filter, e.g. commit.push / ref.update / run.create.")
@click.option("--project", "project_id", default=None, help="Scope to one project id.")
@click.option("--since", default=None, help="ISO-8601 lower bound (inclusive).")
@click.option("--until", default=None, help="ISO-8601 upper bound (inclusive).")
@click.option("--limit", default=50, show_default=True, type=click.IntRange(1, 500))
@click.option("--offset", default=0, show_default=True)
def list_entries(action: str | None, project_id: str | None, since: str | None,
                 until: str | None, limit: int, offset: int) -> None:
    """List recent audit entries from the configured registry."""
    repo_root = ensure_repo()
    client = _client(repo_root)

    params: dict = {"limit": limit, "offset": offset}
    if action:
        params["action"] = action
    if project_id:
        params["project_id"] = project_id
    if since:
        params["since"] = since
    if until:
        params["until"] = until

    try:
        resp = client.session.get(f"{client.server_url}/api/admin/audit", params=params)
        body = resp.json() if resp.status_code == 200 else {}
    except Exception:
        fail(None, "unreachable_queued", "Registry unreachable — audit trail not readable.")

    entries = body.get("entries", [])
    if current_output_mode() == "json":
        emit_json(None, "audit list", data={
            "entries": entries,
            "total": body.get("total", len(entries)),
            "limit": limit, "offset": offset,
            "next_offset": (offset + limit) if offset + limit < body.get("total", 0) else None,
        })
        return

    if not entries:
        click.secho("No matching audit entries.", fg="yellow")
        return
    for e in entries:
        ts = (e.get("ts") or "")[:19].replace("T", " ")
        who = e.get("username") or "-"
        outcome = f" → {e['status_code']}" if e.get("status_code") else ""
        click.echo(f"  [{ts}] {who:<10} {e['action']}{outcome}  {e.get('project_id') or '-'}")
        details = e.get("details")
        if isinstance(details, dict) and details:
            rendered = json.dumps(details, sort_keys=True)[:160]
            click.echo(f"      {rendered}")
    total = body.get("total")
    if total is not None:
        click.secho(f"  ({offset + len(entries)} of {total}; "
                    "--offset for more)", fg="cyan" if total > offset + limit else "")
