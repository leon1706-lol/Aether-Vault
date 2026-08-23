import pytest
from click.testing import CliRunner

from python.av_cli.main import cli


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """An initialized .av repository in a temp directory, with cwd set to it."""
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(cli, ["init", "--mode", "local", "--yes", "--no-repl"])
    assert result.exit_code == 0, result.output
    return tmp_path


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    """Explains skips-by-design at the end of the run.

    Skipping without Docker/optional extras is intended behavior, not something being
    hidden — this makes that explicit in every run's tail output instead of a bare
    '36 skipped' that reads as a warning sign.
    """
    from skipsummary import classify_skip, extract_reason, format_skip_note

    skipped = terminalreporter.stats.get("skipped", [])
    buckets: dict[str, int] = {}
    for report in skipped:
        buckets[classify_skip(extract_reason(report))] = \
            buckets.get(classify_skip(extract_reason(report)), 0) + 1

    note = format_skip_note(buckets)
    if note:
        terminalreporter.write_line("\n" + note + "\n")
