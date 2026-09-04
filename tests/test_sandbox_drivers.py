"""Sandbox driver contract tests (v1.3.1, RSI R5) — fake-subprocess technique matching
tests/test_docker_runtime.py: every driver's `subprocess.run` is monkeypatched, so these
prove command construction/response parsing without a real Docker/Kubernetes/Slurm
backend. LocalDriver is the one exception — it runs REAL subprocesses (`echo`, `python
-c ...`), since local execution needs no external daemon to test for real.
"""
import subprocess as real_subprocess
import sys

import pytest

from python.av_cli.sandbox.base import JobSpec, Mount
from python.av_cli.sandbox.drivers.local import LocalDriver
from python.av_cli.sandbox.drivers.docker import DockerDriver
from python.av_cli.sandbox.drivers.kubernetes import KubernetesDriver
from python.av_cli.sandbox.drivers.slurm import SlurmDriver


class _FakeCompleted:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


# ---------------------------------------------------------------------------
# LocalDriver — real subprocess execution
# ---------------------------------------------------------------------------

def test_local_submit_succeeds(tmp_path):
    driver = LocalDriver(tmp_path)
    spec = JobSpec(job_id="j1", command=[sys.executable, "-c", "print('hello')"])
    status = driver.submit(spec)
    assert status.state == "succeeded"
    assert status.exit_code == 0
    assert "hello" in driver.logs("j1")


def test_local_submit_failure_captures_exit_code(tmp_path):
    driver = LocalDriver(tmp_path)
    spec = JobSpec(job_id="j2", command=[sys.executable, "-c", "import sys; sys.exit(3)"])
    status = driver.submit(spec)
    assert status.state == "failed"
    assert status.exit_code == 3


def test_local_submit_timeout(tmp_path):
    driver = LocalDriver(tmp_path)
    spec = JobSpec(job_id="j3", command=[sys.executable, "-c", "import time; time.sleep(5)"],
                   timeout_secs=1)
    status = driver.submit(spec)
    assert status.state == "failed"
    assert "timed out" in driver.logs("j3").lower()


def test_local_status_replays_persisted_result(tmp_path):
    driver = LocalDriver(tmp_path)
    driver.submit(JobSpec(job_id="j4", command=[sys.executable, "-c", "print('x')"]))
    # A SEPARATE driver instance (simulating a later `av sandbox status` invocation).
    driver2 = LocalDriver(tmp_path)
    status = driver2.status("j4")
    assert status.state == "succeeded"


def test_local_status_unknown_job(tmp_path):
    driver = LocalDriver(tmp_path)
    status = driver.status("nope")
    assert status.state == "failed"
    assert "unknown" in status.message


def test_local_cancel_already_terminal_returns_false(tmp_path):
    driver = LocalDriver(tmp_path)
    driver.submit(JobSpec(job_id="j5", command=[sys.executable, "-c", "print('x')"]))
    assert driver.cancel("j5") is False


def test_local_env_is_merged_not_replaced(tmp_path, monkeypatch):
    monkeypatch.setenv("AV_TEST_PARENT_VAR", "parent-value")
    driver = LocalDriver(tmp_path)
    spec = JobSpec(job_id="j6", command=[sys.executable, "-c",
                   "import os; print(os.environ.get('AV_TEST_PARENT_VAR'), "
                   "os.environ.get('AV_TEST_CHILD_VAR'))"],
                   env={"AV_TEST_CHILD_VAR": "child-value"})
    driver.submit(spec)
    assert "parent-value child-value" in driver.logs("j6")


# ---------------------------------------------------------------------------
# DockerDriver — fake subprocess
# ---------------------------------------------------------------------------

def _patch_docker(monkeypatch, responses):
    """`responses`: dict mapping the joined argv[:2] (e.g. "docker run") to a _FakeCompleted."""
    import python.av_cli.sandbox.drivers.docker as docker_mod

    def fake_run(args, **kwargs):
        key = " ".join(args[:2])
        if key in responses:
            return responses[key]
        return _FakeCompleted(returncode=1, stderr=f"unhandled: {args}")

    monkeypatch.setattr(docker_mod.subprocess, "run", fake_run)


