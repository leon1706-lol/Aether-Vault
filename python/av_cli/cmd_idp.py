"""av idp — SSO identity provider administration (v1.3.3, WP-15), remote over HTTP.

Thin CLI over the `/api/sso-providers*` CRUD routes (`server.py`) — provider secrets
(`client_secret`) are encrypted server-side (`sso_crypto.py`) before storage and never
round-trip back out in plaintext (`av idp show`/`list` always see the masked form).
"""

from .core import *  # noqa: F401,F403 -- shared prelude (stdlib + helpers)
from .core import current_output_mode, emit_json, fail, resolve_remote

import json as _json


def _client(repo_root):
    from .client import VaultClient

    return VaultClient(*resolve_remote(repo_root))


@click.group()
def idp() -> None:
    """Manage SSO identity providers (OIDC/SAML) on the configured registry."""


@idp.command(name="add")
@click.argument("name")
@click.option("--kind", type=click.Choice(["oidc", "saml"]), required=True)
@click.option("--issuer", default=None, help="OIDC: the IdP's issuer URL.")
@click.option("--client-id", default=None, help="OIDC: the client id registered with the IdP.")
@click.option("--client-secret", default=None, help="OIDC: the client secret (encrypted at rest).")
@click.option("--idp-metadata-url", default=None, help="SAML: URL to the IdP's metadata document.")
@click.option("--idp-metadata-file", type=click.Path(exists=True), default=None,
              help="SAML: path to a local IdP metadata XML file (read and inlined).")
@click.option("--jit/--no-jit", "jit_provisioning", default=False,
              help="Just-in-time provision unknown users on first login (default: off, "
                   "meaning only already-linked/pre-provisioned identities can log in).")
@click.option("--claims-email", default="email", help="Claim/attribute name carrying the user's email.")
@click.option("--claims-name", default="name", help="Claim/attribute name carrying the display name.")
@click.option("--claims-groups", default="groups", help="Claim/attribute name carrying group membership.")
@click.option("--group-role", "group_roles", multiple=True,
              help="Map an IdP group to a role, repeatable: --group-role 'Engineering=maintainer'.")
@click.option("--config-json", default=None,
              help="Raw JSON merged over everything above (escape hatch for fields this "
                   "command doesn't have a flag for yet).")
@click.option("--disabled", is_flag=True, help="Create the provider but leave it disabled.")
def idp_add(name, kind, issuer, client_id, client_secret, idp_metadata_url, idp_metadata_file,
            jit_provisioning, claims_email, claims_name, claims_groups, group_roles,
            config_json, disabled) -> None:
    """Register a new SSO provider. Prints its id — pass that to `av login --provider`."""
    repo_root = ensure_repo()
    client = _client(repo_root)

    config: dict = {
        "jit_provisioning": jit_provisioning,
        "claims": {"email": claims_email, "name": claims_name, "groups": claims_groups},
    }
    if kind == "oidc":
        if not (issuer and client_id):
            fail(None, "validation", "--issuer and --client-id are required for --kind oidc.",
                 command="idp add")
        config.update({"issuer": issuer, "client_id": client_id})
        if client_secret:
            config["client_secret"] = client_secret
    else:
        if idp_metadata_file:
            config["idp_metadata_xml"] = Path(idp_metadata_file).read_text(encoding="utf-8")
        elif idp_metadata_url:
            config["idp_metadata_url"] = idp_metadata_url
        else:
            fail(None, "validation",
                 "One of --idp-metadata-url or --idp-metadata-file is required for --kind saml.",
                 command="idp add")

    if group_roles:
        group_role_map = {}
        for entry in group_roles:
            if "=" not in entry:
                fail(None, "validation", f"--group-role must be GROUP=ROLE, got {entry!r}.",
                     command="idp add")
            group_name, role_name = entry.split("=", 1)
            group_role_map[group_name] = role_name
        config["group_role_map"] = group_role_map

    if config_json:
        try:
            config.update(_json.loads(config_json))
        except _json.JSONDecodeError as exc:
            fail(None, "validation", f"--config-json is not valid JSON: {exc}", command="idp add")

    body = {"kind": kind, "name": name, "config": config, "enabled": not disabled}
    try:
        resp = client.session.post(f"{client.server_url}/api/sso-providers", json=body)
    except Exception:
        fail(None, "unreachable_queued", "Registry unreachable — no provider was created.")
    if resp.status_code == 403:
        fail(None, "scope_denied", "Your token lacks the admin scope.", command="idp add")
    if resp.status_code == 422:
        fail(None, "validation", resp.json().get("detail", resp.text[:300]), command="idp add")
    if resp.status_code != 200:
        fail(None, "validation", f"Provider creation failed: HTTP {resp.status_code} {resp.text[:200]}",
             command="idp add")

    result = resp.json()
    if current_output_mode() == "json":
        emit_json(None, "idp add", data=result)
        return
    click.secho(f"Created SSO provider '{name}' ({result['id']}, kind={kind}).", fg="green")


