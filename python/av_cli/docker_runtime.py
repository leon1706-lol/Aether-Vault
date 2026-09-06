"""Shared Docker/compose orchestration for local-mode backend onboarding.

Factored out of the original `av webui` implementation so the same decision tree (not
running / image missing / built-but-stopped / already healthy) is usable from both `av init`
(first-run and reconnect) and `av webui` (explicit launch), without duplicating the
subprocess logic in two places.
"""

from __future__ import annotations

import importlib.resources
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from . import ui

# Images published by .github/workflows/release.yml and docker-edge.yml -- keep these
# names in sync with both workflows and docker/docker-compose.release.yml. One image runs
# ALL subservices (registry + webui) dispatched by AV_ENGINE_ROLE (engine-entrypoint.sh).
RELEASE_IMAGE = "ghcr.io/leon1706-lol/aether-vault-engine:latest"
RELEASE_IMAGES = {
    "aether-vault-engine": RELEASE_IMAGE,
}


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


@dataclass
class DockerUpdateResult:
    checked: bool  # False when running from a dev/source checkout (nothing to pull against)
    updated: bool = False
    message: str | None = None
    old_image_ids: list[str] = None  # populated only when updated=True; for post-restart cleanup

    def __post_init__(self):
        if self.old_image_ids is None:
            self.old_image_ids = []


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


def resolve_compose_file(source_root: Path) -> tuple[Path, bool]:
    """Picks which compose file to run against.

    Returns (path, is_dev_checkout). A real source checkout (editable/dev install) has its own
    docker-compose.yml (build:-based) and keeps using it unchanged. A real `pip install
    aether-vault` end user has no such file on disk at all — `source_root` resolves to a
    meaningless path inside site-packages — so this falls back to the image-based
    docker-compose.release.yml bundled inside the wheel as package data.
    """
    dev_compose_file = source_root / "docker-compose.yml"
    if dev_compose_file.exists():
        return dev_compose_file, True

    release_compose = importlib.resources.files("av_cli.docker").joinpath("docker-compose.release.yml")
    return Path(str(release_compose)), False


