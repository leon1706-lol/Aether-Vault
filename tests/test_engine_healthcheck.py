"""`docker/engine-healthcheck.sh` (V1.6.3): the role-aware, fork-free container probe.
Driven for real with bash against a local HTTP stub when bash is available (Git Bash on
the Windows dev box, /bin/bash in CI); the syntax check runs everywhere bash exists."""
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "docker" / "engine-healthcheck.sh"
BASH = shutil.which("bash")

pytestmark = pytest.mark.skipif(BASH is None, reason="bash not available")


def test_script_is_valid_bash():
    result = subprocess.run([BASH, "-n", str(SCRIPT)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr


class _Stub(BaseHTTPRequestHandler):
    ready = True

    def do_GET(self):
        if self.path == "/api/ready" and self.ready or self.path == "/":
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")
        else:
            self.send_response(503)
            self.end_headers()

    def log_message(self, *args):
        pass


@pytest.fixture
def stub():
    server = HTTPServer(("127.0.0.1", 0), _Stub)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()


def _run(role: str, api_port: int, webui_port: int, env_role_file: str | None = None) -> int:
    import os

    env = {**os.environ, "AV_ENGINE_ROLE": role, "AV_HC_API_PORT": str(api_port),
           "AV_HC_WEBUI_PORT": str(webui_port)}
    result = subprocess.run([BASH, str(SCRIPT)], env=env, capture_output=True, text=True, timeout=60)
    return result.returncode


def test_probes_follow_the_role(stub):
    dead = 1  # nothing listens on port 1
    try:
        assert _run("server", stub, dead) == 0
    except AssertionError:
        # Git Bash on Windows lacks /dev/tcp support in some builds: detect and skip
        # rather than fail on a platform limitation the Linux image doesn't have.
        probe = subprocess.run([BASH, "-c", f'exec 3<>/dev/tcp/127.0.0.1/{stub} && echo yes'],
                               capture_output=True, text=True, timeout=30)
        if "yes" not in probe.stdout:
            pytest.skip("this bash has no /dev/tcp support (the Linux image's bash does)")
        raise
    assert _run("webui", dead, stub) == 0
    assert _run("all", stub, stub) == 0
    assert _run("all", stub, dead) != 0
    assert _run("server", dead, stub) != 0
    assert _run("webui", stub, dead) != 0


def test_unready_api_fails_the_server_probe(stub):
    probe = subprocess.run([BASH, "-c", f'exec 3<>/dev/tcp/127.0.0.1/{stub} && echo yes'],
                           capture_output=True, text=True, timeout=30)
    if "yes" not in probe.stdout:
        pytest.skip("this bash has no /dev/tcp support (the Linux image's bash does)")
    _Stub.ready = False
    try:
        assert _run("server", stub, 1) != 0
    finally:
        _Stub.ready = True
