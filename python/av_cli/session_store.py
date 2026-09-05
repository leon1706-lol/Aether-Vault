"""User-level SSO session storage for `av login`/`logout`/`whoami` (v1.3.3, WP-14/WP-15).

Lives at `~/.aether-vault/session.json` — the SAME user-level directory `update_check.py`
already established (`USER_CONFIG_DIR`, distinct from the per-repo `.av/config`), not the
plan's originally-sketched `~/.av/session.json`: a login session is per-user-per-machine,
not per-repo, and this repo already has exactly one established convention for
"per-user, not per-repo" state — this module follows it rather than adding a second,
differently-named user config directory for no reason.

Never imported by `server.py`/`av_server` — purely a CLI-side concern.
"""
from __future__ import annotations

import json
import os
import stat
from pathlib import Path

from .fsutil import atomic_write_json
from .update_check import USER_CONFIG_DIR

SESSION_PATH = USER_CONFIG_DIR / "session.json"


def _secure_permissions(path: Path) -> None:
    """Best-effort 0600 — POSIX only. Windows' ACL model doesn't map onto `chmod`, and
    this file lives in the user's own profile directory there, which is the platform's
    own equivalent boundary; a failure here is never fatal to login itself."""
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass


def load_session() -> dict | None:
    """Returns the stored session dict (`token`, `url`, `username`, `provider_id`,
    `expires_at`), or None if there is none, it's corrupt, or it's expired. Expiry is
    checked client-side purely as a courtesy (skip a doomed request) — the server's own
    `sessions.expires_at`/`revoked_at` checks are the real enforcement."""
    if not SESSION_PATH.exists():
        return None
    try:
        with open(SESSION_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return None
    expires_at = data.get("expires_at")
    if expires_at is not None:
        import time

        if time.time() > expires_at:
            return None
    return data


def save_session(*, token: str, url: str, username: str | None = None,
                  provider_id: str | None = None, tenant_id: str | None = None,
                  expires_at: float | None = None) -> None:
    USER_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    atomic_write_json(SESSION_PATH, {
        "token": token, "url": url, "username": username,
        "provider_id": provider_id, "tenant_id": tenant_id, "expires_at": expires_at,
    })
    _secure_permissions(SESSION_PATH)


def clear_session() -> None:
    try:
        SESSION_PATH.unlink()
    except FileNotFoundError:
        pass
