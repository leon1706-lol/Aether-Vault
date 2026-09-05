"""av user — DB-backed user administration (v1.3.2), remote over HTTP like `av token`.

Local provisioning (`av user create`) is the manual counterpart to JIT provisioning via
SSO login or SCIM `POST /scim/v2/Users` — for operators who want to create accounts by
hand rather than through an IdP.
"""

from .core import *  # noqa: F401,F403 -- shared prelude (stdlib + helpers)
from .core import current_output_mode, emit_json, fail, resolve_remote


def _client(repo_root):
    from .client import VaultClient

    return VaultClient(*resolve_remote(repo_root))


@click.group()
def user() -> None:
    """Manage users on the configured registry's tenant (remote-administrable)."""


@user.command(name="create")
@click.argument("username")
@click.option("--email", default=None)
@click.option("--display-name", "display_name", default=None)
def user_create(username: str, email: str | None, display_name: str | None) -> None:
    """Provision a local user (source='local')."""
    repo_root = ensure_repo()
    client = _client(repo_root)
    body = {"username": username}
    if email:
        body["email"] = email
    if display_name:
        body["display_name"] = display_name
    try:
        resp = client.session.post(f"{client.server_url}/api/users", json=body)
    except Exception:
        fail(None, "unreachable_queued", "Registry unreachable — no user was created.")
    if resp.status_code == 401:
        fail(None, "auth_failed", "Registry rejected the request (401).", command="user create")
    if resp.status_code == 403:
        fail(None, "scope_denied", "Your token lacks the user:write scope.", command="user create")
    if resp.status_code != 200:
        fail(None, "validation", f"User creation failed: HTTP {resp.status_code} {resp.text[:200]}",
             command="user create")
    result = resp.json()
    if current_output_mode() == "json":
        emit_json(None, "user create", data=result)
        return
    verb = "Created" if result["status"] == "created" else "Already exists"
    click.secho(f"{verb}: {username} ({result['id']})", fg="green")


@user.command(name="list")
def user_list() -> None:
    """List users on your tenant."""
    repo_root = ensure_repo()
    client = _client(repo_root)
    try:
        resp = client.session.get(f"{client.server_url}/api/users")
    except Exception:
        fail(None, "unreachable_queued", "Registry unreachable — user list not readable.")
    if resp.status_code != 200:
        fail(None, "validation", f"List failed: HTTP {resp.status_code} {resp.text[:200]}",
             command="user list")
    users = resp.json().get("users", [])
    if current_output_mode() == "json":
        emit_json(None, "user list", data={"users": users})
        return
    if not users:
        click.secho("No users on this tenant.", fg="yellow")
        return
    for u in users:
        status_color = "green" if u["status"] == "active" else "red"
        click.secho(f"  {u['id']}  {u['username']}  [{u['status']}]  source={u['source']}",
                   fg=status_color)


@user.command(name="suspend")
@click.argument("user_id")
def user_suspend(user_id: str) -> None:
    """Suspend a user and immediately revoke every live session/token they hold."""
    repo_root = ensure_repo()
    client = _client(repo_root)
    try:
        resp = client.session.post(f"{client.server_url}/api/users/{user_id}/suspend")
    except Exception:
        fail(None, "unreachable_queued", "Registry unreachable — user not suspended.")
    if resp.status_code == 404:
        fail(None, "validation", f"No such user '{user_id}' on your tenant.", command="user suspend")
    if resp.status_code != 200:
        fail(None, "validation", f"Suspend failed: HTTP {resp.status_code} {resp.text[:200]}",
             command="user suspend")
    if current_output_mode() == "json":
        emit_json(None, "user suspend", data={"id": user_id, "suspended": True})
        return
    click.secho(f"User '{user_id}' suspended — all sessions/tokens revoked.", fg="green")
