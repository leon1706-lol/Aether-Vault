"""The one sandbox driver protocol every backend implements (v1.3.1, RSI R5).

**Contract every driver satisfies:**
- `submit(spec) -> JobStatus` starts the job and returns its CURRENT status. A driver MAY
  return a terminal status immediately (synchronous execution — see `local`) or a
  `"running"` status for a job that continues after `submit()` returns (asynchronous
  execution — `docker`/`kubernetes`/`slurm`, all of which run against a persistent
  handle — a container name, a pod, a Slurm job id — that survives past this one CLI
  process). Callers must not assume either shape; always check `status.state`.
- `status(job_id) -> JobStatus` re-queries current state. For a driver that already
  returned a terminal status from `submit()`, this just replays the same persisted
  record — it never re-runs anything.
- `cancel(job_id) -> bool` stops a running job. Returns `False` (not an error) when the
  job is already terminal — cancelling something already finished is a no-op, not a
  failure, matching this project's general "an already-satisfied precondition is not an
  error" convention (e.g. `av registry restore`'s idempotent 409s).
- `logs(job_id) -> str` returns captured stdout+stderr (combined, matching how
  `_deliver_one`/every other subprocess call site in this codebase captures output).

**Why `local` is synchronous and the other three are not:** a plain `subprocess.Popen`
has no identity that survives past the Python process that created it except its PID,
and a bare PID is not enough to safely re-attach to "the job I started" days later (PIDs
recycle) without a purpose-built supervisor daemon this project doesn't have and doesn't
need. A container name, a Kubernetes pod, and a Slurm job id are all real, addressable,
persistent handles the backend itself tracks — `docker inspect <name>`,
`kubectl get pod <name>`, and `squeue -j <id>` are exactly the point of those systems.
Rather than fake a uniform async model for `local` with a half-working PID-liveness
guess, it runs to completion inside `submit()` and reports a real terminal result —
`status()`/`logs()` on a local job id just replay that persisted record, `cancel()`
reports `False` (nothing to cancel; it already finished).

**Tool permission manifests (todo.md G.30, `manifest.py`):** every `submit()` call is
handed the resolved `JobSpec` AFTER `verify_spec_against_manifest()` has already checked
it — a driver never has to re-implement policy parsing, only ENFORCE the mounts/network
mode it was given. What enforcement actually MEANS differs by driver capability, and each
driver's own docstring says exactly what it can and cannot guarantee (`docker` can
genuinely block network access; `local` cannot without extra OS tooling this project
doesn't depend on — see `drivers/local.py`).
"""
from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Protocol, runtime_checkable

# Job/task states — a small, closed vocabulary every driver maps onto.
PENDING = "pending"
RUNNING = "running"
SUCCEEDED = "succeeded"
FAILED = "failed"
CANCELLED = "cancelled"
TERMINAL_STATES = frozenset({SUCCEEDED, FAILED, CANCELLED})


@dataclasses.dataclass
class Mount:
    host: str
    container: str
    mode: str = "ro"  # "ro" | "rw"


@dataclasses.dataclass
class JobSpec:
    """What to run and under what constraints. `job_id` is caller-assigned (typically a
    change-set id or a fresh uuid) so the caller controls the addressable name, not the
    driver — this is what lets `av improver apply CS_ID` use the change-set's own id as
    the sandbox job id, with no separate id-mapping table to keep in sync."""
    job_id: str
    command: list[str]
    cwd: Path | None = None
    env: dict[str, str] = dataclasses.field(default_factory=dict)
    mounts: list[Mount] = dataclasses.field(default_factory=list)
    network: str = "none"  # "none" | "bridge" — see each driver for what this can enforce
    cpu_limit: float | None = None       # cores
    memory_limit_mb: int | None = None
    gpu: bool = False
    timeout_secs: int = 3600


@dataclasses.dataclass
class JobStatus:
    job_id: str
    state: str
    exit_code: int | None = None
    message: str | None = None


@runtime_checkable
class SandboxDriver(Protocol):
    name: str

    def submit(self, spec: JobSpec) -> JobStatus: ...
    def status(self, job_id: str) -> JobStatus: ...
    def cancel(self, job_id: str) -> bool: ...
    def logs(self, job_id: str) -> str: ...


_DRIVERS = ("local", "docker", "kubernetes", "slurm")


def get_driver(name: str, repo_root: Path) -> SandboxDriver:
    """Resolves a driver by name — the one place that knows the import path for each,
    so `cmd_sandbox.py` (and tests) never hardcode `drivers.<name>.Driver` themselves."""
    if name not in _DRIVERS:
        raise ValueError(f"Unknown sandbox driver: {name!r} (expected one of {_DRIVERS})")
    if name == "local":
        from .drivers.local import LocalDriver
        return LocalDriver(repo_root)
    if name == "docker":
        from .drivers.docker import DockerDriver
        return DockerDriver(repo_root)
    if name == "kubernetes":
        from .drivers.kubernetes import KubernetesDriver
        return KubernetesDriver(repo_root)
    from .drivers.slurm import SlurmDriver
    return SlurmDriver(repo_root)
