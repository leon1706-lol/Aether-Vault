"""Protected-mode token management (auth group). Patch-target names owned by main.py are
accessed late-bound via `_root.<name>` so test monkeypatching on the main namespace stays
effective.
"""

from .core import *  # noqa: F401,F403 -- shared prelude (stdlib + helpers)
from . import main as _root



@click.group()
def auth() -> None:
    """Manage the optional access-token gate ("Protected" mode). Unset/empty (the default)
    means "Anonymous" — no credentials needed; setting a token switches every route
    (reads included) to require it. The owner uses a shared secret (`set-token`);
    teammates get their own revocable tokens via `add-user`."""


def _restart_server_for_token_change(repo_root: Path) -> bool:
    """Best-effort restart of the running server after `.env`'s AV_API_TOKEN changes, so
    the new value takes effect immediately. Returns False if Docker isn't reachable or the
    restart fails -- `write_env_token` has already succeeded, so this is never data loss."""
    from . import docker_runtime

    # A JSON-mode caller must never get plain text leaked ahead of its emit_json envelope.
    json_mode = current_output_mode() == "json"
    if docker_runtime.check_docker_running() != docker_runtime.DockerCheckResult.RUNNING:
        if not json_mode:
            click.secho(
                "Docker isn't running — saved, but it'll take effect next time the server starts.",
                fg="yellow",
            )
        return False
    compose_file, _ = docker_runtime.resolve_compose_file(_root._find_source_root())
    # v1.2.2 engine topology: one container (aether-vault-engine) runs the registry
    # AND the webui; restarting it restarts both subservices together.
    if not docker_runtime.restart_service(compose_file, "aether-vault-engine"):
        if not json_mode:
            click.secho(
                "Saved, but restarting the server automatically failed — restart it manually "
                "(`docker compose up -d aether-vault-engine`) for the change to take effect.",
                fg="yellow",
            )
        return False
    return True


def _generate_and_apply_token(repo_root: Path, token: str | None = None) -> str:
    """Generates (if not given) and applies a token: writes it to .env, saves it to this
    repo's config, and restarts the running server. Shared by `av auth set-token` and
    `av init`'s "Generate a new token" choice."""
    import secrets as secrets_module

    from . import docker_runtime

    token = token or secrets_module.token_urlsafe(32)

    compose_file, _ = docker_runtime.resolve_compose_file(_root._find_source_root())
    docker_runtime.write_env_token(compose_file, token)

    cfg = load_config(repo_root)
    cfg["remote_api_token"] = token
    save_config(repo_root, cfg)

    _restart_server_for_token_change(repo_root)
    return token


@auth.command(name="set-token")
@click.argument("token", required=False, default=None)
def auth_set_token(token: str | None) -> None:
    """Set (or rotate) the access token, generating one if TOKEN is omitted. There's no
    password-reset flow since this is a shared secret, not a real account -- re-running
    this command IS the recovery path."""
    repo_root = ensure_repo()
    token = _generate_and_apply_token(repo_root, token)
    if current_output_mode() == "json":
        emit_json(None, "auth set-token", data={"token": token})
        return
    click.secho(f"Token set: {token}", fg="green")
    click.secho("Save this — it won't be shown again. Share it with teammates who need access.", fg="yellow")


@auth.command(name="clear")
def auth_clear() -> None:
    """Remove the access token everywhere — back to "Anonymous" mode."""
    from . import docker_runtime

    repo_root = ensure_repo()
    compose_file, _ = docker_runtime.resolve_compose_file(_root._find_source_root())
    docker_runtime.write_env_token(compose_file, None)

    cfg = load_config(repo_root)
    cfg.pop("remote_api_token", None)
    save_config(repo_root, cfg)

    _restart_server_for_token_change(repo_root)
    if current_output_mode() == "json":
        emit_json(None, "auth clear", data={"cleared": True})
        return
    click.secho("Token cleared — this registry is now Anonymous (no token required).", fg="green")


