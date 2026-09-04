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


@pytest.fixture
def unreachable_client(monkeypatch):
    """Forces `VaultClient.server_available()` to return False regardless of what's
    ACTUALLY reachable on this machine (v1.3.1 fix: a real dev stack can genuinely be up
    on localhost:8000 — Docker Desktop with `docker compose up` — in which case a test's
    "no server configured" assumption silently becomes false, and a call that expected to
    queue instead hits the real (differently-versioned) server and gets a live but wrong
    response, e.g. a 404 for an endpoint this checkout added that the running container's
    baked-in code predates). Any test asserting `unreachable_queued`/13 behavior MUST
    request this fixture rather than relying on ambient non-configuration — this is
    exactly the class of bug development/Probleme.md's "test-suite-touched-real-Docker-
    infra" entry already covers for a different code path; this fixture is the general
    fix so it can't recur test-by-test.

    Patches BOTH `python.av_cli.client` and the bare `av_cli.client` import — the two
    resolve to the same file but are DISTINCT module objects (no package alias unifies
    them), and core.py imports via the former (`from .client import VaultClient`) while
    av_sdk/repo.py imports via the latter (`from av_cli.client import VaultClient`).
    Patching only one leaves the SDK seam silently hitting the real server."""
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
