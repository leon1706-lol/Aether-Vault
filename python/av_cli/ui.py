"""Shared pretty-terminal rendering helpers for the av CLI.

Centralizes the rich/questionary calls so `init`, `webui`, `update`, and the REPL all render
consistently instead of each hand-rolling their own click.secho color/emoji prefixes.
"""

from __future__ import annotations

import sys

import click
import questionary
from rich.console import Console
from rich.panel import Panel

console = Console()

_STATUS_STYLE = {
    "info": ("cyan", "🔍"),
    "success": ("green", "✓"),
    "error": ("red", "✗"),
    "warn": ("yellow", "⚠"),
}


def print_banner(title: str, subtitle: str | None = None) -> None:
    """Render a rounded banner panel, e.g. at the top of `av init`."""
    body = f"[bold]{title}[/bold]"
    if subtitle:
        body += f"\n[dim]{subtitle}[/dim]"
    console.print(Panel(body, border_style="cyan", expand=False))


def print_step(msg: str, status: str = "info") -> None:
    """Standardized status line: emoji + color, matching the existing main.py conventions."""
    color, emoji = _STATUS_STYLE.get(status, _STATUS_STYLE["info"])
    click.secho(f"{emoji} {msg}", fg=color)


def select_login_mode() -> str:
    """Prompt the user to choose Local or Enterprise. Returns "local" or "enterprise"."""
    choice = questionary.select(
        "How would you like to use Aether-Vault?",
        choices=[
            questionary.Choice("Local (recommended) — Docker-backed, runs on this machine", value="local"),
            questionary.Choice("Enterprise — sign in with your account", value="enterprise"),
        ],
    ).ask()
    if choice is None:
        # Ctrl+C / Ctrl+D during the prompt — default to local rather than crashing.
        return "local"
    return choice


def is_interactive() -> bool:
    """True only when both stdin and stdout are a real terminal.

    questionary/prompt_toolkit hang (or raise) when stdin isn't a TTY — e.g. under
    `CliRunner.invoke()` in tests, or piped/CI invocations. Callers use this to decide
    whether to prompt at all.
    """
    return sys.stdin.isatty() and sys.stdout.isatty()