@auth.command(name="doctor")
def auth_doctor() -> None:
    """Diagnose Protected-mode onboarding: token configured, server reachable, token
    actually authenticates, AV_AUTH_USERS parses. Read-only; each check reports pass/fail
    plus the exact fix command."""
    from .client import AuthenticationError, VaultClient

    repo_root = ensure_repo()
    cfg = load_config(repo_root)
    json_mode = current_output_mode() == "json"
    checks: list[dict] = []

    def check(name: str, ok: bool, detail: str, fix: str | None = None) -> None:
        checks.append({"name": name, "ok": ok, "detail": detail, "fix": fix})

    token = cfg.get("remote_api_token")
    check("token_configured", bool(token),
          "A token is configured for this repo." if token else "No token configured (Anonymous mode).",
          None if token else "av auth set-token <token>")

    remote_url = cfg.get("remote_url", "http://localhost:8000")
    client = VaultClient(remote_url, token)
    reachable = client.server_available()
    check("server_reachable", reachable,
          f"{remote_url} reachable." if reachable else f"{remote_url} unreachable.",
          None if reachable else "Start the registry (docker compose up -d) or fix remote_url (av config --remote-url)")

    if token and reachable:
        try:
            client.fetch_all_refs()
            check("token_authenticates", True, "Token was accepted by the server.")
        except AuthenticationError:
            check("token_authenticates", False, "Token was REJECTED by the server (401).",
                  "av auth set-token <the-current-token>, or av auth rotate to mint a new one")
    elif token:
        check("token_authenticates", False, "Could not check — server unreachable.", None)

    from . import docker_runtime

    try:
        compose_file, _ = docker_runtime.resolve_compose_file(_root._find_source_root())
        raw = docker_runtime.read_env_token(compose_file, key="AV_AUTH_USERS")
        if raw:
            json.loads(raw)
            check("auth_users_parses", True, "AV_AUTH_USERS is valid JSON.")
        else:
            check("auth_users_parses", True, "AV_AUTH_USERS not set (owner-secret-only mode).")
    except ValueError:
        check("auth_users_parses", False, "AV_AUTH_USERS in .env is not valid JSON.",
              "Fix or remove the AV_AUTH_USERS line in .env by hand.")

    all_ok = all(c["ok"] for c in checks)
    if json_mode:
        emit_json(None, "auth doctor", data={"checks": checks, "healthy": all_ok})
        return
    for c in checks:
        label = "[OK]  " if c["ok"] else "[FAIL]"
        color = "green" if c["ok"] else "red"
        click.secho(f"{label} {c['name']}: {c['detail']}", fg=color)
        if not c["ok"] and c["fix"]:
            click.secho(f"       fix: {c['fix']}", fg="yellow")
    click.echo("")
    click.secho("Healthy." if all_ok else "Problems found — see fixes above.",
                fg="green" if all_ok else "red", bold=True)


@auth.command(name="rotate")
@click.option("--user", "username", default=None,
              help="Rotate this user's personal token instead of the owner shared secret.")
def auth_rotate(username: str | None) -> None:
    """Mint a fresh token, invalidating the old one immediately. Prints the new value once."""
    import secrets as secrets_module

    from . import docker_runtime

    repo_root = ensure_repo()
    json_mode = current_output_mode() == "json"
    new_token = secrets_module.token_urlsafe(32)

    if username:
        compose_file, _ = docker_runtime.resolve_compose_file(_root._find_source_root())
        users = _read_auth_users(compose_file)
        if username not in users:
            if json_mode:
                fail(None, "validation", f"No such user '{username}'.", command="auth rotate")
            click.secho(f"No such user '{username}' — use `av auth add-user` first.", fg="red")
            return
        # Preserve an existing expiry across rotation — a rotate is a credential swap,
        # not a reset of the user's expiry policy.
        existing_expiry = _user_expiry(users[username])
        users[username] = {"token": new_token, "expires_at": existing_expiry} \
            if existing_expiry else new_token
        _write_auth_users(compose_file, users)
        _restart_server_for_token_change(repo_root)
    else:
        _generate_and_apply_token(repo_root, new_token)

    if json_mode:
        emit_json(None, "auth rotate", data={"user": username, "token": new_token})
        return
    label = f"{username}'s" if username else "the"
    click.secho(f"Rotated {label} token: {new_token}", fg="green")
    click.secho("Save this — it won't be shown again. The old token stopped working immediately.",
                fg="yellow")


