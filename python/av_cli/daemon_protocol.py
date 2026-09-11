"""V1.5.0: wire protocol shared by the daemon client (`launcher.py`, deliberately tiny --
see its own docstring) and server (`daemon.py`, which can afford heavier imports since it's
never on the fast path). Framing only depends on `struct`/`json` -- no `requests`, no
`click`, nothing from `av_cli.core`.

Frame shape: 4-byte big-endian unsigned length, then that many bytes of UTF-8 JSON. Chosen
over `multiprocessing.connection`'s framing specifically because importing that module costs
~337ms on this project's own dev box (mostly the `_multiprocessing` C extension) -- more than
the entire startup budget the daemon exists to save. This module's own import cost is a
`struct`/`json` `import` only.

Protocol version bumps whenever the request/response SHAPE changes (not on every release) --
see `PROTOCOL_VERSION`'s own comment.
"""
from __future__ import annotations

import json
import struct

# Bump only when the request/response dict shape itself changes incompatibly -- NOT on
# every `av` release. A mismatch here is a hard refuse (see daemon.py's handshake check);
# `cli_version` in the request is the finer-grained signal used for the self-check/skew
# handling described in daemon.py.
PROTOCOL_VERSION = 1

MAX_FRAME_BYTES = 8 * 1024 * 1024  # 8 MiB -- generous for stdout/stderr text, not a data channel
_LEN_STRUCT = struct.Struct(">I")


class ProtocolError(Exception):
    """Malformed frame, oversized frame, or a connection that closed mid-read."""


def encode_frame(obj: dict) -> bytes:
    payload = json.dumps(obj, separators=(",", ":")).encode("utf-8")
    if len(payload) > MAX_FRAME_BYTES:
        raise ProtocolError(f"frame too large: {len(payload)} bytes > {MAX_FRAME_BYTES}")
    return _LEN_STRUCT.pack(len(payload)) + payload


def _read_exact(read_fn, n: int) -> bytes:
    """`read_fn(n)` must behave like `socket.recv`/`file.read`: return b"" (not raise) on
    a clean EOF, and may return fewer than `n` bytes per call."""
    chunks = []
    remaining = n
    while remaining > 0:
        chunk = read_fn(remaining)
        if not chunk:
            raise ProtocolError("connection closed mid-frame")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def decode_frame(read_fn) -> dict:
    """`read_fn` is a callable taking a max-byte-count and returning up to that many bytes
    (a plain function, not necessarily a socket/file object -- so both the socket-based
    POSIX client and the file-object-based Windows named-pipe client can share this)."""
    header = _read_exact(read_fn, _LEN_STRUCT.size)
    (length,) = _LEN_STRUCT.unpack(header)
    if length > MAX_FRAME_BYTES:
        raise ProtocolError(f"frame too large: {length} bytes > {MAX_FRAME_BYTES}")
    payload = _read_exact(read_fn, length)
    try:
        return json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolError(f"malformed frame payload: {exc}") from exc


def build_request(*, cli_version: str, token: str, nonce: str, argv: list[str], cwd: str,
                   env: dict[str, str], isatty_stdout: bool, isatty_stderr: bool,
                   columns: int | None) -> dict:
    return {
        "protocol": PROTOCOL_VERSION,
        "cli_version": cli_version,
        "token": token,
        "nonce": nonce,
        "argv": argv,
        "cwd": cwd,
        "env": env,
        "isatty": {"stdout": isatty_stdout, "stderr": isatty_stderr},
        "columns": columns,
    }


def build_error_response(error: str) -> dict:
    return {"protocol": PROTOCOL_VERSION, "error": error}


def build_ok_response(*, server_nonce_mac: str, stdout: str, stderr: str, exit_code: int) -> dict:
    return {
        "protocol": PROTOCOL_VERSION,
        "server_nonce_mac": server_nonce_mac,
        "stdout": stdout,
        "stderr": stderr,
        "exit_code": exit_code,
    }
