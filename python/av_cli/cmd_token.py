"""av token — DB-backed, remote-administrable API tokens (v1.3.2). The remote-
administrable alternative to `av auth add-user` (which needs shell access to the compose
`.env` file) — a token minted here works from any machine that can reach the registry
over HTTP, gated by the `token:write` scope. `av auth *` stays the OSS path alongside it,
not a replacement.
"""

from .core import *  # noqa: F401,F403 -- shared prelude (stdlib + helpers)
from .core import current_output_mode, emit_json, fail, resolve_remote


def _client(repo_root):
    from .client import VaultClient

    return VaultClient(*resolve_remote(repo_root))


@click.group()
def token() -> None:
    """Manage DB-backed API tokens on the configured registry (remote-administrable)."""


@token.command(name="create")
@click.argument("name")
@click.option("--scope", "scopes", multiple=True,
              help="Restrict this token to specific permissions (repeatable), e.g. "
                   "--scope improver:write --scope review. Omit for a token that "
                   "carries exactly its creator's role-derived permissions.")
@click.option("--expires-in-days", "expires_in_days", type=int, default=None,
              help="Optional: this token stops authenticating after N days "
                   "(default: never expires).")
def token_create(name: str, scopes: tuple[str, ...], expires_in_days: int | None) -> None:
    """Mint a new API token, scoped to your own tenant. Prints the token exactly once —
    it is never retrievable again (only its hash is stored server-side)."""
    repo_root = ensure_repo()
    client = _client(repo_root)

    body: dict = {"name": name}
    if scopes:
        body["scopes"] = sorted(set(scopes))
    if expires_in_days is not None:
        body["expires_in_days"] = expires_in_days

    try:
        resp = client.session.post(f"{client.server_url}/api/tokens", json=body)
    except Exception:
        fail(None, "unreachable_queued", "Registry unreachable — no token was created.")
    if resp.status_code == 401:
        fail(None, "auth_failed", "Registry rejected the request (401).", command="token create")
    if resp.status_code == 403:
        fail(None, "scope_denied",
             "Your token lacks the token:write scope required to mint tokens.",
             command="token create")
    if resp.status_code != 200:
        fail(None, "validation", f"Token creation failed: HTTP {resp.status_code} {resp.text[:200]}",
             command="token create")

    result = resp.json()
    if current_output_mode() == "json":
        emit_json(None, "token create", data=result)
        return
    click.secho(f"Token created: {result['token']}", fg="green")
    click.secho("Save this — it won't be shown again.", fg="yellow")
    if result.get("expires_at"):
        click.secho(f"Expires: {result['expires_at']}", fg="cyan")


@token.command(name="list")
def token_list() -> None:
    """List tokens for your own tenant (masked — the hash is never returned)."""
    repo_root = ensure_repo()
    client = _client(repo_root)

    try:
        resp = client.session.get(f"{client.server_url}/api/tokens")
    except Exception:
        fail(None, "unreachable_queued", "Registry unreachable — token list not readable.")
    if resp.status_code != 200:
        fail(None, "validation", f"List failed: HTTP {resp.status_code} {resp.text[:200]}",
             command="token list")

    tokens = resp.json().get("tokens", [])
    if current_output_mode() == "json":
        emit_json(None, "token list", data={"tokens": tokens})
        return
    if not tokens:
        click.secho("No tokens on this registry for your tenant.", fg="yellow")
        return
    for t in tokens:
        status = "revoked" if t.get("revoked_at") else "active"
        scope_suffix = f"  [scopes: {', '.join(t['scopes'])}]" if t.get("scopes") else ""
        expiry_suffix = f"  (expires {t['expires_at']})" if t.get("expires_at") else ""
        click.echo(f"  {t['id']}  {t['name']}  {t['prefix']}...  [{status}]{scope_suffix}{expiry_suffix}")


@token.command(name="revoke")
@click.argument("token_id")
def token_revoke(token_id: str) -> None:
    """Revoke a token by id (see `av token list`). Takes effect immediately for any
    new authentication attempt; an already-cached resolution on the server can lag by
    up to AV_AUTH_CACHE_TTL_SECS (default 30s)."""
    repo_root = ensure_repo()
    client = _client(repo_root)

    try:
        resp = client.session.post(f"{client.server_url}/api/tokens/{token_id}/revoke")
    except Exception:
        fail(None, "unreachable_queued", "Registry unreachable — token not revoked.")
    if resp.status_code == 404:
        fail(None, "validation", f"No such token '{token_id}' on your tenant.", command="token revoke")
    if resp.status_code != 200:
        fail(None, "validation", f"Revoke failed: HTTP {resp.status_code} {resp.text[:200]}",
             command="token revoke")

    if current_output_mode() == "json":
        emit_json(None, "token revoke", data={"id": token_id, "revoked": True})
        return
    click.secho(f"Token '{token_id}' revoked.", fg="green")
