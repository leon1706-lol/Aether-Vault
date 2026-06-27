from pathlib import Path

import pytest

from python.av_cli import docker_runtime


class _FakeCompleted:
    def __init__(self, returncode=0, stdout=""):
        self.returncode = returncode
        self.stdout = stdout


def test_check_docker_running_ok(monkeypatch):
    monkeypatch.setattr(
        docker_runtime.subprocess, "run", lambda *a, **k: _FakeCompleted(returncode=0)
    )
    assert docker_runtime.check_docker_running() == docker_runtime.DockerCheckResult.RUNNING


def test_check_docker_running_not_installed(monkeypatch):
    def fake_run(*a, **k):
        raise FileNotFoundError()

    monkeypatch.setattr(docker_runtime.subprocess, "run", fake_run)
    assert docker_runtime.check_docker_running() == docker_runtime.DockerCheckResult.NOT_INSTALLED


def test_check_docker_running_not_running(monkeypatch):
    monkeypatch.setattr(
        docker_runtime.subprocess, "run", lambda *a, **k: _FakeCompleted(returncode=1)
    )
    assert docker_runtime.check_docker_running() == docker_runtime.DockerCheckResult.NOT_RUNNING


def test_check_docker_running_timeout(monkeypatch):
    import subprocess as real_subprocess

    def fake_run(*a, **k):
        raise real_subprocess.TimeoutExpired(cmd="docker", timeout=30)

    monkeypatch.setattr(docker_runtime.subprocess, "run", fake_run)
    assert docker_runtime.check_docker_running() == docker_runtime.DockerCheckResult.TIMEOUT


def test_get_container_health_healthy(monkeypatch):
    monkeypatch.setattr(
        docker_runtime.subprocess, "run",
        lambda *a, **k: _FakeCompleted(returncode=0, stdout="healthy\n"),
    )
    assert docker_runtime.get_container_health("aether-vault-webui") == "healthy"


def test_get_container_health_missing_container(monkeypatch):
    monkeypatch.setattr(
        docker_runtime.subprocess, "run", lambda *a, **k: _FakeCompleted(returncode=1)
    )
    assert docker_runtime.get_container_health("aether-vault-webui") is None


def test_image_exists_true(monkeypatch):
    monkeypatch.setattr(
        docker_runtime.subprocess, "run",
        lambda *a, **k: _FakeCompleted(returncode=0, stdout="abc123\n"),
    )
    assert docker_runtime.image_exists("aether-vault-webui") is True


def test_image_exists_false(monkeypatch):
    monkeypatch.setattr(
        docker_runtime.subprocess, "run",
        lambda *a, **k: _FakeCompleted(returncode=0, stdout=""),
    )
    assert docker_runtime.image_exists("aether-vault-webui") is False


def test_ensure_local_backend_running_docker_not_running(monkeypatch, tmp_path):
    monkeypatch.setattr(
        docker_runtime, "check_docker_running",
        lambda: docker_runtime.DockerCheckResult.NOT_RUNNING,
    )
    result = docker_runtime.ensure_local_backend_running(tmp_path, open_browser=False)
    assert result.success is False
    assert result.already_running is False


def test_ensure_local_backend_running_already_healthy(monkeypatch, tmp_path):
    monkeypatch.setattr(
        docker_runtime, "check_docker_running", lambda: docker_runtime.DockerCheckResult.RUNNING
    )
    monkeypatch.setattr(docker_runtime, "get_container_health", lambda name: "healthy")
    result = docker_runtime.ensure_local_backend_running(tmp_path, open_browser=False)
    assert result.success is True
    assert result.already_running is True
