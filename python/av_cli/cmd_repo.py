"""Repository lifecycle: init/update plus their private helpers.

Bodies moved verbatim from main.py (Point-13 split). Patch-target names owned by
main.py (`_find_source_root`, `_update_readme_test_badge`) are accessed late-bound via
`_root.<name>` so test monkeypatching on the main namespace stays effective.
"""

from .core import *  # noqa: F401,F403 -- shared prelude (stdlib + helpers)
from .core import _init_repo_structure
from . import main as _root
from .cmd_auth import _generate_and_apply_token
from . import __version__
import sys



def _reconnect_existing_repo(repo_root: Path, cfg: dict) -> None:
    """Reconnect to an already-initialized repo's stored backend (no questions asked)."""
    login_mode = cfg.get("login_mode", "local")
    if login_mode == "enterprise":
        from . import enterprise

        enterprise.run_enterprise_login_flow()
        return

    from . import docker_runtime

    try:
        docker_runtime.ensure_local_backend_running(_root._find_source_root(), open_browser=False)
    except Exception as exc:
        click.secho(f"[WARN] Could not reach the local backend: {exc}", fg="yellow")


def _handle_init_protection_choice(
    repo_root: Path, yes: bool, protected_flag: bool, join_token: str | None
) -> None:
    """The Anonymous/Protected prompt `av init` shows after choosing Local mode, plus its
    "Generate a new token" vs "Enter an existing one" follow-up. Saves whatever was decided to
    this repo's config (and, for "generate," writes/applies it via `av auth set-token`'s same
    underlying helper) — never touched at all if the result is Anonymous, matching today's
    behavior exactly.
    """
    from . import ui
    from .client import AuthenticationError, VaultClient

    if join_token:
        choice, token_source = "protected", "existing"
        existing_token = join_token
    elif protected_flag:
        choice, token_source = "protected", "generate"
        existing_token = None
    elif yes or not ui.is_interactive():
        choice, token_source = "anonymous", None
        existing_token = None
    else:
        choice = ui.select_protection_mode()
        token_source = ui.select_token_source() if choice == "protected" else None
        existing_token = ui.prompt_for_existing_token() if token_source == "existing" else None

    if choice != "protected":
        return

    if token_source == "generate":
        token = _generate_and_apply_token(repo_root)
        click.secho(f"Token set: {token}", fg="green")
        click.secho("Save this — it won't be shown again. Share it with teammates who need access.", fg="yellow")
        return

    # "existing" — joining a registry someone else already protected. Validate before saving:
    # an unreachable server and a rejected token must not look the same to the user.
    if not existing_token:
        click.secho("No token entered — leaving this registry Anonymous.", fg="yellow")
        return

    cfg = load_config(repo_root)
    client = VaultClient(cfg.get("remote_url", "http://localhost:8000"), existing_token)
    if not client.server_available():
        click.secho(
            "Could not reach the server to verify this token right now — saved anyway; "
            "you'll find out if it's wrong on your next command (`av auth status` to check).",
            fg="yellow",
        )
    else:
        try:
            client.fetch_all_refs()
        except AuthenticationError:
            click.secho(
                "That token was rejected by the server — leaving this registry Anonymous. "
                "Run `av auth set-token <token>` once you have the correct one.",
                fg="red",
            )
            return

    cfg["remote_api_token"] = existing_token
    save_config(repo_root, cfg)
    click.secho("Token saved.", fg="green")


