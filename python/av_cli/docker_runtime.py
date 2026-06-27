"""Shared Docker/compose orchestration for local-mode backend onboarding.

Factored out of the original `av webui` implementation so the same decision tree (not
running / image missing / built-but-stopped / already healthy) is usable from both `av init`
(first-run and reconnect) and `av webui` (explicit launch), without duplicating the
subprocess logic in two places.
"""

from __future__ import annotations

import subprocess
import time
import urllib.error
import urllib.request
import webbrowser
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from . import ui


class DockerCheckResult(Enum):
    RUNNING = "running"
    NOT_INSTALLED = "not_installed"
    NOT_RUNNING = "not_running"
    TIMEOUT = "timeout"


@dataclass
class DockerOnboardingResult:
    success: bool
    already_running: bool
    backend_url: str | None = None
    message: str | None = None


def check_docker_running() -> DockerCheckResult:
    try:
        result = subprocess.run(["docker", "info"], capture_output=True, timeout=30)
    except FileNotFoundError:
        return DockerCheckResult.NOT_INSTALLED
    except subprocess.TimeoutExpired:
        return DockerCheckResult.TIMEOUT
    return DockerCheckResult.RUNNING if result.returncode == 0 else DockerCheckResult.NOT_RUNNING


def get_container_health(container_name: str) -> str | None:
    try:
        health = subprocess.run(
            ["docker", "inspect", "--format={{.State.Health.Status}}", container_name],
            capture_output=True, text=True, timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if health.returncode != 0:
        return None
    return health.stdout.strip()


def image_exists(image_or_container: str) -> bool:
    """Check whether a compose service's image has already been built/pulled.

    Distinguishes "needs build/pull" from "built but stopped" — `docker images -q` returns
    an empty string when no matching image exists locally.
    """
    try:
        result = subprocess.run(
            ["docker", "images", "-q", image_or_container],
            capture_output=True, text=True, timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0 and bool(result.stdout.strip())


def start_services(
    compose_file: Path, services: list[str], rebuild: bool = False, detached: bool = True
) -> bool:
    if not compose_file.exists():
        ui.print_step(f"docker-compose.yml not found at {compose_file}", status="error")
        return False

    compose_args = ["docker", "compose", "-f", str(compose_file), "up"]
    if detached:
        compose_args.append("-d")
    if rebuild:
        compose_args.append("--build")
    compose_args.extend(services)

    try:
        proc = subprocess.run(compose_args, capture_output=False, timeout=1200)
    except subprocess.TimeoutExpired:
        ui.print_step("Container startup timed out.", status="error")
        return False
    if proc.returncode != 0:
        ui.print_step("Failed to start containers. Check docker compose logs for details.", status="error")
        return False
    return True


def wait_for_http_ready(url: str, attempts: int = 30, interval: float = 2.0) -> bool:
    for attempt in range(attempts):
        time.sleep(interval)
        try:
            urllib.request.urlopen(url, timeout=3)
            return True
        except (urllib.error.URLError, OSError):
            ui.console.print(f"  … waiting ({attempt + 1}/{attempts})")
    return False


def ensure_local_backend_running(
    source_root: Path,
    open_browser: bool,
    rebuild: bool = False,
    container_name: str = "aether-vault-webui",
    service_name: str = "aether-vault-webui",
    url: str = "http://localhost:3000",
) -> DockerOnboardingResult:
    """Top-level orchestrator: not running -> image missing -> start -> wait -> connect."""
    compose_file = source_root / "docker-compose.yml"

    ui.print_step("Checking Docker…", status="info")
    docker_state = check_docker_running()
    if docker_state == DockerCheckResult.NOT_INSTALLED:
        ui.print_step("Docker not found. Install Docker Desktop from https://docker.com", status="error")
        return DockerOnboardingResult(success=False, already_running=False)
    if docker_state == DockerCheckResult.NOT_RUNNING:
        ui.print_step("Docker is not running. Please start Docker Desktop and try again.", status="error")
        return DockerOnboardingResult(success=False, already_running=False)
    if docker_state == DockerCheckResult.TIMEOUT:
        ui.print_step("Docker daemon timed out. Is Docker Desktop running?", status="error")
        return DockerOnboardingResult(success=False, already_running=False)
    ui.print_step("Docker is running", status="success")

    if not rebuild:
        health = get_container_health(container_name)
        if health == "healthy":
            ui.print_step("Backend already running and healthy", status="success")
            if open_browser:
                _open_browser(url)
            return DockerOnboardingResult(success=True, already_running=True, backend_url=url)

    if not image_exists(service_name):
        if not ui.is_interactive():
            ui.print_step(
                f"Docker image for {service_name} not found locally. "
                f"Run `av webui` (or `av webui --rebuild`) once Docker is ready.",
                status="warn",
            )
            return DockerOnboardingResult(success=False, already_running=False)
        import questionary

        build_now = questionary.confirm(
            f"Docker image for {service_name} not found locally — build it now?", default=False
        ).ask()
        if not build_now:
            ui.print_step("Skipped. Run `av webui` later once you're ready.", status="warn")
            return DockerOnboardingResult(success=False, already_running=False)

    ui.print_step("Starting backend…", status="info")
    if not start_services(compose_file, [service_name], rebuild=rebuild):
        return DockerOnboardingResult(success=False, already_running=False)

    ui.print_step(f"Waiting for backend at {url}…", status="info")
    if not wait_for_http_ready(url):
        ui.print_step("Backend did not respond in time.", status="warn")

    if open_browser:
        _open_browser(url)

    return DockerOnboardingResult(success=True, already_running=False, backend_url=url)


def _open_browser(url: str) -> None:
    ui.print_step(f"Opening {url} in your browser…", status="success")
    try:
        webbrowser.open(url)
    except Exception as exc:
        ui.print_step(f"Could not open browser automatically: {exc}", status="warn")
