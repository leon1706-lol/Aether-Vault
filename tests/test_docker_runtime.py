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


# ---------------------------------------------------------------------------
# resolve_compose_file / pull_latest_image / check_for_docker_update
# ---------------------------------------------------------------------------

def test_resolve_compose_file_dev_checkout(tmp_path):
    (tmp_path / "docker-compose.yml").write_text("services: {}\n")
    path, is_dev = docker_runtime.resolve_compose_file(tmp_path)
    assert is_dev is True
    assert path == tmp_path / "docker-compose.yml"


def test_resolve_compose_file_falls_back_to_release_compose(tmp_path):
    # No docker-compose.yml under tmp_path -> falls back to the bundled release compose file
    # shipped as package data, mirroring a real `pip install aether-vault` end user.
    path, is_dev = docker_runtime.resolve_compose_file(tmp_path)
    assert is_dev is False
    assert path.name == "docker-compose.release.yml"
    assert path.exists()


def test_pull_latest_image_first_pull_no_old_image_to_clean_up(monkeypatch, tmp_path):
    ids = iter(["", "newid123"])  # before (no local image at all) -> after (freshly pulled)
    monkeypatch.setattr(docker_runtime, "_image_id", lambda image: next(ids))
    monkeypatch.setattr(docker_runtime.subprocess, "run", lambda *a, **k: _FakeCompleted(returncode=0))
    changed, old_id = docker_runtime.pull_latest_image(tmp_path / "compose.yml", "svc", "ghcr.io/x/svc:latest")
    assert changed is True
    assert old_id is None  # nothing existed before, so nothing to clean up


def test_pull_latest_image_update_reports_old_id_for_cleanup(monkeypatch, tmp_path):
    ids = iter(["oldid456", "newid123"])  # before (existing image) -> after (newer image pulled)
    monkeypatch.setattr(docker_runtime, "_image_id", lambda image: next(ids))
    monkeypatch.setattr(docker_runtime.subprocess, "run", lambda *a, **k: _FakeCompleted(returncode=0))
    changed, old_id = docker_runtime.pull_latest_image(tmp_path / "compose.yml", "svc", "ghcr.io/x/svc:latest")
    assert changed is True
    assert old_id == "oldid456"


def test_pull_latest_image_no_change(monkeypatch, tmp_path):
    monkeypatch.setattr(docker_runtime, "_image_id", lambda image: "sameid")
    monkeypatch.setattr(docker_runtime.subprocess, "run", lambda *a, **k: _FakeCompleted(returncode=0))
    changed, old_id = docker_runtime.pull_latest_image(tmp_path / "compose.yml", "svc", "ghcr.io/x/svc:latest")
    assert changed is False
    assert old_id is None


def test_remove_old_images_skips_empty_ids(monkeypatch):
    calls = []
    monkeypatch.setattr(
        docker_runtime.subprocess, "run", lambda args, **k: calls.append(args) or _FakeCompleted(returncode=0)
    )
    docker_runtime.remove_old_images(["", None, "realid123"])
    assert len(calls) == 1
    assert calls[0] == ["docker", "rmi", "realid123"]


def test_check_for_docker_update_dev_checkout_noops(tmp_path):
    (tmp_path / "docker-compose.yml").write_text("services: {}\n")
    result = docker_runtime.check_for_docker_update(tmp_path)
    assert result.checked is False
    assert "source checkout" in result.message


def test_check_for_docker_update_release_path_no_change(monkeypatch, tmp_path):
    monkeypatch.setattr(docker_runtime, "check_docker_running", lambda: docker_runtime.DockerCheckResult.RUNNING)
    monkeypatch.setattr(docker_runtime, "pull_latest_image", lambda compose_file, service, image: (False, None))
    result = docker_runtime.check_for_docker_update(tmp_path)
    assert result.checked is True
    assert result.updated is False
    assert result.old_image_ids == []


def test_check_for_docker_update_fails_fast_when_docker_not_running(monkeypatch, tmp_path):
    # Regression test: must not attempt `docker compose pull` (which can hang for minutes) when
    # Docker isn't even running — found via manual debugging.
    monkeypatch.setattr(docker_runtime, "check_docker_running", lambda: docker_runtime.DockerCheckResult.NOT_RUNNING)
    pull_calls = []
    monkeypatch.setattr(
        docker_runtime, "pull_latest_image",
        lambda compose_file, service, image: (pull_calls.append(service), (False, None))[1],
    )
    result = docker_runtime.check_for_docker_update(tmp_path)
    assert result.checked is False
    assert "not running" in result.message.lower()
    assert pull_calls == []


def test_check_for_docker_update_release_path_with_change(monkeypatch, tmp_path):
    monkeypatch.setattr(docker_runtime, "check_docker_running", lambda: docker_runtime.DockerCheckResult.RUNNING)
    monkeypatch.setattr(
        docker_runtime, "pull_latest_image", lambda compose_file, service, image: (True, f"old-{service}")
    )
    result = docker_runtime.check_for_docker_update(tmp_path)
    assert result.checked is True
    assert result.updated is True
    assert set(result.old_image_ids) == {f"old-{s}" for s in docker_runtime.RELEASE_IMAGES}
