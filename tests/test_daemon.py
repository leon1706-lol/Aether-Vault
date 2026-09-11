"""V1.5.0 `av daemon`: protocol framing, naming/enablement logic, request handling, and a
real end-to-end round trip over the actual platform transport (named pipe on Windows,
AF_UNIX elsewhere) -- the daemon server runs in a background thread of the SAME test
process, which is what makes this a genuine transport-level test rather than a mock.

conftest.py sets `AV_NO_DAEMON=1` globally as a belt-and-braces guard for the rest of the
suite (which never reaches the daemon anyway -- it drives commands via `CliRunner`, not
`av_cli.launcher`). This file needs the real path enabled to test it at all, so
`_clear_av_no_daemon` below clears it per-test; the one test that specifically wants it SET
(`test_call_daemon_respects_av_no_daemon`) sets it back itself via `monkeypatch`.
"""
import json
import os
import sys
import threading
import time

import pytest
from click.testing import CliRunner

from python.av_cli import daemon as daemon_module
from python.av_cli import daemon_client, daemon_common
from python.av_cli.daemon_protocol import (
    PROTOCOL_VERSION,
    ProtocolError,
    decode_frame,
    encode_frame,
)
from python.av_cli.main import cli


@pytest.fixture(autouse=True)
def _clear_av_no_daemon(monkeypatch):
    """See module docstring -- conftest.py sets this globally; this file needs it cleared
    to exercise the real daemon path, except the one test that wants it set."""
    monkeypatch.delenv("AV_NO_DAEMON", raising=False)


# ---------------------------------------------------------------------------
# Protocol framing (no network)
# ---------------------------------------------------------------------------


def test_encode_decode_frame_roundtrip():
    obj = {"a": 1, "b": [1, 2, 3], "c": "hello"}
    frame = encode_frame(obj)
    buf = io_bytes(frame)
    assert decode_frame(buf.read) == obj


def io_bytes(data: bytes):
    import io

    return io.BytesIO(data)


def test_decode_frame_raises_on_truncated_stream():
    frame = encode_frame({"x": 1})
    truncated = io_bytes(frame[:-2])
    with pytest.raises(ProtocolError):
        decode_frame(truncated.read)


def test_encode_frame_rejects_oversized_payload(monkeypatch):
    monkeypatch.setattr("python.av_cli.daemon_protocol.MAX_FRAME_BYTES", 10)
    with pytest.raises(ProtocolError):
        encode_frame({"data": "x" * 100})


# ---------------------------------------------------------------------------
# Naming / enablement
# ---------------------------------------------------------------------------


def test_endpoint_key_is_stable_for_same_inputs(tmp_path):
    k1 = daemon_common.endpoint_key(tmp_path, 1, "1.5.0")
    k2 = daemon_common.endpoint_key(tmp_path, 1, "1.5.0")
    assert k1 == k2
    assert len(k1) == 16


def test_endpoint_key_differs_on_version_change(tmp_path):
    k1 = daemon_common.endpoint_key(tmp_path, 1, "1.5.0")
    k2 = daemon_common.endpoint_key(tmp_path, 1, "1.5.1")
    assert k1 != k2, "a new cli_version must resolve to a different endpoint (skew protection)"


def test_endpoint_key_differs_on_protocol_change(tmp_path):
    k1 = daemon_common.endpoint_key(tmp_path, 1, "1.5.0")
    k2 = daemon_common.endpoint_key(tmp_path, 2, "1.5.0")
    assert k1 != k2


def test_endpoint_key_differs_per_repo(tmp_path):
    a = tmp_path / "repo_a"
    b = tmp_path / "repo_b"
    a.mkdir()
    b.mkdir()
    assert daemon_common.endpoint_key(a, 1, "1.5.0") != daemon_common.endpoint_key(b, 1, "1.5.0")


def test_enabled_mode_never_wins(monkeypatch):
    monkeypatch.setenv("AV_NO_DAEMON", "1")
    monkeypatch.setenv("AV_DAEMON", "1")
    assert daemon_common.enabled_mode() == "never"


