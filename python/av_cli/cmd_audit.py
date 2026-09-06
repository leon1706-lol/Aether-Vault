"""av audit — read-side CLI for the registry's audit trail (v1.2.2). The trail itself is
server-side (DBAuditLog); this is its human/agent-facing query surface for filtering and
paging results. Read-only — pruning lives server-side.
"""

import datetime

from .core import *  # noqa: F401,F403 -- shared prelude (stdlib + helpers)
from .core import current_output_mode, emit_json, resolve_remote


def _client(repo_root):
    from .client import VaultClient

    return VaultClient(*resolve_remote(repo_root))


@click.group()
def audit() -> None:
    """Registry audit trail: who did what, when, with what outcome."""


def _common_audit_filters(f):
    """Shared filter-option stack for `list` and `export`, kept as one decorator so the
    two commands can't drift on what they accept."""
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


@audit.command("verify")
@click.option("--since-id", default=0, type=int,
              help="Only re-verify rows after this id (from a previous check's last_id). "
                   "Default 0 verifies the WHOLE chain from the beginning.")
@click.option("--export", "export_path", default=None, type=click.Path(exists=True, dir_okay=False),
              help="Verify OFFLINE against a local jsonl export instead of asking the "
                   "server — genuine independent verification: no server access is used "
                   "at all beyond fetching the public key once (or pass --public-key to "
                   "skip that too).")
@click.option("--public-key", "public_key_hex", default=None,
              help="Hex-encoded ed25519 public key for --export mode. Without it, "
                   "--export fetches the key from the server once (still not trusting "
                   "the server's own verify computation, just its published key).")
def verify_chain(since_id: int, export_path: str | None, public_key_hex: str | None) -> None:
    """Verify the audit log's hash chain is intact (v1.3.3, WP-32) — reports the first
    broken row, if any, and whether present signatures verify."""
    repo_root = ensure_repo()
    client = _client(repo_root)

    if export_path:
        _verify_export_offline(client, export_path, public_key_hex)
        return

    try:
        resp = client.session.get(f"{client.server_url}/api/admin/audit/verify",
                                  params={"since_id": since_id})
    except Exception:
        fail(None, "unreachable_queued", "Registry unreachable — audit chain not verifiable right now.")
    if resp.status_code != 200:
        fail(None, "validation", f"Verify failed: HTTP {resp.status_code} {resp.text[:200]}")

    body = resp.json()
    if current_output_mode() == "json":
        emit_json(None, "audit verify", data=body)
        return
    if body["ok"]:
        click.secho(f"[OK] Chain intact — {body['checked']} row(s) verified "
                   f"(last_id={body['last_id']}).", fg="green")
    else:
        click.secho(f"[FAIL] Chain broken at audit_log.id={body['broken_at_id']} "
                   f"({body['checked']} row(s) verified before the break).", fg="red")
        raise SystemExit(15)
    sig = body["signature_checks"]
    if sig["verified"] or sig["failed"]:
        click.echo(f"  Signatures: {sig['verified']} verified, {sig['failed']} FAILED, "
                  f"{sig['absent']} unsigned.")
        if sig["failed"]:
            raise SystemExit(15)


def _verify_export_offline(client, export_path: str, public_key_hex: str | None) -> None:
    """Recomputes the chain entirely locally from a jsonl export, never asking the server
    to grade its own homework. The only optional server round trip fetches the public key."""
    from .audit_chain_verify import verify_export

    if public_key_hex is None:
        try:
            resp = client.session.get(f"{client.server_url}/api/admin/audit/public-key")
            if resp.status_code == 200:
                public_key_hex = resp.json().get("public_key")
        except Exception:
            pass  # chain verification still works with no public key; just skips signatures

    with open(export_path, encoding="utf-8") as f:
        rows = [json.loads(line) for line in f if line.strip()]

    result = verify_export(rows, public_key_hex)
    if current_output_mode() == "json":
        emit_json(None, "audit verify", data=result)
        return
    if result["ok"]:
        click.secho(f"[OK] Chain intact (offline, {result['checked']} row(s), "
                   f"no server trust required for the chain itself).", fg="green")
    else:
        click.secho(f"[FAIL] Chain broken at audit_log.id={result['broken_at_id']}.", fg="red")
        raise SystemExit(15)
    sig = result["signature_checks"]
    if sig["verified"] or sig["failed"]:
        click.echo(f"  Signatures: {sig['verified']} verified, {sig['failed']} FAILED, "
                  f"{sig['absent']} unsigned.")
        if sig["failed"]:
            raise SystemExit(15)


@audit.command("prune")
@click.option("--before-days", default=None, type=int,
              help="Delete entries older than this many days (default: the registry's "
                   "AV_AUDIT_RETENTION_DAYS).")
@click.option("--yes", "-y", is_flag=True, default=False, help="Skip the confirmation prompt.")
@click.option("--dry-run", "dry_run", is_flag=True, default=False,
              help="Report how many entries WOULD be deleted without deleting anything.")
def prune_entries(before_days: int | None, yes: bool, dry_run: bool) -> None:
    """Prune old audit entries on the registry. Admin-only, irreversible (unless --dry-run)."""
    repo_root = ensure_repo()
    client = _client(repo_root)

    if not dry_run and not yes and current_output_mode() != "json":
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
    if dry_run:
        params["dry_run"] = "true"
    try:
        resp = client.session.delete(f"{client.server_url}/api/admin/audit", params=params)
    except Exception:
        fail(None, "unreachable_queued", "Registry unreachable — audit trail not prunable right now.")
    if resp.status_code != 200:
        fail(None, "validation", f"Prune failed: HTTP {resp.status_code} {resp.text[:200]}")

    body = resp.json()
    deleted = body.get("deleted", 0)
    would_delete = body.get("would_delete")
    if current_output_mode() == "json":
        emit_json(None, "audit prune", data={"deleted": deleted, "would_delete": would_delete,
                                             "dry_run": dry_run, "before_days": before_days})
        return
    if dry_run:
        click.secho(f"Would delete {would_delete} audit entry(ies) — nothing was changed.", fg="cyan")
        return
    click.secho(f"Pruned {deleted} audit entr{'y' if deleted == 1 else 'ies'}.", fg="green")