def pull_latest_image(compose_file: Path, service: str, image: str) -> tuple[bool, str | None]:
    """Pulls the given service's image. Returns (changed, old_image_id). Compares `docker
    images -q <image>` before and after the pull rather than parsing compose's text
    output; `old_image_id` lets the caller remove the dangling old image surgically,
    never a blanket prune."""
    before = _image_id(image)
    try:
        subprocess.run(
            ["docker", "compose", "-f", str(compose_file), "pull", service],
            capture_output=False, timeout=600,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False, None
    after = _image_id(image)
    if before and before != after:
        return True, before
    return before != after, None


def _image_id(image: str) -> str:
    try:
        result = subprocess.run(
            ["docker", "images", "-q", image], capture_output=True, text=True, timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def restart_service(compose_file: Path, service: str) -> bool:
    """Recreates the container against whatever image is now cached locally for it.
    `docker compose up -d` doubles as "start" when called against a stopped stack.
    Returns False on any failure -- callers should report a clear message rather than
    assume the new config is live."""
    try:
        proc = subprocess.run(
            ["docker", "compose", "-f", str(compose_file), "up", "-d", service],
            capture_output=False, timeout=300,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0


def _quote_env_value(value: str) -> str:
    """Wraps a .env value in double quotes, escaping embedded backslashes/quotes. A
    user-supplied token could contain anything (`#`, embedded quotes) -- always quoting
    avoids having to detect every character that would require it."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _unquote_env_value(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] == '"':
        return value[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    return value


def read_env_token(compose_file: Path, key: str = "AV_API_TOKEN") -> str | None:
    """Reads a key's current value from the .env file next to `compose_file`, if any."""
    env_path = compose_file.parent / ".env"
    if not env_path.exists():
        return None
    for line in env_path.read_text(encoding="utf-8").splitlines():
        if line.strip().startswith(f"{key}="):
            value = _unquote_env_value(line.split("=", 1)[1])
            return value or None
    return None


def write_env_token(compose_file: Path, token: str | None, key: str = "AV_API_TOKEN") -> None:
    """Read-modify-write the .env file next to `compose_file`, setting/removing `key`.
    Preserves every other line already in the file. An empty/None token removes the line
    entirely rather than writing `KEY=""`, so unset stays the single source of truth for
    "Anonymous mode."""
    env_path = compose_file.parent / ".env"
    lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []

    new_lines = [line for line in lines if not line.strip().startswith(f"{key}=")]
    if token:
        new_lines.append(f"{key}={_quote_env_value(token)}")

    env_path.write_text("\n".join(new_lines) + ("\n" if new_lines else ""), encoding="utf-8")


def remove_old_images(image_ids: list[str]) -> None:
    """Removes specific, exact image IDs left dangling after an update -- never a blanket
    prune. Called only after the new container is confirmed running; failures are
    silently ignored, this is best-effort cleanup."""
    for image_id in image_ids:
        if not image_id:
            continue
        subprocess.run(["docker", "rmi", image_id], capture_output=True, timeout=30)


def check_for_docker_update(source_root: Path) -> DockerUpdateResult:
    """Pulls the latest published images and reports whether anything actually changed.
    Only does real work against the release (image-based) compose file -- a dev/source
    checkout's `build:`-based compose file has nothing meaningful to pull."""
    compose_file, is_dev_checkout = resolve_compose_file(source_root)
    if is_dev_checkout:
        return DockerUpdateResult(
            checked=False,
            message="Running from a source checkout — use `git pull` + `av webui --rebuild` instead.",
        )

    # Fail fast and clearly instead of letting `docker compose pull` hang against a daemon
    # that isn't even running.
    docker_state = check_docker_running()
    if docker_state != DockerCheckResult.RUNNING:
        return DockerUpdateResult(
            checked=False, message="Docker is not running. Please start Docker Desktop and try again.",
        )

    old_image_ids = []
    for service, image in RELEASE_IMAGES.items():
        changed, old_id = pull_latest_image(compose_file, service, image)
        if changed and old_id:
            old_image_ids.append(old_id)

    if not old_image_ids:
        return DockerUpdateResult(checked=True, updated=False, message="Docker backend is already up to date.")
    return DockerUpdateResult(
        checked=True, updated=True, message="A newer Docker image was pulled.", old_image_ids=old_image_ids,
    )


def ensure_local_backend_running(
    source_root: Path,
    open_browser: bool,
    rebuild: bool = False,
    container_name: str = "aether-vault-engine",
    service_name: str = "aether-vault-engine",
    url: str = "http://localhost:3000",
    api_token: str | None = None,
) -> DockerOnboardingResult:
    """Top-level orchestrator: not running -> image missing -> start -> wait -> connect.
    The engine is ONE container running both the registry and the webui, so waiting for
    the webui URL proves the whole engine came up. `api_token`, when already configured,
    is passed through to `_open_browser` so the webui skips its manual token-entry prompt."""
    compose_file, _ = resolve_compose_file(source_root)

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
                _open_browser(url, api_token)
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
        _open_browser(url, api_token)

    return DockerOnboardingResult(success=True, already_running=False, backend_url=url)


def _open_browser(url: str, token: str | None = None) -> None:
    """Opens the webui. When `token` is given, appends it as a one-time `av_token` query
    param so TokenGate.tsx can save it to localStorage and strip it from the URL on load."""
    full_url = url
    if token:
        full_url = f"{url}{'&' if '?' in url else '?'}av_token={urllib.parse.quote(token)}"
    ui.print_step(f"Opening {url} in your browser…", status="success")
    try:
        webbrowser.open(full_url)
    except Exception as exc:
        ui.print_step(f"Could not open browser automatically: {exc}", status="warn")