def test_enabled_mode_auto_spawn(monkeypatch):
    monkeypatch.delenv("AV_NO_DAEMON", raising=False)
    monkeypatch.setenv("AV_DAEMON", "1")
    assert daemon_common.enabled_mode() == "auto_spawn"


def test_enabled_mode_default_is_use_only(monkeypatch):
    monkeypatch.delenv("AV_NO_DAEMON", raising=False)
    monkeypatch.delenv("AV_DAEMON", raising=False)
    assert daemon_common.enabled_mode() == "use_only"


def test_allowed_commands_is_exactly_the_documented_three():
    assert daemon_common.ALLOWED_COMMANDS == {"add", "status", "commit"}


# ---------------------------------------------------------------------------
# DaemonServer.handle_request (no network -- direct function calls)
# ---------------------------------------------------------------------------


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    r = CliRunner().invoke(cli, ["init", "--mode", "local", "--yes", "--no-repl"])
    assert r.exit_code == 0, r.output
    return tmp_path


def _base_request(**overrides):
    req = {
        "protocol": PROTOCOL_VERSION, "cli_version": "test-version", "token": "",
        "nonce": "abc", "argv": ["status"], "cwd": ".", "env": {},
        "isatty": {"stdout": False, "stderr": False}, "columns": None,
    }
    req.update(overrides)
    return req


def test_handle_request_rejects_wrong_protocol(repo):
    server = daemon_module.DaemonServer(repo, "test-version")
    resp = server.handle_request(_base_request(protocol=999))
    assert resp["error"] == "version_skew"


def test_handle_request_rejects_bad_token(repo):
    server = daemon_module.DaemonServer(repo, "test-version")
    resp = server.handle_request(_base_request(token="wrong"))
    assert resp["error"] == "auth_failed"


def test_handle_request_rejects_disallowed_command(repo):
    server = daemon_module.DaemonServer(repo, "test-version")
    resp = server.handle_request(_base_request(token=server.token, argv=["login"]))
    assert resp["error"] == "not_allowed"


def test_handle_request_rejects_cli_version_mismatch(repo):
    server = daemon_module.DaemonServer(repo, "test-version")
    resp = server.handle_request(_base_request(token=server.token, cli_version="other-version"))
    assert resp["error"] == "version_skew"


def test_handle_request_executes_status_and_returns_valid_mac(repo):
    server = daemon_module.DaemonServer(repo, "test-version")
    req = _base_request(token=server.token, argv=["status"], cwd=str(repo))
    resp = server.handle_request(req)
    assert "error" not in resp
    assert resp["exit_code"] == 0
    assert "On branch" in resp["stdout"]
    import hmac

    expected_mac = hmac.new(server.token.encode(), req["nonce"].encode(), "sha256").hexdigest()
    assert resp["server_nonce_mac"] == expected_mac


def test_handle_request_add_actually_stages_the_file(repo):
    (repo / "f.py").write_text("x = 1")
    server = daemon_module.DaemonServer(repo, "test-version")
    req = _base_request(token=server.token, argv=["add", "f.py"], cwd=str(repo))
    resp = server.handle_request(req)
    assert resp["exit_code"] == 0
    idx_text = (repo / ".av" / "index").read_text()
    assert "f.py" in idx_text


def test_handle_request_serializes_concurrent_calls(repo):
    """The exec lock must make two overlapping requests run strictly one at a time --
    proven by having the first request hold the lock artificially and confirming the
    second reports busy rather than interleaving."""
    server = daemon_module.DaemonServer(repo, "test-version")
    server._exec_lock.acquire()
    try:
        resp = server.handle_request(_base_request(token=server.token, argv=["status"], cwd=str(repo)))
        assert resp["error"] == "busy"
    finally:
        server._exec_lock.release()


# ---------------------------------------------------------------------------
# Real end-to-end round trip over the actual platform transport
# ---------------------------------------------------------------------------


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


