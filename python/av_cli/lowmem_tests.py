"""Run the pytest suite one file (or one chunk of a file) per subprocess, with a free-RAM
floor between units -- what makes the full suite runnable on a memory-constrained box
where a single `pytest tests/` process gets OOM-killed (V1.6.3; the owner's dev box has
~3.9 GB with Docker's `vmmem` taking ~500 MB of it).

Used by `scripts/run_tests_lowmem.py` and `av test --lowmem`. Each unit is a fresh
interpreter: `-p no:cacheprovider` (no `.pytest_cache` churn), `--no-cov`, `-o addopts=`
(pyproject's addopts never apply), `-q`. Peak RSS per unit is measured through
`av_cli.sysres.run_measured` so the runner's own report doubles as evidence.

State (`state.json`, default under the OS temp dir) records every unit that passed, so a
killed run resumes where it stopped with `resume=True` -- the default.
"""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

#: Files known to need chunking on a small box: {relative path: node ids per subprocess}.
_DEFAULT_CHUNKS = {
    "tests/test_server.py": 40,
    "tests/test_daemon.py": 60,
}

@dataclass
class FileResult:
    path: str
    status: str  # passed | failed | error | skipped-all | timeout
    passed: int = 0
    failed: int = 0
    skipped: int = 0
    errors: int = 0
    peak_rss_mb: float | None = None
    elapsed_s: float = 0.0
    chunks: int = 1
    summary: str = ""


@dataclass
class LowmemSummary:
    passed: int = 0
    failed: int = 0
    skipped: int = 0
    errors: int = 0
    files: list[FileResult] = field(default_factory=list)
    resumed: int = 0

    @property
    def ok(self) -> bool:
        return self.failed == 0 and self.errors == 0 and not any(f.status == "timeout" for f in self.files)

    def pytest_style_line(self) -> str:
        parts = [f"{self.passed} passed"]
        if self.failed:
            parts.append(f"{self.failed} failed")
        if self.skipped:
            parts.append(f"{self.skipped} skipped")
        if self.errors:
            parts.append(f"{self.errors} error")
        return ", ".join(parts)


def default_state_path() -> Path:
    return Path(tempfile.gettempdir()) / "av-lowmem-tests" / "state.json"


def parse_counts(output: str) -> tuple[int, int, int, int]:
    """(passed, failed, skipped, errors) from pytest's final summary line (colour-free)."""
    text = re.sub(r"\x1b\[[0-9;]*m", "", output)
    passed = failed = skipped = errors = 0
    for line in reversed(text.strip().splitlines()):
        if " in " in line and any(k in line for k in ("passed", "failed", "skipped", "error", "no tests ran")):
            for count, key in re.findall(r"(\d+) (passed|failed|skipped|errors?|deselected|warnings?|xfailed|xpassed)", line):
                if key == "passed":
                    passed = int(count)
                elif key == "failed":
                    failed = int(count)
                elif key == "skipped":
                    skipped = int(count)
                elif key.startswith("error"):
                    errors = int(count)
            break
    return passed, failed, skipped, errors


def _wait_for_free_ram(min_free_mb: int, echo, max_wait_s: float = 120.0) -> None:
    from .sysres import available_mb

    waited = 0.0
    while waited < max_wait_s:
        free = available_mb()
        if free is None or free >= min_free_mb:
            return
        if waited == 0.0:
            echo(f"  waiting for free RAM: {free:.0f} MB < {min_free_mb} MB floor ...")
        time.sleep(2.0)
        waited += 2.0
    echo(f"  free RAM still below {min_free_mb} MB after {max_wait_s:.0f}s -- running anyway")


def _collect_node_ids(test_file: Path, source_root: Path, k_expr: str | None, timeout: float) -> list[str]:
    import subprocess

    args = [sys.executable, "-m", "pytest", str(test_file), "--collect-only", "-q", "-p", "no:cacheprovider",
            "--no-cov", "-o", "addopts="]
    if k_expr:
        args += ["-k", k_expr]
    result = subprocess.run(args, cwd=str(source_root), capture_output=True, text=True, encoding="utf-8",
                            errors="replace", timeout=timeout)
    ids = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if "::" in line and not line.startswith(("=", "<", "warnings", "-")):
            ids.append(line)
    return ids


def _load_state(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"passed_units": []}


