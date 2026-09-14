"""Benchmark #4 — no-op `status`/`add` speed at scale.

Stages the fixture once, commits it, then re-runs the staging step with *nothing*
changed and times that second, no-op run, plus a separate `status` row on the same clean
tree. Aether's `add()` has an explicit size+mtime short-circuit (`compare_meta_safe`,
core.py) that skips re-hashing unchanged files entirely; this benchmark is what that
optimization is for. MLflow has no incremental staging/status concept comparable to
add/status, so it's marked N/A on both rows.
"""

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from av_cli import speedcheck  # noqa: E402

from benchmarks.tool_runner import (  # noqa: E402
    BenchmarkResult,
    Row,
    ToolStatus,
    detect_tools,
    pop_rss_median,
    repeat_median,
    time_subprocess,
)

_populate_fixture = speedcheck.populate_cli_fixture
FILE_COUNT = speedcheck.CLI_CODE_FILE_COUNT + speedcheck.CLI_LARGE_FILE_COUNT


def _bench_av() -> dict[str, float] | None:
    av_path = shutil.which("av")
    if av_path is None:
        return None
    with tempfile.TemporaryDirectory(prefix="bench-noop-av-", ignore_cleanup_errors=True) as tmp:
        root = Path(tmp)
        _populate_fixture(root)
        time_subprocess([av_path, "init", "--mode", "local", "--yes", "--no-repl"], root,
                        env={"AV_NO_UPDATE_CHECK": "1"})
        time_subprocess([av_path, "add", "."], root)
        time_subprocess([av_path, "commit", "-m", "bench", "--no-upload"], root)
        speedcheck.await_daemon(av_path, root)
        return {
            "re-add": time_subprocess([av_path, "add", "."], root, rss_key="noop_status_speed:re-add"),
            "status": time_subprocess([av_path, "status"], root, rss_key="noop_status_speed:status"),
        }


def _bench_git_lfs() -> dict[str, float] | None:
    git_path = shutil.which("git")
    git_lfs = shutil.which("git-lfs")
    if git_path is None or git_lfs is None:
        return None
    with tempfile.TemporaryDirectory(prefix="bench-noop-gitlfs-", ignore_cleanup_errors=True) as tmp:
        root = Path(tmp)
        _populate_fixture(root)
        time_subprocess(["git", "init"], root)
        time_subprocess([git_lfs, "install", "--local"], root)
        time_subprocess([git_lfs, "track", "*.bin"], root)
        subprocess.run(["git", "config", "user.email", "bench@example.com"], cwd=root)
        subprocess.run(["git", "config", "user.name", "bench"], cwd=root)
        time_subprocess(["git", "add", "."], root)
        time_subprocess(["git", "commit", "-m", "bench"], root)
        return {
            "re-add": time_subprocess(["git", "add", "."], root),
            "status": time_subprocess(["git", "status"], root),
        }


def _bench_dvc() -> dict[str, float] | None:
    dvc_path = shutil.which("dvc")
    git_path = shutil.which("git")
    if dvc_path is None or git_path is None:
        return None
    with tempfile.TemporaryDirectory(prefix="bench-noop-dvc-", ignore_cleanup_errors=True) as tmp:
        root = Path(tmp)
        _populate_fixture(root)
        time_subprocess(["git", "init"], root)
        time_subprocess([dvc_path, "init"], root)
        subprocess.run(["git", "config", "user.email", "bench@example.com"], cwd=root)
        subprocess.run(["git", "config", "user.name", "bench"], cwd=root)
        large_files = [str(p) for p in root.glob("model_*.bin")]
        time_subprocess([dvc_path, "add", *large_files], root)
        time_subprocess(["git", "add", "."], root)
        time_subprocess(["git", "commit", "-m", "bench"], root)
        return {
            "re-add": time_subprocess([dvc_path, "add", *large_files], root),
            # DVC's own status walk is genuinely slow at this scale -- a real number, kept.
            "status": time_subprocess([dvc_path, "status"], root),
        }


def run(tool_order: list[str] | None = None, repeat: int = 1) -> BenchmarkResult:
    tool_order = tool_order or ["av", "git-lfs", "dvc", "mlflow"]
    tools = detect_tools()
    raw = {
        "av": repeat_median(_bench_av, repeat),
        "git-lfs": repeat_median(_bench_git_lfs, repeat),
        "dvc": repeat_median(_bench_dvc, repeat),
        "mlflow": None,
    }

    rows = []
    for op, label in [("re-add", f"re-add unchanged ({FILE_COUNT} files)"), ("status", f"status (clean tree, {FILE_COUNT} files)")]:
        values: dict[str, float | None] = {}
        statuses: dict[str, ToolStatus] = {}
        notes: dict[str, str] = {}
        for tool in tool_order:
            if tool == "mlflow":
                values[tool] = None
                statuses[tool] = ToolStatus.NOT_APPLICABLE
                notes[tool] = "no incremental staging/status primitive"
            elif raw[tool] is None or raw[tool].get(op) is None:
                values[tool] = None
                statuses[tool] = tools[tool].status
            else:
                values[tool] = raw[tool][op]
                statuses[tool] = ToolStatus.AVAILABLE
        rows.append(Row(operation=label, values=values, statuses=statuses, unit="ms", notes=notes,
                        rss_mb={"av": pop_rss_median(f"noop_status_speed:{op}")}))

    return BenchmarkResult(
        name="noop_status_speed",
        title="No-Op status/add Speed at Scale",
        description="Re-running the staging step, and a plain `status`, a second time with nothing changed.",
        tool_order=tool_order,
        rows=rows,
    )


if __name__ == "__main__":
    from benchmarks.tool_runner import print_table
    print_table(run())
