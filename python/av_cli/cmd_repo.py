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

        enterprise.run_enterprise_login_flow(repo_root)
        return

    from . import docker_runtime

    try:
        docker_runtime.ensure_local_backend_running(_root._find_source_root(), open_browser=False)
    except Exception as exc:
        click.secho(f"[WARN] Could not reach the local backend: {exc}", fg="yellow")


def _handle_init_protection_choice(
    repo_root: Path, yes: bool, protected_flag: bool, join_token: str | None
) -> dict | None:
    """The Anonymous/Protected prompt `av init` shows after choosing Local mode, plus its
    token-source follow-up. Saves whatever was decided to this repo's config; returns a
    dict describing the outcome in JSON mode (folded into `init`'s envelope), None in
    text mode."""
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

    json_mode = current_output_mode() == "json"

    if choice != "protected":
        return {"protection": "anonymous"} if json_mode else None

    if token_source == "generate":
        token = _generate_and_apply_token(repo_root)
        if json_mode:
            return {"protection": "protected", "token_source": "generated", "token": token}
        click.secho(f"Token set: {token}", fg="green")
        click.secho("Save this — it won't be shown again. Share it with teammates who need access.", fg="yellow")
        return None

    # "existing" — joining a registry someone else already protected. Validate before saving:
    # an unreachable server and a rejected token must not look the same to the user.
    if not existing_token:
        if json_mode:
            return {"protection": "anonymous", "reason": "no_token_entered"}
        click.secho("No token entered — leaving this registry Anonymous.", fg="yellow")
        return None

    cfg = load_config(repo_root)
    client = VaultClient(cfg.get("remote_url", "http://localhost:8000"), existing_token)
    unreachable = False
    if not client.server_available():
        unreachable = True
        if not json_mode:
            click.secho(
                "Could not reach the server to verify this token right now — saved anyway; "
                "you'll find out if it's wrong on your next command (`av auth status` to check).",
                fg="yellow",
            )
    else:
        try:
            client.fetch_all_refs()
        except AuthenticationError:
            if json_mode:
                return {"protection": "anonymous", "reason": "token_rejected"}
            click.secho(
                "That token was rejected by the server — leaving this registry Anonymous. "
                "Run `av auth set-token <token>` once you have the correct one.",
                fg="red",
            )
            return None

    cfg["remote_api_token"] = existing_token
    save_config(repo_root, cfg)
    if json_mode:
        return {"protection": "protected", "token_source": "existing",
                "server_verified": not unreachable}
    click.secho("Token saved.", fg="green")
    return None


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

    json_mode = current_output_mode() == "json"
    # init's whole shape is an interactive wizard by design -- JSON mode requires the
    # fully-scripted invocation shape (--mode, --yes, --no-repl) instead of degrading silently.
    if json_mode and (mode is None or not yes or not no_repl):
        fail(None, "validation",
             "av init --output json requires --mode, --yes, and --no-repl — it never "
             "prompts or opens the REPL in JSON mode.", command="init")

    repo_root = Path.cwd()
    av_dir = repo_root / ".av"

    if av_dir.exists():
        if json_mode:
            emit_json(None, "init", data={"initialized": False, "reason": "already_initialized",
                                          "path": str(av_dir)})
            return
        click.secho(f"Repository already initialized at {av_dir}", fg="yellow")
        cfg = load_config(repo_root)
        if not no_repl:
            _reconnect_existing_repo(repo_root, cfg)
            from . import repl

            repl.run_repl(repo_root, login_mode=cfg.get("login_mode", "local"))
        return

    if not json_mode:
        ui.print_banner("Aether-Vault", "version control for ML models & datasets")

    # --mode enterprise logs in against the registry's configured OIDC provider
    # (enterprise.py). Not offered by an interactive picker -- only reachable via this flag.
    if mode is not None:
        login_mode = mode
    else:
        login_mode = "local"

    _init_repo_structure(repo_root)
    if not json_mode:
        click.secho(f"Initialized empty Aether-Vault repository in {av_dir}", fg="green")

    if login_mode == "enterprise":
        from . import enterprise

        established = enterprise.run_enterprise_login_flow(repo_root)
        if not established:
            login_mode = "local"

    cfg = load_config(repo_root)
    cfg["login_mode"] = login_mode
    save_config(repo_root, cfg)

    # Anonymous-vs-Protected only applies to Local mode -- this shared-secret token is the
    # free/OSS-tier mechanism, not layered underneath Enterprise login.
    protection_result = None
    if login_mode == "local":
        protection_result = _handle_init_protection_choice(repo_root, yes, protected_flag, join_token)
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

    if json_mode:
        # --no-repl is required above, so the REPL/update-notice human-only branches below
        # never execute in JSON mode — this is the one clean envelope for the whole command.
        emit_json(None, "init", data={
            "initialized": True, "path": str(av_dir), "login_mode": login_mode,
            "protection": protection_result,
        })
        return

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

    json_mode = current_output_mode() == "json"
    # Every confirm() below defaults to True and only prompts when not already answered --
    # requiring --yes in JSON mode makes the "no prompt in JSON mode" guarantee explicit.
    if json_mode and docker_flag and not yes:
        fail(None, "validation",
             "av update --docker --output json requires --yes (no interactive confirm in "
             "JSON mode).", command="update")

    if docker_flag:
        from . import docker_runtime

        result = docker_runtime.check_for_docker_update(_root._find_source_root())
        if not result.checked:
            if json_mode:
                emit_json(None, "update", data={"docker_checked": False, "message": result.message})
                return
            click.secho(result.message, fg="yellow")
            return
        if not json_mode:
            click.secho(result.message, fg="green" if not result.updated else "yellow")
        if not result.updated:
            if json_mode:
                emit_json(None, "update", data={"docker_checked": True, "docker_updated": False,
                                                "message": result.message})
                return
            return

        if yes or (not json_mode and click.confirm("Restart the local backend now to apply it?", default=True)):
            compose_file, _ = docker_runtime.resolve_compose_file(_root._find_source_root())
            for service in docker_runtime.RELEASE_IMAGES:
                docker_runtime.restart_service(compose_file, service)
            # Only remove the old images once the new containers are confirmed up on the new
            # ones — never leave a window where neither image is safely runnable.
            docker_runtime.remove_old_images(result.old_image_ids)
            if json_mode:
                emit_json(None, "update", data={"docker_checked": True, "docker_updated": True,
                                                "restarted": True})
                return
            click.secho("Local backend restarted and old images cleaned up.", fg="green")
        elif json_mode:
            emit_json(None, "update", data={"docker_checked": True, "docker_updated": True,
                                            "restarted": False})
        return

    if enable_auto_update or disable_auto_update:
        cfg = update_check.load_user_config()
        cfg["auto_update"] = bool(enable_auto_update)
        update_check.save_user_config(cfg)
        state = "enabled" if enable_auto_update else "disabled"
        if json_mode:
            emit_json(None, "update", data={"auto_update": bool(enable_auto_update)})
            return
        click.secho(f"Auto-update {state}.", fg="green")
        return

    if list_versions_flag:
        versions = update_check.list_versions()
        if versions is None:
            if json_mode:
                fail(None, "unreachable_queued", "Could not reach PyPI to list versions.",
                     command="update")
            click.secho("Could not reach PyPI to list versions.", fg="red")
            return
        if json_mode:
            emit_json(None, "update", data={"versions": versions, "installed": __version__})
            return
        for v in versions:
            marker = " (installed)" if v == __version__ else ""
            click.echo(f"{v}{marker}")
        click.echo(f"\nRun `pip install {update_check.PACKAGE_NAME}==<version>` to switch.")
        return

    result = update_check.check_for_update(force=True)
    if result is None:
        if json_mode:
            fail(None, "unreachable_queued", "Could not reach PyPI to check for updates.",
                 command="update")
        click.secho("Could not reach PyPI to check for updates.", fg="red")
        return
    if not result.is_outdated:
        if json_mode:
            emit_json(None, "update", data={"current": result.current, "is_outdated": False})
            return
        click.secho(f"aether-vault {result.current} is up to date.", fg="green")
        return

    if not json_mode:
        click.secho(f"aether-vault {result.current} → {result.latest} available.", fg="yellow")
    if check_only:
        if json_mode:
            emit_json(None, "update", data={"current": result.current, "latest": result.latest,
                                            "is_outdated": True, "upgraded": False})
            return
        return

    if json_mode and not yes:
        fail(None, "validation",
             "av update --output json requires --yes to actually upgrade (pass --check "
             "instead to only report).", command="update")

    if yes or (not json_mode and click.confirm("Upgrade now?", default=True)):
        import subprocess

        subprocess.run([sys.executable, "-m", "pip", "install", "--upgrade", update_check.PACKAGE_NAME])
        if json_mode:
            emit_json(None, "update", data={"current": result.current, "latest": result.latest,
                                            "is_outdated": True, "upgraded": True})
    elif json_mode:
        emit_json(None, "update", data={"current": result.current, "latest": result.latest,
                                        "is_outdated": True, "upgraded": False})
