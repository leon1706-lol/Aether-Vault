"""V1.5.0: the `av daemon` server -- a warm interpreter that executes allowlisted commands
(`add`/`status`/`commit`) on behalf of a thin client (`launcher.py`), skipping the ~450-600ms
`import av_cli.main` cost on every invocation. Off by default; see `daemon_common.enabled_mode`.

Design invariants (see the V1.5.0 plan's trap table for the reasoning behind each):
  - No write-back cache, ever. Every command runs against the real filesystem exactly as
    the in-process path would; the daemon's only "cache" is a warm interpreter and a warm
    `aether_core` module. State is revalidated by stat on every request (see
    `_state_is_fresh`), never trusted from a previous request.
  - Exactly one command executes at a time (a real lock, not just best-effort) -- this
    preserves the single-writer invariant around `_finalize_commit` for free.
  - The daemon calls the SAME click command objects the in-process path uses
    (`cli.main(argv, standalone_mode=False, ...)`), not a parallel implementation --
    this is what makes byte-identical-output testing against the in-process path possible.
  - Any failure here must be recoverable by the client falling back in-process; the daemon
    must never be the reason a command fails that would otherwise have succeeded.
"""
from __future__ import annotations

import contextlib
import hmac
import io
import json
import os
import secrets
import socket
import sys
import threading
import time
from pathlib import Path

from . import daemon_common
from .daemon_protocol import (
    PROTOCOL_VERSION,
    ProtocolError,
    build_error_response,
    build_ok_response,
    decode_frame,
    encode_frame,
)

IDLE_TIMEOUT_SECONDS = 900
MAX_CONSECUTIVE_INTERNAL_ERRORS = 3
# The real allowlist filtering happens client-side now (daemon_client.call_daemon, before a
# single byte goes on the wire) -- see daemon_common.allowlisted_env's docstring for why.
# Re-exported here, and re-applied in _execute() below, purely as defense in depth: never
# trust a request's `env` field to already be filtered just because today's only client
# filters it.
allowlisted_env = daemon_common.allowlisted_env


class _PoisonedDaemon(Exception):
    """Raised internally to trigger a clean shutdown after too many consecutive internal
    errors -- never let a daemon serve garbage indefinitely once something is clearly wrong
    with it."""


