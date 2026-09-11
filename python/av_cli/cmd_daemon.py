"""`av daemon start/stop/status/restart` -- lifecycle management for V1.5.0's opt-in
background command executor. See `daemon.py`'s module docstring for the design invariants.
"""
from .core import *  # noqa: F401,F403 -- shared prelude (stdlib + helpers)
from .core import current_output_mode, emit_json  # noqa: E402


@click.group()
def daemon() -> None:
    """Manage the optional `av` background daemon (opt-in; off by default).

    A warm interpreter that serves `add`/`status`/`commit` without paying Python's own
    startup cost on every invocation. Never auto-starts unless AV_DAEMON=1 or
    `.av/config`'s `"daemon": {"enabled": true}` is set -- running `av daemon start` IS
    the opt-in otherwise. Falls back to the normal in-process path silently on any failure;
    it can only make `av` faster, never change what a command does.
    """


def _cli_version() -> str:
    from . import __version__

    return __version__


@daemon.command("start")
@click.option("--idle-timeout", type=float, default=None,
              help="Seconds of inactivity before the daemon exits on its own (default: 900).")
@click.option("--foreground", is_flag=True, default=False,
              help="Run in this process instead of spawning a detached background one -- for debugging.")
def daemon_start(idle_timeout: float | None, foreground: bool) -> None:
    """Start the daemon for the current repo, if one isn't already running."""
    from . import daemon as daemon_module
    from . import daemon_client, daemon_common

    repo_root = ensure_repo()
    cli_version = _cli_version()
    json_mode = current_output_mode() == "json"

    existing = daemon_client.read_status(repo_root, cli_version)
    if existing:
        if json_mode:
            emit_json(None, "daemon", data={"action": "start", "already_running": True, **existing})
        else:
            click.secho(f"Daemon already running (pid {existing.get('pid')}).", fg="yellow")
        return

    kwargs = {}
    if idle_timeout is not None:
        kwargs["idle_timeout"] = idle_timeout

    if foreground:
        if not json_mode:
            click.secho("Running daemon in the foreground (Ctrl+C to stop)...", fg="cyan")
        daemon_module.run(repo_root, cli_version, **kwargs)
        return

    lock_path = daemon_common.lock_file(repo_root, daemon_module.PROTOCOL_VERSION, cli_version)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
    except FileExistsError:
        # Another `av daemon start` may have just won the race, or a stale lock from a
        # crashed daemon -- either way, check whether a daemon is actually live now.
        existing = daemon_client.read_status(repo_root, cli_version)
        if existing:
            if json_mode:
                emit_json(None, "daemon", data={"action": "start", "already_running": True, **existing})
            else:
                click.secho(f"Daemon already running (pid {existing.get('pid')}).", fg="yellow")
            return
        lock_path.unlink(missing_ok=True)  # stale -- clear it and fall through to spawn

    args = [sys.executable, "-m", "av_cli.daemon", str(repo_root), cli_version]
    if idle_timeout is not None:
        args.append(str(idle_timeout))
    daemon_client.spawn_detached(args)
    lock_path.unlink(missing_ok=True)

    # Give it a moment to bind and write its state file before reporting -- best-effort,
    # not a hard guarantee; a caller relying on the daemon being ready this instant should
    # check `av daemon status`.
    import time as _time

    deadline = _time.monotonic() + 3.0
    status = None
    while _time.monotonic() < deadline:
        status = daemon_client.read_status(repo_root, cli_version)
        if status:
            break
        _time.sleep(0.1)

    if json_mode:
        emit_json(None, "daemon", data={"action": "start", "started": status is not None,
                                        **(status or {})})
    elif status:
        click.secho(f"Daemon started (pid {status.get('pid')}).", fg="green")
    else:
        click.secho("Daemon spawn requested, but it hasn't reported ready yet -- "
                     "check `av daemon status` shortly.", fg="yellow")


@daemon.command("stop")
def daemon_stop() -> None:
    """Stop the daemon running for the current repo, if any."""
    from . import daemon as daemon_module
    from . import daemon_client, daemon_common

    repo_root = ensure_repo()
    cli_version = _cli_version()
    json_mode = current_output_mode() == "json"

    status = daemon_client.read_status(repo_root, cli_version)
    if not status:
        if json_mode:
            emit_json(None, "daemon", data={"action": "stop", "was_running": False})
        else:
            click.secho("No daemon running for this repo.", fg="yellow")
        return

    pid = status.get("pid")
    stopped = False
    if pid:
        try:
            if sys.platform == "win32":
                subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                               capture_output=True, check=False)
            else:
                os.kill(pid, 15)  # SIGTERM
            stopped = True
        except (OSError, ProcessLookupError):
            pass

    state_path = daemon_common.state_file(repo_root, daemon_module.PROTOCOL_VERSION, cli_version)
    for p in (state_path, state_path.with_suffix(".key")):
        p.unlink(missing_ok=True)

    if json_mode:
        emit_json(None, "daemon", data={"action": "stop", "was_running": True, "stopped": stopped})
    else:
        click.secho("Daemon stopped." if stopped else
                     "Daemon's state file removed (process may have already exited).", fg="green")


@daemon.command("status")
def daemon_status() -> None:
    """Show whether a daemon is running for the current repo."""
    from . import daemon_client

    repo_root = ensure_repo()
    cli_version = _cli_version()
    status = daemon_client.read_status(repo_root, cli_version)

    if current_output_mode() == "json":
        emit_json(None, "daemon", data={"running": status is not None, **(status or {})})
        return

    if not status:
        click.secho("No daemon running for this repo.", fg="yellow")
        return
    click.secho("Daemon is running:", fg="green")
    click.echo(f"  pid          : {status.get('pid')}")
    click.echo(f"  cli_version  : {status.get('cli_version')}")
    click.echo(f"  protocol     : {status.get('protocol')}")
    click.echo(f"  repo_root    : {status.get('repo_root')}")
    click.echo(f"  endpoint     : {status.get('endpoint')}")


@daemon.command("restart")
@click.pass_context
def daemon_restart(ctx: click.Context) -> None:
    """Stop then start the daemon for the current repo."""
    ctx.invoke(daemon_stop)
    ctx.invoke(daemon_start, idle_timeout=None, foreground=False)
