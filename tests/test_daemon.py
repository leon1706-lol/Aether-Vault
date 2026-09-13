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


def test_enabled_mode_default_is_auto_spawn(monkeypatch):
    """V1.6.0: the default flipped from "use_only" to "auto_spawn" -- a daemon now
    auto-starts on first use per repo unless explicitly opted out (AV_NO_DAEMON=1,
    AV_DAEMON=0/false/no, or `.av/config`'s `"daemon":{"enabled":false}`)."""
    monkeypatch.delenv("AV_NO_DAEMON", raising=False)
    monkeypatch.delenv("AV_DAEMON", raising=False)
    assert daemon_common.enabled_mode() == "auto_spawn"


def test_enabled_mode_av_daemon_zero_is_use_only(monkeypatch):
    monkeypatch.delenv("AV_NO_DAEMON", raising=False)
    monkeypatch.setenv("AV_DAEMON", "0")
    assert daemon_common.enabled_mode() == "use_only"


def test_enabled_mode_config_false_is_use_only(repo, monkeypatch):
    monkeypatch.delenv("AV_NO_DAEMON", raising=False)
    monkeypatch.delenv("AV_DAEMON", raising=False)
    (repo / ".av" / "config").write_text(json.dumps({"daemon": {"enabled": False}}))
    assert daemon_common.enabled_mode(repo) == "use_only"


def test_allowed_commands_matches_v160_set():
    assert daemon_common.ALLOWED_COMMANDS == {
        "add", "status", "commit", "push", "fetch", "unstage", "log", "diff", "context", "run",
    }


# ---------------------------------------------------------------------------
# first_subcommand -- must resolve exactly what click's own parser would for main.py's global
# options (--verbose/--silent/--version are bare flags, --output takes one value). V1.6.0:
# moved here from test_launcher.py when the implementation itself moved from being a private
# copy in launcher.py to the one shared home every daemon-path layer uses -- see
# test_handle_request_allows_allowlisted_command_with_leading_global_options and
# test_call_daemon_reaches_state_lookup_with_leading_global_options for why a single shared
# implementation matters here, not just launcher.py's own entry gate.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("argv,expected", [
    (["status"], "status"),
    (["add", "f.py"], "add"),
    ([], None),
    (["--verbose", "status"], "status"),
    (["--silent", "add", "f.py"], "add"),
    (["--output", "json", "status"], "status"),  # the exact bug this fixes
    (["--output=json", "status"], "status"),
    (["--verbose", "--output", "json", "commit", "-m", "x"], "commit"),
    (["--output", "json", "--verbose", "push"], "push"),
    (["--version"], None),
    (["--help"], "--help"),  # not a global option in this module's list
    (["--output", "json"], None),  # global option consumes its value, nothing left
])
def test_first_subcommand_skips_global_options(argv, expected):
    assert daemon_common.first_subcommand(argv) == expected


def test_first_subcommand_does_not_mistake_a_command_named_like_a_flag_value_for_a_flag():
    # "--output" always consumes exactly the next token as its value, even if that token
    # happens to look like it could be a command -- matches click's own real parsing.
    assert daemon_common.first_subcommand(["--output", "status"]) is None


