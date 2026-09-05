"""av login/logout/whoami — SSO device-code login for the CLI (v1.3.3, WP-12/WP-15).

A browser redirect is the wrong UX for a terminal, so this drives the OAuth2 device-code
flow (`sso_oidc.py`'s `/api/auth/device/*` routes) rather than an authorization-code
redirect: print a URL + short code, the user approves in their own browser, this process
polls until approved. The resulting session is stored via `session_store.py`
(`~/.aether-vault/session.json`) and picked up automatically by every other command
through `resolve_remote()`'s v1.3.3 extension — no per-command wiring needed.

`av auth *` (the `.env`-based OSS path) and `av token *` (DB-backed static tokens) are
both untouched; this is the third, SSO-driven credential path alongside them.
"""

from .core import *  # noqa: F401,F403 -- shared prelude (stdlib + helpers)
from .core import current_output_mode, emit_json, fail, find_repo_root, load_config, resolve_remote
from . import session_store

import time


def _resolve_url(url_opt: str | None) -> str:
    if url_opt:
        return url_opt.rstrip("/")
    repo_root = find_repo_root()
    if repo_root is not None:
        cfg = load_config(repo_root)
        return cfg.get("remote_url", "http://localhost:8000")
    return "http://localhost:8000"


@click.command(name="login")
@click.option("--provider", "provider_id", default=None,
              help="SSO provider id (see `av idp list`). Required when the server has "
                   "more than one configured provider.")
@click.option("--url", "url_opt", default=None,
              help="Registry URL. Defaults to this repo's configured remote if run "
                   "inside one, else http://localhost:8000.")
@click.option("--no-browser", is_flag=True,
              help="Don't try to open the login URL automatically -- just print it.")
def login(provider_id: str | None, url_opt: str | None, no_browser: bool) -> None:
    """Log in via SSO and store a session for this machine (`av logout` clears it)."""
    import requests

    url = _resolve_url(url_opt)
    ctx = click.get_current_context(silent=True)

    if not provider_id:
        try:
            resp = requests.get(f"{url}/api/sso-providers", timeout=10)
        except Exception:
            fail(ctx, "unreachable_queued", f"Could not reach {url} to list SSO providers.")
        if resp.status_code == 200:
            providers = [p for p in resp.json().get("providers", []) if p.get("kind") == "oidc"]
            if len(providers) == 1:
                provider_id = providers[0]["id"]
            elif len(providers) > 1:
                fail(ctx, "validation",
                     "Multiple SSO providers are configured -- pass --provider. "
                     f"Available: {', '.join(p['id'] for p in providers)}",
                     command="login")
            else:
                fail(ctx, "validation",
                     "No OIDC SSO provider is configured on this registry. "
                     "An admin must run `av idp add` first.", command="login")

    try:
        resp = requests.post(f"{url}/api/auth/device/code", json={"provider_id": provider_id}, timeout=10)
    except Exception:
        fail(ctx, "unreachable_queued", f"Could not reach {url} to start login.")
    if resp.status_code == 404:
        fail(ctx, "validation", f"No such SSO provider {provider_id!r}.", command="login")
    if resp.status_code != 200:
        fail(ctx, "validation", f"Could not start device login: HTTP {resp.status_code} {resp.text[:200]}",
             command="login")

    device = resp.json()
    verify_url = device["verification_uri_complete"]
    json_mode = current_output_mode() == "json"
    if not json_mode:
        # v1.3.3 fix (found before this ever shipped, by writing this command's own
        # --output json exit-code repro): printing this unconditionally would have
        # broken test_contract_matrix.py's "exactly one clean JSON envelope" contract
        # for every agent-facing caller of `av login --output json` -- the verification
        # URL/code are still surfaced in JSON mode, just inside the eventual envelope
        # (see the `data=` on both the success and the `login_required` failure below),
        # never as loose stdout text ahead of it.
        click.secho(f"To log in, open this URL in your browser:\n\n  {verify_url}\n", fg="cyan")
        click.secho(f"User code: {device['user_code']}", fg="cyan")

    if not no_browser:
        import webbrowser

        try:
            webbrowser.open(verify_url)
        except Exception:
            pass

    interval = device.get("interval", 5)
    deadline = time.time() + device.get("expires_in", 600)
    session_token = None
    while time.time() < deadline:
        time.sleep(interval)
        try:
            poll_resp = requests.post(f"{url}/api/auth/device/token",
                                      json={"device_code": device["device_code"]}, timeout=10)
        except Exception:
            continue
        if poll_resp.status_code == 200:
            session_token = poll_resp.json()["access_token"]
            break
        # 400 authorization_pending is the expected steady state while waiting -- keep
        # polling. Any other detail (expired_token) means give up now, not silently
        # loop until the deadline for no reason.
        detail = poll_resp.json().get("detail", {}) if poll_resp.headers.get("content-type", "").startswith("application/json") else {}
        if isinstance(detail, dict) and detail.get("error") == "expired_token":
            break

    if session_token is None:
        fail(ctx, "login_required", "Login was not completed in time -- run `av login` again.",
             command="login", data={"verification_uri_complete": verify_url, "user_code": device["user_code"]})

    whoami_resp = requests.get(f"{url}/api/auth/whoami", headers={"Authorization": f"Bearer {session_token}"}, timeout=10)
    who = whoami_resp.json() if whoami_resp.status_code == 200 else {}

    session_store.save_session(
        token=session_token, url=url, username=who.get("username"),
        provider_id=provider_id, tenant_id=who.get("tenant_id"),
        expires_at=time.time() + 8 * 3600,
    )

    if current_output_mode() == "json":
        emit_json(ctx, "login", data={"url": url, "username": who.get("username")})
        return
    click.secho(f"Logged in as {who.get('username', '(unknown)')} on {url}.", fg="green")


@click.command(name="logout")
def logout() -> None:
    """Clear the locally stored SSO session (does not revoke it server-side -- use
    `av token revoke` for a static token, or ask an admin to revoke your session row)."""
    session_store.clear_session()
    ctx = click.get_current_context(silent=True)
    if current_output_mode() == "json":
        emit_json(ctx, "logout", data={"logged_out": True})
        return
    click.secho("Logged out.", fg="green")


@click.command(name="whoami")
def whoami() -> None:
    """Show the identity the currently configured registry resolves you as."""
    import requests

    ctx = click.get_current_context(silent=True)
    repo_root = find_repo_root()
    if repo_root is not None:
        url, token = resolve_remote(repo_root, load_config(repo_root))
    else:
        session = session_store.load_session()
        url = session["url"] if session else "http://localhost:8000"
        token = session["token"] if session else None

    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        resp = requests.get(f"{url}/api/auth/whoami", headers=headers, timeout=10)
    except Exception:
        fail(ctx, "unreachable_queued", f"Could not reach {url}.")
    if resp.status_code != 200:
        fail(ctx, "validation", f"whoami failed: HTTP {resp.status_code} {resp.text[:200]}", command="whoami")

    result = resp.json()
    if current_output_mode() == "json":
        emit_json(ctx, "whoami", data=result)
        return
    click.echo(f"  registry:    {url}")
    click.echo(f"  username:    {result['username']}")
    click.echo(f"  auth method: {result['auth_method']}")
    click.echo(f"  tenant:      {result.get('tenant_id') or '(none -- anonymous)'}")
    if result.get("role_names"):
        click.echo(f"  roles:       {', '.join(result['role_names'])}")
    click.echo(f"  scopes:      {', '.join(result.get('scopes') or [])}")
