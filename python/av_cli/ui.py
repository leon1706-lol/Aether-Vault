"""Shared pretty-terminal rendering helpers for the av CLI.

Centralizes the rich/questionary calls so `init`, `webui`, `update`, and the REPL all render
consistently instead of each hand-rolling their own click.secho color/emoji prefixes.
"""

from __future__ import annotations

import sys

import click

# Added to pyproject.toml's core dependencies alongside the rest of the "pretty av init" work —
# an environment whose install predates that (or whose reinstall didn't pick up the new
# dependency set) would otherwise hit a raw ModuleNotFoundError partway through this file's
# imports. This module is only ever imported lazily (`from . import ui` inside
# init/update/webui/the REPL entry point), so raising one clear, actionable error here — listing
# every missing package at once rather than crashing on the first and rediscovering the rest one
# `av init` retry at a time — doesn't affect commands that never touch this path.
_missing_deps: list[str] = []
try:
    import questionary
except ImportError:
    _missing_deps.append("questionary")
try:
    from rich.console import Console
except ImportError:
    _missing_deps.append("rich")

if _missing_deps:
    raise click.ClickException(
        f"Missing required dependencies: {', '.join(_missing_deps)}.\n"
        "Reinstall aether-vault to pick them up:\n"
        "  pip install --upgrade aether-vault   (if installed from PyPI)\n"
        "  pip install -e .                     (if installed from source)"
    )

console = Console()

_STATUS_STYLE = {
    "info": ("cyan", "[INFO]"),
    "success": ("green", "[OK]"),
    "error": ("red", "[ERROR]"),
    "warn": ("yellow", "[WARN]"),
}

# Shell banner derived from development/logo.png's beveled mark: a graphite wireframe "A"
# pierced by a copper dash-bolt, above the signature rule with the ▲ glyph, the spaced
# wordmark, and the tagline — all inside a rounded, dim-copper frame. Colors are TrueColor
# RGB via rich markup so they degrade gracefully on legacy consoles.
_COPPER = "rgb(230,140,60)"
_COPPER_DIM = "rgb(150,95,35)"
_EDGE = "rgb(170,170,175)"
_WORDMARK = "bold white"


def _get_version() -> str:
    """Banner version, resolved locally with zero network cost.

    setuptools-scm regenerates `av_cli/_version.py` on every build/reinstall, so a fresh
    tag automatically flows into the banner on the next installed run; metadata and a
    literal fallback cover source-checkouts without that file.
    """
    try:
        from ._version import __version__

        return __version__
    except Exception:
        pass
    try:
        from importlib.metadata import version

        return version("aether-vault")
    except Exception:
        return "dev"


def _render_logo_art() -> list[str]:
    """The art lines with rich color markup applied per stroke group."""
    C, CD, E = _COPPER, _COPPER_DIM, _EDGE
    g_open, g_close = f"[{_EDGE}]", "[/]"
    c_open, c_close = f"[{C}]", "[/]"
    cd_open, cd_close = f"[{CD}]", "[/]"
    return [
        "",
        f"        {g_open}╱╲[/]",
        f"       {g_open}╱  ╲[/]"
        + " " * 24
        + f"{c_open}━━━━━━[/]{cd_open}╸[/]",
        f"      {g_open}╱ [/]{c_open}━╸[/]{g_open} ╲[/]"
        + " " * 16
        + f"{c_open}━━━━[/]{cd_open}╯[/]",
        f"     {g_open}╱ [/]{c_open}━━━╸[/]{g_open} ╲[/]"
        + " " * 9
        + f"{c_open}━━[/]{cd_open}╯[/]",
        f"     {g_open}╱__╱  ╲__╲╱[/]"
        + " " * 4
        + f"{cd_open}━━╯[/]",
        "",
        f"     {c_open}━━━━━━━━[/]  [{E}]▲[/]  {c_open}━━━━━━━━[/]",
    ]


def print_banner(title: str, subtitle: str | None = None) -> None:
    """Render the framed shell banner: beveled-mark art + signature rule + spaced
    wordmark + tagline, inside a rounded dim-copper panel with the package version in
    the top-right corner (auto-derived from the build metadata).

    `title` is letter-spaced into the wordmark line; `subtitle` becomes the dim tagline
    beneath it (omitted entirely when None).
    """
    from rich import box
    from rich.console import Group
    from rich.panel import Panel
    from rich.text import Text

    art = Group(*[Text.from_markup(line) for line in _render_logo_art()])
    wordmark_text = " ".join(list((title or "Aether-Vault").upper()))
    wordmark = Text.from_markup(f"  ⬡  [{_WORDMARK}]{wordmark_text}[/]")
    body = Group(
        art,
        Text(""),
        wordmark,
        Text.from_markup(f"   [dim]{subtitle}[/]" if subtitle else ""),
    )

    console.print()
    console.print(
        Panel(
            body,
            box=box.ROUNDED,
            border_style=f"dim {_COPPER_DIM}",
            title=f"[dim]v{_get_version()}[/]",
            title_align="right",
            padding=(0, 2),
        )
    )


def print_step(msg: str, status: str = "info") -> None:
    """Standardized status line: bracketed tag + color, matching `av doctor`'s existing
    [OK]/[WARN] convention so the whole CLI reads consistently."""
    color, tag = _STATUS_STYLE.get(status, _STATUS_STYLE["info"])
    click.secho(f"{tag} {msg}", fg=color)


def select_protection_mode() -> str:
    """Prompt the user to choose Anonymous or Protected (the shared-secret access token).
    Returns "anonymous" or "protected"."""
    anon_choice = questionary.Choice("Anonymous — no access token, anyone reachable can use it", value="anonymous")
    choice = questionary.select(
        "Run this registry anonymously, or protect it with an access token?",
        choices=[
            anon_choice,
            questionary.Choice("Protected — requires an access token for every action", value="protected"),
        ],
        default=anon_choice,
        instruction="",
    ).ask()
    if choice is None:
        return "anonymous"
    return choice


def select_token_source() -> str:
    """Once "Protected" is chosen, asks whether this is a fresh registry (generate a token) or
    joining one a teammate already protected (enter their existing token). Returns "generate"
    or "existing"."""
    generate_choice = questionary.Choice(
        "Generate a new token — I'm setting up this registry for the first time", value="generate"
    )
    choice = questionary.select(
        "Is this a new registry, or are you joining one someone else already protected?",
        choices=[
            generate_choice,
            questionary.Choice("Enter an existing token — I'm joining a registry a teammate set up", value="existing"),
        ],
        default=generate_choice,
        instruction="",
    ).ask()
    if choice is None:
        return "generate"
    return choice


def prompt_for_existing_token() -> str | None:
    """Prompts for a token value when joining an already-protected registry. Returns None on
    empty input/Ctrl+C — callers must treat that as "no token entered", never silently store
    an empty string (which is indistinguishable from "no token configured" downstream)."""
    token = questionary.password("Access token:").ask()
    return token or None


def is_interactive() -> bool:
    """True only when both stdin and stdout are a real terminal.

    questionary/prompt_toolkit hang (or raise) when stdin isn't a TTY — e.g. under
    `CliRunner.invoke()` in tests, or piped/CI invocations. Callers use this to decide
    whether to prompt at all.
    """
    return sys.stdin.isatty() and sys.stdout.isatty()