def test_docker_submit_builds_expected_args(monkeypatch, tmp_path):
    captured = {}

    def fake_run(args, **kwargs):
        captured["args"] = args
        return _FakeCompleted(returncode=0, stdout="containerid123\n")

    import python.av_cli.sandbox.drivers.docker as docker_mod
    monkeypatch.setattr(docker_mod.subprocess, "run", fake_run)

    driver = DockerDriver(tmp_path)
    spec = JobSpec(job_id="job-1", command=["python:3.12", "python", "-c", "print(1)"],
                  mounts=[Mount(host="/host/data", container="/data", mode="ro")],
                  cpu_limit=2.0, memory_limit_mb=512)
    status = driver.submit(spec)
    assert status.state == "running"
    args = captured["args"]
    assert args[:3] == ["docker", "run", "-d"]
    assert "--name" in args and "av-sandbox-job-1" in args
    assert "--network" in args and "none" in args
    assert "--cpus" in args and "2.0" in args
    assert "--memory" in args and "512m" in args
    assert "-v" in args and "/host/data:/data:ro" in args


def test_docker_submit_requests_bridge_network(monkeypatch, tmp_path):
    captured = {}

    def fake_run(args, **kwargs):
        captured["args"] = args
        return _FakeCompleted(returncode=0)

    import python.av_cli.sandbox.drivers.docker as docker_mod
    monkeypatch.setattr(docker_mod.subprocess, "run", fake_run)

    driver = DockerDriver(tmp_path)
    driver.submit(JobSpec(job_id="job-net", command=["alpine", "true"], network="bridge"))
    idx = captured["args"].index("--network")
    assert captured["args"][idx + 1] == "bridge"


def test_docker_submit_failure_is_reported(monkeypatch, tmp_path):
    def fake_run(args, **kwargs):
        return _FakeCompleted(returncode=1, stderr="no such image")

    import python.av_cli.sandbox.drivers.docker as docker_mod
    monkeypatch.setattr(docker_mod.subprocess, "run", fake_run)

    driver = DockerDriver(tmp_path)
    status = driver.submit(JobSpec(job_id="job-fail", command=["bad-image", "true"]))
    assert status.state == "failed"
    assert "no such image" in status.message


def test_docker_status_running(monkeypatch, tmp_path):
    def fake_run(args, **kwargs):
        return _FakeCompleted(returncode=0, stdout="running \n")

    import python.av_cli.sandbox.drivers.docker as docker_mod
    monkeypatch.setattr(docker_mod.subprocess, "run", fake_run)
    driver = DockerDriver(tmp_path)
    assert driver.status("job-1").state == "running"


def test_docker_status_succeeded(monkeypatch, tmp_path):
    def fake_run(args, **kwargs):
        return _FakeCompleted(returncode=0, stdout="exited 0\n")

    import python.av_cli.sandbox.drivers.docker as docker_mod
    monkeypatch.setattr(docker_mod.subprocess, "run", fake_run)
    driver = DockerDriver(tmp_path)
    status = driver.status("job-1")
    assert status.state == "succeeded"
    assert status.exit_code == 0


def test_docker_status_failed_nonzero_exit(monkeypatch, tmp_path):
    def fake_run(args, **kwargs):
        return _FakeCompleted(returncode=0, stdout="exited 137\n")

    import python.av_cli.sandbox.drivers.docker as docker_mod
    monkeypatch.setattr(docker_mod.subprocess, "run", fake_run)
    driver = DockerDriver(tmp_path)
    status = driver.status("job-1")
    assert status.state == "failed"
    assert status.exit_code == 137


def test_docker_cancel_only_stops_running_jobs(monkeypatch, tmp_path):
    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)
        if args[:2] == ["docker", "inspect"]:
            return _FakeCompleted(returncode=0, stdout="exited 0\n")
        return _FakeCompleted(returncode=0)

    import python.av_cli.sandbox.drivers.docker as docker_mod
    monkeypatch.setattr(docker_mod.subprocess, "run", fake_run)
    driver = DockerDriver(tmp_path)
    assert driver.cancel("job-1") is False  # already terminal
    assert not any(c[:2] == ["docker", "stop"] for c in calls)


