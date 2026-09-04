"""Tool permission manifests — a per-improver-version allowlist of what a sandboxed job
may touch (v1.3.1, RSI R5: todo.md G.30).

A manifest is a CAS object (`casobj.py`, the same pattern every other RSI artifact
uses): `{"writable_paths": [glob, ...], "network": "none"|"bridge",
"network_destinations": [str, ...], "gpu": bool}`. `verify_spec_against_manifest()` is
the ONE place policy is checked — every driver calls it (indirectly, via
`cmd_sandbox.py`) before `submit()`, so no driver re-implements the parsing, only the
enforcement mechanics it's actually capable of (see `base.py`'s module docstring and each
driver's own docstring for what "enforced" means there).

`network_destinations` is recorded for the AUDIT TRAIL (which endpoints this improver
version's jobs are declared to need) even where a driver cannot enforce a per-destination
allowlist natively — `docker`'s enforcement is binary (`--network none` blocks
everything; `bridge` allows everything), matching what `docker run` actually offers
without an additional sidecar proxy this project doesn't depend on. That gap is
documented here once, not silently implied to be finer-grained than it is.
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
    restrictive: no writable paths, no network, no GPU) when none has been set — fails
    CLOSED, not open, matching this project's freeze/scope conventions elsewhere."""
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
    """Checks a `JobSpec` (see `base.py`) against `manifest` BEFORE any driver runs it.
    Returns (ok, reason) — a violation must abort the job before it ever starts, not be
    discovered after the fact. Checked, in order: every mount's host path must match one
    of `writable_paths` (glob); `spec.network` must not request MORE than the manifest
    allows (`"bridge"` requested against a `"none"` manifest is a violation; the reverse
    is always fine — asking for LESS access than you're allowed is never a violation);
    `spec.gpu` must not exceed `manifest["gpu"]`.
    """
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
