"""DockerDriver — real `docker run` isolation, asynchronous (v1.3.1, RSI R5).

`submit()` launches a DETACHED container named `av-sandbox-<job_id>` and returns
immediately with state `"running"` — the container itself is the persistent handle
`status()`/`cancel()`/`logs()` re-attach to in a LATER, separate CLI invocation (this is
exactly why Docker doesn't need the synchronous workaround `LocalDriver` uses — see
`base.py`'s module docstring).

**What is actually enforced, stated plainly:** `--network none` (the default; `spec.network
== "bridge"` switches to the default bridge network) is a REAL, OS-level guarantee —
Docker's network namespace isolation, not a convention. Mounts are REAL: only the host
paths in `spec.mounts` are ever visible inside the container, `ro` mounted read-only via
Docker's own `:ro` flag. `--cpus`/`--memory` are real Docker resource limits (Linux
cgroups). `--gpus all` is passed only when `spec.gpu` is set (requires the NVIDIA
container runtime on the host; this driver does not attempt to detect or install it —
that is a host prerequisite, same as Docker itself). `network_destinations` in a tool
manifest is NOT enforced per-destination here — see `manifest.py`'s own docstring for why
that would need an additional sidecar proxy this project doesn't depend on.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from ..base import FAILED, RUNNING, SUCCEEDED, JobSpec, JobStatus

_CONTAINER_PREFIX = "av-sandbox-"


class DockerDriver:
    name = "docker"

    def __init__(self, repo_root: Path):
        self.repo_root = Path(repo_root)

    def _container_name(self, job_id: str) -> str:
        return f"{_CONTAINER_PREFIX}{job_id}"

    def submit(self, spec: JobSpec) -> JobStatus:
        name = self._container_name(spec.job_id)
        args = ["docker", "run", "-d", "--name", name,
               "--network", "none" if spec.network != "bridge" else "bridge"]
        if spec.cpu_limit:
            args += ["--cpus", str(spec.cpu_limit)]
        if spec.memory_limit_mb:
            args += ["--memory", f"{spec.memory_limit_mb}m"]
        if spec.gpu:
            args += ["--gpus", "all"]
        for mount in spec.mounts:
            args += ["-v", f"{mount.host}:{mount.container}:{mount.mode}"]
        for key, value in spec.env.items():
            args += ["-e", f"{key}={value}"]
        if spec.cwd:
            args += ["-w", str(spec.cwd)]
        # A bare image name isn't meaningful here — the caller's command IS the workload,
        # so the image is always the first element of `command` by convention (same shape
        # `docker run <image> <cmd...>` always takes); callers build `spec.command` as
        # `[image, *argv]`.
        args += spec.command

        try:
            proc = subprocess.run(args, capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return JobStatus(job_id=spec.job_id, state=FAILED, message=f"docker run failed: {exc}")
        if proc.returncode != 0:
            return JobStatus(job_id=spec.job_id, state=FAILED,
                             message=f"docker run failed: {proc.stderr.strip()}")
        return JobStatus(job_id=spec.job_id, state=RUNNING)

    def status(self, job_id: str) -> JobStatus:
        name = self._container_name(job_id)
        try:
            proc = subprocess.run(
                ["docker", "inspect", "--format",
                 "{{.State.Status}} {{.State.ExitCode}}", name],
                capture_output=True, text=True, timeout=15,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return JobStatus(job_id=job_id, state=FAILED, message=str(exc))
        if proc.returncode != 0:
            return JobStatus(job_id=job_id, state=FAILED, message="container not found")
        parts = proc.stdout.strip().split()
        docker_state = parts[0] if parts else "unknown"
        exit_code = int(parts[1]) if len(parts) > 1 else None
        if docker_state == "running":
            return JobStatus(job_id=job_id, state=RUNNING)
        if docker_state == "exited":
            state = SUCCEEDED if exit_code == 0 else FAILED
            return JobStatus(job_id=job_id, state=state, exit_code=exit_code)
        return JobStatus(job_id=job_id, state=FAILED, message=f"unexpected container state: {docker_state}")

    def cancel(self, job_id: str) -> bool:
        current = self.status(job_id)
        if current.state != RUNNING:
            return False
        name = self._container_name(job_id)
        try:
            proc = subprocess.run(["docker", "stop", name], capture_output=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired):
            return False
        return proc.returncode == 0

    def logs(self, job_id: str) -> str:
        name = self._container_name(job_id)
        try:
            proc = subprocess.run(["docker", "logs", name], capture_output=True, text=True, timeout=15)
        except (OSError, subprocess.TimeoutExpired):
            return ""
        return proc.stdout + proc.stderr
