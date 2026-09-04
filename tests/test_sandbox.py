"""Tool permission manifests + driver resolution (v1.3.1, RSI R5)."""
import pytest

from python.av_cli.sandbox.base import JobSpec, Mount, get_driver
from python.av_cli.sandbox.manifest import (
    DEFAULT_MANIFEST,
    load_manifest,
    save_manifest,
    verify_spec_against_manifest,
)


def test_load_manifest_defaults_to_maximally_restrictive(tmp_path):
    manifest = load_manifest(tmp_path, "no-such-improver")
    assert manifest == DEFAULT_MANIFEST
    assert manifest["network"] == "none"
    assert manifest["gpu"] is False


def test_save_and_load_round_trip(tmp_path):
    save_manifest(tmp_path, "imp-1", {"writable_paths": ["/data/*"], "network": "bridge"})
    manifest = load_manifest(tmp_path, "imp-1")
    assert manifest["writable_paths"] == ["/data/*"]
    assert manifest["network"] == "bridge"
    assert manifest["gpu"] is False  # merged with DEFAULT_MANIFEST


def test_load_manifest_corrupt_file_falls_back_to_default(tmp_path):
    path = tmp_path / ".av" / "tool_manifests" / "imp-2.json"
    path.parent.mkdir(parents=True)
    path.write_text("not json", encoding="utf-8")
    assert load_manifest(tmp_path, "imp-2") == DEFAULT_MANIFEST


def test_verify_rejects_mount_outside_allowlist():
    manifest = {"writable_paths": ["/allowed/*"], "network": "none", "gpu": False}
    spec = JobSpec(job_id="j1", command=["true"],
                   mounts=[Mount(host="/forbidden/data", container="/data", mode="rw")])
    ok, reason = verify_spec_against_manifest(spec, manifest)
    assert ok is False
    assert "/forbidden/data" in reason


def test_verify_allows_mount_matching_glob():
    manifest = {"writable_paths": ["/allowed/*"], "network": "none", "gpu": False}
    spec = JobSpec(job_id="j1", command=["true"],
                   mounts=[Mount(host="/allowed/data", container="/data", mode="rw")])
    ok, reason = verify_spec_against_manifest(spec, manifest)
    assert ok is True


def test_verify_ignores_readonly_mounts_against_writable_paths():
    """A read-only mount is never a writable_paths violation, regardless of the glob."""
    manifest = {"writable_paths": [], "network": "none", "gpu": False}
    spec = JobSpec(job_id="j1", command=["true"],
                   mounts=[Mount(host="/anywhere", container="/data", mode="ro")])
    ok, _ = verify_spec_against_manifest(spec, manifest)
    assert ok is True


def test_verify_rejects_network_escalation():
    manifest = {"writable_paths": [], "network": "none", "gpu": False}
    spec = JobSpec(job_id="j1", command=["true"], network="bridge")
    ok, reason = verify_spec_against_manifest(spec, manifest)
    assert ok is False
    assert "network" in reason


def test_verify_allows_requesting_less_than_granted():
    manifest = {"writable_paths": [], "network": "bridge", "gpu": True}
    spec = JobSpec(job_id="j1", command=["true"], network="none", gpu=False)
    ok, _ = verify_spec_against_manifest(spec, manifest)
    assert ok is True


def test_verify_rejects_gpu_escalation():
    manifest = {"writable_paths": [], "network": "none", "gpu": False}
    spec = JobSpec(job_id="j1", command=["true"], gpu=True)
    ok, reason = verify_spec_against_manifest(spec, manifest)
    assert ok is False
    assert "gpu" in reason.lower()


# ---------------------------------------------------------------------------
# get_driver
# ---------------------------------------------------------------------------

def test_get_driver_resolves_all_four(tmp_path):
    for name in ("local", "docker", "kubernetes", "slurm"):
        driver = get_driver(name, tmp_path)
        assert driver.name == name


def test_get_driver_rejects_unknown_name(tmp_path):
    with pytest.raises(ValueError, match="Unknown sandbox driver"):
        get_driver("nonsense", tmp_path)
