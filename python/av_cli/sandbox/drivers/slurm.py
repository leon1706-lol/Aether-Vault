"""SlurmDriver — real `sbatch`/`squeue`/`sacct`/`scancel` isolation, asynchronous (v1.3.1).
Addressed by JOB NAME (via `--job-name`), not Slurm's numeric job id, since `scancel -n`/
`sacct --name=` both accept a name filter natively -- avoids a separate id-mapping file.

`submit()` writes a batch script to `.av/sandbox/slurm/<job_id>.sh` and calls `sbatch`.
Status is queried from `squeue` first (still-queued jobs), falling back to `sacct` (its
accounting log) since `squeue` omits jobs that have already finished. Verified by
contract tests against fixed command output, not a live Slurm cluster.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from ..base import FAILED, RUNNING, SUCCEEDED, JobSpec, JobStatus

_JOB_PREFIX = "av-sandbox-"


def _job_name(job_id: str) -> str:
    return f"{_JOB_PREFIX}{job_id}"


class SlurmDriver:
    name = "slurm"

    def __init__(self, repo_root: Path):
        self.repo_root = Path(repo_root)
        self.scripts_dir = self.repo_root / ".av" / "sandbox" / "slurm"
        self.scripts_dir.mkdir(parents=True, exist_ok=True)

    def _script_path(self, job_id: str) -> Path:
        return self.scripts_dir / f"{job_id}.sh"

    def _output_path(self, job_id: str) -> Path:
        return self.scripts_dir / f"{job_id}.out"

    def submit(self, spec: JobSpec) -> JobStatus:
        from ...fsutil import atomic_write_text

        name = _job_name(spec.job_id)
        out_path = self._output_path(spec.job_id)
        lines = ["#!/bin/sh", f"#SBATCH --job-name={name}", f"#SBATCH --output={out_path}"]
        if spec.cpu_limit:
            lines.append(f"#SBATCH --cpus-per-task={int(spec.cpu_limit)}")
        if spec.memory_limit_mb:
            lines.append(f"#SBATCH --mem={spec.memory_limit_mb}M")
        if spec.gpu:
            lines.append("#SBATCH --gres=gpu:1")
        for key, value in spec.env.items():
            lines.append(f"export {key}={value!r}")
        if spec.cwd:
            lines.append(f"cd {spec.cwd}")
        lines.append(" ".join(spec.command))
        script_path = self._script_path(spec.job_id)
        atomic_write_text(script_path, "\n".join(lines) + "\n")

        try:
            proc = subprocess.run(["sbatch", str(script_path)], capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return JobStatus(job_id=spec.job_id, state=FAILED, message=f"sbatch failed: {exc}")
        if proc.returncode != 0:
            return JobStatus(job_id=spec.job_id, state=FAILED, message=f"sbatch failed: {proc.stderr.strip()}")
        return JobStatus(job_id=spec.job_id, state=RUNNING)

    def status(self, job_id: str) -> JobStatus:
        name = _job_name(job_id)
        try:
            queued = subprocess.run(
                ["squeue", "--name", name, "--noheader", "--format=%T"],
                capture_output=True, text=True, timeout=15,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return JobStatus(job_id=job_id, state=FAILED, message=str(exc))
        state_line = queued.stdout.strip()
        if state_line:
            # PENDING, RUNNING, COMPLETING, CONFIGURING, ... — all still "in flight".
            return JobStatus(job_id=job_id, state=RUNNING, message=state_line)

        # Not in the live queue anymore — check accounting for the terminal record.
        try:
            done = subprocess.run(
                ["sacct", "--name", name, "--format=State,ExitCode", "--noheader", "--parsable2"],
                capture_output=True, text=True, timeout=15,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return JobStatus(job_id=job_id, state=FAILED, message=str(exc))
        line = next((l for l in done.stdout.strip().splitlines() if l.strip()), "")
        if not line:
            return JobStatus(job_id=job_id, state=FAILED, message="job not found in queue or accounting")
        raw_state, _, raw_exit = line.partition("|")
        exit_code = int(raw_exit.split(":")[0]) if raw_exit else None
        if raw_state.strip() == "COMPLETED":
            return JobStatus(job_id=job_id, state=SUCCEEDED, exit_code=exit_code or 0)
        return JobStatus(job_id=job_id, state=FAILED, exit_code=exit_code, message=raw_state.strip())

    def cancel(self, job_id: str) -> bool:
        current = self.status(job_id)
        if current.state != RUNNING:
            return False
        try:
            proc = subprocess.run(["scancel", "--name", _job_name(job_id)],
                                  capture_output=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired):
            return False
        return proc.returncode == 0

    def logs(self, job_id: str) -> str:
        out_path = self._output_path(job_id)
        if not out_path.exists():
            return ""
        try:
            return out_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""