def test_end_to_end_status_and_add_over_real_transport(repo):
    cli_version = "e2e-test-version"
    server = daemon_module.DaemonServer(repo, cli_version, idle_timeout=30)
    _run_daemon_in_thread(server)
    try:
        _wait_for_state_file(repo, cli_version)

        resp = daemon_client.call_daemon(repo, cli_version, ["status"])
        assert resp is not None and resp["exit_code"] == 0

        (repo / "hello.py").write_text("print(1)")
        resp2 = daemon_client.call_daemon(repo, cli_version, ["add", "hello.py"])
        assert resp2 is not None and resp2["exit_code"] == 0
        assert "hello.py" in (repo / ".av" / "index").read_text()

        # A second status call proves the server is still serving after the first two --
        # not a one-shot accept loop.
        resp3 = daemon_client.call_daemon(repo, cli_version, ["status"])
        assert resp3 is not None and resp3["exit_code"] == 0
        assert "hello.py" in resp3["stdout"]  # now shown as staged
    finally:
        server._stop.set()
        time.sleep(0.3)


def test_end_to_end_output_matches_in_process_cli(repo, monkeypatch):
    """The core promise: a command run through the daemon produces the same observable
    result as running it in-process. Compares actual index/commit state, not just stdout
    text (which can legitimately differ in incidental whitespace)."""
    cli_version = "e2e-parity-test"
    (repo / "a.py").write_text("a" * 50)

    # In-process baseline in a sibling directory.
    baseline_dir = repo.parent / "baseline"
    baseline_dir.mkdir()
    monkeypatch.chdir(baseline_dir)
    CliRunner().invoke(cli, ["init", "--mode", "local", "--yes", "--no-repl"])
    (baseline_dir / "a.py").write_text("a" * 50)
    r = CliRunner().invoke(cli, ["add", "a.py"])
    assert r.exit_code == 0

    monkeypatch.chdir(repo)
    server = daemon_module.DaemonServer(repo, cli_version, idle_timeout=30)
    _run_daemon_in_thread(server)
    try:
        _wait_for_state_file(repo, cli_version)
        resp = daemon_client.call_daemon(repo, cli_version, ["add", "a.py"])
        assert resp is not None and resp["exit_code"] == 0
    finally:
        server._stop.set()
        time.sleep(0.3)

    baseline_entry = json.loads((baseline_dir / ".av" / "index").read_text())["entries"]["a.py"]
    daemon_entry = json.loads((repo / ".av" / "index").read_text())["entries"]["a.py"]
    assert baseline_entry["hash"] == daemon_entry["hash"]
    assert baseline_entry["size"] == daemon_entry["size"]
    assert baseline_entry["type"] == daemon_entry["type"]


def test_call_daemon_returns_none_when_no_daemon_running(repo):
    resp = daemon_client.call_daemon(repo, "no-such-version", ["status"])
    assert resp is None


def test_call_daemon_returns_none_for_disallowed_command(repo):
    resp = daemon_client.call_daemon(repo, "any-version", ["login"])
    assert resp is None


def test_call_daemon_respects_av_no_daemon(repo, monkeypatch):
    monkeypatch.setenv("AV_NO_DAEMON", "1")
    cli_version = "e2e-no-daemon-test"
    server = daemon_module.DaemonServer(repo, cli_version, idle_timeout=30)
    _run_daemon_in_thread(server)
    try:
        _wait_for_state_file(repo, cli_version)
        resp = daemon_client.call_daemon(repo, cli_version, ["status"])
        assert resp is None, "AV_NO_DAEMON=1 must always win, even with a live daemon"
    finally:
        server._stop.set()
        time.sleep(0.3)


