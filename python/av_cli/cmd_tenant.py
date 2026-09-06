"""av tenant — DB-backed tenant administration (v1.3.2), remote over HTTP. `create` is
gated behind an operator's own `admin` scope rather than a separate platform-superadmin
tier, which this phase does not build. There is no `list`/`update` yet -- that needs the
same not-yet-built platform-operator concept.
"""

from .core import *  # noqa: F401,F403 -- shared prelude (stdlib + helpers)
from .core import current_output_mode, emit_json, fail, resolve_remote


def _client(repo_root):
    from .client import VaultClient

    return VaultClient(*resolve_remote(repo_root))


@click.group()
def tenant() -> None:
    """Manage the tenant boundary on the configured registry (remote-administrable)."""


@tenant.command(name="create")
@click.argument("slug")
@click.argument("name")
def tenant_create(slug: str, name: str) -> None:
    """Provision a new tenant (requires an admin-scoped credential)."""
    repo_root = ensure_repo()
    client = _client(repo_root)
    try:
        resp = client.session.post(f"{client.server_url}/api/tenants",
                                    json={"slug": slug, "name": name})
    except Exception:
        fail(None, "unreachable_queued", "Registry unreachable — no tenant was created.")
    if resp.status_code == 403:
        fail(None, "scope_denied", "Your token lacks the admin scope.", command="tenant create")
    if resp.status_code != 200:
        fail(None, "validation", f"Tenant creation failed: HTTP {resp.status_code} {resp.text[:200]}",
             command="tenant create")
    result = resp.json()
    if current_output_mode() == "json":
        emit_json(None, "tenant create", data=result)
        return
    verb = "Created" if result["status"] == "created" else "Already exists"
    click.secho(f"{verb}: {slug} ({result['id']})", fg="green")


@tenant.command(name="show")
def tenant_show() -> None:
    """Show the tenant your currently-configured credential resolves to."""
    repo_root = ensure_repo()
    client = _client(repo_root)
    try:
        resp = client.session.get(f"{client.server_url}/api/tenants/me")
    except Exception:
        fail(None, "unreachable_queued", "Registry unreachable — tenant not readable.")
    if resp.status_code != 200:
        fail(None, "validation", f"Show failed: HTTP {resp.status_code} {resp.text[:200]}",
             command="tenant show")
    result = resp.json()
    if current_output_mode() == "json":
        emit_json(None, "tenant show", data=result)
        return
    click.echo(f"  {result['id']}  {result['slug']}  {result['name']}  [{result['status']}]")
