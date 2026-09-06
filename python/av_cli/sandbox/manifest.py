"""Tool permission manifests — a per-improver-version allowlist of what a sandboxed job
may touch (v1.3.1): `{"writable_paths": [glob, ...], "network": "none"|"bridge",
"network_destinations": [str, ...], "gpu": bool}`. `verify_spec_against_manifest()` is
the ONE place policy is checked, before any driver's `submit()`. `network_destinations`
is recorded for the audit trail even where a driver's enforcement is only binary
(`docker`'s `--network none`/`bridge`, no per-destination allowlist).
"""
from __future__ import annotations

import fnmatch
import json
from pathlib import Path

DEFAULT_MANIFEST = {
    "writable_paths": [],
    "network": "none",
    "network_destinations": [],
    "gpu": False,
}


class ToolManifest(dict):
    """A thin dict subclass so callers get attribute-free but still type-hinted access;
    behaves exactly like the plain dict `casobj.read_object()` already returns."""


def _manifest_path(repo_root: Path, improver_id: str) -> Path:
    return repo_root / ".av" / "tool_manifests" / f"{improver_id}.json"


def load_manifest(repo_root: Path, improver_id: str) -> ToolManifest:
    """The manifest ARMED for `improver_id`, or `DEFAULT_MANIFEST` (maximally
    restrictive) when none has been set -- fails CLOSED, not open."""
    path = _manifest_path(repo_root, improver_id)
    if not path.exists():
        return ToolManifest(DEFAULT_MANIFEST)
    try:
        return ToolManifest(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError):
        return ToolManifest(DEFAULT_MANIFEST)


def save_manifest(repo_root: Path, improver_id: str, manifest: dict) -> None:
    from ..fsutil import atomic_write_text

    path = _manifest_path(repo_root, improver_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    merged = {**DEFAULT_MANIFEST, **manifest}
    atomic_write_text(path, json.dumps(merged, indent=2, sort_keys=True))


def verify_spec_against_manifest(spec, manifest: dict) -> tuple[bool, str]:
    """Checks a `JobSpec` against `manifest` BEFORE any driver runs it. Returns (ok,
    reason). Checked in order: every rw mount's host path matches a `writable_paths`
    glob; `spec.network` doesn't request MORE than the manifest allows; `spec.gpu`
    doesn't exceed `manifest["gpu"]`."""
    allowed_paths = manifest.get("writable_paths") or []
    for mount in spec.mounts:
        if mount.mode != "rw":
            continue
        host = str(mount.host)
        if not any(fnmatch.fnmatch(host, pattern) for pattern in allowed_paths):
            return False, f"mount {host!r} (rw) is not covered by any writable_paths glob"

    manifest_network = manifest.get("network", "none")
    if spec.network == "bridge" and manifest_network == "none":
        return False, "job requests network access but the manifest declares network: none"

    if spec.gpu and not manifest.get("gpu", False):
        return False, "job requests GPU access but the manifest does not grant it"

    return True, "ok"
