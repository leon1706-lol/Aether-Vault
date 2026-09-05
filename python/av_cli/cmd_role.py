"""av role — DB-backed RBAC: list built-in/custom roles and grant/revoke bindings
(v1.3.2), remote over HTTP like `av token`/`av user`.

Roles are expressed in the SAME scope vocabulary `av auth add-user --scope` already
uses (`server.py::require_scope`'s scope strings) — a role binding is just a different,
DB-backed way to arrive at the same scopes list, not a parallel permission system.
"""

from .core import *  # noqa: F401,F403 -- shared prelude (stdlib + helpers)
from .core import current_output_mode, emit_json, fail, resolve_remote


def _client(repo_root):
    from .client import VaultClient

    return VaultClient(*resolve_remote(repo_root))


@click.group()
def role() -> None:
    """List roles and manage grants (remote-administrable)."""


@role.command(name="list")
def role_list() -> None:
    """List every role visible to your tenant — the six built-ins plus any custom ones."""
    repo_root = ensure_repo()
    client = _client(repo_root)
    try:
        resp = client.session.get(f"{client.server_url}/api/roles")
    except Exception:
        fail(None, "unreachable_queued", "Registry unreachable — role list not readable.")
    if resp.status_code != 200:
        fail(None, "validation", f"List failed: HTTP {resp.status_code} {resp.text[:200]}",
             command="role list")
    roles = resp.json().get("roles", [])
    if current_output_mode() == "json":
        emit_json(None, "role list", data={"roles": roles})
        return
    for r in roles:
        kind = "builtin" if r["builtin"] else "custom"
        click.echo(f"  {r['name']:<12} [{kind}]  {', '.join(r['permissions'])}")


@role.command(name="grant")
@click.argument("subject_type", type=click.Choice(["user", "group", "token"]))
@click.argument("subject_id")
@click.argument("role_id")
@click.option("--project", "project_id", default=None,
              help="Scope this grant to one project instead of the whole tenant.")
def role_grant(subject_type: str, subject_id: str, role_id: str, project_id: str | None) -> None:
    """Grant ROLE_ID (see `av role list`) to SUBJECT_ID."""
    repo_root = ensure_repo()
    client = _client(repo_root)
    body = {"subject_type": subject_type, "subject_id": subject_id, "role_id": role_id}
    if project_id:
        body["scope_type"] = "project"
        body["scope_id"] = project_id
    try:
        resp = client.session.post(f"{client.server_url}/api/role-bindings", json=body)
    except Exception:
        fail(None, "unreachable_queued", "Registry unreachable — nothing granted.")
    if resp.status_code == 403:
        fail(None, "scope_denied", "Your token lacks the user:write scope.", command="role grant")
    if resp.status_code != 200:
        fail(None, "validation", f"Grant failed: HTTP {resp.status_code} {resp.text[:200]}",
             command="role grant")
    result = resp.json()
    if current_output_mode() == "json":
        emit_json(None, "role grant", data=result)
        return
    click.secho(f"Granted '{role_id}' to {subject_type} '{subject_id}'.", fg="green")


@role.command(name="bindings")
@click.option("--subject", "subject_id", default=None, help="Filter to one subject id.")
def role_bindings(subject_id: str | None) -> None:
    """List active role bindings on your tenant."""
    repo_root = ensure_repo()
    client = _client(repo_root)
    params = {"subject_id": subject_id} if subject_id else None
    try:
        resp = client.session.get(f"{client.server_url}/api/role-bindings", params=params)
    except Exception:
        fail(None, "unreachable_queued", "Registry unreachable — bindings not readable.")
    if resp.status_code != 200:
        fail(None, "validation", f"List failed: HTTP {resp.status_code} {resp.text[:200]}",
             command="role bindings")
    bindings = resp.json().get("bindings", [])
    if current_output_mode() == "json":
        emit_json(None, "role bindings", data={"bindings": bindings})
        return
    for b in bindings:
        scope_suffix = f" (project {b['scope_id']})" if b["scope_type"] == "project" else ""
        click.echo(f"  {b['id']}  {b['subject_type']}:{b['subject_id']} -> {b['role_id']}{scope_suffix}")


@role.command(name="revoke")
@click.argument("binding_id")
def role_revoke(binding_id: str) -> None:
    """Revoke a role binding by id (see `av role bindings`)."""
    repo_root = ensure_repo()
    client = _client(repo_root)
    try:
        resp = client.session.post(f"{client.server_url}/api/role-bindings/{binding_id}/revoke")
    except Exception:
        fail(None, "unreachable_queued", "Registry unreachable — nothing revoked.")
    if resp.status_code == 404:
        fail(None, "validation", f"No such binding '{binding_id}'.", command="role revoke")
    if resp.status_code != 200:
        fail(None, "validation", f"Revoke failed: HTTP {resp.status_code} {resp.text[:200]}",
             command="role revoke")
    if current_output_mode() == "json":
        emit_json(None, "role revoke", data={"id": binding_id, "revoked": True})
        return
    click.secho(f"Binding '{binding_id}' revoked.", fg="green")
