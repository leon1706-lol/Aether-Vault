"""av webhooks — management CLI for the registry's signed event subscriptions (v1.2.1).

Talks to the stable /api/webhooks API (same auth as every other client call). Secrets
are write-only from the CLI's perspective: the server stores them for signing and the
list endpoint only ever returns a masked prefix.
"""

import json

from .core import *  # noqa: F401,F403 -- shared prelude (stdlib + helpers)
from .core import current_output_mode, emit_json


def _client(repo_root):
    from .client import VaultClient

    cfg = load_config(repo_root)
    return VaultClient(cfg.get("remote_url", "http://localhost:8000"),
                       cfg.get("remote_api_token"))


def _request(client, method: str, path: str, json_body=None, params=None):
    import requests

    try:
        return client.session.request(method, f"{client.server_url}{path}",
                                      json=json_body, params=params, timeout=30)
    except requests.RequestException as exc:
        fail(None, "unreachable_queued", f"Registry unreachable: {exc}")


@click.group()
def webhooks() -> None:
    """Manage signed event-webhook subscriptions on the registry."""
    _ = json  # keep json in module namespace for parity with sibling modules


@webhooks.command()
@click.argument("url")
@click.option("--secret", required=True, help="Signing secret (HMAC-SHA256 over body).")
@click.option("--project", "project_id", default=None, help="Scope to one project.")
@click.option("--kind", "kinds", multiple=True, help="Event kind filter (repeatable).")
def add(url: str, secret: str, project_id: str | None, kinds: tuple) -> None:
    """Subscribe URL to registry events, signed with SECRET."""
    repo_root = ensure_repo()
    client = _client(repo_root)
    resp = _request(client, "POST", "/api/webhooks", {
        "url": url,
        "secret": secret,
        "project_id": project_id,
        "kinds": list(kinds) or None,
    })
    if resp.status_code == 422:
        fail(None, "validation", resp.json().get("detail", "invalid payload"))
    if resp.status_code != 200:
        fail(None, "validation", f"registry rejected webhook ({resp.status_code})")
    body = resp.json()
    if current_output_mode() == "json":
        emit_json(None, "webhooks add", data=body)
        return
    scope = project_id or "all projects"
    click.secho(f"Webhook {body['id'][:8]} added → {url} [{scope}]", fg="green")


@webhooks.command("list")
def list_webhooks() -> None:
    """List webhooks (secrets masked — never printed in full)."""
    repo_root = ensure_repo()
    client = _client(repo_root)
    resp = _request(client, "GET", "/api/webhooks")
    rows = resp.json().get("webhooks", []) if resp.status_code == 200 else []
    if current_output_mode() == "json":
        emit_json(None, "webhooks list", data={"webhooks": rows})
        return
    if not rows:
        click.secho("No webhooks configured.", fg="yellow")
        return
    for w in rows:
        state = "active" if w.get("active") else "inactive"
        scope = w.get("project_id") or "*"
        health = ""
        if w.get("consecutive_failures"):
            health = f"  ({w['consecutive_failures']} consecutive failure(s))"
        click.echo(f"  [{state}] {w['id'][:8]}  {w['url']}  [{scope}]  secret={w.get('secret')}{health}")
        if w.get("disabled_reason"):
            click.secho(f"      {w['disabled_reason']}", fg="red")


@webhooks.command()
@click.argument("webhook_id")
def remove(webhook_id: str) -> None:
    """Delete a webhook subscription by id."""
    repo_root = ensure_repo()
    client = _client(repo_root)
    resp = _request(client, "DELETE", f"/api/webhooks/{webhook_id}")
    if resp.status_code == 404:
        fail(None, "validation", f"No such webhook: {webhook_id}")
    if current_output_mode() == "json":
        emit_json(None, "webhooks remove", data={"deleted": webhook_id})
        return
    click.secho(f"Webhook {webhook_id[:8]} deleted.", fg="green")


@webhooks.command()
@click.argument("webhook_id")
def test(webhook_id: str) -> None:
    """Deliver a signed ping event to this webhook now."""
    repo_root = ensure_repo()
    client = _client(repo_root)
    resp = _request(client, "POST", f"/api/webhooks/{webhook_id}/test")
    if resp.status_code == 404:
        fail(None, "validation", f"No such webhook: {webhook_id}")
    if current_output_mode() == "json":
        emit_json(None, "webhooks test", data={"delivered": True})
        return
    click.secho("Signed ping delivered (check your receiver logs).", fg="green")


@webhooks.command()
@click.argument("webhook_id")
def enable(webhook_id: str) -> None:
    """v1.2.5: re-enable a webhook (e.g. after it auto-disabled) and clear its failure streak."""
    repo_root = ensure_repo()
    client = _client(repo_root)
    resp = _request(client, "POST", f"/api/webhooks/{webhook_id}/enable")
    if resp.status_code == 404:
        fail(None, "validation", f"No such webhook: {webhook_id}")
    if current_output_mode() == "json":
        emit_json(None, "webhooks enable", data={"enabled": webhook_id})
        return
    click.secho(f"Webhook {webhook_id[:8]} re-enabled.", fg="green")


