"""v1.3.0 (todo.md item 26): documentation rot becomes a test failure. Parses every fenced
`av ...` command out of `docs/*.md` and asserts the command path AND every flag it uses
actually exist in the live Click tree — a doc that references a renamed/removed
command or flag fails here instead of quietly going stale.
"""
import re
import shlex
from pathlib import Path

import click
import pytest

from python.av_cli.main import cli

DOCS_DIR = Path(__file__).resolve().parents[1] / "docs"

# Matches a fenced code block (```bash, ```, etc.) and captures its body.
_FENCE_RE = re.compile(r"```[a-zA-Z]*\n(.*?)```", re.DOTALL)


def _iter_doc_commands():
    """Yields (doc_relpath, line_no, raw_line) for every line beginning with `av ` inside
    a fenced code block in any docs/*.md file."""
    for doc_path in sorted(DOCS_DIR.glob("*.md")):
        text = doc_path.read_text(encoding="utf-8")
        for fence_match in _FENCE_RE.finditer(text):
            block = fence_match.group(1)
            block_start_line = text[: fence_match.start()].count("\n") + 2  # first line inside the fence
            for offset, raw_line in enumerate(block.splitlines()):
                stripped = raw_line.strip()
                if stripped.startswith("av "):
                    yield doc_path.name, block_start_line + offset, stripped


ALL_DOC_COMMANDS = list(_iter_doc_commands())


def _strip_trailing_comment(line: str) -> str:
    """Removes a trailing `# ...` shell comment — careful not to treat a `#` inside a
    quoted string as a comment marker."""
    try:
        tokens = shlex.split(line, comments=True)
    except ValueError:
        return line
    return shlex.join(tokens)


def _find_click_option(cmd: click.Command, token: str) -> click.Option | None:
    """token is a bare flag like '--output' or '-m' (no '=value' suffix — callers strip
    that first). Matches against every declared alias (click.Option.opts includes both
    the long and short forms)."""
    for param in cmd.params:
        if isinstance(param, click.Option) and token in param.opts:
            return param
    return None


def _resolve(tokens: list[str]) -> tuple[click.Command, list[str]]:
    """tokens excludes the leading 'av'. Walks group-level options (e.g. `--output json`
    before the subcommand name), then subcommand names down the tree, and returns the
    resolved leaf command plus whatever tokens remain (its own args/flags)."""
    cmd: click.Command = cli
    i = 0
    while i < len(tokens) and tokens[i].startswith("-"):
        bare = tokens[i].split("=", 1)[0]
        opt = _find_click_option(cmd, bare)
        if opt is None:
            break  # not a recognized group-level option — leave it for the leaf command
        i += 1
        if not opt.is_flag and "=" not in tokens[i - 1]:
            i += 1  # this option takes a separate value token
    while i < len(tokens) and isinstance(cmd, click.Group) and tokens[i] in cmd.commands:
        cmd = cmd.commands[tokens[i]]
        i += 1
    return cmd, tokens[i:]


@pytest.mark.parametrize("doc,line_no,raw", ALL_DOC_COMMANDS,
                         ids=[f"{d}:{n}" for d, n, _ in ALL_DOC_COMMANDS])
def test_documented_command_and_flags_exist_in_the_live_cli(doc, line_no, raw):
    line = _strip_trailing_comment(raw)
    tokens = shlex.split(line)
    assert tokens and tokens[0] == "av", f"{doc}:{line_no}: expected to start with 'av', got: {raw!r}"

    leaf_cmd, remaining = _resolve(tokens[1:])
    # A Group is only wrong here if it can't be invoked bare -- some (av handoff, av
    # stash, the top-level `av`) declare invoke_without_command=True for exactly that.
    if isinstance(leaf_cmd, click.Group):
        assert leaf_cmd.invoke_without_command, (
            f"{doc}:{line_no}: {raw!r} resolves to a GROUP ({leaf_cmd.name}) that requires "
            "a subcommand — likely a typo'd or removed subcommand name"
        )

    for token in remaining:
        if not token.startswith("-"):
            continue  # a positional argument or an option's value, not a flag itself
        bare = token.split("=", 1)[0]
        assert _find_click_option(leaf_cmd, bare) is not None, (
            f"{doc}:{line_no}: {bare!r} is not a recognized flag of `av "
            f"{' '.join(t for t in tokens[1:] if not t.startswith('-'))}` — {raw!r}"
        )


def test_at_least_one_command_was_found_in_each_doc_file():
    """Guards against the parser itself silently matching nothing (a fence-regex typo, a
    doc renamed out from under DOCS_DIR) for every doc that's actually a CLI-usage
    walkthrough — an empty result for one of those is itself a bug worth catching."""
    found_docs = {doc for doc, _, _ in ALL_DOC_COMMANDS}
    all_docs = {p.name for p in DOCS_DIR.glob("*.md")}
    # README.md is a pure index; the rest are reference/policy docs, not CLI walkthroughs,
    # so none of them has `av` examples of its own.
    NO_COMMANDS_EXPECTED = {"README.md", "contracts.md", "avattributes.md", "slo.md", "sla.md"}
    expected_with_commands = all_docs - NO_COMMANDS_EXPECTED
    missing = expected_with_commands - found_docs
    assert not missing, f"no `av` commands found in: {missing} — check the fence-parsing regex"