def _save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def run_lowmem(
    tests_dir: Path,
    *,
    min_free_mb: int = 350,
    chunk_size: int = 0,
    state_path: Path | None = None,
    resume: bool = True,
    k_expr: str | None = None,
    files: list[str] | None = None,
    per_file_timeout: float = 900.0,
    extra_args: tuple[str, ...] = (),
    echo=print,
) -> LowmemSummary:
    """One pytest subprocess per test file (or per `chunk_size` node ids for the files in
    `_DEFAULT_CHUNKS`, or every file when `chunk_size > 0`). `files` restricts the run
    to those paths (relative to `tests_dir`'s parent or absolute). Never deadlocks on the
    RAM floor: after 120 s it runs the unit anyway and says so."""
    from .sysres import run_measured

    tests_dir = Path(tests_dir)
    source_root = tests_dir.parent
    state_path = Path(state_path) if state_path else default_state_path()
    state = _load_state(state_path) if resume else {"passed_units": []}
    passed_units = set(state.get("passed_units", []))

    if files:
        selected = []
        for f in files:
            p = Path(f)
            selected.append(p if p.is_absolute() else source_root / p)
    else:
        selected = sorted(tests_dir.glob("test_*.py"))

    summary = LowmemSummary()
    base = ["-q", "-p", "no:cacheprovider", "--no-cov", "-o", "addopts=", *extra_args]
    if k_expr:
        base += ["-k", k_expr]

    for test_file in selected:
        try:
            rel = test_file.resolve().relative_to(source_root.resolve()).as_posix()
        except ValueError:
            rel = test_file.as_posix()
        per_file_chunk = chunk_size or _DEFAULT_CHUNKS.get(rel, 0)
        units: list[tuple[str, list[str]]]
        if per_file_chunk > 0:
            ids = _collect_node_ids(test_file, source_root, k_expr, per_file_timeout)
            if not ids:
                units = [(rel, [str(test_file)])]
            else:
                units = [(f"{rel}#{i // per_file_chunk}", ids[i:i + per_file_chunk])
                         for i in range(0, len(ids), per_file_chunk)]
        else:
            units = [(rel, [str(test_file)])]

        file_result = FileResult(path=rel, status="passed", chunks=len(units))
        for unit_name, targets in units:
            if unit_name in passed_units:
                summary.resumed += 1
                continue
            _wait_for_free_ram(min_free_mb, echo)
            args = [sys.executable, "-m", "pytest", *targets, *base]
            # `-k` already narrowed the collected ids for chunked units.
            if per_file_chunk > 0 and k_expr:
                args = [a for i, a in enumerate(args) if not (a == "-k" or (i > 0 and args[i - 1] == "-k"))]
            start = time.perf_counter()
            try:
                run = run_measured(args, cwd=str(source_root), capture_output=True, timeout=per_file_timeout)
            except Exception as exc:  # subprocess.TimeoutExpired and friends
                file_result.status = "timeout"
                file_result.summary = f"{type(exc).__name__}"
                echo(f"  {unit_name:<44} TIMEOUT after {per_file_timeout:.0f}s")
                continue
            elapsed = time.perf_counter() - start
            p, f, s, e = parse_counts(run.stdout or "")
            file_result.passed += p
            file_result.failed += f
            file_result.skipped += s
            file_result.errors += e
            file_result.elapsed_s += elapsed
            if run.peak_rss_mb is not None:
                file_result.peak_rss_mb = max(file_result.peak_rss_mb or 0.0, run.peak_rss_mb)
            tail = (run.stdout or "").strip().splitlines()
            file_result.summary = tail[-1] if tail else ""
            unit_ok = run.returncode in (0, 5) and f == 0 and e == 0  # 5 = no tests collected
            if run.returncode not in (0, 5) and f == 0 and e == 0:
                # pytest died without a summary (a killed interpreter): count it, loudly.
                file_result.errors += 1
                e = 1
                file_result.summary = f"pytest exited {run.returncode} without a summary"
                unit_ok = False
            if unit_ok:
                passed_units.add(unit_name)
                state["passed_units"] = sorted(passed_units)
                _save_state(state_path, state)
            else:
                file_result.status = "failed" if f else "error"
            rss = f"{run.peak_rss_mb:.0f} MB" if run.peak_rss_mb is not None else "n/a"
            echo(f"  {unit_name:<44} {p:>4} passed {f:>3} failed {s:>3} skipped {e:>2} err  "
                 f"{elapsed:6.1f}s  peak {rss}")
        if file_result.status == "passed" and file_result.passed == 0 and file_result.failed == 0 and file_result.errors == 0:
            file_result.status = "skipped-all" if file_result.skipped else "passed"
        summary.passed += file_result.passed
        summary.failed += file_result.failed
        summary.skipped += file_result.skipped
        summary.errors += file_result.errors
        summary.files.append(file_result)

    echo("")
    echo(f"{summary.pytest_style_line()} across {len(summary.files)} file(s)"
         + (f" ({summary.resumed} unit(s) resumed from {state_path})" if summary.resumed else ""))
    worst = sorted((f for f in summary.files if f.peak_rss_mb is not None), key=lambda f: -f.peak_rss_mb)[:5]
    if worst:
        echo("heaviest files by peak RSS: " + ", ".join(f"{f.path} {f.peak_rss_mb:.0f} MB" for f in worst))
    bad = [f for f in summary.files if f.status not in ("passed", "skipped-all")]
    for f in bad:
        echo(f"  [{f.status.upper()}] {f.path}: {f.summary}")
    return summary


def summary_to_dict(summary: LowmemSummary) -> dict:
    return {
        "passed": summary.passed, "failed": summary.failed, "skipped": summary.skipped,
        "errors": summary.errors, "resumed": summary.resumed, "ok": summary.ok,
        "files": [asdict(f) for f in summary.files],
    }
