"""python.av_cli.sandbox — pluggable sandbox executor (v1.3.1, RSI R5: todo.md G.29).

One driver protocol (`base.SandboxDriver`), several backends:
- `drivers.local`  — real subprocess isolation (cwd/env scoping), synchronous.
- `drivers.docker` — real `docker run` isolation (`--network none` by default, explicit
  mounts, cpu/memory caps), asynchronous (a container persists across CLI invocations).
- `drivers.kubernetes` / `drivers.slurm` — same protocol via `kubectl`/`sbatch` subprocess
  calls, proven by contract tests against recorded fixtures (no live cluster needed),
  same shape as `tests/test_docker_runtime.py`'s fake-subprocess approach.

See `base.py`'s module docstring for the exact contract every driver must satisfy, and
`development/architecture.md`'s Sandbox Execution Contract section for the design
rationale (especially why `local` is synchronous while the other three are async).
"""
from .base import JobSpec, JobStatus, SandboxDriver, get_driver
from .manifest import ToolManifest, load_manifest, save_manifest, verify_spec_against_manifest

__all__ = ["JobSpec", "JobStatus", "SandboxDriver", "get_driver",
          "ToolManifest", "load_manifest", "save_manifest", "verify_spec_against_manifest"]