class DaemonServer:
    def __init__(self, repo_root: Path, cli_version: str, idle_timeout: float = IDLE_TIMEOUT_SECONDS):
        self.repo_root = Path(repo_root).resolve()
        self.cli_version = cli_version
        self.idle_timeout = idle_timeout
        self.token = secrets.token_hex(32)
        self._exec_lock = threading.Lock()
        self._last_activity = time.monotonic()
        self._consecutive_errors = 0
        self._self_check_snapshot: tuple | None = None
        self._stop = threading.Event()
        self.requests_served = 0
        self.started_at = time.time()

    # -- self-check / staleness -------------------------------------------------

    def _current_self_check(self) -> tuple:
        """(mtime_ns, size) of the two files whose drift means "this daemon's code is
        stale" -- av_cli's own package marker and the compiled aether_core extension, if
        loaded. A `pip install -e .` mid-session changes at least one of these."""
        stamps = []
        for p in self._watched_files():
            try:
                st = p.stat()
                stamps.append((str(p), st.st_mtime_ns, st.st_size))
            except OSError:
                stamps.append((str(p), None, None))
        return tuple(stamps)

    def _watched_files(self) -> list[Path]:
        files = [Path(__file__).resolve().with_name("__init__.py")]
        try:
            import aether_core

            if getattr(aether_core, "__file__", None):
                files.append(Path(aether_core.__file__).resolve())
        except ImportError:
            pass
        return files

    def _version_drifted(self) -> bool:
        current = self._current_self_check()
        if self._self_check_snapshot is None:
            self._self_check_snapshot = current
            return False
        return current != self._self_check_snapshot

    # -- request handling ---------------------------------------------------

    def handle_request(self, request: dict) -> dict:
        if not isinstance(request, dict) or request.get("protocol") != PROTOCOL_VERSION:
            return build_error_response("version_skew")
        if not hmac.compare_digest(str(request.get("token", "")), self.token):
            return build_error_response("auth_failed")
        if request.get("cli_version") != self.cli_version or self._version_drifted():
            return build_error_response("version_skew")

        argv = request.get("argv") or []
        if not argv or argv[0] not in daemon_common.ALLOWED_COMMANDS:
            return build_error_response("not_allowed")

        acquired = self._exec_lock.acquire(timeout=0.25)
        if not acquired:
            return build_error_response("busy")
        try:
            self._last_activity = time.monotonic()
            self.requests_served += 1
            try:
                result = self._execute(request)
                self._consecutive_errors = 0
                return result
            except Exception as exc:  # pragma: no cover - defensive last resort
                self._consecutive_errors += 1
                if self._consecutive_errors >= MAX_CONSECUTIVE_INTERNAL_ERRORS:
                    self._stop.set()
                return build_error_response(f"internal: {exc}")
        finally:
            self._exec_lock.release()

    def _execute(self, request: dict) -> dict:
        from .main import cli  # heavy import -- fine here, the daemon pays it once at startup

        argv = request["argv"]
        cwd = request.get("cwd") or str(self.repo_root)
        env_overlay = allowlisted_env(request.get("env") or {})

        stdout_buf = io.StringIO()
        stderr_buf = io.StringIO()
        old_cwd = os.getcwd()
        old_env = {k: os.environ.get(k) for k in env_overlay}
        exit_code = 0
        try:
            os.chdir(cwd)
            os.environ.update(env_overlay)
            with contextlib.redirect_stdout(stdout_buf), contextlib.redirect_stderr(stderr_buf):
                try:
                    cli.main(args=argv, prog_name="av", standalone_mode=False)
                except SystemExit as exc:
                    exit_code = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
                except Exception as exc:  # click's own ClickException etc.
                    import click

                    if isinstance(exc, click.ClickException):
                        exc.show(file=stderr_buf)
                        exit_code = exc.exit_code
                    else:
                        raise
        finally:
            os.chdir(old_cwd)
            for k, v in old_env.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

        nonce = str(request.get("nonce", ""))
        mac = hmac.new(self.token.encode("utf-8"), nonce.encode("utf-8"), "sha256").hexdigest()
        return build_ok_response(
            server_nonce_mac=mac, stdout=stdout_buf.getvalue(), stderr=stderr_buf.getvalue(),
            exit_code=exit_code,
        )

    # -- lifecycle ------------------------------------------------------------

    def idle_seconds(self) -> float:
        return time.monotonic() - self._last_activity

    def should_stop(self) -> bool:
        if self._stop.is_set():
            return True
        if not self.repo_root.exists() or not (self.repo_root / ".av").exists():
            return True
        if self.idle_seconds() > self.idle_timeout:
            return True
        return False

    def write_state_file(self, endpoint: str) -> None:
        state = {
            "pid": os.getpid(), "started_at": self.started_at, "protocol": PROTOCOL_VERSION,
            "cli_version": self.cli_version, "repo_root": str(self.repo_root), "endpoint": endpoint,
        }
        path = daemon_common.state_file(self.repo_root, PROTOCOL_VERSION, self.cli_version)
        path.parent.mkdir(parents=True, exist_ok=True)

        # Token written BEFORE the state file, deliberately: a client only ever discovers
        # this daemon by first seeing the state file exist (see daemon_client._read_state /
        # _wait_for_state_file in tests), so by the time that's observable the token file
        # must already be the real, final one -- otherwise a client could read a not-yet-
        # written or about-to-be-replaced token and either fail spuriously or (worse) race
        # a stale one.
        token_path = path.with_suffix(".key")
        tmp_key = token_path.with_suffix(".key.tmp")
        tmp_key.write_text(self.token, encoding="utf-8")
        os.replace(tmp_key, token_path)
        with contextlib.suppress(OSError):
            os.chmod(token_path, 0o600)

        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state), encoding="utf-8")
        os.replace(tmp, path)

    def cleanup_state_files(self) -> None:
        path = daemon_common.state_file(self.repo_root, PROTOCOL_VERSION, self.cli_version)
        for p in (path, path.with_suffix(".key")):
            with contextlib.suppress(OSError):
                p.unlink()


# ---------------------------------------------------------------------------
# Transport: one connection handled fully before the next is accepted (matches the
# single-exec-lock model above -- there is never a reason to accept a second connection
# while the first is mid-request, since it would just block on the lock anyway).
# ---------------------------------------------------------------------------


def _start_should_stop_watchdog(server: DaemonServer, check_interval: float = 2.0) -> None:
    """Windows' `_winapi.ConnectNamedPipe` (used by `run_windows` below) blocks
    indefinitely waiting for a client -- with no client ever connecting, `should_stop()`
    (idle timeout, `.av` removed, poisoned-daemon shutdown) would never get re-checked and
    the process would never exit on its own. POSIX's `listener.settimeout(1.0)` accept loop
    already polls `should_stop()` on its own, but this watchdog is started unconditionally
    on both platforms for one uniform, simple guarantee: `should_stop()` becoming true is
    *always* honored within `check_interval`, never dependent on a connection arriving.
    `os._exit()` (not `sys.exit()`) because the main thread may be blocked in a C-level
    blocking call it cannot be interrupted out of -- cleanup runs here, in the watchdog,
    since the main thread's own `finally` block will never get a chance to.
    """
    def _watch():
        while not server.should_stop():
            time.sleep(check_interval)
        server.cleanup_state_files()
        os._exit(0)

    threading.Thread(target=_watch, daemon=True).start()


