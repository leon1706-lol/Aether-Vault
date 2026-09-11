"""V1.5.0: the daemon CLIENT -- used by both `launcher.py` (the hot path: import cost here
is directly subtracted from the daemon's whole reason to exist) and `cmd_daemon.py`'s
`status`/`stop` (which can afford to import this the normal way, no different from any other
command module).

Import budget actually measured on this project's dev box: `socket` ~18ms, `json` ~43ms,
this module's own logic is negligible -- nowhere near `multiprocessing.connection`'s ~337ms.
`hmac` is imported lazily, only after a successful connect, so a failed/absent daemon never
pays even that.
"""
from __future__ import annotations

import contextlib
import os
import socket
import sys
import time

from . import daemon_common
from .daemon_protocol import (
    PROTOCOL_VERSION,
    ProtocolError,
    build_request,
    decode_frame,
    encode_frame,
)

CONNECT_TIMEOUT_SECONDS = 0.2
REQUEST_TIMEOUT_SECONDS = 30.0


class DaemonUnavailable(Exception):
    """Any reason the daemon path can't be used -- the caller's only correct response is
    to fall back in-process silently (optionally noisy under AV_DAEMON_DEBUG=1)."""


def _debug(msg: str) -> None:
    if os.environ.get("AV_DAEMON_DEBUG", "").strip() not in ("", "0"):
        sys.stderr.write(f"[av daemon] {msg}\n")


def _read_state(repo_root, cli_version: str) -> dict | None:
    path = daemon_common.state_file(repo_root, PROTOCOL_VERSION, cli_version)
    try:
        import json

        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _read_token(repo_root, cli_version: str) -> str | None:
    path = daemon_common.state_file(repo_root, PROTOCOL_VERSION, cli_version)
    key_path = path.with_suffix(".key")
    try:
        return key_path.read_text(encoding="utf-8").strip()
    except OSError:
        return None


def _connect_posix(endpoint: str) -> socket.socket:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(CONNECT_TIMEOUT_SECONDS)
    try:
        sock.connect(endpoint)
    except OSError as exc:
        sock.close()
        raise DaemonUnavailable(str(exc)) from exc
    sock.settimeout(REQUEST_TIMEOUT_SECONDS)
    return sock


class _WindowsPipeConn:
    """Wraps a named pipe opened as a plain binary file -- CPython supports this directly,
    no `_winapi`/`multiprocessing` import needed client-side."""

    def __init__(self, path: str):
        try:
            self._f = open(path, "r+b", buffering=0)
        except OSError as exc:
            raise DaemonUnavailable(str(exc)) from exc

    def recv(self, n: int) -> bytes:
        return self._f.read(n) or b""

    def sendall(self, b: bytes) -> None:
        self._f.write(b)

    def close(self) -> None:
        with contextlib.suppress(OSError):
            self._f.close()


def _connect(endpoint: str):
    if sys.platform == "win32":
        return _WindowsPipeConn(endpoint)
    return _connect_posix(endpoint)


def call_daemon(repo_root, cli_version: str, argv: list[str]) -> dict | None:
    """Returns the daemon's response dict, or None if the daemon path isn't usable right
    now (any reason at all) -- callers must treat None exactly like "fall back in-process",
    never as an error to surface. Never raises."""
    if daemon_common.enabled_mode() == "never":
        return None
    if not argv or argv[0] not in daemon_common.ALLOWED_COMMANDS:
        return None

    state = _read_state(repo_root, cli_version)
    if state is None:
        _debug("no state file -- daemon not running for this repo/version")
        return None
    token = _read_token(repo_root, cli_version)
    if token is None:
        return None

    try:
        conn = _connect(state["endpoint"])
    except (DaemonUnavailable, KeyError, OSError) as exc:
        _debug(f"connect failed: {exc}")
        _cleanup_stale_state(repo_root, cli_version)
        return None

    try:
        import hmac
        import secrets

        nonce = secrets.token_hex(16)
        request = build_request(
            cli_version=cli_version, token=token, nonce=nonce, argv=argv, cwd=os.getcwd(),
            env=daemon_common.allowlisted_env(dict(os.environ)),
            isatty_stdout=sys.stdout.isatty(), isatty_stderr=sys.stderr.isatty(),
            columns=None,
        )
        conn.sendall(encode_frame(request))
        response = decode_frame(conn.recv)
        if response.get("error"):
            _debug(f"daemon returned error: {response['error']}")
            if response["error"] == "version_skew":
                _cleanup_stale_state(repo_root, cli_version)
            return None
        expected_mac = hmac.new(token.encode("utf-8"), nonce.encode("utf-8"), "sha256").hexdigest()
        if not hmac.compare_digest(str(response.get("server_nonce_mac", "")), expected_mac):
            _debug("server nonce MAC mismatch -- refusing response (impersonation attempt?)")
            return None
        return response
    except (ProtocolError, OSError, KeyError) as exc:
        _debug(f"request failed: {exc}")
        return None
    finally:
        conn.close()


