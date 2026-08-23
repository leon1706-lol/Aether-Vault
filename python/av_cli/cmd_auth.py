"""Protected-mode token management (auth group).

Bodies moved verbatim from main.py (Point-13 split). Patch-target names owned by
main.py (`_find_source_root`, `_update_readme_test_badge`) are accessed late-bound via
`_root.<name>` so test monkeypatching on the main namespace stays effective.
"""

from .core import *  # noqa: F401,F403 -- shared prelude (stdlib + helpers)
from . import main as _root



@click.group()
def auth() -> None:
    """Manage the optional access-token gate ("Protected" mode).

    Unset/empty (the default) means the registry is "Anonymous" — every route behaves exactly
    as it always has, no credentials needed. Setting a token switches the server to
    "Protected" — every route (reads included) then requires it. The owner uses a shared
    secret (`set-token`); teammates get their own revocable tokens via `add-user`. See
    `av init`'s Anonymous/Protected prompt for the same choice at setup time.
    """


def _restart_server_for_token_change(repo_root: Path) -> bool:
    """Best-effort restart of the running server after `.env`'s AV_API_TOKEN changes, so the
    new value takes effect immediately instead of only on the next manual restart. Returns
    False (with a clear message already printed) if Docker isn't reachable or the restart
    itself fails — `write_env_token` has already succeeded by the time this runs, so a failed
    restart here just means "takes effect next time the stack starts," not data loss.
    """
    from . import docker_runtime

    if docker_runtime.check_docker_running() != docker_runtime.DockerCheckResult.RUNNING:
        click.secho(
            "Docker isn't running — saved, but it'll take effect next time the server starts.",
            fg="yellow",
        )
        return False
    compose_file, _ = docker_runtime.resolve_compose_file(_root._find_source_root())
    if not docker_runtime.restart_service(compose_file, "aether-vault-server"):
        click.secho(
            "Saved, but restarting the server automatically failed — restart it manually "
            "(`docker compose up -d aether-vault-server`) for the change to take effect.",
            fg="yellow",
        )
        return False
    return True


def _generate_and_apply_token(repo_root: Path, token: str | None = None) -> str:
    """Generates (if not given) and applies a token: writes it to .env next to whichever
    compose file is in play, saves it to this repo's config, and restarts the running server
    so it takes effect immediately. Shared by `av auth set-token` and `av init`'s "Generate a
    new token" choice so the two don't duplicate this logic."""
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
    """Set (or rotate) the access token, generating one if TOKEN is omitted.

    Writes it to the .env file next to whichever docker-compose file is in play, saves the
    same value to this repo's local config so the CLI starts sending it immediately, and
    restarts the running server so the change takes effect right away. Re-running this with a
    new value (or none, to generate a fresh one) is also the "I forgot it" path — there's no
    password-reset flow since this is a shared secret, not a real account: recovery is simply
    "you have shell access to the machine running the server."
    """
    repo_root = ensure_repo()
    token = _generate_and_apply_token(repo_root, token)
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
    click.secho("Token cleared — this registry is now Anonymous (no token required).", fg="green")


@auth.command(name="status")
def auth_status() -> None:
    """Report whether an access token is currently configured, without printing it."""
    repo_root = ensure_repo()
    cfg = load_config(repo_root)
    token = cfg.get("remote_api_token")
    if not token:
        click.secho("Anonymous — no token configured for this repo.", fg="yellow")
        return
    masked = f"{'*' * max(len(token) - 4, 0)}{token[-4:]}"
    click.secho(f"Protected — token configured for this repo: {masked}", fg="green")


# ---------------------------------------------------------------------------
# Per-user access tokens (AV_AUTH_USERS JSON map in .env)
# ---------------------------------------------------------------------------

def _read_auth_users(compose_file: Path) -> dict[str, str]:
    """Reads the AV_AUTH_USERS JSON map from .env; empty dict when absent/unset."""
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
    return {str(k): str(v) for k, v in parsed.items()}


def _write_auth_users(compose_file: Path, users: dict[str, str]) -> None:
    """Writes the merged map back; an empty map removes the line entirely (Anonymous)."""
    from . import docker_runtime

    docker_runtime.write_env_token(
        compose_file, json.dumps(users) if users else None, key="AV_AUTH_USERS"
    )


@auth.command(name="add-user")
@click.argument("name")
@click.argument("token", required=False, default=None)
def auth_add_user(name: str, token: str | None) -> None:
    """Grant NAME its own access token (generating one if TOKEN is omitted).

    Per-user tokens work alongside the owner's shared secret: each teammate puts their
    personal token into their own repo (`av auth set-token <their-token>` on their
    machine) and their pushes are attributed to NAME when they don't set a custom
    AV_AUTHOR. The token prints once — share it over a trusted channel.
    """
    import secrets as secrets_module

    from . import docker_runtime

    repo_root = ensure_repo()
    token = token or secrets_module.token_urlsafe(32)
    compose_file, _ = docker_runtime.resolve_compose_file(_root._find_source_root())
    users = _read_auth_users(compose_file)
    if name in users:
        click.secho(
            f"User '{name}' already exists — revoke with `av auth remove-user {name}` "
            "and re-add to rotate.",
            fg="yellow",
        )
        return
    users[name] = token
    _write_auth_users(compose_file, users)
    _restart_server_for_token_change(repo_root)
    click.secho(f"User '{name}' added. Token: {token}", fg="green")
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
    if not users:
        click.secho("No per-user tokens configured (the owner shared secret may still apply).", fg="yellow")
        return
    for name, tok in sorted(users.items()):
        masked = f"{'*' * max(len(tok) - 4, 0)}{tok[-4:]}"
        click.echo(f"  {name}: {masked}")


@auth.command(name="remove-user")
@click.argument("name")
def auth_remove_user(name: str) -> None:
    """Revoke NAME's personal access token."""
    from . import docker_runtime

    repo_root = ensure_repo()
    compose_file, _ = docker_runtime.resolve_compose_file(_root._find_source_root())
    users = _read_auth_users(compose_file)
    if name not in users:
        click.secho(f"No such user '{name}'.", fg="yellow")
        return
    del users[name]
    _write_auth_users(compose_file, users)
    _restart_server_for_token_change(repo_root)
    click.secho(f"User '{name}' revoked.", fg="green")