def test_wrong_token_is_rejected_by_the_server_over_real_transport(repo, monkeypatch):
    """A client with a stale/forged token must be refused, not just a token mismatch
    checked in isolation -- this drives it through the real handshake."""
    cli_version = "e2e-badtoken-test"
    server = daemon_module.DaemonServer(repo, cli_version, idle_timeout=30)
    _run_daemon_in_thread(server)
    try:
        _wait_for_state_file(repo, cli_version)
        # Corrupt the on-disk token the client would read.
        state_path = daemon_common.state_file(repo, PROTOCOL_VERSION, cli_version)
        key_path = state_path.with_suffix(".key")
        key_path.write_text("0" * 64, encoding="utf-8")

        resp = daemon_client.call_daemon(repo, cli_version, ["status"])
        assert resp is None
    finally:
        server._stop.set()
        time.sleep(0.3)


def test_server_self_check_detects_extension_drift(repo, tmp_path):
    """Simulates a `pip install -e .` mid-session by touching the file the self-check
    watches -- the daemon must refuse the next request with version_skew rather than
    silently keep serving from stale loaded code."""
    server = daemon_module.DaemonServer(repo, "test-version")
    req = _base_request(token=server.token, argv=["status"], cwd=str(repo))
    resp1 = server.handle_request(req)
    assert "error" not in resp1  # first call establishes the baseline snapshot

    # Touch the watched marker file to simulate a code change.
    watched = server._watched_files()[0]
    old_bytes = watched.read_bytes()
    try:
        watched.write_bytes(old_bytes + b"\n# touched\n")
        resp2 = server.handle_request(_base_request(token=server.token, argv=["status"], cwd=str(repo)))
        assert resp2["error"] == "version_skew"
    finally:
        watched.write_bytes(old_bytes)


def test_should_stop_when_av_dir_removed(repo):
    server = daemon_module.DaemonServer(repo, "test-version")
    assert server.should_stop() is False
    import shutil

    shutil.rmtree(repo / ".av")
    assert server.should_stop() is True


def test_should_stop_on_idle_timeout(repo):
    server = daemon_module.DaemonServer(repo, "test-version", idle_timeout=0.05)
    assert server.should_stop() is False
    time.sleep(0.1)
    assert server.should_stop() is True


def test_allowlisted_env_only_forwards_expected_keys():
    env = {
        "AV_THREADS": "4", "PATH": "/usr/bin", "HOME": "/home/x",
        "AWS_SECRET_ACCESS_KEY": "shh", "NO_COLOR": "1", "RANDOM_VAR": "x",
    }
    out = daemon_module.allowlisted_env(env)
    assert out == {"AV_THREADS": "4", "HOME": "/home/x", "NO_COLOR": "1"}


# ---------------------------------------------------------------------------
# Config-based auto-spawn opt-in (.av/config's "daemon": {"enabled": true})
# ---------------------------------------------------------------------------


def test_config_allows_auto_spawn_reads_nested_flag(repo):
    (repo / ".av" / "config").write_text(json.dumps({"daemon": {"enabled": True}}))
    assert daemon_common.config_allows_auto_spawn(repo) is True


def test_config_allows_auto_spawn_false_when_absent(repo):
    (repo / ".av" / "config").write_text(json.dumps({"lfs_threshold_mb": 50}))
    assert daemon_common.config_allows_auto_spawn(repo) is False


def test_config_allows_auto_spawn_false_when_no_config_file(tmp_path):
    assert daemon_common.config_allows_auto_spawn(tmp_path) is False


def test_config_allows_auto_spawn_false_on_malformed_json(repo):
    (repo / ".av" / "config").write_text("{not valid json")
    assert daemon_common.config_allows_auto_spawn(repo) is False


def test_enabled_mode_with_repo_root_honors_config_opt_in(repo, monkeypatch):
    monkeypatch.delenv("AV_DAEMON", raising=False)
    (repo / ".av" / "config").write_text(json.dumps({"daemon": {"enabled": True}}))
    assert daemon_common.enabled_mode(repo) == "auto_spawn"


def test_enabled_mode_without_repo_root_ignores_config(repo, monkeypatch):
    """The cheap env-only call (no repo_root) must never touch the filesystem for this --
    that's the whole point of the two-tier check in launcher.py."""
    monkeypatch.delenv("AV_DAEMON", raising=False)
    (repo / ".av" / "config").write_text(json.dumps({"daemon": {"enabled": True}}))
    assert daemon_common.enabled_mode() == "use_only"


