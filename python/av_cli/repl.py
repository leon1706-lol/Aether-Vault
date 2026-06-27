"""Interactive REPL session entered after `av init` / bare `av` in an initialized repo.

Lets a user stay "inside" aether-vault and run several commands (still prefixed with `av`,
for muscle-memory consistency with using it outside the shell) without re-invoking the
process each time. Dispatches into the same Click group used for one-shot invocations, so
behavior never diverges between "av status" typed in a normal shell vs. inside this session.
"""

from __future__ import annotations

import shlex
from pathlib import Path

import click

try:
    from prompt_toolkit import PromptSession
    from prompt_toolkit.history import FileHistory
except ImportError:
    # Same stale-install scenario as ui.py's guarded imports — see that file's comment.
    raise click.ClickException(
        "Missing required dependency: prompt_toolkit.\n"
        "Reinstall aether-vault to pick it up:\n"
        "  pip install --upgrade aether-vault   (if installed from PyPI)\n"
        "  pip install -e .                     (if installed from source)"
    )

from . import ui

_EXIT_WORDS = {"exit", "quit"}


def _prompt_label(repo_root: Path, login_mode: str) -> str:
    return f"aether-vault ({login_mode}) [{repo_root.name}] > "


def run_repl(repo_root: Path, login_mode: str = "local") -> None:
    from .main import cli  # local import: avoids a circular import at module load time

    history_path = repo_root / ".av" / "repl_history"
    history_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        session = PromptSession(history=FileHistory(str(history_path)))
    except Exception:
        # Some terminals report isatty()=True but still can't back a real prompt_toolkit
        # session (e.g. Git Bash/mintty on Windows lacks a real Win32 console handle).
        # Degrade to "just run one-shot commands" instead of crashing the whole invocation.
        ui.print_step(
            "Interactive session isn't available in this terminal — run `av <command>` "
            "directly instead.",
            status="warn",
        )
        return

    ui.console.print(
        "[dim]Type a command (e.g. `av status`), or `exit`/`quit` to leave.[/dim]"
    )

    while True:
        try:
            line = session.prompt(_prompt_label(repo_root, login_mode))
        except EOFError:
            break
        except KeyboardInterrupt:
            continue
        except Exception as exc:
            # Same terminal-compatibility concern as the construction guard above, but
            # surfacing mid-session — bail out of the REPL instead of crashing the process.
            ui.print_step(f"Interactive session ended unexpectedly: {exc}", status="warn")
            break

        line = line.strip()
        if not line:
            continue
        if line.lower() in _EXIT_WORDS:
            break

        try:
            tokens = shlex.split(line)
        except ValueError as exc:
            ui.print_step(f"Could not parse command: {exc}", status="error")
            continue
        if not tokens:
            continue
        if tokens[0] == "av":
            tokens = tokens[1:]
        if not tokens:
            continue
        if tokens[0] == "init":
            ui.print_step("Already inside an aether-vault session.", status="warn")
            continue

        try:
            cli.main(args=tokens, prog_name="av", standalone_mode=False)
        except click.ClickException as exc:
            exc.show()
        except (click.exceptions.Exit, SystemExit):
            pass
        except Exception as exc:  # noqa: BLE001 - keep the session alive on unexpected errors
            ui.print_step(f"{exc}", status="error")
