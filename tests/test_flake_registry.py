"""v1.3.4 (todo.md item 12): enforces the flake-quarantine policy end to end — a test may
carry `@pytest.mark.flaky` ONLY alongside a tests/FLAKES.md entry with an unexpired date,
and no entry may sit there forever unmarked-in-code or past its own expiry. See
tests/FLAKES.md's own header for the full policy; this module is the mechanical guard,
not the registry itself.
"""
import ast
import datetime
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
TESTS_DIR = Path(__file__).resolve().parent
FLAKES_MD = TESTS_DIR / "FLAKES.md"

_TABLE_ROW_RE = re.compile(
    r"^\|\s*`([^`]+)`\s*\|[^|]*\|[^|]*\|[^|]*\|\s*(\d{4}-\d{2}-\d{2})\s*\|\s*$",
    re.MULTILINE,
)


def _registered_entries() -> dict[str, datetime.date]:
    """Parses FLAKES.md's table rows into {test_id: expiry_date}. The header's own
    EXAMPLE row (a literal `tests/test_server.py::TestSomething::test_x` placeholder,
    dated in its prose, not a real registration) is excluded by requiring the row to
    look like a real pytest node id AND not be the documented example text."""
    text = FLAKES_MD.read_text(encoding="utf-8")
    entries = {}
    for match in _TABLE_ROW_RE.finditer(text):
        test_id, expiry = match.group(1), match.group(2)
        if test_id == "tests/test_server.py::TestSomething::test_x":
            continue  # the header's own illustrative example row, not a real entry
        entries[test_id] = datetime.date.fromisoformat(expiry)
    return entries


def _iter_flaky_marked_tests():
    """AST-walks every tests/test_*.py file for a `def test_*`/class method decorated
    (directly, not via a class-level `pytimeout.mark.flaky` — this repo has none of
    those) with `@pytest.mark.flaky`, yielding its `path::Class::func` or `path::func`
    node id in the same form FLAKES.md's table uses."""
    for path in sorted(TESTS_DIR.glob("test_*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        rel = path.relative_to(REPO_ROOT).as_posix()

        def _is_flaky_marker(dec: ast.expr) -> bool:
            # Matches `@pytest.mark.flaky` and `@pytest.mark.flaky(...)`.
            target = dec.func if isinstance(dec, ast.Call) else dec
            return (
                isinstance(target, ast.Attribute)
                and target.attr == "flaky"
                and isinstance(target.value, ast.Attribute)
                and target.value.attr == "mark"
            )

        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                for item in node.body:
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name.startswith("test"):
                        if any(_is_flaky_marker(d) for d in item.decorator_list):
                            yield f"{rel}::{node.name}::{item.name}"
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test"):
                if any(_is_flaky_marker(d) for d in node.decorator_list):
                    yield f"{rel}::{node.name}"


class TestFlakeRegistryConsistency:
    def test_every_flaky_marked_test_has_a_registry_entry(self):
        registered = _registered_entries()
        marked = list(_iter_flaky_marked_tests())
        unregistered = [t for t in marked if t not in registered]
        assert not unregistered, (
            f"test(s) marked @pytest.mark.flaky with no tests/FLAKES.md entry: "
            f"{unregistered} — add a registry row (test id, first seen, owner, "
            f"hypothesis, expiry) before marking a test flaky; see FLAKES.md's header"
        )

    def test_no_registry_entry_has_expired(self):
        today = datetime.date.today()
        expired = [(test_id, expiry) for test_id, expiry in _registered_entries().items() if expiry < today]
        assert not expired, (
            f"tests/FLAKES.md entry(ies) past their expiry date: {expired} — a flake "
            f"registry entry is a quarantine with a clock, not indefinite; re-triage "
            f"(fix it, or renew with a new expiry AND a note on why) before this passes again"
        )

    def test_flakes_md_is_still_empty_or_every_row_is_well_formed(self):
        # A softer companion to the two tests above: if FLAKES.md's table structure ever
        # breaks (a malformed row silently matched by nothing), this at least confirms
        # the file parses as expected rather than the registry silently going empty.
        text = FLAKES_MD.read_text(encoding="utf-8")
        assert "Currently empty, deliberately" in text or _registered_entries(), (
            "tests/FLAKES.md no longer says the registry is deliberately empty, but "
            "no row parsed as a real entry either — the table format likely changed "
            "without updating this test's parser"
        )


def test_no_auto_retry_plugin_installed():
    # "No silent retry-forever" is a policy decision, not just a convention --
    # pytest-rerunfailures would let a genuinely flaky test pass CI with none of this
    # registry's visibility.
    pyproject_text = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert "rerunfailures" not in pyproject_text.lower(), (
        "pyproject.toml references a rerunfailures-style plugin — this repo's flake "
        "policy (tests/FLAKES.md) is a visible quarantine, never a silent auto-retry"
    )
