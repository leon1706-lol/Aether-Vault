"""python.av_cli.sandbox — pluggable sandbox executor (v1.3.1). One driver protocol
(`base.SandboxDriver`), several backends: `local` (real subprocess isolation,
synchronous), `docker` (real `docker run` isolation, async), and `kubernetes`/`slurm`
(same protocol via `kubectl`/`sbatch`). See `base.py` for the exact contract every
driver must satisfy.
"""
from .base import JobSpec, JobStatus, SandboxDriver, get_driver
from .manifest import ToolManifest, load_manifest, save_manifest, verify_spec_against_manifest

__all__ = ["JobSpec", "JobStatus", "SandboxDriver", "get_driver",
          "ToolManifest", "load_manifest", "save_manifest", "verify_spec_against_manifest"]