def _cleanup_stale_state(repo_root, cli_version: str) -> None:
    path = daemon_common.state_file(repo_root, PROTOCOL_VERSION, cli_version)
    for p in (path, path.with_suffix(".key")):
        with contextlib.suppress(OSError):
            p.unlink()


def read_status(repo_root, cli_version: str) -> dict | None:
    """For `av daemon status` -- reads the state file and confirms the endpoint is truly
    live with a zero-op connect, rather than trusting a possibly-stale file."""
    state = _read_state(repo_root, cli_version)
    if state is None:
        return None
    try:
        conn = _connect(state["endpoint"])
        conn.close()
    except (DaemonUnavailable, KeyError, OSError):
        return None
    return state


def spawn_detached(args: list[str]) -> None:
    """Spawns `args` as a background process that survives the calling command's own exit.
    `subprocess` is imported lazily here, not at module scope -- this function is only ever
    called on the (rare, at-most-once-per-repo-session) path where a daemon needs to be
    started, so `call_daemon`'s far more common "just talk to an existing daemon" path never
    pays for it.

    POSIX: `start_new_session=True` (setsid) -- the standard, well-tested pattern.

    Windows: `DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP` detaches from the console, but a
    console-script launcher (the `av.exe` stub pip/distlib generates) commonly wraps its own
    child in a Windows Job Object with "kill on job close" semantics specifically so the
    launcher can guarantee its child doesn't outlive it by accident -- exactly what a daemon
    needs to defeat here. `CREATE_BREAKAWAY_FROM_JOB` does that, but only succeeds when every
    enclosing job (there can be more than one nested) has explicitly allowed breakaway; a
    locked-down job (some sandboxes, some CI runners, some enterprise policy) raises
    `PermissionError` (WinError 5) instead -- falls back to spawning without the flag rather
    than failing outright. Verified during V1.5.0 development: in a heavily sandboxed dev/CI
    environment, a spawned process can be torn down a few seconds later regardless of
    technique -- confirmed by testing `schtasks /run` (Windows Task Scheduler), which
    normally escapes ANY job object including ones that refuse breakaway, and it was killed
    on the same timeline as the plain detached spawn. That level of process-lifetime
    supervision is a property of that sandbox itself, not of any job object this function
    could break away from -- there is no Win32-level fix for it, and this function does not
    attempt one; `av daemon start --foreground` under an external supervisor is the correct
    pattern in such an environment.
    """
    import subprocess

    if sys.platform == "win32":
        base_flags = (
            getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
        )
        breakaway = getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0x01000000)
        try:
            subprocess.Popen(
                args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, creationflags=base_flags | breakaway,
            )
            return
        except PermissionError:
            pass  # this job doesn't allow breakaway -- fall through to spawning without it
        subprocess.Popen(
            args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, creationflags=base_flags,
        )
    else:
        subprocess.Popen(
            args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, start_new_session=True,
        )


def maybe_auto_spawn(repo_root, cli_version: str, idle_timeout: float | None = None) -> bool:
    """Fire-and-forget: spawns a daemon for `repo_root` if none is running and nothing else
    is already in the middle of spawning one (an `O_CREAT|O_EXCL` lock file, same one
    `av daemon start` uses, guards the race between two concurrent `av` invocations both
    hitting a cold repo at once). Returns whether a spawn was actually initiated -- never
    waits for the daemon to become ready; the caller's OWN command still runs in-process
    for this invocation regardless, exactly as if no daemon existed yet.
    """
    if read_status(repo_root, cli_version) is not None:
        return False  # already running

    lock_path = daemon_common.lock_file(repo_root, PROTOCOL_VERSION, cli_version)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
    except FileExistsError:
        return False  # another invocation is already spawning one -- don't pile on

    try:
        args = [sys.executable, "-m", "av_cli.daemon", str(repo_root), cli_version]
        if idle_timeout is not None:
            args.append(str(idle_timeout))
        spawn_detached(args)
        return True
    finally:
        with contextlib.suppress(OSError):
            lock_path.unlink()