@idp.command(name="list")
def idp_list() -> None:
    """List SSO providers configured for your tenant (secrets always masked)."""
    repo_root = ensure_repo()
    client = _client(repo_root)
    try:
        resp = client.session.get(f"{client.server_url}/api/sso-providers")
    except Exception:
        fail(None, "unreachable_queued", "Registry unreachable — provider list not readable.")
    if resp.status_code != 200:
        fail(None, "validation", f"List failed: HTTP {resp.status_code} {resp.text[:200]}",
             command="idp list")

    providers = resp.json().get("providers", [])
    if current_output_mode() == "json":
        emit_json(None, "idp list", data={"providers": providers})
        return
    if not providers:
        click.secho("No SSO providers configured for your tenant.", fg="yellow")
        return
    for p in providers:
        status = "enabled" if p["enabled"] else "disabled"
        click.echo(f"  {p['id']}  {p['name']}  [{p['kind']}, {status}]")


@idp.command(name="show")
@click.argument("provider_id")
def idp_show(provider_id: str) -> None:
    """Show one SSO provider's config (secrets masked)."""
    repo_root = ensure_repo()
    client = _client(repo_root)
    try:
        resp = client.session.get(f"{client.server_url}/api/sso-providers/{provider_id}")
    except Exception:
        fail(None, "unreachable_queued", "Registry unreachable — provider not readable.")
    if resp.status_code == 404:
        fail(None, "validation", f"No such SSO provider '{provider_id}'.", command="idp show")
    if resp.status_code != 200:
        fail(None, "validation", f"Show failed: HTTP {resp.status_code} {resp.text[:200]}",
             command="idp show")

    result = resp.json()
    if current_output_mode() == "json":
        emit_json(None, "idp show", data=result)
        return
    click.echo(json_module_dumps(result))


def json_module_dumps(obj) -> str:
    return _json.dumps(obj, indent=2, sort_keys=True)


@idp.command(name="test")
@click.argument("provider_id")
def idp_test(provider_id: str) -> None:
    """Check that a provider's IdP metadata/config is reachable (not a full login)."""
    repo_root = ensure_repo()
    client = _client(repo_root)
    try:
        resp = client.session.get(f"{client.server_url}/api/sso-providers/{provider_id}/test")
    except Exception:
        fail(None, "unreachable_queued", "Registry unreachable — could not run the test.")
    if resp.status_code == 404:
        fail(None, "validation", f"No such SSO provider '{provider_id}'.", command="idp test")
    if resp.status_code != 200:
        fail(None, "validation", f"Test failed: HTTP {resp.status_code} {resp.text[:200]}",
             command="idp test")

    result = resp.json()
    if current_output_mode() == "json":
        emit_json(None, "idp test", data=result)
        return
    if result.get("ok"):
        click.secho(f"OK — provider '{provider_id}' is reachable.", fg="green")
    else:
        click.secho(f"FAILED — {result.get('error') or result.get('note', 'unreachable')}", fg="red")


@idp.command(name="remove")
@click.argument("provider_id")
def idp_remove(provider_id: str) -> None:
    """Delete an SSO provider. Existing `user_identities` links are left in place
    (harmless once the provider itself is gone -- login for it simply 404s)."""
    repo_root = ensure_repo()
    client = _client(repo_root)
    try:
        resp = client.session.delete(f"{client.server_url}/api/sso-providers/{provider_id}")
    except Exception:
        fail(None, "unreachable_queued", "Registry unreachable — provider not removed.")
    if resp.status_code == 404:
        fail(None, "validation", f"No such SSO provider '{provider_id}'.", command="idp remove")
    if resp.status_code != 200:
        fail(None, "validation", f"Remove failed: HTTP {resp.status_code} {resp.text[:200]}",
             command="idp remove")

    if current_output_mode() == "json":
        emit_json(None, "idp remove", data={"id": provider_id, "removed": True})
        return
    click.secho(f"Removed SSO provider '{provider_id}'.", fg="green")