@auth.command(name="status")
def auth_status() -> None:
    """Report whether an access token is currently configured, without printing it."""
    repo_root = ensure_repo()
    cfg = load_config(repo_root)
    token = cfg.get("remote_api_token")
    if current_output_mode() == "json":
        masked = f"{'*' * max(len(token) - 4, 0)}{token[-4:]}" if token else None
        emit_json(None, "auth status",
                  data={"configured": bool(token), "masked_token": masked})
        return
    if not token:
        click.secho("Anonymous — no token configured for this repo.", fg="yellow")
        return
    masked = f"{'*' * max(len(token) - 4, 0)}{token[-4:]}"
    click.secho(f"Protected — token configured for this repo: {masked}", fg="green")


# ---------------------------------------------------------------------------
# Per-user access tokens (AV_AUTH_USERS JSON map in .env)
# ---------------------------------------------------------------------------

def _read_auth_users(compose_file: Path) -> dict:
    """Reads the AV_AUTH_USERS JSON map from .env; empty dict when absent/unset. A value
    is either a bare token string or {"token", "expires_at"} -- use `_user_token()`/
    `_user_expiry()` below to normalize."""
    from . import docker_runtime

    raw = docker_runtime.read_env_token(compose_file, key="AV_AUTH_USERS")
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except ValueError:
        raise click.ClickException(
            "AV_AUTH_USERS in .env is not valid JSON — fix or remove that line by hand."
        )
    if not isinstance(parsed, dict):
        raise click.ClickException("AV_AUTH_USERS must be a JSON object of {username: token}.")
    return {str(k): v for k, v in parsed.items()}


def _user_token(value) -> str:
    return value["token"] if isinstance(value, dict) else str(value)


def _user_expiry(value) -> str | None:
    return value.get("expires_at") if isinstance(value, dict) else None


def _user_scopes(value) -> list[str] | None:
    """This user's declared scopes, or None when unrestricted (the default)."""
    if isinstance(value, dict):
        scopes = value.get("scopes")
        if isinstance(scopes, list) and scopes:
            return scopes
    return None


def _write_auth_users(compose_file: Path, users: dict) -> None:
    """Writes the merged map back; an empty map removes the line entirely (Anonymous)."""
    from . import docker_runtime

    docker_runtime.write_env_token(
        compose_file, json.dumps(users) if users else None, key="AV_AUTH_USERS"
    )


@auth.command(name="add-user")
@click.argument("name")
@click.argument("token", required=False, default=None)
@click.option("--expires-in-days", "expires_in_days", type=int, default=None,
              help="Optional: this user's token stops authenticating after N days "
                   "(default: never expires).")
@click.option("--scope", "scopes", multiple=True,
              help="v1.3.1: restrict this token to specific permissions (repeatable), "
                   "e.g. --scope eval:write --scope review. Omit for an unrestricted "
                   "token (the default — matches every pre-v1.3.1 user). Enforced "
                   "server-side (server.py::require_scope) on routes that declare one;"
                   " see docs/rsi-operator-guide.md for the scope vocabulary.")
