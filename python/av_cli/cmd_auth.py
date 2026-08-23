"""Protected-mode token management (auth group).

Bodies moved verbatim from main.py (Point-13 split). Patch-target names owned by
main.py (`_find_source_root`, `_update_readme_test_badge`) are accessed late-bound via
`_root.<name>` so test monkeypatching on the main namespace stays effective.
"""

from .core import *  # noqa: F401,F403 -- shared prelude (stdlib + helpers)
from . import main as _root



@click.group()
def auth() -> None:
    """Manage the optional shared-secret access token ("Protected" mode).

    Unset/empty (the default) means the registry is "Anonymous" — every route behaves exactly
    as it always has, no credentials needed. Setting a token switches the server to
    "Protected" — every route (reads included) then requires it. See `av init`'s
    Anonymous/Protected prompt for the same choice at setup time.
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