def test_allowlisted_command_modules_never_prompt():
    """Static verification, not just an assertion of trust: every module backing an
    allowlisted command must be free of anything that could block waiting on a real
    terminal -- questionary/prompt_toolkit imports, or click's own prompt/confirm helpers,
    or a bare `input(`. Growing ALLOWED_COMMANDS later without also passing this check is
    exactly the mistake this guards against."""
    import ast
    from pathlib import Path

    command_modules = {
        "add": "cmd_staging.py", "status": "cmd_staging.py", "unstage": "cmd_staging.py",
        "commit": "cmd_history.py", "push": "cmd_history.py", "log": "cmd_history.py",
        "fetch": "cmd_sync.py", "diff": "cmd_diff.py", "context": "cmd_context.py",
        "run": "cmd_run.py",
    }
    assert set(command_modules) == daemon_common.ALLOWED_COMMANDS, (
        "this test's own module map is out of sync with ALLOWED_COMMANDS -- update both"
    )
    src_dir = Path(daemon_common.__file__).resolve().parent
    forbidden_imports = {"questionary", "prompt_toolkit"}
    forbidden_calls = {"input"}  # click.prompt/click.confirm are attribute calls, caught below
    for module_file in set(command_modules.values()):
        tree = ast.parse((src_dir / module_file).read_text(encoding="utf-8"), filename=module_file)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert alias.name.split(".")[0] not in forbidden_imports, (
                        f"{module_file} imports {alias.name} -- not safe for the daemon allowlist"
                    )
            elif isinstance(node, ast.ImportFrom):
                assert (node.module or "").split(".")[0] not in forbidden_imports, (
                    f"{module_file} imports from {node.module} -- not safe for the daemon allowlist"
                )
            elif isinstance(node, ast.Call):
                func = node.func
                name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
                assert name not in forbidden_calls, (
                    f"{module_file} calls {name}() -- not safe for the daemon allowlist"
                )
                if isinstance(func, ast.Attribute) and func.attr in ("prompt", "confirm"):
                    owner = func.value.id if isinstance(func.value, ast.Name) else None
                    assert owner != "click", (
                        f"{module_file} calls click.{func.attr}() -- not safe for the daemon allowlist"
                    )


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


def test_handle_request_allows_allowlisted_command_with_leading_global_options(repo):
    """V1.6.0 real bug (found building the native launcher's own test coverage): this
    server-side re-check used to be a plain `argv[0] not in ALLOWED_COMMANDS`, so a request
    carrying `["--output", "json", "status"]` -- exactly what a real client actually sends,
    since the daemon protocol forwards the client's whole original argv, globals included --
    was rejected with `not_allowed` even though `status` is allowlisted and the CLIENT'S OWN
    gate (`daemon_client.call_daemon`, `launcher.py`) had already validated it. See
    `daemon_common.first_subcommand`'s docstring for the full account."""
    server = daemon_module.DaemonServer(repo, "test-version")
    req = _base_request(token=server.token, argv=["--output", "json", "status"], cwd=str(repo))
    resp = server.handle_request(req)
    assert "error" not in resp
    assert resp["exit_code"] == 0


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


def test_auth_failure_via_daemon_returns_clean_exit_not_a_hang(repo, monkeypatch):
    """The daemon's stdin is DEVNULL (daemon_client.spawn_detached) and its stdout is
    redirected to an io.StringIO (daemon.py::_execute) for the duration of every request --
    both make `ui.is_interactive()` (stdin AND stdout must both be a real tty) false by
    construction, so `_AuthRetryGroup`'s 401-retry prompt (core.py) can never actually try
    to block waiting on a real terminal inside the daemon. No separate "needs_interactive"
    protocol concept is needed -- proven end-to-end here: a `status` call that hits
    AuthenticationError deep inside must return a clean auth_failed exit, not hang the
    request or crash the daemon."""
    import python.av_cli.cmd_staging as cmd_staging_module
    from python.av_cli.client import AuthenticationError

    def _raise(*a, **k):
        raise AuthenticationError("nope")

    monkeypatch.setattr(cmd_staging_module, "compute_status", _raise)

    server = daemon_module.DaemonServer(repo, "test-version")
    req = _base_request(token=server.token, argv=["status"], cwd=str(repo))
    resp = server.handle_request(req)
    assert "error" not in resp  # a completed response, not a protocol-level refusal
    assert resp["exit_code"] == 12  # auth_failed -- never a hang, never a crash
    assert "protected" in (resp["stdout"] + resp["stderr"]).lower()


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


def test_zero_byte_disconnect_does_not_kill_the_daemon_thread(repo):
    """V1.6.0 real bug (found by the native launcher's own test suite -- see
    `daemon.py::_serve_connection`'s comment for the full account): `read_status()`
    deliberately connects and disconnects WITHOUT sending anything, to check reachability --
    exactly what `av daemon status` and `maybe_auto_spawn` (in turn called from every
    `launcher.py` invocation whenever `call_daemon` returns None) do on a real daemon in
    normal use. No existing test drove that real path against a real live daemon (every
    `maybe_auto_spawn` test mocks `read_status`), so this uncaught-`OSError`-kills-the-
    thread bug shipped unnoticed. Proven here: `read_status` for real, then a REAL
    subsequent request must still be served -- the thread must not have died."""
    cli_version = "e2e-zero-byte-test"
    server = daemon_module.DaemonServer(repo, cli_version, idle_timeout=30)
    _run_daemon_in_thread(server)
    try:
        _wait_for_state_file(repo, cli_version)

        status = daemon_client.read_status(repo, cli_version)
        assert status is not None  # the zero-op connect itself must report "reachable"

        resp = daemon_client.call_daemon(repo, cli_version, ["status"])
        assert resp is not None and resp["exit_code"] == 0, (
            "the daemon thread must survive a zero-byte reachability probe and keep serving"
        )
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