@click.command()
@click.option(
    "--mode", "mode", type=click.Choice(["local", "enterprise"]), default=None,
    help="Skip the interactive prompt and use this login mode.",
)
@click.option("--yes", "-y", is_flag=True, default=False, help="Skip interactive prompts.")
@click.option(
    "--no-repl", is_flag=True, default=False,
    help="Don't enter the interactive session after init (used by scripts/CI).",
)
@click.option(
    "--protected", "protected_flag", is_flag=True, default=False,
    help="Non-interactive equivalent of choosing Protected + Generate a new token.",
)
@click.option(
    "--token", "join_token", default=None,
    help="Non-interactive equivalent of choosing Protected + Enter an existing token — for "
         "joining a registry someone else already protected.",
)
def init(mode: str | None, yes: bool, no_repl: bool, protected_flag: bool, join_token: str | None) -> None:
    """Initialize a new Aether-Vault repository in the current directory."""
    from . import ui

    repo_root = Path.cwd()
    av_dir = repo_root / ".av"

    if av_dir.exists():
        click.secho(f"Repository already initialized at {av_dir}", fg="yellow")
        cfg = load_config(repo_root)
        if not no_repl:
            _reconnect_existing_repo(repo_root, cfg)
            from . import repl

            repl.run_repl(repo_root, login_mode=cfg.get("login_mode", "local"))
        return

    ui.print_banner("Aether-Vault", "version control for ML models & datasets")

    # Enterprise mode is intentionally not offered interactively yet (the account-login flow
    # is unbuilt; selecting it today just falls back to Local) — the choice stays reachable
    # only via the explicit `--mode enterprise` flag so scripts keep working and the
    # enterprise.py seam stays wired for the real implementation.
    if mode is not None:
        login_mode = mode
    else:
        login_mode = "local"

    _init_repo_structure(repo_root)
    click.secho(f"Initialized empty Aether-Vault repository in {av_dir}", fg="green")

    if login_mode == "enterprise":
        from . import enterprise

        established = enterprise.run_enterprise_login_flow()
        if not established:
            login_mode = "local"

    cfg = load_config(repo_root)
    cfg["login_mode"] = login_mode
    save_config(repo_root, cfg)

    # Anonymous-vs-Protected only applies to Local mode — Enterprise has its own (separate,
    # not-yet-built) account-based auth system; this shared-secret token is the free/OSS-tier
    # mechanism, not something to layer underneath Enterprise login too.
    if login_mode == "local":
        _handle_init_protection_choice(repo_root, yes, protected_flag, join_token)
        cfg = load_config(repo_root)  # re-load: the protection-choice handler may have saved a token

    if login_mode == "local" and not no_repl:
        from . import docker_runtime

        try:
            docker_runtime.ensure_local_backend_running(
                _root._find_source_root(), open_browser=False, api_token=cfg.get("remote_api_token"),
            )
        except Exception as exc:
            click.secho(
                f"[WARN] Could not start the local backend ({exc}). Run `av webui` later once Docker is ready.",
                fg="yellow",
            )

    from . import update_check

    result = update_check.check_for_update()
    if result is not None and result.is_outdated:
        click.secho(
            f"\naether-vault {result.current} → {result.latest} available — run `av update`",
            fg="yellow",
        )

    if not no_repl:
        from . import repl

        repl.run_repl(repo_root, login_mode=login_mode)


@click.command()
@click.option("--check", "check_only", is_flag=True, default=False, help="Only report; don't prompt to upgrade.")
@click.option("--list-versions", "list_versions_flag", is_flag=True, default=False, help="List every published version.")
@click.option("--enable-auto-update", is_flag=True, default=False, help="Turn on silent auto-update.")
@click.option("--disable-auto-update", is_flag=True, default=False, help="Turn off silent auto-update.")
@click.option("--docker", "docker_flag", is_flag=True, default=False,
              help="Pull the latest local Docker backend image and restart it if it changed. "
                   "Separate from the CLI update above — never bundled into plain `av update`.")
@click.option("--yes", "-y", is_flag=True, default=False, help="Skip the restart confirmation prompt (used with --docker).")
def update(check_only: bool, list_versions_flag: bool, enable_auto_update: bool, disable_auto_update: bool,
           docker_flag: bool, yes: bool) -> None:
    """Check for, and optionally install, the latest aether-vault release."""
    from . import update_check

    if docker_flag:
        from . import docker_runtime

        result = docker_runtime.check_for_docker_update(_root._find_source_root())
        if not result.checked:
            click.secho(result.message, fg="yellow")
            return
        click.secho(result.message, fg="green" if not result.updated else "yellow")
        if not result.updated:
            return

        if yes or click.confirm("Restart the local backend now to apply it?", default=True):
            compose_file, _ = docker_runtime.resolve_compose_file(_root._find_source_root())
            for service in docker_runtime.RELEASE_IMAGES:
                docker_runtime.restart_service(compose_file, service)
            # Only remove the old images once the new containers are confirmed up on the new
            # ones — never leave a window where neither image is safely runnable.
            docker_runtime.remove_old_images(result.old_image_ids)
            click.secho("Local backend restarted and old images cleaned up.", fg="green")
        return

    if enable_auto_update or disable_auto_update:
        cfg = update_check.load_user_config()
        cfg["auto_update"] = bool(enable_auto_update)
        update_check.save_user_config(cfg)
        state = "enabled" if enable_auto_update else "disabled"
        click.secho(f"Auto-update {state}.", fg="green")
        return

    if list_versions_flag:
        versions = update_check.list_versions()
        if versions is None:
            click.secho("Could not reach PyPI to list versions.", fg="red")
            return
        for v in versions:
            marker = " (installed)" if v == __version__ else ""
            click.echo(f"{v}{marker}")
        click.echo(f"\nRun `pip install {update_check.PACKAGE_NAME}==<version>` to switch.")
        return

    result = update_check.check_for_update(force=True)
    if result is None:
        click.secho("Could not reach PyPI to check for updates.", fg="red")
        return
    if not result.is_outdated:
        click.secho(f"aether-vault {result.current} is up to date.", fg="green")
        return

    click.secho(f"aether-vault {result.current} → {result.latest} available.", fg="yellow")
    if check_only:
        return

    if click.confirm("Upgrade now?", default=True):
        import subprocess

        subprocess.run([sys.executable, "-m", "pip", "install", "--upgrade", update_check.PACKAGE_NAME])
