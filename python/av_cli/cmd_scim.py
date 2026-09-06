"""av scim — SCIM provisioning-token administration (v1.3.3). SCIM itself is driven by
the IdP directly, not by this CLI; what this CLI does is mint the `scim`-scoped token the
IdP authenticates with, reusing the existing DB-backed token machinery (`cmd_token.py`)
rather than inventing a second token system.
"""

from .core import *  # noqa: F401,F403 -- shared prelude (stdlib + helpers)
from .core import current_output_mode, emit_json, fail, resolve_remote


def _client(repo_root):
    from .client import VaultClient

    return VaultClient(*resolve_remote(repo_root))


@click.group()
def scim() -> None:
    """Manage SCIM 2.0 provisioning access on the configured registry."""


@scim.command(name="status")
def scim_status() -> None:
    """Confirm the SCIM endpoint is mounted and reachable (needs `pysaml2`-free — SCIM
    has no optional dependency — but still absent if `python/av_server/scim.py` failed
    to import for some other reason)."""
    repo_root = ensure_repo()
    client = _client(repo_root)
    try:
        resp = client.session.get(f"{client.server_url}/scim/v2/ServiceProviderConfig")
    except Exception:
        fail(None, "unreachable_queued", "Registry unreachable — could not check SCIM status.")

    mounted = resp.status_code in (200, 401, 403)
    result = {"mounted": mounted, "status_code": resp.status_code}
    if current_output_mode() == "json":
        emit_json(None, "scim status", data=result)
        return
    if mounted:
        click.secho("SCIM (/scim/v2) is mounted on this registry.", fg="green")
    else:
        click.secho(f"SCIM does not appear to be mounted (HTTP {resp.status_code}).", fg="yellow")


@scim.group(name="token")
def scim_token() -> None:
    """Create/revoke tokens carrying the `scim` scope, for your IdP's provisioning connector."""


@scim_token.command(name="create")
@click.argument("name")
@click.option("--expires-in-days", "expires_in_days", type=int, default=None,
              help="Optional: this token stops authenticating after N days.")
def scim_token_create(name: str, expires_in_days: int | None) -> None:
    """Mint a `scim`-scoped token — paste it into your IdP's SCIM connector as its
    Bearer credential. Printed exactly once, same rule as every other token in this
    system."""
    repo_root = ensure_repo()
    client = _client(repo_root)

    body: dict = {"name": name, "scopes": ["scim"]}
    if expires_in_days is not None:
        body["expires_in_days"] = expires_in_days

    try:
        resp = client.session.post(f"{client.server_url}/api/tokens", json=body)
    except Exception:
        fail(None, "unreachable_queued", "Registry unreachable — no SCIM token was created.")
    if resp.status_code == 403:
        fail(None, "scope_denied", "Your token lacks the token:write scope.", command="scim token create")
    if resp.status_code != 200:
        fail(None, "validation", f"Token creation failed: HTTP {resp.status_code} {resp.text[:200]}",
             command="scim token create")

    result = resp.json()
    if current_output_mode() == "json":
        emit_json(None, "scim token create", data=result)
        return
    click.secho(f"SCIM token created: {result['token']}", fg="green")
    click.secho("Save this — it won't be shown again. Configure it as your IdP's SCIM "
                "Bearer token, pointed at "
                f"{client.server_url}/scim/v2", fg="yellow")
    click.secho("This token can create, update, and deprovision every user and group in "
                "your tenant — treat it with the same care as an admin credential, not a "
                "read-only one.", fg="yellow")


@scim_token.command(name="revoke")
@click.argument("token_id")
def scim_token_revoke(token_id: str) -> None:
    """Revoke a SCIM token by id (see `av token list` — SCIM tokens show `scim` in their
    scopes). Takes effect immediately for new requests."""
    repo_root = ensure_repo()
    client = _client(repo_root)
    try:
        resp = client.session.post(f"{client.server_url}/api/tokens/{token_id}/revoke")
    except Exception:
        fail(None, "unreachable_queued", "Registry unreachable — token not revoked.")
    if resp.status_code == 404:
        fail(None, "validation", f"No such token '{token_id}' on your tenant.", command="scim token revoke")
    if resp.status_code != 200:
        fail(None, "validation", f"Revoke failed: HTTP {resp.status_code} {resp.text[:200]}",
             command="scim token revoke")

    if current_output_mode() == "json":
        emit_json(None, "scim token revoke", data={"id": token_id, "revoked": True})
        return
    click.secho(f"SCIM token '{token_id}' revoked.", fg="green")