def test_docker_logs(monkeypatch, tmp_path):
    def fake_run(args, **kwargs):
        return _FakeCompleted(returncode=0, stdout="line1\n", stderr="err1\n")

    import python.av_cli.sandbox.drivers.docker as docker_mod
    monkeypatch.setattr(docker_mod.subprocess, "run", fake_run)
    driver = DockerDriver(tmp_path)
    assert driver.logs("job-1") == "line1\nerr1\n"


# ---------------------------------------------------------------------------
# KubernetesDriver — fake subprocess
# ---------------------------------------------------------------------------

def test_kubernetes_submit_applies_manifest(monkeypatch, tmp_path):
    captured = {}

    def fake_run(args, **kwargs):
        captured["args"] = args
        captured["input"] = kwargs.get("input")
        return _FakeCompleted(returncode=0)

    import python.av_cli.sandbox.drivers.kubernetes as k8s_mod
    monkeypatch.setattr(k8s_mod.subprocess, "run", fake_run)

    driver = KubernetesDriver(tmp_path)
    spec = JobSpec(job_id="k1", command=["myimage:latest", "run.sh"],
                  mounts=[Mount(host="/data", container="/data", mode="rw")], gpu=True)
    status = driver.submit(spec)
    assert status.state == "running"
    assert captured["args"][:3] == ["kubectl", "apply", "-f"]
    import json
    manifest = json.loads(captured["input"])
    assert manifest["metadata"]["name"] == "av-sandbox-k1"
    assert manifest["spec"]["containers"][0]["image"] == "myimage:latest"
    assert manifest["spec"]["volumes"][0]["hostPath"]["path"] == "/data"
    assert manifest["spec"]["containers"][0]["resources"]["limits"]["nvidia.com/gpu"] == "1"


def test_kubernetes_status_running(monkeypatch, tmp_path):
    def fake_run(args, **kwargs):
        return _FakeCompleted(returncode=0, stdout="Running ")

    import python.av_cli.sandbox.drivers.kubernetes as k8s_mod
    monkeypatch.setattr(k8s_mod.subprocess, "run", fake_run)
    driver = KubernetesDriver(tmp_path)
    assert driver.status("k1").state == "running"


def test_kubernetes_status_succeeded(monkeypatch, tmp_path):
    def fake_run(args, **kwargs):
        return _FakeCompleted(returncode=0, stdout="Succeeded 0")

    import python.av_cli.sandbox.drivers.kubernetes as k8s_mod
    monkeypatch.setattr(k8s_mod.subprocess, "run", fake_run)
    driver = KubernetesDriver(tmp_path)
    status = driver.status("k1")
    assert status.state == "succeeded"
    assert status.exit_code == 0


def test_kubernetes_status_failed(monkeypatch, tmp_path):
    def fake_run(args, **kwargs):
        return _FakeCompleted(returncode=0, stdout="Failed 1")

    import python.av_cli.sandbox.drivers.kubernetes as k8s_mod
    monkeypatch.setattr(k8s_mod.subprocess, "run", fake_run)
    driver = KubernetesDriver(tmp_path)
    status = driver.status("k1")
    assert status.state == "failed"
    assert status.exit_code == 1


def test_kubernetes_status_pod_not_found(monkeypatch, tmp_path):
    def fake_run(args, **kwargs):
        return _FakeCompleted(returncode=1, stderr="not found")

    import python.av_cli.sandbox.drivers.kubernetes as k8s_mod
    monkeypatch.setattr(k8s_mod.subprocess, "run", fake_run)
    driver = KubernetesDriver(tmp_path)
    assert driver.status("nope").state == "failed"


def test_kubernetes_cancel_deletes_running_pod(monkeypatch, tmp_path):
    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)
        if args[0:2] == ["kubectl", "get"]:
            return _FakeCompleted(returncode=0, stdout="Running ")
        return _FakeCompleted(returncode=0)

    import python.av_cli.sandbox.drivers.kubernetes as k8s_mod
    monkeypatch.setattr(k8s_mod.subprocess, "run", fake_run)
    driver = KubernetesDriver(tmp_path)
    assert driver.cancel("k1") is True
    assert any(c[:2] == ["kubectl", "delete"] for c in calls)