def _serve_connection(server: DaemonServer, recv, send, close) -> None:
    try:
        request = decode_frame(recv)
    except ProtocolError:
        close()
        return
    try:
        response = server.handle_request(request)
    except Exception as exc:  # pragma: no cover - defensive
        response = build_error_response(f"internal: {exc}")
    try:
        send(encode_frame(response))
    except OSError:
        pass
    close()


def run_posix(server: DaemonServer) -> None:
    sock_path = daemon_common.posix_socket_path(server.repo_root, PROTOCOL_VERSION, server.cli_version)
    sock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with contextlib.suppress(FileNotFoundError):
        sock_path.unlink()

    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        listener.bind(str(sock_path))
        os.chmod(sock_path, 0o600)
        listener.listen(4)
        listener.settimeout(1.0)
        server.write_state_file(str(sock_path))
        try:
            while not server.should_stop():
                try:
                    conn, _ = listener.accept()
                except socket.timeout:
                    continue
                with conn:
                    conn.settimeout(30.0)
                    _serve_connection(
                        server, lambda n: conn.recv(n), lambda b: conn.sendall(b), lambda: None
                    )
        finally:
            server.cleanup_state_files()
    finally:
        listener.close()
        with contextlib.suppress(FileNotFoundError):
            sock_path.unlink()


def run_windows(server: DaemonServer) -> None:
    import _winapi  # stdlib on Windows; import cost only paid server-side

    pipe_name = daemon_common.windows_pipe_name(server.repo_root, PROTOCOL_VERSION, server.cli_version)
    server.write_state_file(pipe_name)
    try:
        first = True
        while not server.should_stop():
            flags = _winapi.PIPE_ACCESS_DUPLEX
            if first:
                flags |= getattr(_winapi, "FILE_FLAG_FIRST_PIPE_INSTANCE", 0)
            # Win32's PIPE_TYPE_BYTE/PIPE_READMODE_BYTE are both 0 (byte mode is the
            # absence of the MESSAGE flags) -- CPython's _winapi module only exposes the
            # MESSAGE-mode constants by name, so byte mode is spelled as plain 0 here.
            handle = _winapi.CreateNamedPipe(
                pipe_name, flags,
                0 | _winapi.PIPE_WAIT,
                _winapi.PIPE_UNLIMITED_INSTANCES, 65536, 65536, 0, _winapi.NULL,
            )
            first = False
            try:
                try:
                    _winapi.ConnectNamedPipe(handle, _winapi.NULL)
                except OSError:
                    pass  # a client may have connected between CreateNamedPipe and here -- fine

                def _recv(n, _h=handle):
                    data, _ = _winapi.ReadFile(_h, n)
                    return data

                def _send(b, _h=handle):
                    _winapi.WriteFile(_h, b)

                _serve_connection(server, _recv, _send, lambda: None)
            finally:
                # CPython's _winapi doesn't bind DisconnectNamedPipe -- CloseHandle alone
                # is sufficient here since every loop iteration creates a brand new pipe
                # instance rather than reusing this handle for a second client.
                with contextlib.suppress(OSError):
                    _winapi.CloseHandle(handle)
    finally:
        server.cleanup_state_files()


def run(repo_root: Path, cli_version: str, idle_timeout: float = IDLE_TIMEOUT_SECONDS) -> None:
    server = DaemonServer(repo_root, cli_version, idle_timeout)
    _start_should_stop_watchdog(server)
    if sys.platform == "win32":
        run_windows(server)
    else:
        run_posix(server)


if __name__ == "__main__":  # pragma: no cover - exercised via subprocess, not unit tests
    # Spawned by `av daemon start` as `python -m av_cli.daemon <repo_root> <cli_version>
    # [idle_timeout]` -- a real, unpatched interpreter invocation, not a normal test path.
    _repo_root = Path(sys.argv[1])
    _cli_version = sys.argv[2]
    _idle_timeout = float(sys.argv[3]) if len(sys.argv) > 3 else IDLE_TIMEOUT_SECONDS
    run(_repo_root, _cli_version, _idle_timeout)
