import os
import tempfile

# Must run before ANY test module is imported: server.py builds CASStorage(DATA_DIR) at
# import time, defaulting to '/data' when AV_DATA_DIR is unset, which dies at collection
# with PermissionError on jobs (e.g. nightly.yml) that don't export it themselves.
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


@pytest.fixture
def unreachable_client(monkeypatch):
    """Forces `VaultClient.server_available()` to return False regardless of what's
    actually reachable -- a real dev stack can genuinely be up on localhost:8000, in which
    case a test's "no server configured" assumption silently becomes false. Any test
    asserting `unreachable_queued`/13 behavior must request this fixture.

    Patches BOTH `python.av_cli.client` and the bare `av_cli.client` import: they resolve
    to the same file but are distinct module objects, and core.py/av_sdk each import via
    a different one -- patching only one leaves the SDK seam hitting the real server."""
    import python.av_cli.client as client_module

    class _UnreachableClient(client_module.VaultClient):
        def server_available(self) -> bool:
            return False

    monkeypatch.setattr(client_module, "VaultClient", _UnreachableClient)
    try:
        import av_cli.client as bare_client_module
    except ImportError:
        bare_client_module = None
    if bare_client_module is not None and bare_client_module is not client_module:
        monkeypatch.setattr(bare_client_module, "VaultClient", _UnreachableClient)


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    """Explains skips-by-design at the end of the run, instead of a bare '36 skipped'
    that reads as a warning sign."""
    from skipsummary import classify_skip, extract_reason, format_skip_note

    skipped = terminalreporter.stats.get("skipped", [])
    buckets: dict[str, int] = {}
    for report in skipped:
        buckets[classify_skip(extract_reason(report))] = \
            buckets.get(classify_skip(extract_reason(report)), 0) + 1

    note = format_skip_note(buckets)
    if note:
        terminalreporter.write_line("\n" + note + "\n")
