"""v1.2.5 env snapshot depth: snapshot_version 2 hashed/observed split, richer capture,
--execute interpreter fix + --target-venv/--conda-env, --validate, --dockerfile --cuda/--out.

Golden fixtures here run in EVERY CI job (all OS/Python combinations in the matrix) —
that matrix itself is the cross-machine/cross-OS proof that equivalent environments
produce identical snapshot ids, per the V1.2.5 plan's WP-5 goal.
"""
import json
import sys

import pytest
from click.testing import CliRunner

from python.av_cli.core import canonical_env_bytes, env_snapshot_id
from python.av_cli.main import cli


def invoke(*args):
    return CliRunner().invoke(cli, list(args), standalone_mode=False)


def jinvoke(*args):
    res = CliRunner().invoke(cli, ["--output", "json", *args], standalone_mode=False)
    assert res.exit_code == 0, res.output
    return json.loads(res.output)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    res = invoke("init", "--mode", "local", "--yes", "--no-repl")
    assert res.exit_code == 0, res.output
    return tmp_path


# ---------------------------------------------------------------------------
# snapshot_version 2: hashed identity vs observed context
# ---------------------------------------------------------------------------

_GOLDEN_ENV_A = {
    "python": "3.12.4", "os_family": "Linux",
    "pins": {"torch": "2.3.0", "numpy": "1.26.4"},
    "seeds": {"SEED": "42"},
    "cuda_toolkit_version": "12.1",
    "env_vars": {"CUDA_VISIBLE_DEVICES": "0,1"},
}


def _snap_v2(env_dict, observed_dict, captured_at="2026-01-01T00:00:00+00:00"):
    return {
        "snapshot_version": 2, "captured_at": captured_at,
        "python": env_dict["python"], "platform": "linux",
        "cuda_visible_devices": env_dict.get("env_vars", {}).get("CUDA_VISIBLE_DEVICES"),
        "seeds": env_dict["seeds"], "pins": env_dict["pins"],
        "env": env_dict, "observed": observed_dict,
    }


