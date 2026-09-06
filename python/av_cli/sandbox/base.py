"""The one sandbox driver protocol every backend implements (v1.3.1).

**Contract every driver satisfies:**
- `submit(spec) -> JobStatus` starts the job and returns its CURRENT status -- may be
  terminal immediately (`local`, synchronous) or `"running"` for a job that continues
  after `submit()` returns (`docker`/`kubernetes`/`slurm`, which track a persistent
  handle). Callers must not assume either shape; always check `status.state`.
- `status(job_id) -> JobStatus` re-queries current state; for an already-terminal
  driver this just replays the persisted record.
- `cancel(job_id) -> bool` stops a running job; returns `False` (not an error) when
  already terminal.
- `logs(job_id) -> str` returns captured stdout+stderr, combined.

`local` is synchronous because a bare PID isn't a safe handle to re-attach to days
later; the other three track a real, addressable, persistent handle (container name,
pod, Slurm job id) their own backend provides.

Every `submit()` call is handed the resolved `JobSpec` AFTER `verify_spec_against_manifest()`
(`manifest.py`) has already checked it -- a driver only ENFORCES the mounts/network mode
it was given, never re-parses policy. What "enforced" means differs by driver capability;
see each driver's own docstring.
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
    """What to run and under what constraints. `job_id` is caller-assigned so the caller
    controls the addressable name, not the driver -- lets `av improver apply CS_ID` reuse
    the change-set's own id with no separate mapping table."""
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
    """Resolves a driver by name -- the one place that knows the import path for each."""
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
