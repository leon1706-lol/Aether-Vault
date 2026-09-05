"""v1.3.3.9: none of README.md's `tests-N%2FM passing` badge, its two "N-test suite /
N tests across M files" prose mentions, or tests/README.md's own opening line are ever
touched by CI. `av test`'s own sync path (`_update_readme_test_badge`/
`_rewrite_test_count_prose` in python/av_cli/cmd_devtools.py) only runs when a
maintainer runs `av test` locally -- CI's own `test` job runs bare `pytest tests/`
(.github/workflows/tests.yml), which never calls it at all.

Found live, mid-session: the "1,471" a maintainer had JUST hand-verified and written
into all three of those spots had already drifted (both the test count AND the file
count) by the very next collection run (a new test module landing in the same working
tree) -- proof that a one-time manual fix isn't a fix, only a snapshot.

This file covers only the FILE-count half of that drift, which is cheap and exact to
check right here (a plain `glob`, no test run needed) -- so it runs in the ordinary
`pytest tests/` CI already invokes on every push, no workflow change required. The
harder half -- whether the *test* counts (badge numerator/denominator, both prose
totals) match what actually passed -- needs a real run to know (skipped tests are
collected but not "passed", so a `--collect-only` count is NOT the same number the
badge tracks; re-deriving it here would mean running the whole suite a second time from
inside itself). That half is checked for free by scripts/check_readme_test_freshness.py,
wired into the `test` CI job right after the run that already produces the real
passed/failed numbers -- see that script's own docstring.
"""
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
TESTS_DIR = REPO_ROOT / "tests"


def _real_file_count() -> int:
    return sum(1 for _ in TESTS_DIR.glob("test_*.py"))


def _readme_text() -> str:
    return (REPO_ROOT / "README.md").read_text(encoding="utf-8")


def test_readme_module_table_row_file_count_matches_real_file_count():
    text = _readme_text()
    match = re.search(r"~?[\d,]+-test suite across (\d+)\+? files", text)
    assert match, "README.md's `tests/` module-table row ('N-test suite across M files') not found"
    file_count = int(match.group(1))
    assert file_count == _real_file_count(), (
        f"README.md's module-table row says {file_count} test files, but tests/ "
        f"actually has {_real_file_count()} test_*.py files right now — run `av test` "
        "to resync (see python/av_cli/cmd_devtools.py::_update_readme_test_badge)."
    )


def test_readme_section_prose_file_count_matches_real_file_count():
    text = _readme_text()
    match = re.search(r"~?[\d,]+ tests across (\d+)\+? files", text)
    assert match, "README.md's Test Suite section prose ('N tests across M files') not found"
    file_count = int(match.group(1))
    assert file_count == _real_file_count(), (
        f"README.md's Test Suite section says {file_count} test files, but tests/ "
        f"actually has {_real_file_count()} test_*.py files right now — run `av test` "
        "to resync."
    )


def test_tests_readme_file_count_matches_real_file_count():
    text = (TESTS_DIR / "README.md").read_text(encoding="utf-8")
    match = re.search(r"~?[\d,]+ tests across (\d+)\+? files", text)
    assert match, "tests/README.md's opening line ('N tests across M files') not found"
    file_count = int(match.group(1))
    assert file_count == _real_file_count(), (
        f"tests/README.md says {file_count} test files, but tests/ actually has "
        f"{_real_file_count()} test_*.py files right now — run `av test` to resync."
    )
