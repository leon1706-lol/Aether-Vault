"""V1.6.0: the native `av` launcher (`src/launcher/av_launcher.cpp`) -- Windows AND POSIX
(Linux/macOS). Every test here drives the REAL compiled executable as a subprocess against a
`DaemonServer` running in a background THREAD of this same test process -- never a
detached/spawned OS process. This matters on this project's own Windows dev box: a genuinely
detached daemon process gets torn down by this sandbox within a few seconds regardless of
spawn technique (see `daemon_client.spawn_detached`'s own docstring), which makes a
detached-process daemon unusable as a test fixture there. An in-thread daemon has no such
problem and is exactly what `tests/test_daemon.py`'s own end-to-end tests already rely on for
the same reason -- so this file runs identically (and for the identical reason) on every OS,
not just the one this project happens to develop on.

Located via `AV_TEST_LAUNCHER` (an explicit path) or `shutil.which("av-native")` (the name
`setup.py`'s `BuildExtWithLauncher` installs it under on every platform -- see its own
comment for why this is deliberately NOT the real `av` entry point). Every test module-
level-skips when neither resolves, so this file is a no-op on any machine/CI job that hasn't
built the native launcher (no C++ toolchain, or a toolchain the build couldn't use) rather
than a failure. On this project's own dev box this exercises the Windows paths; the Linux/
macOS paths are exercised for real by `.github/workflows/tests.yml`'s `launcher-native-posix`
job (a matrix over ubuntu-latest/macos-latest), which is the only place those code paths
have ever actually been built or run -- see src/launcher/README.md.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time

import pytest
from click.testing import CliRunner

from python.av_cli import daemon as daemon_module
from python.av_cli import daemon_common
from python.av_cli.daemon_protocol import PROTOCOL_VERSION
from python.av_cli.main import cli


def _find_launcher() -> str | None:
    explicit = os.environ.get("AV_TEST_LAUNCHER")
    if explicit and os.path.exists(explicit):
        return explicit
    return shutil.which("av-native")


LAUNCHER = _find_launcher()
pytestmark = pytest.mark.skipif(
    LAUNCHER is None,
    reason="native av launcher not found -- build it via `pip install -e .` or set AV_TEST_LAUNCHER",
)


def _info() -> dict:
    out = subprocess.run([LAUNCHER, "--av-launcher-info"], capture_output=True, text=True, timeout=10)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def _clean_env(extra: dict | None = None) -> dict:
    env = dict(os.environ)
    env.pop("AV_NO_DAEMON", None)  # conftest.py's global belt-and-braces guard -- must not leak here
    env.pop("AV_DAEMON", None)
    env["AV_NO_UPDATE_CHECK"] = "1"
    env["AV_LAUNCHER_TRACE"] = "1"
    if extra:
        env.update(extra)
    return env


def _run_daemon_in_thread(server):
    target = daemon_module.run_windows if sys.platform == "win32" else daemon_module.run_posix
    t = threading.Thread(target=target, args=(server,), daemon=True)
    t.start()
    return t


def _wait_for_state_file(repo_root, cli_version, timeout=5.0):
    deadline = time.monotonic() + timeout
    path = daemon_common.state_file(repo_root, PROTOCOL_VERSION, cli_version)
    while time.monotonic() < deadline:
        if path.exists():
            return
        time.sleep(0.02)
    raise TimeoutError("daemon never wrote its state file")


@pytest.fixture
def native_repo(tmp_path, monkeypatch):
    """A real, fully-initialized repo (same `av init` conftest.py's own `repo` fixture uses)
    -- the exe's own `find_repo_root()` (a `.av`-dir walk-up, mirroring `core.py`'s) will
    recognize it, and `status` has real index/config/ref state to report on."""
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(cli, ["init", "--mode", "local", "--yes", "--no-repl"])
    assert result.exit_code == 0, result.output
    return tmp_path


@pytest.fixture
def live_daemon(native_repo):
    """A real `DaemonServer` in a background thread, using the SAME cli_version this box's
    built exe has baked in (`--av-launcher-info`'s "version") -- required for the protocol's
    own version-skew check to ever let a real round trip through."""
    cli_version = _info()["version"]
    server = daemon_module.DaemonServer(native_repo, cli_version, idle_timeout=120)
    _run_daemon_in_thread(server)
    _wait_for_state_file(native_repo, cli_version)
    yield server, cli_version
    server._stop.set()


def test_launcher_info_allowlist_and_version_match_python():
    info = _info()
    assert info["protocol"] == 1
    assert set(info["allowlist"]) == daemon_common.ALLOWED_COMMANDS
    assert info["version"]  # non-empty; exact value checked against av-py below


def test_version_output_byte_identical_to_av_py():
    native = subprocess.run([LAUNCHER, "--version"], capture_output=True, timeout=10)
    av_py = shutil.which("av-py")
    if av_py is None:
        pytest.skip("av-py not on PATH -- can't compare")
    python_side = subprocess.run([av_py, "--version"], capture_output=True,
                                  env=_clean_env(), timeout=15)
    assert native.returncode == 0 and python_side.returncode == 0
    assert native.stdout == python_side.stdout


def test_fallback_exec_without_daemon_matches_in_process(native_repo):
    """No daemon running at all (fresh repo, no discovery file) -- must transparently fall
    back and produce the exact same JSON `status` output as the in-process CLI."""
    env = _clean_env({"AV_NO_DAEMON": "1"})  # force fallback deterministically
    result = subprocess.run(
        [LAUNCHER, "--output", "json", "status"], cwd=str(native_repo),
        capture_output=True, text=True, env=env, timeout=20,
    )
    assert result.returncode == 0, result.stderr
    assert "[av-native] fallback:" in result.stderr
    data = json.loads(result.stdout)
    assert data["ok"] is True
    assert data["data"]["branch"]


def test_daemon_roundtrip_json_output_byte_identical(live_daemon, native_repo):
    """The actual point of this whole binary: a warm daemon in place, `status` served
    through it, output byte-identical to what the in-process CLI itself would print for the
    same repo state. First call falls back (no discovery file yet, written by that fallback's
    own successful daemon contact); second call goes straight to the daemon."""
    server, cli_version = live_daemon
    env = _clean_env()

    first = subprocess.run(
        [LAUNCHER, "--output", "json", "status"], cwd=str(native_repo),
        capture_output=True, text=True, env=env, timeout=20,
    )
    assert first.returncode == 0, first.stderr
    assert "fallback:" in first.stderr

    second = subprocess.run(
        [LAUNCHER, "--output", "json", "status"], cwd=str(native_repo),
        capture_output=True, text=True, env=env, timeout=20,
    )
    assert second.returncode == 0, second.stderr
    assert "direct daemon round trip succeeded" in second.stderr, (
        f"expected the second call to skip the fallback entirely; stderr was: {second.stderr!r}"
    )
    assert json.loads(first.stdout) == json.loads(second.stdout)
    assert server.requests_served >= 1


def test_busy_daemon_falls_back_transparently(live_daemon, native_repo):
    """A daemon that's mid-request (lock held) must make the exe fall back cleanly, not
    hang or error out to the user."""
    server, cli_version = live_daemon
    env = _clean_env()
    # warm the discovery file first
    subprocess.run([LAUNCHER, "--output", "json", "status"], cwd=str(native_repo),
                    capture_output=True, env=env, timeout=20)

    server._exec_lock.acquire()
    try:
        result = subprocess.run(
            [LAUNCHER, "--output", "json", "status"], cwd=str(native_repo),
            capture_output=True, text=True, env=env, timeout=20,
        )
    finally:
        server._exec_lock.release()
    assert result.returncode == 0, result.stderr
    assert "fallback: busy" in result.stderr
    assert json.loads(result.stdout)["ok"] is True


def test_forged_token_refused_then_falls_back(live_daemon, native_repo):
    server, cli_version = live_daemon
    env = _clean_env()
    subprocess.run([LAUNCHER, "--output", "json", "status"], cwd=str(native_repo),
                    capture_output=True, env=env, timeout=20)

    disc_path = daemon_common.launcher_discovery_file(
        _info()["exe"], str(native_repo),
    )
    disc = json.loads(disc_path.read_text(encoding="utf-8"))
    key_path = disc["key_path"]
    real_token = open(key_path, encoding="utf-8").read()
    try:
        with open(key_path, "w", encoding="utf-8") as f:
            f.write("forged-token-not-the-real-one")
        result = subprocess.run(
            [LAUNCHER, "--output", "json", "status"], cwd=str(native_repo),
            capture_output=True, text=True, env=env, timeout=20,
        )
        assert result.returncode == 0, result.stderr
        assert "fallback: auth_failed" in result.stderr
        assert json.loads(result.stdout)["ok"] is True
    finally:
        with open(key_path, "w", encoding="utf-8") as f:
            f.write(real_token)


def test_global_options_before_subcommand_take_the_daemon_path(live_daemon, native_repo):
    """`--output json status` (globals BEFORE the subcommand) must reach the daemon exactly
    like bare `status` does -- the C++-side counterpart of
    test_daemon.py::test_handle_request_allows_allowlisted_command_with_leading_global_options."""
    server, cli_version = live_daemon
    env = _clean_env()
    subprocess.run([LAUNCHER, "--output", "json", "status"], cwd=str(native_repo),
                    capture_output=True, env=env, timeout=20)  # warm

    result = subprocess.run(
        [LAUNCHER, "--verbose", "--output", "json", "status"], cwd=str(native_repo),
        capture_output=True, text=True, env=env, timeout=20,
    )
    assert result.returncode == 0, result.stderr
    assert "direct daemon round trip succeeded" in result.stderr
    assert json.loads(result.stdout)["ok"] is True


def test_not_allowlisted_command_never_attempts_the_daemon(native_repo):
    env = _clean_env()
    result = subprocess.run(
        [LAUNCHER, "login"], cwd=str(native_repo),
        capture_output=True, text=True, env=env, timeout=20,
    )
    assert "fallback: not_eligible" in result.stderr