def test_enabled_mode_env_var_wins_over_config(repo, monkeypatch):
    monkeypatch.setenv("AV_NO_DAEMON", "1")
    (repo / ".av" / "config").write_text(json.dumps({"daemon": {"enabled": True}}))
    assert daemon_common.enabled_mode(repo) == "never"


# ---------------------------------------------------------------------------
# maybe_auto_spawn -- fire-and-forget spawn trigger
# ---------------------------------------------------------------------------


def test_maybe_auto_spawn_skips_when_daemon_already_running(repo, monkeypatch):
    monkeypatch.setattr(daemon_client, "read_status", lambda *a, **k: {"pid": 123})
    spawned = []
    monkeypatch.setattr(daemon_client, "spawn_detached", lambda args: spawned.append(args))

    result = daemon_client.maybe_auto_spawn(repo, "v1")
    assert result is False
    assert spawned == []


def test_maybe_auto_spawn_spawns_when_nothing_running(repo, monkeypatch):
    monkeypatch.setattr(daemon_client, "read_status", lambda *a, **k: None)
    spawned = []
    monkeypatch.setattr(daemon_client, "spawn_detached", lambda args: spawned.append(args))

    result = daemon_client.maybe_auto_spawn(repo, "v1")
    assert result is True
    assert len(spawned) == 1
    assert spawned[0][:2] == [sys.executable, "-m"]
    assert "av_cli.daemon" in spawned[0]


def test_maybe_auto_spawn_passes_idle_timeout(repo, monkeypatch):
    monkeypatch.setattr(daemon_client, "read_status", lambda *a, **k: None)
    spawned = []
    monkeypatch.setattr(daemon_client, "spawn_detached", lambda args: spawned.append(args))

    daemon_client.maybe_auto_spawn(repo, "v1", idle_timeout=42.0)
    assert spawned[0][-1] == "42.0"


def test_maybe_auto_spawn_does_not_double_spawn_on_lock_race(repo, monkeypatch):
    """A stale-looking lock from a concurrent invocation must prevent a second spawn --
    simulated by pre-creating the exact lock file maybe_auto_spawn itself would use."""
    monkeypatch.setattr(daemon_client, "read_status", lambda *a, **k: None)
    spawned = []
    monkeypatch.setattr(daemon_client, "spawn_detached", lambda args: spawned.append(args))

    lock_path = daemon_common.lock_file(repo, PROTOCOL_VERSION, "v1")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text("999999")
    try:
        result = daemon_client.maybe_auto_spawn(repo, "v1")
        assert result is False
        assert spawned == []
    finally:
        lock_path.unlink(missing_ok=True)


def test_maybe_auto_spawn_cleans_up_its_own_lock_file(repo, monkeypatch):
    monkeypatch.setattr(daemon_client, "read_status", lambda *a, **k: None)
    monkeypatch.setattr(daemon_client, "spawn_detached", lambda args: None)

    daemon_client.maybe_auto_spawn(repo, "v1")
    lock_path = daemon_common.lock_file(repo, PROTOCOL_VERSION, "v1")
    assert not lock_path.exists()


def test_spawn_detached_uses_start_new_session_on_posix(repo, monkeypatch):
    if sys.platform == "win32":
        pytest.skip("posix-only spawn path")
    calls = []

    class _FakePopen:
        def __init__(self, *a, **k):
            calls.append(k)

    monkeypatch.setattr("subprocess.Popen", _FakePopen)
    daemon_client.spawn_detached(["true"])
    assert calls[0].get("start_new_session") is True


