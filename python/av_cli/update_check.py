"""PyPI update checking — user-level config, distinct from the per-repo `.av/config`.

Checked only from `av init` and the explicit `av update` command (never on every routine
command), so normal commands (`av add`, `av status`, ...) never pay a network-call cost.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path

import requests
from packaging.version import InvalidVersion, Version

from . import __version__
from .fsutil import atomic_write_json

PACKAGE_NAME = "aether-vault"
PYPI_JSON_URL = f"https://pypi.org/pypi/{PACKAGE_NAME}/json"
USER_CONFIG_DIR = Path.home() / ".aether-vault"
USER_CONFIG_PATH = USER_CONFIG_DIR / "config.json"

_DEFAULT_USER_CONFIG = {
    "auto_update": False,
    "update_check_enabled": True,
    "last_check_ts": 0.0,
    "last_check_result": None,
}


@dataclass
class UpdateCheckResult:
    current: str
    latest: str
    is_outdated: bool


def load_user_config() -> dict:
    if USER_CONFIG_PATH.exists():
        try:
            with open(USER_CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            merged = dict(_DEFAULT_USER_CONFIG)
            merged.update(cfg)
            return merged
        except (json.JSONDecodeError, OSError):
            pass
    return dict(_DEFAULT_USER_CONFIG)


def save_user_config(cfg: dict) -> None:
    atomic_write_json(USER_CONFIG_PATH, cfg)


def _fetch_latest_version(timeout: float = 2.0) -> str | None:
    try:
        resp = requests.get(PYPI_JSON_URL, timeout=timeout)
        resp.raise_for_status()
        return resp.json()["info"]["version"]
    except (requests.RequestException, KeyError, ValueError):
        return None


def _is_outdated(current: str, latest: str) -> bool:
    try:
        return Version(current) < Version(latest)
    except InvalidVersion:
        return current != latest


def check_for_update(force: bool = False, cache_hours: float = 12.0) -> UpdateCheckResult | None:
    cfg = load_user_config()

    if not force and not cfg.get("update_check_enabled", True):
        return None

    now = time.time()
    last_check_ts = cfg.get("last_check_ts", 0.0)
    cached = cfg.get("last_check_result")
    if not force and cached and (now - last_check_ts) < cache_hours * 3600:
        return UpdateCheckResult(
            current=cached["checked_version"],
            latest=cached["latest_version"],
            is_outdated=_is_outdated(cached["checked_version"], cached["latest_version"]),
        )

    latest = _fetch_latest_version()
    if latest is None:
        # Network failure: don't poison the cache, so the next call retries instead of
        # waiting out the cache window on a stale failure.
        return None

    cfg["last_check_ts"] = now
    cfg["last_check_result"] = {"checked_version": __version__, "latest_version": latest}
    save_user_config(cfg)

    return UpdateCheckResult(
        current=__version__, latest=latest, is_outdated=_is_outdated(__version__, latest)
    )


def list_versions() -> list[str] | None:
    """Every published version, newest first. Returns None on network failure."""
    try:
        resp = requests.get(PYPI_JSON_URL, timeout=5.0)
        resp.raise_for_status()
        releases = resp.json()["releases"]
    except (requests.RequestException, KeyError, ValueError):
        return None

    # An empty file list means the release was yanked/removed from PyPI.
    versions = [v for v, files in releases.items() if files]
    try:
        versions.sort(key=Version, reverse=True)
    except InvalidVersion:
        versions.sort(reverse=True)
    return versions


def maybe_auto_update() -> bool:
    """Upgrades via pip if the user has opted in and an update is available.

    Only call this right before the process would exit anyway — never mid-command.
    """
    import subprocess
    import sys

    cfg = load_user_config()
    if not cfg.get("auto_update", False):
        return False

    result = check_for_update(force=True)
    if result is None or not result.is_outdated:
        return False

    subprocess.run([sys.executable, "-m", "pip", "install", "--upgrade", PACKAGE_NAME])
    return True
