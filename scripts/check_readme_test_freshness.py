"""v1.3.3.9: closes the one gap `av test`'s own local sync path
(python/av_cli/cmd_devtools.py::_update_readme_test_badge/_rewrite_test_count_prose)
can't cover on its own — CI's own `test` job runs bare `pytest tests/`, never `av test`,
so nothing in a normal CI run ever re-checks README.md's `tests-N%2FM passing` badge,
its two "N-test suite / N tests across M files" prose mentions, or tests/README.md's
own opening line against reality. Found live, mid-session: numbers a maintainer had
JUST hand-verified drifted again within the same working tree the moment one more test
file landed — a one-time fix isn't a fix, only a snapshot.

Per this project's own no-CI-commits rule (tests/test_ci_policy.py), CI can never
auto-correct these the way `av test` does locally — so this script is the other half of
that rule's contract: fail the job loudly the instant they disagree with what the run
that JUST happened actually produced. No second pytest run: it re-parses the same
summary line `av test` itself parses, from a log the "Run test suite" CI step already
captured.

Usage (wired into .github/workflows/tests.yml's `test` job, one matrix leg only):
    python scripts/check_readme_test_freshness.py PYTEST_OUTPUT_LOG_PATH

The three regexes below are intentionally separate literals from
python/av_cli/cmd_devtools.py's own copies, not an import of them — mirroring
tests/test_benchmark_docs_freshness.py's own stated reasoning for the same choice: this
is a freshness *check*, and re-deriving its ground truth via the exact same code it's
checking would let a bug in that code hide from the check. They must stay byte-identical
in shape to cmd_devtools.py's patterns; a change to one belongs with a change to the
other.
"""
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
README_PATH = REPO_ROOT / "README.md"
TESTS_README_PATH = REPO_ROOT / "tests" / "README.md"
TESTS_DIR = REPO_ROOT / "tests"

BADGE_PATTERN = re.compile(
    r'https://img\.shields\.io/badge/tests-(\d+)%2F(\d+)%20passing-[a-z]+(?:\?[^"]*)?"'
    r'\s+alt="\d+ of \d+ tests passing"'
)
ROW_PATTERN = re.compile(r"~?([\d,]+)-test suite across (\d+)\+? files")
PROSE_PATTERN = re.compile(r"~?([\d,]+) tests across (\d+)\+? files")


def _real_file_count() -> int:
    return sum(1 for _ in TESTS_DIR.glob("test_*.py"))


def _parse_pytest_summary(log_text: str) -> int | None:
    """Same three-regex approach as cmd_devtools.py's `test_cmd` (passed + failed +
    error, never counting skipped) — ANSI escapes stripped first for the same reason:
    `--color=yes`-forced output can otherwise put an escape code between a number and
    its "passed"/"failed" word."""
    cleaned = re.sub(r"\x1b\[[0-9;]*m", "", log_text)
    passed_match = re.search(r"(\d+) passed", cleaned)
    if not passed_match:
        return None
    failed_match = re.search(r"(\d+) failed", cleaned)
    error_match = re.search(r"(\d+) error", cleaned)
    passed = int(passed_match.group(1))
    failed = (int(failed_match.group(1)) if failed_match else 0) + (
        int(error_match.group(1)) if error_match else 0
    )
    return passed, passed + failed


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: check_readme_test_freshness.py PYTEST_OUTPUT_LOG_PATH", file=sys.stderr)
        return 2
    log_path = Path(sys.argv[1])
    if not log_path.is_file():
        print(f"::error::pytest output log not found at {log_path}", file=sys.stderr)
        return 1

    parsed = _parse_pytest_summary(log_path.read_text(encoding="utf-8", errors="replace"))
    if parsed is None:
        print(
            f"::error::could not find a '<N> passed' summary line in {log_path} — "
            "this check's own parsing broke, or the run produced no output to parse.",
            file=sys.stderr,
        )
        return 1
    passed, total = parsed
    file_count = _real_file_count()
    print(f"this run: {passed} passed, {total} total (passed+failed+error, excl. skipped); "
          f"{file_count} test_*.py files on disk")

    readme_text = README_PATH.read_text(encoding="utf-8")
    tests_readme_text = TESTS_README_PATH.read_text(encoding="utf-8")

    problems: list[str] = []

    badge_match = BADGE_PATTERN.search(readme_text)
    if badge_match is None:
        problems.append("README.md's tests-N%2FM-passing badge not found (pattern changed?)")
    else:
        badge_passed, badge_total = int(badge_match.group(1)), int(badge_match.group(2))
        if badge_total != total:
            problems.append(
                f"README.md's test badge denominator is {badge_total} but this run "
                f"actually collected {total} (passed+failed+error)"
            )
        if badge_passed != passed:
            problems.append(
                f"README.md's test badge numerator is {badge_passed} but this run "
                f"actually passed {passed}"
            )

    def _check(label: str, pattern: re.Pattern, text: str) -> None:
        match = pattern.search(text)
        if match is None:
            problems.append(f"{label} not found (pattern changed?)")
            return
        mentioned_total = int(match.group(1).replace(",", ""))
        mentioned_files = int(match.group(2))
        if mentioned_total != total:
            problems.append(f"{label} says {mentioned_total} tests but this run actually collected {total}")
        if mentioned_files != file_count:
            problems.append(f"{label} says {mentioned_files} test files but tests/ actually has {file_count}")

    _check("README.md's module-table row ('N-test suite across M files')", ROW_PATTERN, readme_text)
    _check("README.md's Test Suite section prose ('N tests across M files')", PROSE_PATTERN, readme_text)
    _check("tests/README.md's opening line ('N tests across M files')", PROSE_PATTERN, tests_readme_text)

    if problems:
        print("::error::README test-count drift detected — run a full `av test` (no `-k`) "
              "locally to resync README.md/tests/README.md, then commit the result:")
        for p in problems:
            print(f"::error::  - {p}")
        return 1

    print("README.md / tests/README.md test counts match this run. OK.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