def test_call_daemon_reaches_state_lookup_with_leading_global_options(repo, monkeypatch):
    """V1.6.0 real bug (found building the native launcher's own test coverage):
    `call_daemon`'s own allowlist guard used to check `argv[0]` positionally, so
    `["--output", "json", "status"]` was rejected right here -- before ever consulting the
    state file -- indistinguishable from "no daemon running" to the caller. `launcher.py`'s
    own gate had already correctly resolved "status" and called this function anyway, so the
    net effect was: `av --output json status` silently never reached a daemon even when one
    was running. Proven here by observing the state-file lookup is actually attempted (it
    still returns None -- no daemon is actually running in this test -- but for the RIGHT
    reason, not because the guard rejected it)."""
    calls = []
    real_read_state = daemon_client._read_state

    def _spy(repo_root, cli_version):
        calls.append(1)
        return real_read_state(repo_root, cli_version)

    monkeypatch.setattr(daemon_client, "_read_state", _spy)
    resp = daemon_client.call_daemon(repo, "any-version", ["--output", "json", "status"])
    assert resp is None
    assert calls, "the allowlist guard must resolve 'status' and proceed to the state lookup"


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


# ---------------------------------------------------------------------------
# WS6.1: idle-trim watchdog -- release_pool()/gc.collect()/malloc_trim after
# AV_DAEMON_TRIM_SECS (default 30) idle, once per idle stretch.
# ---------------------------------------------------------------------------

def test_trim_after_secs_defaults_to_30(repo):
    server = daemon_module.DaemonServer(repo, "test-version")
    assert server._trim_after_secs == daemon_module.DEFAULT_TRIM_AFTER_SECONDS == 30.0


def test_av_daemon_trim_secs_env_overrides_default(repo, monkeypatch):
    monkeypatch.setenv("AV_DAEMON_TRIM_SECS", "5")
    server = daemon_module.DaemonServer(repo, "test-version")
    assert server._trim_after_secs == 5.0


def test_av_daemon_trim_secs_malformed_falls_back_to_default(repo, monkeypatch):
    monkeypatch.setenv("AV_DAEMON_TRIM_SECS", "not-a-number")
    server = daemon_module.DaemonServer(repo, "test-version")
    assert server._trim_after_secs == daemon_module.DEFAULT_TRIM_AFTER_SECONDS


def test_maybe_trim_idle_does_nothing_before_the_threshold(repo):
    server = daemon_module.DaemonServer(repo, "test-version")
    server._trim_after_secs = 60.0
    assert server.maybe_trim_idle() is False
    assert server.trim_count == 0


def test_watchdog_trims_after_idle(repo):
    """The plan's own test name for this behavior: past the threshold, `maybe_trim_idle()`
    actually trims -- once, not on every subsequent tick while still idle."""
    server = daemon_module.DaemonServer(repo, "test-version")
    server._trim_after_secs = 0.01
    server._last_activity = time.monotonic() - 1.0  # well past the threshold

    assert server.maybe_trim_idle() is True
    assert server.trim_count == 1

    # Still idle, no new activity -- must not trim again.
    assert server.maybe_trim_idle() is False
    assert server.trim_count == 1