def test_spawn_detached_falls_back_when_breakaway_denied_on_windows(monkeypatch):
    if sys.platform != "win32":
        pytest.skip("windows-only spawn path")
    attempts = []

    def _fake_popen(*args, **kwargs):
        attempts.append(kwargs.get("creationflags"))
        if len(attempts) == 1:
            raise PermissionError("simulated: this job disallows breakaway")

        class _P:
            pass

        return _P()

    monkeypatch.setattr("subprocess.Popen", _fake_popen)
    daemon_client.spawn_detached([sys.executable, "-c", "pass"])
    assert len(attempts) == 2, "must retry without CREATE_BREAKAWAY_FROM_JOB after PermissionError"
    assert attempts[0] != attempts[1]


# ---------------------------------------------------------------------------
# `av daemon` CLI surface (start/stop/status/restart), via CliRunner
# ---------------------------------------------------------------------------


def _invoke(*args):
    return CliRunner().invoke(cli, list(args))


def test_cli_daemon_status_when_none_running(repo):
    r = _invoke("daemon", "status")
    assert r.exit_code == 0
    assert "No daemon running" in r.output


def test_cli_daemon_stop_when_none_running(repo):
    r = _invoke("daemon", "stop")
    assert r.exit_code == 0
    assert "No daemon running" in r.output


def test_cli_daemon_start_reports_already_running(repo, monkeypatch):
    monkeypatch.setattr(daemon_client, "read_status",
                         lambda *a, **k: {"pid": 4242, "cli_version": "x", "protocol": 1,
                                          "repo_root": str(repo), "endpoint": "fake"})
    r = _invoke("daemon", "start")
    assert r.exit_code == 0
    assert "already running" in r.output.lower()
    assert "4242" in r.output


def test_cli_daemon_start_json_mode_already_running(repo, monkeypatch):
    monkeypatch.setattr(daemon_client, "read_status",
                         lambda *a, **k: {"pid": 4242, "cli_version": "x", "protocol": 1,
                                          "repo_root": str(repo), "endpoint": "fake"})
    r = _invoke("--output", "json", "daemon", "start")
    assert r.exit_code == 0
    payload = json.loads(r.output)
    assert payload["ok"] is True
    assert payload["data"]["already_running"] is True


def test_cli_daemon_start_spawns_and_reports_started(repo, monkeypatch):
    """Mocks spawn_detached (never actually launches a process) and read_status to
    simulate the daemon coming up immediately -- proves the CLI wiring end to end
    without depending on real OS process spawning (covered separately/directly by
    the maybe_auto_spawn / spawn_detached unit tests above)."""
    calls = []
    monkeypatch.setattr(daemon_client, "spawn_detached", lambda args: calls.append(args))

    state = {"pid": 555, "cli_version": "x", "protocol": 1, "repo_root": str(repo), "endpoint": "fake"}
    call_count = {"n": 0}

    def _fake_read_status(*a, **k):
        call_count["n"] += 1
        return state if call_count["n"] > 1 else None  # not-yet-running, then running

    monkeypatch.setattr(daemon_client, "read_status", _fake_read_status)

    r = _invoke("daemon", "start")
    assert r.exit_code == 0
    assert len(calls) == 1
    assert "started" in r.output.lower()
    assert "555" in r.output


def test_cli_daemon_start_foreground_runs_inline(repo, monkeypatch):
    ran = []
    monkeypatch.setattr(daemon_client, "read_status", lambda *a, **k: None)
    monkeypatch.setattr(daemon_module, "run", lambda *a, **k: ran.append((a, k)))

    r = _invoke("daemon", "start", "--foreground")
    assert r.exit_code == 0
    assert len(ran) == 1


def test_cli_daemon_stop_kills_and_removes_state(repo, monkeypatch):
    state = {"pid": 999999, "cli_version": "x", "protocol": 1, "repo_root": str(repo), "endpoint": "fake"}
    monkeypatch.setattr(daemon_client, "read_status", lambda *a, **k: state)

    from python.av_cli import __version__ as cli_version

    state_path = daemon_common.state_file(repo, PROTOCOL_VERSION, cli_version)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text("{}")
    key_path = state_path.with_suffix(".key")
    key_path.write_text("token")

    r = _invoke("daemon", "stop")
    assert r.exit_code == 0
    assert not state_path.exists()
    assert not key_path.exists()


