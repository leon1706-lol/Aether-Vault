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

# Commands the daemon will execute on the caller's behalf. --help/--version/anything
# interactive (login, the REPL, a questionary prompt) always takes the normal in-process
# path regardless of this set. V1.6.0 grew this from the V1.5.0 {"add","status","commit"}
# to every command module verified (by direct source inspection, not just "seems safe") to
# never import questionary/prompt_toolkit and never call click.prompt/click.confirm/input().
# Deliberately still excludes checkout/clone/pull/merge/stash: all five rewrite the working
# tree, and the daemon has no cancel semantics yet for a client that disconnects mid-request
# (see daemon.py's module docstring) -- revisit once that's built. `av run`/`av context` are
# whole command GROUPS here (their subcommands share one entry point, `argv[0]`), verified
# prompt-free across every subcommand, not just the group's own top-level help.
ALLOWED_COMMANDS = frozenset({
    "add", "status", "commit", "push", "fetch", "unstage", "log", "diff", "context", "run",
})

# Env vars forwarded across the daemon protocol -- never the whole environment. Lives here
# (not daemon.py/daemon_client.py individually) so the CLIENT filters before a single byte
# goes on the wire, not just the server after receiving everything -- sending the full
# environment and filtering server-side would mean secrets (AWS keys, tokens, ...) briefly
# transit the connection and sit in the server's own request dict even though nothing ever
# uses them, which the "never the whole environment" design intent explicitly rules out.
ENV_ALLOWLIST_PREFIXES = ("AV_",)
ENV_ALLOWLIST_EXACT = frozenset({"NO_COLOR", "FORCE_COLOR", "HOME", "USERPROFILE", "COLUMNS"})


# `cli`'s own global options (main.py) -- mirrored here rather than imported from click, so
# every daemon-path consumer (the client's own entry point, the client's daemon-eligibility
# check, AND the server's re-check of what the client sent) can resolve "what subcommand is
# this argv actually invoking" without importing click. `--verbose`/`--silent`/`--version`
# are bare flags; `--output <value>`/`--output=value` takes one.
GLOBAL_FLAGS = frozenset({"--verbose", "--silent", "--version"})
GLOBAL_VALUE_OPTS = frozenset({"--output"})


def first_subcommand(argv: list[str]) -> str | None:
    """The first token in argv that isn't one of `cli`'s own global options -- what click's
    parser would resolve as the subcommand name, computed without importing click at all.

    V1.6.0 (Probleme.md real bug, found building the native launcher's own test coverage):
    this used to be `launcher.py`'s own private helper, used correctly at THAT layer (the
    module's `main()`/`_try_daemon` gate), but `call_daemon()` and `daemon.py`'s
    `handle_request()` each had their OWN, cruder re-check of `argv[0] not in
    ALLOWED_COMMANDS` -- a plain positional check that (correctly) passes for `["status"]`
    but (incorrectly) fails for `["--output", "json", "status"]`, since `argv[0]` there is
    `"--output"`, not `"status"`. `launcher.py`'s own gate got fixed for this in V1.6.0, but
    both of `call_daemon`'s and the server's OWN independent re-checks still used the old
    positional form -- so `av --output json status` (the exact agent-facing shape this fix
    was supposed to unblock) reached `_try_daemon`, correctly passed ITS gate, then got
    silently rejected one layer deeper, never reaching the daemon at all and falling back
    in-process every time, with no error and no debug signal (`call_daemon` returning `None`
    for "not allowlisted" is indistinguishable from "no daemon running" by design). One
    shared implementation, used identically at all three layers, is what makes this class of
    drift structurally impossible instead of merely fixed today.
    """
    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok in GLOBAL_FLAGS:
            i += 1
            continue
        if tok in GLOBAL_VALUE_OPTS:
            i += 2
            continue
        if any(tok.startswith(opt + "=") for opt in GLOBAL_VALUE_OPTS):
            i += 1
            continue
        return tok
    return None


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


def _falsy(value: str) -> bool:
    return value.strip().lower() in ("0", "false", "no")


