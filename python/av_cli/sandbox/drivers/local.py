"""LocalDriver — real subprocess isolation, synchronous (v1.3.1, RSI R5).

Runs `spec.command` as a real subprocess, scoped to `spec.cwd` and `spec.env` (merged
over, not replacing, the current process's environment — matches every other subprocess
call site in this codebase, e.g. `cmd_eval.py::adapter_run`). `submit()` BLOCKS until the
process exits or `spec.timeout_secs` elapses, then persists the terminal result to
`.av/sandbox/jobs/<job_id>.json` (state + exit code + combined stdout/stderr) — see
`base.py`'s module docstring for why this driver is synchronous while the others aren't.

**Enforcement limits, stated plainly:** this driver enforces `writable_paths` at the
MOUNT level (a `Mount` outside the manifest's globs is validated away by
`manifest.verify_spec_against_manifest()` before `submit()` is ever called — see
`cmd_sandbox.py`), but it CANNOT stop the launched process from writing anywhere else the
OS user account can reach, and it CANNOT enforce `network: "none"` at all — a local
subprocess shares this machine's real network stack. Use the `docker` driver when network
isolation actually matters; `local` is for trusted, low-risk jobs (a lint check, a unit
test run) where the isolation that matters is "not polluting the working tree with an
unreviewed change," not "cannot reach the network."
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from ..base import FAILED, SUCCEEDED, JobSpec, JobStatus


class LocalDriver:
    name = "local"

    def __init__(self, repo_root: Path):
        self.repo_root = Path(repo_root)
        self.jobs_dir = self.repo_root / ".av" / "sandbox" / "jobs"
        self.jobs_dir.mkdir(parents=True, exist_ok=True)

    def _job_path(self, job_id: str) -> Path:
        return self.jobs_dir / f"{job_id}.json"

    def _load(self, job_id: str) -> dict | None:
        path = self._job_path(job_id)
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def _save(self, job_id: str, record: dict) -> None:
        from ...fsutil import atomic_write_text

        atomic_write_text(self._job_path(job_id), json.dumps(record, indent=2))

    def submit(self, spec: JobSpec) -> JobStatus:
        env = {**os.environ, **spec.env}
        cwd = str(spec.cwd) if spec.cwd else str(self.repo_root)
        try:
            proc = subprocess.run(
                spec.command, cwd=cwd, env=env, capture_output=True, text=True,
                timeout=spec.timeout_secs,
            )
            state = SUCCEEDED if proc.returncode == 0 else FAILED
            record = {"job_id": spec.job_id, "state": state, "exit_code": proc.returncode,
                      "output": proc.stdout + proc.stderr}
        except subprocess.TimeoutExpired as exc:
            record = {"job_id": spec.job_id, "state": FAILED, "exit_code": None,
                      "output": (exc.stdout or "") + (exc.stderr or "") + "\n[timed out]"}
        except OSError as exc:
            record = {"job_id": spec.job_id, "state": FAILED, "exit_code": None,
                      "output": f"[failed to launch: {exc}]"}
        self._save(spec.job_id, record)
        return JobStatus(job_id=spec.job_id, state=record["state"],
                         exit_code=record["exit_code"])

    def status(self, job_id: str) -> JobStatus:
        record = self._load(job_id)
        if record is None:
            return JobStatus(job_id=job_id, state=FAILED, message="unknown local job")
        return JobStatus(job_id=job_id, state=record["state"], exit_code=record.get("exit_code"))

    def cancel(self, job_id: str) -> bool:
        # submit() already ran to completion by the time any cancel() could be called —
        # there is nothing in-flight to stop. Honest False, not an error (see base.py).
        return False

    def logs(self, job_id: str) -> str:
        record = self._load(job_id)
        return record.get("output", "") if record else ""
