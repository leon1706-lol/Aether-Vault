"""KubernetesDriver — real `kubectl` isolation, asynchronous (v1.3.1, RSI R5: todo.md G.32).

Same protocol as `docker.py`, addressed by Pod name (`av-sandbox-<job_id>`) instead of a
container name — a Pod is exactly as persistent a handle as a container, for the same
reason `base.py`'s module docstring gives for why this driver doesn't need `local`'s
synchronous workaround. `submit()` builds a minimal Pod manifest (image = first element
of `spec.command`, matching `docker.py`'s convention) and applies it via
`kubectl apply -f -` (stdin) rather than `kubectl run` flags, because mounts (`hostPath`
volumes) and resource requests/limits need more structure than the flag-based form
supports cleanly.

**Verified by contract tests against fixed `kubectl` output, not a live cluster** — this
project has no Kubernetes dependency and doesn't add one; anyone WITH a cluster gets a
working driver, anyone without one gets fully-tested command construction/parsing they
can trust once they do. `--network none`'s Docker-specific guarantee has no direct
Kubernetes equivalent via bare `kubectl` (that needs a NetworkPolicy resource, a
cluster-level concern this driver does not attempt to manage) — `spec.network` is
recorded on the Pod's labels for audit purposes but is NOT enforced by this driver alone;
a cluster operator wanting real network isolation must apply a `NetworkPolicy`
separately. Documented here once, not silently implied otherwise.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from ..base import FAILED, RUNNING, SUCCEEDED, JobSpec, JobStatus

_POD_PREFIX = "av-sandbox-"


def _pod_name(job_id: str) -> str:
    return f"{_POD_PREFIX}{job_id}"


def _build_pod_manifest(spec: JobSpec) -> dict:
    image, *args = spec.command
    container: dict = {"name": "job", "image": image}
    if args:
        container["args"] = args
    resources: dict = {}
    if spec.cpu_limit or spec.memory_limit_mb:
        limits = {}
        if spec.cpu_limit:
            limits["cpu"] = str(spec.cpu_limit)
        if spec.memory_limit_mb:
            limits["memory"] = f"{spec.memory_limit_mb}Mi"
        resources["limits"] = limits
    if spec.gpu:
        resources.setdefault("limits", {})["nvidia.com/gpu"] = "1"
    if resources:
        container["resources"] = resources
    if spec.env:
        container["env"] = [{"name": k, "value": v} for k, v in spec.env.items()]

    volumes, volume_mounts = [], []
    for i, mount in enumerate(spec.mounts):
        vol_name = f"vol-{i}"
        volumes.append({"name": vol_name, "hostPath": {"path": mount.host}})
        volume_mounts.append({"name": vol_name, "mountPath": mount.container,
                              "readOnly": mount.mode == "ro"})
    if volume_mounts:
        container["volumeMounts"] = volume_mounts

    pod: dict = {
        "apiVersion": "v1", "kind": "Pod",
        "metadata": {"name": _pod_name(spec.job_id),
                    "labels": {"app": "av-sandbox", "av-network-policy": spec.network}},
        "spec": {"containers": [container], "restartPolicy": "Never"},
    }
    if volumes:
        pod["spec"]["volumes"] = volumes
    return pod


class KubernetesDriver:
    name = "kubernetes"

    def __init__(self, repo_root: Path):
        self.repo_root = Path(repo_root)

    def submit(self, spec: JobSpec) -> JobStatus:
        manifest = _build_pod_manifest(spec)
        try:
            proc = subprocess.run(
                ["kubectl", "apply", "-f", "-"], input=json.dumps(manifest),
                capture_output=True, text=True, timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return JobStatus(job_id=spec.job_id, state=FAILED, message=f"kubectl apply failed: {exc}")
        if proc.returncode != 0:
            return JobStatus(job_id=spec.job_id, state=FAILED,
                             message=f"kubectl apply failed: {proc.stderr.strip()}")
        return JobStatus(job_id=spec.job_id, state=RUNNING)

    def status(self, job_id: str) -> JobStatus:
        name = _pod_name(job_id)
        try:
            proc = subprocess.run(
                ["kubectl", "get", "pod", name, "-o",
                 "jsonpath={.status.phase} {.status.containerStatuses[0].state.terminated.exitCode}"],
                capture_output=True, text=True, timeout=15,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return JobStatus(job_id=job_id, state=FAILED, message=str(exc))
        if proc.returncode != 0:
            return JobStatus(job_id=job_id, state=FAILED, message="pod not found")
        parts = proc.stdout.strip().split()
        phase = parts[0] if parts else "Unknown"
        exit_code = int(parts[1]) if len(parts) > 1 and parts[1].lstrip("-").isdigit() else None
        if phase in ("Pending", "Running"):
            return JobStatus(job_id=job_id, state=RUNNING)
        if phase == "Succeeded":
            return JobStatus(job_id=job_id, state=SUCCEEDED, exit_code=exit_code if exit_code is not None else 0)
        if phase == "Failed":
            return JobStatus(job_id=job_id, state=FAILED, exit_code=exit_code)
        return JobStatus(job_id=job_id, state=FAILED, message=f"unexpected pod phase: {phase}")

    def cancel(self, job_id: str) -> bool:
        current = self.status(job_id)
        if current.state != RUNNING:
            return False
        try:
            proc = subprocess.run(["kubectl", "delete", "pod", _pod_name(job_id), "--now"],
                                  capture_output=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired):
            return False
        return proc.returncode == 0

    def logs(self, job_id: str) -> str:
        try:
            proc = subprocess.run(["kubectl", "logs", _pod_name(job_id)],
                                  capture_output=True, text=True, timeout=15)
        except (OSError, subprocess.TimeoutExpired):
            return ""
        return proc.stdout + proc.stderr