def auth_add_user(name: str, token: str | None, expires_in_days: int | None,
                   scopes: tuple[str, ...]) -> None:
    """Grant NAME its own access token (generating one if TOKEN is omitted). Per-user
    tokens work alongside the owner's shared secret; the token prints once."""
    import secrets as secrets_module

    from . import docker_runtime

    repo_root = ensure_repo()
    token = token or secrets_module.token_urlsafe(32)
    compose_file, _ = docker_runtime.resolve_compose_file(_root._find_source_root())
    users = _read_auth_users(compose_file)
    if name in users:
        if current_output_mode() == "json":
            emit_json(None, "auth add-user", data={"name": name, "added": False,
                                                     "reason": "already_exists"})
            return
        click.secho(
            f"User '{name}' already exists — revoke with `av auth remove-user {name}` "
            "and re-add to rotate.",
            fg="yellow",
        )
        return
    expires_at = None
    if expires_in_days is not None:
        import datetime as _dt

        expires_at = (_dt.datetime.now(_dt.timezone.utc)
                     + _dt.timedelta(days=expires_in_days)).isoformat()
    scope_list = sorted(set(scopes)) if scopes else None
    if expires_at is not None or scope_list:
        value: dict = {"token": token}
        if expires_at is not None:
            value["expires_at"] = expires_at
        if scope_list:
            value["scopes"] = scope_list
    else:
        value = token
    users[name] = value
    _write_auth_users(compose_file, users)
    _restart_server_for_token_change(repo_root)
    if current_output_mode() == "json":
        emit_json(None, "auth add-user", data={"name": name, "added": True, "token": token,
                                                "expires_at": expires_at,
                                                "scopes": scope_list})
        return
    click.secho(f"User '{name}' added. Token: {token}", fg="green")
    if expires_at:
        click.secho(f"Expires: {expires_at}", fg="cyan")
    if scope_list:
        click.secho(f"Scopes: {', '.join(scope_list)}", fg="cyan")
    click.secho(
        "Share it with them — they enable it via `av auth set-token <token>` in their repo.",
        fg="yellow",
    )


@auth.command(name="list-users")
def auth_list_users() -> None:
    """List per-user access tokens (masked, never printed in full)."""
    from . import docker_runtime

    compose_file, _ = docker_runtime.resolve_compose_file(_root._find_source_root())
    users = _read_auth_users(compose_file)
    if current_output_mode() == "json":
        emit_json(None, "auth list-users", data={
            "users": [{"name": n, "masked_token": f"{'*' * max(len(_user_token(v)) - 4, 0)}{_user_token(v)[-4:]}",
                      "expires_at": _user_expiry(v), "scopes": _user_scopes(v)}
                      for n, v in sorted(users.items())],
        })
        return
    if not users:
        click.secho("No per-user tokens configured (the owner shared secret may still apply).", fg="yellow")
        return
    for name, val in sorted(users.items()):
        tok = _user_token(val)
        masked = f"{'*' * max(len(tok) - 4, 0)}{tok[-4:]}"
        expiry_suffix = f"  (expires {_user_expiry(val)})" if _user_expiry(val) else ""
        scopes = _user_scopes(val)
        scope_suffix = f"  [scopes: {', '.join(scopes)}]" if scopes else ""
        click.echo(f"  {name}: {masked}{expiry_suffix}{scope_suffix}")


@auth.command(name="remove-user")
@click.argument("name")
def auth_remove_user(name: str) -> None:
    """Revoke NAME's personal access token."""
    from . import docker_runtime

    repo_root = ensure_repo()
    compose_file, _ = docker_runtime.resolve_compose_file(_root._find_source_root())
    users = _read_auth_users(compose_file)
    if name not in users:
        if current_output_mode() == "json":
            emit_json(None, "auth remove-user", data={"name": name, "removed": False,
                                                        "reason": "not_found"})
            return
        click.secho(f"No such user '{name}'.", fg="yellow")
        return
    del users[name]
    _write_auth_users(compose_file, users)
    _restart_server_for_token_change(repo_root)
    if current_output_mode() == "json":
        emit_json(None, "auth remove-user", data={"name": name, "removed": True})
        return
    click.secho(f"User '{name}' revoked.", fg="green")