@webhooks.command()
@click.argument("webhook_id")
def show(webhook_id: str) -> None:
    """v1.2.5: config + health summary + last 5 delivery outcomes for one webhook."""
    repo_root = ensure_repo()
    client = _client(repo_root)
    resp = _request(client, "GET", "/api/webhooks")
    rows = resp.json().get("webhooks", []) if resp.status_code == 200 else []
    row = next((w for w in rows if w["id"] == webhook_id or w["id"].startswith(webhook_id)), None)
    if row is None:
        fail(None, "validation", f"No such webhook: {webhook_id}")

    deliveries_resp = _request(client, "GET", "/api/admin/webhook-deliveries",
                               params={"webhook_id": row["id"], "limit": 5})
    recent = deliveries_resp.json().get("deliveries", []) if deliveries_resp.status_code == 200 else []

    if current_output_mode() == "json":
        emit_json(None, "webhooks show", data={"webhook": row, "recent_deliveries": recent})
        return

    state = "active" if row.get("active") else "inactive"
    click.echo(f"  {row['id']}  [{state}]  {row['url']}")
    click.echo(f"  project: {row.get('project_id') or '*'}   kinds: {row.get('kinds') or 'all'}")
    click.echo(f"  last success: {row.get('last_success_at') or 'never'}")
    click.echo(f"  last failure: {row.get('last_failure_at') or 'never'}")
    click.echo(f"  consecutive failures: {row.get('consecutive_failures', 0)}")
    if row.get("disabled_reason"):
        click.secho(f"  disabled: {row['disabled_reason']}", fg="red")
    if recent:
        click.echo("  recent deliveries:")
        for d in recent:
            click.echo(f"    [{d['status']:<10}] attempt {d['attempt']}  "
                       f"{d.get('response_code') or d.get('last_error') or ''}  ({d['updated_at']})")


@webhooks.command()
@click.option("--webhook-id", default=None, help="Scope to one webhook.")
@click.option("--status", default=None, type=click.Choice(["pending", "delivered", "failed", "dead"]))
@click.option("--kind", "event_kind", default=None, help="Filter by event kind (e.g. commit).")
@click.option("--since", default=None, help="ISO-8601 lower bound (inclusive).")
@click.option("--until", default=None, help="ISO-8601 upper bound (inclusive).")
@click.option("--limit", default=50, show_default=True, type=click.IntRange(1, 500))
@click.option("--cursor", default=None, help="Page token from a previous response's next_cursor.")
def deliveries(webhook_id: str | None, status: str | None, event_kind: str | None,
               since: str | None, until: str | None, limit: int, cursor: str | None) -> None:
    """v1.2.5: inspect the delivery ledger — attempts, outcomes, dead-letters."""
    repo_root = ensure_repo()
    client = _client(repo_root)
    params: dict = {"limit": limit}
    if webhook_id:
        params["webhook_id"] = webhook_id
    if status:
        params["status"] = status
    if event_kind:
        params["event_kind"] = event_kind
    if since:
        params["since"] = since
    if until:
        params["until"] = until
    if cursor:
        params["cursor"] = cursor

    resp = _request(client, "GET", "/api/admin/webhook-deliveries", params=params)
    body = resp.json() if resp.status_code == 200 else {}
    rows = body.get("deliveries", [])
    if current_output_mode() == "json":
        emit_json(None, "webhooks deliveries", data=body)
        return
    if not rows:
        click.secho("No matching deliveries.", fg="yellow")
        return
    for d in rows:
        click.echo(f"  #{d['id']}  [{d['status']:<10}] webhook={d['webhook_id'][:8]}  "
                   f"kind={d.get('event_kind') or '-'}  attempt={d['attempt']}  "
                   f"{d.get('response_code') or d.get('last_error') or ''}")
    if body.get("next_cursor"):
        click.echo(f"  next: av webhooks deliveries --cursor {body['next_cursor']} ...")


@webhooks.command()
@click.argument("delivery_id", type=int)
def replay(delivery_id: int) -> None:
    """v1.2.5: re-queue one failed/dead delivery for immediate retry."""
    repo_root = ensure_repo()
    client = _client(repo_root)
    resp = _request(client, "POST", f"/api/admin/webhook-deliveries/{delivery_id}/replay")
    if resp.status_code == 404:
        fail(None, "validation", f"No such delivery: {delivery_id}")
    if resp.status_code == 409:
        fail(None, "validation", resp.json().get("detail", "delivery cannot be replayed"))
    if current_output_mode() == "json":
        emit_json(None, "webhooks replay", data=resp.json())
        return
    click.secho(f"Delivery {delivery_id} re-queued for immediate retry.", fg="green")
