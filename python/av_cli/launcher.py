"""V1.5.0: the REAL console-script entry point (`[project.scripts] av = "av_cli.launcher:main"`).

This module's own import list is deliberately tiny -- `av_cli.main` (the previous entry
point) costs ~450-600ms to import even after V1.5.0's lazy-command-registration work,
because click, `core.py`'s prelude, and whatever the target command needs all still have to
load. A daemon can only ever help if the CLIENT'S OWN entry point avoids that cost for the
commands it can serve -- so this module imports only `sys` at module scope, decides
daemon-vs-fallback, and only reaches into `av_cli.daemon_client` (itself os/socket/json only
until a connection actually succeeds) on the daemon path. `av_cli.main:run` is untouched and
still directly invocable (tests, CI, anyone with an older console-script shim from before an
upgrade) -- this module is purely an optional fast path in front of it, never a replacement.
"""
from __future__ import annotations

import sys


def _repo_root_or_none():
    """Standalone copy of core.py's find_repo_root() -- deliberately not imported from
    there, since importing core.py at all defeats this module's entire purpose. Simple and
    stable enough that duplicating it is the right trade."""
    from pathlib import Path

    cwd = Path.cwd().resolve()
    for parent in [cwd, *cwd.parents]:
        if (parent / ".av").is_dir():
            return parent
    return None


def _try_daemon(argv: list[str]) -> int | None:
    """Returns an exit code if the daemon handled the command, else None (any reason at
    all -- caller must fall back in-process). When no daemon is reachable but this repo/env
    has opted into auto-spawn (AV_DAEMON=1, or `.av/config`'s `"daemon": {"enabled": true}`),
    fires one off in the background for NEXT time -- this invocation still always falls back
    in-process rather than waiting on a cold daemon's warm-up."""
    from . import daemon_common

    mode = daemon_common.enabled_mode()  # cheap env-only check first
    if mode == "never":
        return None
    if not argv or argv[0] not in daemon_common.ALLOWED_COMMANDS:
        return None
    repo_root = _repo_root_or_none()
    if repo_root is None:
        return None
    if mode == "use_only":
        mode = daemon_common.enabled_mode(repo_root)  # also honors .av/config's opt-in

    from . import __version__ as cli_version
    from .daemon_client import call_daemon

    response = call_daemon(repo_root, cli_version, argv)
    if response is None:
        if mode == "auto_spawn":
            from .daemon_client import maybe_auto_spawn

            try:
                maybe_auto_spawn(repo_root, cli_version)
            except Exception:
                pass  # never let a spawn attempt turn into a failed command
        return None
    sys.stdout.write(response.get("stdout", ""))
    sys.stderr.write(response.get("stderr", ""))
    return int(response.get("exit_code", 1))


def main() -> None:
    argv = sys.argv[1:]
    # --help/--version and anything not a bare allowlisted-command invocation always takes
    # the normal path without even trying the daemon -- keeps the decision cheap (no state
    # file read) for the majority of invocations that could never qualify anyway.
    if argv and argv[0] in ("add", "status", "commit") and "--help" not in argv:
        try:
            exit_code = _try_daemon(argv)
        except Exception:
            exit_code = None  # any daemon-path failure at all -> fall back, never raise here
        if exit_code is not None:
            sys.exit(exit_code)

    from .main import run

    run()


if __name__ == "__main__":
    main()