def test_cli_daemon_restart_invokes_stop_then_start(repo, monkeypatch):
    order = []
    monkeypatch.setattr(daemon_client, "read_status", lambda *a, **k: None)
    monkeypatch.setattr(daemon_client, "spawn_detached", lambda args: order.append("spawn"))

    import python.av_cli.cmd_daemon as cmd_daemon_module

    original_stop = cmd_daemon_module.daemon_stop.callback

    def _tracked_stop():
        order.append("stop")
        return original_stop()

    monkeypatch.setattr(cmd_daemon_module.daemon_stop, "callback", _tracked_stop)

    r = _invoke("daemon", "restart")
    assert r.exit_code == 0
    assert order[0] == "stop"
    assert "spawn" in order


# ---------------------------------------------------------------------------
# Watchdog: should_stop() must be honored even while the main thread is blocked
# ---------------------------------------------------------------------------


def test_should_stop_watchdog_fires_on_idle_timeout(repo, monkeypatch):
    """Regression test for a real bug found in this session: run_windows's
    ConnectNamedPipe blocks indefinitely with no client connecting, so without a
    watchdog should_stop() (idle timeout, .av removed) would never be re-checked and the
    process would hang forever. `os._exit` is mocked -- calling the real thing would kill
    the test process."""
    exited = threading.Event()
    exit_codes = []

    def _fake_exit(code):
        exit_codes.append(code)
        exited.set()

    monkeypatch.setattr(os, "_exit", _fake_exit)

    server = daemon_module.DaemonServer(repo, "test-version", idle_timeout=0.05)
    daemon_module._start_should_stop_watchdog(server, check_interval=0.02)

    assert exited.wait(timeout=3.0), "watchdog never fired within 3s of the idle timeout elapsing"
    assert exit_codes == [0]
    # cleanup_state_files() must have run before the (mocked) exit.
    state_path = daemon_common.state_file(repo, PROTOCOL_VERSION, "test-version")
    assert not state_path.exists()


def test_should_stop_watchdog_does_not_fire_while_active(repo, monkeypatch):
    exited = threading.Event()
    monkeypatch.setattr(os, "_exit", lambda code: exited.set())

    server = daemon_module.DaemonServer(repo, "test-version", idle_timeout=5.0)
    daemon_module._start_should_stop_watchdog(server, check_interval=0.02)

    assert not exited.wait(timeout=0.3), "watchdog fired despite the daemon still being within its idle window"
    server._stop.set()  # let the background thread wind down cleanly before the test ends


def test_call_daemon_filters_env_before_sending_on_the_wire(repo, monkeypatch):
    """Regression test for a real bug found in this session: env filtering used to happen
    only server-side (daemon.py's allowlisted_env, applied to the already-received
    request), so the FULL environment -- secrets included -- was serialized and sent over
    the wire even though the daemon only ever used the allowlisted subset. Verifies the
    request the client actually transmits never contains a non-allowlisted key."""
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "super-secret-value")
    monkeypatch.setenv("AV_THREADS", "4")

    cli_version = "env-filter-test"
    server = daemon_module.DaemonServer(repo, cli_version, idle_timeout=30)
    captured = {}
    original_handle = server.handle_request

    def _spy_handle(request):
        captured.update(request)
        return original_handle(request)

    monkeypatch.setattr(server, "handle_request", _spy_handle)
    _run_daemon_in_thread(server)
    try:
        _wait_for_state_file(repo, cli_version)
        resp = daemon_client.call_daemon(repo, cli_version, ["status"])
        assert resp is not None and resp["exit_code"] == 0
    finally:
        server._stop.set()
        time.sleep(0.3)

    assert "AWS_SECRET_ACCESS_KEY" not in captured.get("env", {}), (
        "a non-allowlisted env var reached the wire -- client-side filtering regressed"
    )
    assert captured.get("env", {}).get("AV_THREADS") == "4"
