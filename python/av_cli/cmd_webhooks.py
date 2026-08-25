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


def _request(client, method: str, path: str, json_body=None):
    import requests

    try:
        return client.session.request(method, f"{client.server_url}{path}",
                                      json=json_body, timeout=30)
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
        click.echo(f"  [{state}] {w['id'][:8]}  {w['url']}  [{scope}]  secret={w.get('secret')}")


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