# ---------------------------------------------------------------------------
# SlurmDriver — fake subprocess
# ---------------------------------------------------------------------------

def test_slurm_submit_writes_script_and_calls_sbatch(monkeypatch, tmp_path):
    captured = {}

    def fake_run(args, **kwargs):
        captured["args"] = args
        return _FakeCompleted(returncode=0, stdout="Submitted batch job 42\n")

    import python.av_cli.sandbox.drivers.slurm as slurm_mod
    monkeypatch.setattr(slurm_mod.subprocess, "run", fake_run)

    driver = SlurmDriver(tmp_path)
    spec = JobSpec(job_id="s1", command=["python", "train.py"], cpu_limit=4, memory_limit_mb=2048)
    status = driver.submit(spec)
    assert status.state == "running"
    assert captured["args"][0] == "sbatch"
    script_path = driver._script_path("s1")
    assert script_path.exists()
    content = script_path.read_text(encoding="utf-8")
    assert "#SBATCH --job-name=av-sandbox-s1" in content
    assert "#SBATCH --cpus-per-task=4" in content
    assert "#SBATCH --mem=2048M" in content
    assert "python train.py" in content


def test_slurm_status_running_from_squeue(monkeypatch, tmp_path):
    def fake_run(args, **kwargs):
        if args[0] == "squeue":
            return _FakeCompleted(returncode=0, stdout="RUNNING\n")
        return _FakeCompleted(returncode=0, stdout="")

    import python.av_cli.sandbox.drivers.slurm as slurm_mod
    monkeypatch.setattr(slurm_mod.subprocess, "run", fake_run)
    driver = SlurmDriver(tmp_path)
    assert driver.status("s1").state == "running"


def test_slurm_status_completed_from_sacct(monkeypatch, tmp_path):
    def fake_run(args, **kwargs):
        if args[0] == "squeue":
            return _FakeCompleted(returncode=0, stdout="")  # not in live queue anymore
        return _FakeCompleted(returncode=0, stdout="COMPLETED|0:0\n")

    import python.av_cli.sandbox.drivers.slurm as slurm_mod
    monkeypatch.setattr(slurm_mod.subprocess, "run", fake_run)
    driver = SlurmDriver(tmp_path)
    status = driver.status("s1")
    assert status.state == "succeeded"
    assert status.exit_code == 0


def test_slurm_status_failed_from_sacct(monkeypatch, tmp_path):
    def fake_run(args, **kwargs):
        if args[0] == "squeue":
            return _FakeCompleted(returncode=0, stdout="")
        return _FakeCompleted(returncode=0, stdout="FAILED|1:0\n")

    import python.av_cli.sandbox.drivers.slurm as slurm_mod
    monkeypatch.setattr(slurm_mod.subprocess, "run", fake_run)
    driver = SlurmDriver(tmp_path)
    status = driver.status("s1")
    assert status.state == "failed"
    assert status.exit_code == 1


def test_slurm_cancel_only_when_running(monkeypatch, tmp_path):
    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)
        if args[0] == "squeue":
            return _FakeCompleted(returncode=0, stdout="")
        if args[0] == "sacct":
            return _FakeCompleted(returncode=0, stdout="COMPLETED|0:0\n")
        return _FakeCompleted(returncode=0)

    import python.av_cli.sandbox.drivers.slurm as slurm_mod
    monkeypatch.setattr(slurm_mod.subprocess, "run", fake_run)
    driver = SlurmDriver(tmp_path)
    assert driver.cancel("s1") is False
    assert not any(c[0] == "scancel" for c in calls)


def test_slurm_logs_reads_output_file(tmp_path):
    driver = SlurmDriver(tmp_path)
    out_path = driver._output_path("s1")
    out_path.write_text("job output here\n", encoding="utf-8")
    assert "job output here" in driver.logs("s1")


def test_slurm_logs_missing_file_returns_empty(tmp_path):
    driver = SlurmDriver(tmp_path)
    assert driver.logs("nope") == ""
