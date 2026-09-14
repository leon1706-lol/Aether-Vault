"""Benchmark #3 — commit + push latency, end-to-end.

Extends scripts/run_benchmark_comparison.py's init/add/commit comparison with an explicit
push step and a fourth tool, MLflow. A tool with no separate "push" step (MLflow: logging
an artifact *is* the remote write) gets that cell marked N/A with a footnote.
"""

import shutil
import subprocess
import sys
import tempfile
import time
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


def _bench_av() -> dict[str, float | None] | None:
    av_path = shutil.which("av")
    if av_path is None:
        return None
    with tempfile.TemporaryDirectory(prefix="bench-push-av-", ignore_cleanup_errors=True) as tmp:
        root = Path(tmp)
        # commit_upload=False: the commit row is a pure local finalize, like git-lfs's and
        # dvc's own `git commit` (neither ever touches a network on commit) -- the real
        # upload is what the push row measures, matching `dvc push`.
        probes = speedcheck.run_av_cli_probes(
            av_path, root, commit_upload=False, env={"AV_NO_UPDATE_CHECK": "1"}, warm_daemon=True,
        )
        result = {
            "init": speedcheck.probe_ms(probes, "av init"),
            "add": speedcheck.probe_ms(probes, "av add ."),
            "commit": speedcheck.probe_ms(probes, "av commit"),
        }
        push_ms = time_subprocess([av_path, "push"], root, rss_key="commit_push_latency:push")
        result["push"] = push_ms
        subprocess.run([av_path, "daemon", "stop"], cwd=root)
        return result


def _bench_git_lfs() -> dict[str, float | None] | None:
    git_path = shutil.which("git")
    git_lfs = shutil.which("git-lfs")
    if git_path is None or git_lfs is None:
        return None
    with tempfile.TemporaryDirectory(prefix="bench-push-gitlfs-", ignore_cleanup_errors=True) as tmp:
        root = Path(tmp)
        remote = root.parent / f"{root.name}-remote.git"
        subprocess.run(["git", "init", "--bare", str(remote)])
        work = root / "work"
        work.mkdir()
        _populate_fixture(work)
        init_ms = time_subprocess(["git", "init"], work)
        init_ms += time_subprocess([git_lfs, "install", "--local"], work)
        init_ms += time_subprocess([git_lfs, "track", "*.bin"], work)
        subprocess.run(["git", "config", "user.email", "bench@example.com"], cwd=work)
        subprocess.run(["git", "config", "user.name", "bench"], cwd=work)
        subprocess.run(["git", "remote", "add", "origin", str(remote)], cwd=work)
        add_ms = time_subprocess(["git", "add", "."], work)
        commit_ms = time_subprocess(["git", "commit", "-m", "bench"], work)
        push_ms = time_subprocess(["git", "push", "origin", "HEAD"], work)
        shutil.rmtree(remote, ignore_errors=True)
        return {"init": init_ms, "add": add_ms, "commit": commit_ms, "push": push_ms}


def _bench_dvc() -> dict[str, float | None] | None:
    dvc_path = shutil.which("dvc")
    git_path = shutil.which("git")
    if dvc_path is None or git_path is None:
        return None
    with tempfile.TemporaryDirectory(prefix="bench-push-dvc-", ignore_cleanup_errors=True) as tmp:
        root = Path(tmp)
        remote = root / "dvc-remote"
        remote.mkdir()
        work = root / "work"
        work.mkdir()
        _populate_fixture(work)
        init_ms = time_subprocess(["git", "init"], work)
        init_ms += time_subprocess([dvc_path, "init"], work)
        subprocess.run(["git", "config", "user.email", "bench@example.com"], cwd=work)
        subprocess.run(["git", "config", "user.name", "bench"], cwd=work)
        subprocess.run([dvc_path, "remote", "add", "-d", "bench-remote", str(remote)], cwd=work)
        large_files = [str(p) for p in work.glob("model_*.bin")]
        add_ms = time_subprocess([dvc_path, "add", *large_files], work)
        add_ms += time_subprocess(["git", "add", "."], work)
        commit_ms = time_subprocess(["git", "commit", "-m", "bench"], work)
        push_ms = time_subprocess([dvc_path, "push"], work)
        return {"init": init_ms, "add": add_ms, "commit": commit_ms, "push": push_ms}


def _bench_mlflow() -> dict[str, float | None] | None:
    if shutil.which("mlflow") is None:
        return None
    import mlflow

    # Manual mkdtemp + ignore_errors cleanup: mlflow's sqlite backend can still hold the
    # DB file open on Windows when TemporaryDirectory's `with` block would try to clean up.
    root = Path(tempfile.mkdtemp(prefix="bench-push-mlflow-"))
    try:
        fixture_dir = root / "fixture"
        fixture_dir.mkdir()
        _populate_fixture(fixture_dir)
        start = time.perf_counter()
        mlflow.set_tracking_uri(f"sqlite:///{root / 'mlflow.db'}")
        experiment_id = mlflow.create_experiment("bench-commit-push", artifact_location=f"file:{root / 'mlartifacts'}")
        init_ms = (time.perf_counter() - start) * 1000

        start = time.perf_counter()
        with mlflow.start_run(experiment_id=experiment_id):
            mlflow.log_artifacts(str(fixture_dir), artifact_path="fixture")
        commit_ms = (time.perf_counter() - start) * 1000
        # log_artifacts() writes directly to the store; no separate push step.
        return {"init": init_ms, "add": 0.0, "commit": commit_ms, "push": None}
    finally:
        shutil.rmtree(root, ignore_errors=True)


def run(tool_order: list[str] | None = None, repeat: int = 1) -> BenchmarkResult:
    tool_order = tool_order or ["av", "git-lfs", "dvc", "mlflow"]
    results = {
        "av": repeat_median(_bench_av, repeat),
        "git-lfs": repeat_median(_bench_git_lfs, repeat),
        "dvc": repeat_median(_bench_dvc, repeat),
        "mlflow": repeat_median(_bench_mlflow, repeat),
    }
    tools = detect_tools()

    rows = []
    notes_by_tool = {"mlflow": "no separate push step — log_artifacts() writes directly to the store"}
    for op, label in [("init", "init"), ("add", f"add ({FILE_COUNT} files)"), ("commit", "commit"), ("push", "push")]:
        values: dict[str, float | None] = {}
        statuses: dict[str, ToolStatus] = {}
        notes: dict[str, str] = {}
        for tool in tool_order:
            r = results[tool]
            if r is None:
                values[tool] = None
                statuses[tool] = tools[tool].status
            elif r.get(op) is None:
                values[tool] = None
                statuses[tool] = ToolStatus.NOT_APPLICABLE
                if tool in notes_by_tool:
                    notes[tool] = notes_by_tool[tool]
            else:
                values[tool] = r[op]
                statuses[tool] = ToolStatus.AVAILABLE
        rows.append(Row(operation=label, values=values, statuses=statuses, unit="ms", notes=notes,
                        rss_mb={"av": pop_rss_median(f"commit_push_latency:{op}")}))

    return BenchmarkResult(
        name="commit_push_latency",
        title="Commit + Push Latency, End-to-End",
        description=f"init/add/commit/push on the same {FILE_COUNT}-file mixed fixture, across all four tools.",
        tool_order=tool_order,
        rows=rows,
    )


if __name__ == "__main__":
    from benchmarks.tool_runner import print_table
    print_table(run())
