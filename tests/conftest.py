import os
import tempfile

# Must run before ANY test module is imported: python/av_server/server.py builds
# CASStorage(DATA_DIR) at import time (server.py:238-239), defaulting DATA_DIR to '/data'
# when AV_DATA_DIR is unset. That's fine on CI jobs that export AV_DATA_DIR (tests.yml) but
# nightly.yml does not, and any test module that imports python.av_server.server without
# setting it itself (e.g. test_audit_coverage.py) dies at collection with
# PermissionError: /data. conftest.py is always imported before test modules, so a
# setdefault here — not inside a fixture — is the one place that reliably runs first for
# every module, regardless of which test file happens to import server.py first.
os.environ.setdefault("AV_DATA_DIR", tempfile.mkdtemp(prefix="av-test-data-"))

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
