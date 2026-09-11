"""V1.5.0: naming, paths, and small shared helpers for the `av daemon` -- imported by both
the tiny client (`launcher.py`) and the full server (`daemon.py`). Keep this module's own
import list minimal (stdlib only) since `launcher.py` pays for it on every `av` invocation
that even considers the daemon path.
"""
from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path

# Commands the daemon will execute on the caller's behalf. Deliberately small for V1.5.0:
# --help/--version/anything interactive (login, the REPL, a questionary prompt) always
# takes the normal in-process path. Growing this list later just means adding names here --
# the execution model (real click command objects, one at a time) doesn't change.
ALLOWED_COMMANDS = frozenset({"add", "status", "commit"})

# Env vars forwarded across the daemon protocol -- never the whole environment. Lives here
# (not daemon.py/daemon_client.py individually) so the CLIENT filters before a single byte
# goes on the wire, not just the server after receiving everything -- sending the full
# environment and filtering server-side would mean secrets (AWS keys, tokens, ...) briefly
# transit the connection and sit in the server's own request dict even though nothing ever
# uses them, which the "never the whole environment" design intent explicitly rules out.
ENV_ALLOWLIST_PREFIXES = ("AV_",)
ENV_ALLOWLIST_EXACT = frozenset({"NO_COLOR", "FORCE_COLOR", "HOME", "USERPROFILE", "COLUMNS"})


def allowlisted_env(env: dict) -> dict:
    return {
        k: v for k, v in env.items()
        if k in ENV_ALLOWLIST_EXACT or k.startswith(ENV_ALLOWLIST_PREFIXES)
    }


def user_runtime_dir() -> Path:
    """Where daemon state/lock/token files live -- NOT necessarily where the transport
    endpoint itself lives (POSIX puts the socket here too; Windows named pipes are a
    separate namespace and don't need a filesystem path at all, but still use this
    directory for the lock/state bookkeeping every platform needs)."""
    xdg = os.environ.get("XDG_RUNTIME_DIR")
    if xdg:
        base = Path(xdg) / "aether-vault"
    else:
        base = Path.home() / ".aether-vault" / "run"
    return base


def _av_cli_source_marker() -> str:
    """A path that changes when the installed `av_cli` package changes location (a fresh
    `pip install`/`pip install -e .` into a different tree) -- part of the endpoint name so
    an old daemon from a since-replaced install can never be found by a new one. Cheap:
    just this module's own `__file__`, no package import beyond what's already loaded."""
    try:
        return str(Path(__file__).resolve())
    except OSError:
        return __file__


def endpoint_key(repo_root: str | os.PathLike, protocol_version: int, cli_version: str) -> str:
    """16 hex chars identifying (repo, protocol, cli version, python version, install
    location) -- folded into both the POSIX socket filename and the Windows pipe name, so a
    version-skewed daemon is simply unreachable by construction rather than something a
    client has to detect after connecting (the handshake/self-check in daemon.py are the
    second and third independent layers, for skew that happens *after* a client already
    connected -- e.g. a `pip install -e .` that lands mid-session)."""
    realpath = str(Path(repo_root).resolve())
    material = "|".join([
        str(protocol_version), cli_version,
        f"{sys.version_info[0]}.{sys.version_info[1]}",
        _av_cli_source_marker(), realpath,
    ])
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


def state_file(repo_root: str | os.PathLike, protocol_version: int, cli_version: str) -> Path:
    key = endpoint_key(repo_root, protocol_version, cli_version)
    return user_runtime_dir() / f"{key}.json"


def lock_file(repo_root: str | os.PathLike, protocol_version: int, cli_version: str) -> Path:
    key = endpoint_key(repo_root, protocol_version, cli_version)
    return user_runtime_dir() / f"{key}.lock"


def posix_socket_path(repo_root: str | os.PathLike, protocol_version: int, cli_version: str) -> Path:
    key = endpoint_key(repo_root, protocol_version, cli_version)
    return user_runtime_dir() / f"{key}.sock"


def windows_pipe_name(repo_root: str | os.PathLike, protocol_version: int, cli_version: str) -> str:
    key = endpoint_key(repo_root, protocol_version, cli_version)
    user = os.environ.get("USERNAME") or os.environ.get("USER") or "unknown"
    # Backslashes/pipe-unsafe characters in a username are vanishingly rare on Windows and
    # would already break plenty else; not defended against beyond staying ASCII-safe here.
    safe_user = "".join(c if c.isalnum() else "-" for c in user)
    return rf"\\.\pipe\aether-vault-{safe_user}-{key}"


def _truthy(value: str) -> bool:
    return value.strip().lower() in ("1", "true", "yes")


def config_allows_auto_spawn(repo_root: str | os.PathLike) -> bool:
    """Lightweight, direct read of `.av/config`'s `"daemon": {"enabled": true}` -- NOT
    `core.load_config()`, deliberately: that function backfills/persists `project_id` on
    first read (a real write side effect), and importing `core.py` at all defeats the
    whole point of keeping this module cheap for `launcher.py`'s fast path. A missing,
    unreadable, or malformed config is just "not enabled" -- never an error here."""
    try:
        import json

        with open(Path(repo_root) / ".av" / "config", "r", encoding="utf-8") as f:
            cfg = json.load(f)
        return bool(cfg.get("daemon", {}).get("enabled", False))
    except (OSError, ValueError, AttributeError):
        return False


def enabled_mode(repo_root: str | os.PathLike | None = None) -> str:
    """Three-state daemon enablement, checked by the launcher before it does anything else.
    Precedence, highest first:
      - "never"      AV_NO_DAEMON=1 always wins, unconditionally.
      - "auto_spawn" AV_DAEMON=1, or (when `repo_root` is given) `.av/config`'s
                     `"daemon": {"enabled": true}` -- opt in to the daemon auto-starting on
                     demand when none is running yet.
      - "use_only"   the default: use an already-running daemon if one happens to be up for
                     this repo, but never spawn one. `av daemon start` IS the opt-in.
    Pass `repo_root=None` (the default) for the cheap, env-only check -- callers that
    already know they're not in the "never" case and have a repo_root in hand should pass
    it to also honor the config-file opt-in.
    """
    if _truthy(os.environ.get("AV_NO_DAEMON", "")):
        return "never"
    if _truthy(os.environ.get("AV_DAEMON", "")):
        return "auto_spawn"
    if repo_root is not None and config_allows_auto_spawn(repo_root):
        return "auto_spawn"
    return "use_only"