def test_maybe_trim_idle_rearms_after_new_activity(repo):
    """Real flake found live on a shared CI runner, TWICE (test (3.10), both the V1.6.1
    push and its own follow-up fix): the first fix widened the margin between the second
    and third `maybe_trim_idle()` calls, but that was never the actual race. The real one
    is between `self._trimmed_at` (set INSIDE the first `maybe_trim_idle()` call) and this
    test's own `server._last_activity = time.monotonic()` on the very next line -- two
    `time.monotonic()` calls close enough together that a coarse/virtualized clock on a
    loaded CI runner can return the SAME tick for both, making `_trimmed_at >=
    _last_activity` true (a false tie) and `maybe_trim_idle()`'s rearm check wrongly think
    the trim already covers this "new" activity. `time.sleep()` (unlike a bare
    `time.monotonic()` reassignment) always advances real elapsed time regardless of clock
    granularity, so inserting one between the trim and the reassignment closes the race
    for real -- widening the LATER sleep (the one after this fix) never touched it."""
    server = daemon_module.DaemonServer(repo, "test-version")
    server._trim_after_secs = 0.05
    server._last_activity = time.monotonic() - 1.0
    assert server.maybe_trim_idle() is True

    time.sleep(0.05)  # real gap between the trim above and the "new activity" below
    server._last_activity = time.monotonic()  # a request just came in
    assert server.maybe_trim_idle() is False  # not idle long enough yet
    time.sleep(0.3)  # real time passes -- well past the threshold again
    assert server.maybe_trim_idle() is True  # idle again -> trims again
    assert server.trim_count == 2


def test_maybe_trim_idle_skips_while_a_request_is_in_flight(repo):
    server = daemon_module.DaemonServer(repo, "test-version")
    server._trim_after_secs = 0.01
    server._last_activity = time.monotonic() - 1.0
    server._exec_lock.acquire()  # simulate a command mid-execution
    try:
        assert server.maybe_trim_idle() is False
        assert server.trim_count == 0
    finally:
        server._exec_lock.release()


@pytest.mark.skipif(sys.platform != "win32", reason="exercises the win32 ctypes path specifically")
def test_process_rss_mb_returns_a_real_positive_value_on_windows():
    """Real bug (found live in a manual scratch-repo pass): without explicit
    argtypes/restype, ctypes truncated `GetCurrentProcess()`'s pseudo-handle and
    `GetProcessMemoryInfo` rejected it with ERROR_INVALID_HANDLE every single call --
    `rss_mb` silently never appeared in `av daemon status` on Windows at all."""
    rss = daemon_module._process_rss_mb()
    assert rss is not None
    assert rss > 0


def test_maybe_trim_idle_updates_status_fields(repo):
    server = daemon_module.DaemonServer(repo, "test-version")
    server._trim_after_secs = 0.01
    server._last_activity = time.monotonic() - 1.0
    server.write_state_file("fake-endpoint")

    server.maybe_trim_idle()

    path = daemon_common.state_file(server.repo_root, daemon_module.PROTOCOL_VERSION, "test-version")
    state = json.loads(path.read_text(encoding="utf-8"))
    assert state["trimmed"] is True


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
    that's the whole point of the two-tier check in launcher.py. Config here says
    enabled:true (would resolve to "auto_spawn" if read), but the point of this test is
    that it's never read at all -- the assertion is on the V1.6.0 *default* precisely
    because that's what a repo_root-less call falls through to without ever looking."""
    monkeypatch.delenv("AV_DAEMON", raising=False)
    (repo / ".av" / "config").write_text(json.dumps({"daemon": {"enabled": True}}))
    assert daemon_common.enabled_mode() == "auto_spawn"


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
    # Real bug found via a CI failure (test_daemon.py's own pytest process getting
    # silently killed, exit 0, no summary line -- see Probleme.md #149/#152): this test
    # used to call server._stop.set() and return immediately, with nothing waiting for
    # the background watchdog thread to actually observe it. monkeypatch's teardown then
    # restores the REAL os._exit before the thread's next ~20ms poll notices should_stop()
    # is now true -- so the watchdog calls the genuine os._exit(0) shortly after this test
    # ends, killing the whole process mid-way through whatever test happens to be running
    # next. Waiting on `exited` after setting `_stop` closes that race: the mocked exit is
    # guaranteed to fire (and be observed) while the monkeypatch is still active.
    exited = threading.Event()
    monkeypatch.setattr(os, "_exit", lambda code: exited.set())

    server = daemon_module.DaemonServer(repo, "test-version", idle_timeout=5.0)
    daemon_module._start_should_stop_watchdog(server, check_interval=0.02)

    assert not exited.wait(timeout=0.3), "watchdog fired despite the daemon still being within its idle window"
    server._stop.set()
    assert exited.wait(timeout=3.0), "watchdog never fired within 3s of _stop being set"


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
