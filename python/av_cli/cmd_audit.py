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


def _common_audit_filters(f):
    """Shared --action/--project/--since/--until/--username/--status-code/--outcome/
    --action-prefix option stack for `list` and `export` (v1.2.5 additions past the
    first four) — kept as one decorator so the two commands can't drift on what they
    accept."""
    f = click.option("--action", default=None,
                      help="Exact action filter, e.g. commit.push / ref.update / run.create.")(f)
    f = click.option("--action-prefix", default=None,
                      help="Route-family filter, e.g. 'commit.' matches commit.push/commit.pull.")(f)
    f = click.option("--project", "project_id", default=None, help="Scope to one project id.")(f)
    f = click.option("--username", default=None, help="Scope to one resolved actor identity.")(f)
    f = click.option("--status-code", "status_code", default=None, type=int,
                      help="Exact HTTP outcome filter, e.g. 409.")(f)
    f = click.option("--outcome", default=None, type=click.Choice(["ok", "error"]),
                      help="ok = 2xx/3xx, error = 4xx/5xx.")(f)
    f = click.option("--since", default=None, help="ISO-8601 lower bound (inclusive).")(f)
    f = click.option("--until", default=None, help="ISO-8601 upper bound (inclusive).")(f)
    return f


def _audit_filter_params(action, action_prefix, project_id, username, status_code,
                          outcome, since, until) -> dict:
    params: dict = {}
    if action:
        params["action"] = action
    if action_prefix:
        params["action_prefix"] = action_prefix
    if project_id:
        params["project_id"] = project_id
    if username:
        params["username"] = username
    if status_code is not None:
        params["status_code"] = status_code
    if outcome:
        params["outcome"] = outcome
    if since:
        params["since"] = since
    if until:
        params["until"] = until
    return params


@audit.command("list")
@_common_audit_filters
@click.option("--limit", default=50, show_default=True, type=click.IntRange(1, 500))
@click.option("--offset", default=0, show_default=True,
              help="Legacy offset pagination. Prefer --cursor for repeated/agent polling.")
@click.option("--cursor", default=None,
              help="Opaque page token from a previous response's next_cursor — stable "
                   "under concurrent inserts, unlike --offset. Mutually exclusive with --offset.")
def list_entries(action: str | None, action_prefix: str | None, project_id: str | None,
                 username: str | None, status_code: int | None, outcome: str | None,
                 since: str | None, until: str | None, limit: int, offset: int,
                 cursor: str | None) -> None:
    """List recent audit entries from the configured registry."""
    repo_root = ensure_repo()
    client = _client(repo_root)

    params = _audit_filter_params(action, action_prefix, project_id, username,
                                   status_code, outcome, since, until)
    params["limit"] = limit
    if cursor:
        params["cursor"] = cursor
    else:
        params["offset"] = offset

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
            "next_cursor": body.get("next_cursor"),
        })
        return

    if not entries:
        click.secho("No matching audit entries.", fg="yellow")
        return
    for e in entries:
        ts = (e.get("ts") or "")[:19].replace("T", " ")
        who = e.get("username") or "-"
        outcome_str = f" → {e['status_code']}" if e.get("status_code") else ""
        click.echo(f"  [{ts}] {who:<10} {e['action']}{outcome_str}  {e.get('project_id') or '-'}")
        details = e.get("details")
        if isinstance(details, dict) and details:
            rendered = json.dumps(details, sort_keys=True)[:160]
            click.echo(f"      {rendered}")
    total = body.get("total")
    if total is not None:
        click.secho(f"  ({offset + len(entries)} of {total}; "
                    "--offset/--cursor for more)", fg="cyan" if total > offset + limit else "")
    if body.get("next_cursor"):
        click.echo(f"  next: av audit list --cursor {body['next_cursor']} ...")


@audit.command("export")
@_common_audit_filters
@click.option("--format", "fmt", type=click.Choice(["jsonl", "csv"]), default="jsonl",
              show_default=True)
@click.option("--out", "out_path", default=None, type=click.Path(dir_okay=False),
              help="Write to this file instead of stdout.")
def export_entries(action: str | None, action_prefix: str | None, project_id: str | None,
                   username: str | None, status_code: int | None, outcome: str | None,
                   since: str | None, until: str | None, fmt: str, out_path: str | None) -> None:
    """Export the FILTERED audit trail (no pagination) as jsonl or csv, for compliance."""
    repo_root = ensure_repo()
    client = _client(repo_root)

    params = _audit_filter_params(action, action_prefix, project_id, username,
                                   status_code, outcome, since, until)
    params["format"] = fmt

    try:
        resp = client.session.get(f"{client.server_url}/api/admin/audit/export", params=params)
    except Exception:
        fail(None, "unreachable_queued", "Registry unreachable — audit trail not readable.")
    if resp.status_code != 200:
        fail(None, "validation", f"Export failed: HTTP {resp.status_code} {resp.text[:200]}")

    if out_path:
        with open(out_path, "wb") as f:
            f.write(resp.content)
        if current_output_mode() == "json":
            emit_json(None, "audit export", data={"bytes": len(resp.content), "path": out_path, "format": fmt})
        else:
            click.secho(f"Wrote {len(resp.content)} byte(s) to {out_path}", fg="green")
        return

    if current_output_mode() == "json":
        emit_json(None, "audit export", data={
            "bytes": len(resp.content), "format": fmt,
            "content": resp.content.decode("utf-8", errors="replace"),
        })
        return
    click.echo(resp.content.decode("utf-8", errors="replace"), nl=False)


@audit.command("prune")
@click.option("--before-days", default=None, type=int,
              help="Delete entries older than this many days (default: the registry's "
                   "AV_AUDIT_RETENTION_DAYS).")
@click.option("--yes", "-y", is_flag=True, default=False, help="Skip the confirmation prompt.")
def prune_entries(before_days: int | None, yes: bool) -> None:
    """Prune old audit entries on the registry. Admin-only, irreversible."""
    repo_root = ensure_repo()
    client = _client(repo_root)

    if not yes and current_output_mode() != "json":
        label = f"older than {before_days} day(s)" if before_days is not None else "past the registry's default retention window"
        if not click.confirm(
            f"This permanently deletes audit entries {label} on {client.server_url}. Continue?",
            default=False,
        ):
            click.secho("Aborted — nothing pruned.", fg="yellow")
            return

    params: dict = {}
    if before_days is not None:
        params["before_days"] = before_days
    try:
        resp = client.session.delete(f"{client.server_url}/api/admin/audit", params=params)
    except Exception:
        fail(None, "unreachable_queued", "Registry unreachable — audit trail not prunable right now.")
    if resp.status_code != 200:
        fail(None, "validation", f"Prune failed: HTTP {resp.status_code} {resp.text[:200]}")

    deleted = resp.json().get("deleted", 0)
    if current_output_mode() == "json":
        emit_json(None, "audit prune", data={"deleted": deleted, "before_days": before_days})
        return
    click.secho(f"Pruned {deleted} audit entr{'y' if deleted == 1 else 'ies'}.", fg="green")