def launcher_discovery_file(exe: str, repo_root: str) -> Path:
    """V1.6.0: where the Python side leaves a breadcrumb for the native `av` launcher
    (`src/launcher/av_launcher.cpp`) to find on its NEXT invocation for this (exe, repo)
    pair -- `{"protocol","cli_version","endpoint","key_path","pid"}`.

    The exe cannot compute `endpoint_key()` itself (that folds in this module's own
    `__file__` and the Python version, neither of which the exe can observe), so it can't
    find its own state/lock files directly. Instead it hashes the two strings it DOES know
    -- its own resolved path and the repo root it found by walking up from cwd -- and looks
    for a file under that key. This function must hash those exact same two strings, used
    VERBATIM (never re-resolved/re-normalized) -- both `exe` and `repo_root` here come
    straight from the `AV_LAUNCHER_EXE`/`AV_LAUNCHER_REPO` env vars the exe set on its own
    fallback exec, so the two independent implementations (this module, the C++ launcher)
    are guaranteed to compute the same 16-hex-char key without needing to agree on any path
    normalization convention -- only on not touching what the other side already decided.
    """
    material = f"{exe}|{repo_root}"
    key = hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]
    return user_runtime_dir() / f"launcher-{key}.json"


def config_daemon_enabled(repo_root: str | os.PathLike) -> bool | None:
    """Lightweight, direct read of `.av/config`'s `"daemon": {"enabled": ...}` -- NOT
    `core.load_config()`, deliberately: that function backfills/persists `project_id` on
    first read (a real write side effect), and importing `core.py` at all defeats the
    whole point of keeping this module cheap for `launcher.py`'s fast path. Returns None
    (no opinion either way) when the key is absent, unreadable, or malformed -- never an
    error here -- and the real bool otherwise, so a caller can tell "not set" apart from
    "explicitly set to false" (V1.5.0's `config_allows_auto_spawn` collapsed both to
    False, which was fine when the default was itself False; V1.6.0's default flipped to
    auto-spawn, so "absent" and "explicitly false" now need to mean different things)."""
    try:
        import json

        with open(Path(repo_root) / ".av" / "config", "r", encoding="utf-8") as f:
            cfg = json.load(f)
        daemon_cfg = cfg.get("daemon")
        if not isinstance(daemon_cfg, dict) or "enabled" not in daemon_cfg:
            return None
        return bool(daemon_cfg["enabled"])
    except (OSError, ValueError, AttributeError):
        return None


def config_allows_auto_spawn(repo_root: str | os.PathLike) -> bool:
    """V1.5.0-compatible shape (True/False, never None) kept for existing callers/tests --
    prefer `config_daemon_enabled()` for new code, which distinguishes "absent" from
    "explicitly false"."""
    return config_daemon_enabled(repo_root) is True


def enabled_mode(repo_root: str | os.PathLike | None = None) -> str:
    """Three-state daemon enablement, checked by the launcher before it does anything else.
    Precedence, highest first:
      - "never"      AV_NO_DAEMON=1 always wins, unconditionally.
      - "use_only"   AV_DAEMON is explicitly falsy (0/false/no) -- opt out of auto-spawn
                     for this invocation while still using an already-running daemon.
      - "auto_spawn" AV_DAEMON is explicitly truthy (1/true/yes).
      - "use_only"   (when `repo_root` is given) `.av/config`'s `"daemon":{"enabled":false}`
                     -- a per-repo opt-out, checked before the default below.
      - "auto_spawn" **the default since V1.6.0** (was "use_only" in V1.5.0): a daemon
                     auto-starts on first use per repo unless one of the opt-outs above
                     applies. `AV_NO_DAEMON=1` is the unconditional escape hatch; `av
                     daemon start`/`stop` remain available regardless of mode either way.
    Pass `repo_root=None` (the default) for the cheap, env-only check -- callers that
    already know they're not in the "never"/explicit-env case and have a repo_root in hand
    should pass it to also honor the config-file opt-out/opt-in.
    """
    if _truthy(os.environ.get("AV_NO_DAEMON", "")):
        return "never"
    av_daemon = os.environ.get("AV_DAEMON", "")
    if av_daemon and _falsy(av_daemon):
        return "use_only"
    if _truthy(av_daemon):
        return "auto_spawn"
    if repo_root is not None:
        cfg_enabled = config_daemon_enabled(repo_root)
        if cfg_enabled is False:
            return "use_only"
        if cfg_enabled is True:
            return "auto_spawn"
    return "auto_spawn"