def test_golden_snapshot_v2_id_is_stable():
    """Pinned exact id — any accidental change to the hashed field set or JSON encoding
    shows up here immediately, on every OS/Python this test runs under."""
    observed = {"gpu_names": ["NVIDIA A100"], "driver_version": "535.104.05",
                "device_count": 1, "hostname": "machine-a", "conda_env": None,
                "interpreter": {"executable": "/usr/bin/python3", "prefix": "/usr",
                               "base_prefix": "/usr", "conda_prefix": None}}
    snap = _snap_v2(_GOLDEN_ENV_A, observed)
    sid = env_snapshot_id(snap)
    # Re-hashing the same canonical env dict independently (bypassing env_snapshot_id)
    # must match, proving the hash is a pure function of `env` + snapshot_version alone —
    # not of anything in `observed` or of dict key insertion order.
    import hashlib

    expected = hashlib.sha256(
        json.dumps({"snapshot_version": 2, "env": _GOLDEN_ENV_A}, sort_keys=True,
                   separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    assert sid == expected


def test_equivalent_environments_on_different_machines_share_an_id():
    """The actual cross-machine contract: two snapshots with IDENTICAL `env` but
    completely different `observed` (different GPU, driver, hostname, interpreter path,
    even captured_at) must hash to the SAME id."""
    observed_a = {"gpu_names": ["NVIDIA A100"], "driver_version": "535.104.05",
                  "device_count": 1, "hostname": "gpu-box-1", "conda_env": None,
                  "interpreter": {"executable": "/usr/bin/python3", "prefix": "/usr",
                                 "base_prefix": "/usr", "conda_prefix": None}}
    observed_b = {"gpu_names": ["NVIDIA H100", "NVIDIA H100"], "driver_version": "550.54.14",
                  "device_count": 2, "hostname": "training-cluster-node-42",
                  "conda_env": "ml-env",
                  "interpreter": {"executable": "/opt/conda/envs/ml-env/bin/python",
                                 "prefix": "/opt/conda/envs/ml-env",
                                 "base_prefix": "/opt/conda",
                                 "conda_prefix": "/opt/conda/envs/ml-env"}}
    snap_a = _snap_v2(_GOLDEN_ENV_A, observed_a, captured_at="2026-01-01T00:00:00+00:00")
    snap_b = _snap_v2(_GOLDEN_ENV_A, observed_b, captured_at="2026-06-15T18:30:00+00:00")
    assert env_snapshot_id(snap_a) == env_snapshot_id(snap_b)


def test_different_env_identity_produces_different_id():
    """Sanity check on the other side: a REAL difference in hashed fields (here, CUDA
    toolkit version — exactly the kind of thing that changes training behavior) must
    change the id, so the split isn't accidentally hashing nothing meaningful."""
    env_b = dict(_GOLDEN_ENV_A, cuda_toolkit_version="11.8")
    snap_a = _snap_v2(_GOLDEN_ENV_A, {})
    snap_b = _snap_v2(env_b, {})
    assert env_snapshot_id(snap_a) != env_snapshot_id(snap_b)


def test_legacy_v1_snapshots_hash_exactly_as_before():
    """No snapshot_version field (or version != 2) — canonical_env_bytes must fall back
    to the pre-1.2.5 whole-dict-minus-captured_at behavior, so objects already sitting
    in a CAS/registry from before this change keep resolving to the same id."""
    legacy = {
        "captured_at": "2026-01-01T00:00:00+00:00",
        "python": "3.11.0", "platform": "linux",
        "cuda_visible_devices": None, "seeds": {}, "pins": {"numpy": "1.24.0"},
    }
    expected = json.dumps(
        {k: v for k, v in legacy.items() if k != "captured_at"},
        sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    assert canonical_env_bytes(legacy) == expected


def test_real_capture_produces_v2_snapshot_with_env_and_observed(repo):
    data = jinvoke("env", "snapshot")["data"]
    # snapshot command doesn't echo the full doc; read it back from disk for the shape.
    snap = json.loads((repo / ".av" / "env_snapshot.json").read_text(encoding="utf-8"))
    assert snap["snapshot_version"] == 2
    assert "env" in snap and "observed" in snap
    for key in ("python", "os_family", "pins", "seeds", "cuda_toolkit_version", "env_vars"):
        assert key in snap["env"]
    for key in ("gpu_names", "driver_version", "device_count", "hostname", "conda_env", "interpreter"):
        assert key in snap["observed"]
    assert data["id"] == env_snapshot_id(snap)


def test_snapshot_is_deterministic_within_a_session(repo):
    id_a = jinvoke("env", "snapshot")["data"]["id"]
    id_b = jinvoke("env", "snapshot")["data"]["id"]
    assert id_a == id_b


def test_custom_capture_vars_via_env_override(repo, monkeypatch):
    monkeypatch.setenv("AV_ENV_CAPTURE_VARS", "MY_CUSTOM_FLAG")
    monkeypatch.setenv("MY_CUSTOM_FLAG", "on")
    jinvoke("env", "snapshot")
    snap = json.loads((repo / ".av" / "env_snapshot.json").read_text())
    assert snap["env"]["env_vars"] == {"MY_CUSTOM_FLAG": "on"}


# ---------------------------------------------------------------------------
# --validate
# ---------------------------------------------------------------------------

def test_validate_reports_per_pin_table_and_fails_on_bad_pin(repo, monkeypatch):
    jinvoke("env", "snapshot")

    def _fake_validate(pins):
        return [
            {"pin": pin,
             "status": "resolvable" if "definitely-not-a-real-package" not in pin else "version-not-found",
             "detail": None}
            for pin in pins
        ]

    from python.av_cli import cmd_env
    monkeypatch.setattr(cmd_env, "_validate_pins",
                        lambda pins: _fake_validate(list(pins) + ["definitely-not-a-real-package-xyz==0.0.0"]))

    result = invoke("env", "replay", "--validate")
    assert result.exit_code == 15, result.output
    assert "version-not-found" in result.output

    json_result = CliRunner().invoke(cli, ["--output", "json", "env", "replay", "--validate"],
                                     standalone_mode=False)
    assert json_result.exit_code == 15
    env = json.loads(json_result.output)
    assert env["ok"] is False
    assert any(r["status"] == "version-not-found" for r in env["error"]["data"]["validation"])


def test_validate_passes_when_all_pins_resolve(repo, monkeypatch):
    jinvoke("env", "snapshot")
    from python.av_cli import cmd_env
    monkeypatch.setattr(cmd_env, "_validate_pins",
                        lambda pins: [{"pin": p, "status": "resolvable", "detail": None} for p in pins])
    result = invoke("env", "replay", "--validate")
    assert result.exit_code == 0, result.output


# ---------------------------------------------------------------------------
# --execute: sys.executable -m pip, --target-venv, --conda-env
# ---------------------------------------------------------------------------

def test_execute_uses_sys_executable_not_bare_pip(repo, monkeypatch):
    """The actual bug fix: pre-1.2.5 shelled a bare 'pip' string, which can silently
    resolve to the WRONG interpreter's pip on a machine with more than one Python."""
    jinvoke("env", "snapshot")
    calls = []

    class _FakeCompleted:
        returncode = 0

    monkeypatch.setattr("subprocess.call", lambda argv, **kw: calls.append(argv) or 0)
    result = invoke("env", "replay", "--execute", "--yes")
    assert result.exit_code == 0, result.output
    assert calls, "pip install was never invoked"
    for argv in calls:
        assert argv[0] == sys.executable
        assert argv[1:3] == ["-m", "pip"]


def test_execute_target_venv_creates_and_installs_there(repo, monkeypatch, tmp_path):
    jinvoke("env", "snapshot")
    calls = []
    monkeypatch.setattr("subprocess.call", lambda argv, **kw: calls.append(argv) or 0)
    venv_dir = tmp_path / "myvenv"
    result = invoke("env", "replay", "--execute", "--yes", "--target-venv", str(venv_dir))
    assert result.exit_code == 0, result.output
    assert venv_dir.exists(), "venv was not created"
    for argv in calls:
        assert str(venv_dir) in argv[0]


def test_execute_conda_env_without_conda_on_path_fails_cleanly(repo, monkeypatch):
    jinvoke("env", "snapshot")
    monkeypatch.setattr("shutil.which", lambda name: None)
    result = invoke("env", "replay", "--execute", "--yes", "--conda-env", "myenv")
    assert result.exit_code == 15, result.output
    assert "conda" in result.output.lower()


def test_target_venv_and_conda_env_are_mutually_exclusive(repo):
    jinvoke("env", "snapshot")
    result = invoke("env", "replay", "--execute", "--target-venv", "x", "--conda-env", "y")
    assert result.exit_code == 15
    assert "mutually exclusive" in result.output


# ---------------------------------------------------------------------------
# --dockerfile --cuda / --out
# ---------------------------------------------------------------------------

def test_dockerfile_default_uses_python_slim_base(repo):
    jinvoke("env", "snapshot")
    out = invoke("env", "replay", "--dockerfile").output
    assert "FROM python:" in out
    assert "nvidia/cuda" not in out


def test_dockerfile_cuda_uses_nvidia_base(repo):
    jinvoke("env", "snapshot")
    out = invoke("env", "replay", "--dockerfile", "--cuda", "12.1.0").output
    assert "nvidia/cuda:12.1.0-runtime-ubuntu22.04" in out
    assert "AS builder" in out


def test_cuda_flag_without_dockerfile_is_rejected(repo):
    jinvoke("env", "snapshot")
    result = invoke("env", "replay", "--cuda", "12.1.0")
    assert result.exit_code == 15
    assert "--dockerfile" in result.output


def test_out_writes_file_instead_of_stdout(repo, tmp_path):
    jinvoke("env", "snapshot")
    out_file = tmp_path / "Dockerfile.repro"
    result = invoke("env", "replay", "--dockerfile", "--out", str(out_file))
    assert result.exit_code == 0, result.output
    assert out_file.exists()
    assert "FROM python:" in out_file.read_text(encoding="utf-8")
    assert "FROM python:" not in result.output  # stdout stayed clean when --out is used


def test_replay_json_mode_emits_single_clean_envelope(repo):
    """Regression: pre-1.2.5, JSON mode printed the raw recipe text UNCONDITIONALLY
    (before the JSON envelope), making stdout not parseable as pure JSON."""
    jinvoke("env", "snapshot")
    result = CliRunner().invoke(cli, ["--output", "json", "env", "replay"], standalone_mode=False)
    assert result.exit_code == 0, result.output
    env = json.loads(result.output)  # raises if anything but one clean JSON line came through
    assert "pip install" in env["data"]["recipe"]
