"""Benchmark #5 — cold clone / first pull time.

`av clone` (added in the v1.1.1 cycle) materializes a fresh working copy of a pushed
project from the registry: full commit metadata + tip objects, into an empty directory.
Setup (init/add/commit/push of the fixture) happens untimed; only `av clone` is timed.

Git LFS and DVC get real numbers the same way: push the fixture once to a local bare/dir
remote (setup, not timed), then time a fresh `git clone` + `git lfs pull` / `git clone` +
`dvc pull` into an empty directory. MLflow is N/A — it has no project-level clone/pull
concept (artifacts are fetched per-run via `download_artifacts`, not a wholesale "give me
this project" verb). av requires a running registry (docker compose stack) — without one
its column reports "server unreachable", not a fabricated number.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from av_cli import speedcheck  # noqa: E402

from benchmarks.tool_runner import (  # noqa: E402
    BenchmarkResult,
    Row,
    ToolStatus,
    detect_tools,
    time_subprocess,
)

_populate_fixture = speedcheck.populate_cli_fixture

_AV_REGISTRY = "http://localhost:8000"


def _av_server_reachable() -> bool:
    try:
        with urllib.request.urlopen(f"{_AV_REGISTRY}/api/health", timeout=2) as resp:
            return resp.status == 200
    except (urllib.error.URLError, OSError):
        return False


def _bench_av() -> tuple[float | None, str | None]:
    """Pushes the standard CLI fixture to the registry (untimed), then times `av clone`.

    Returns (ms, note). The project name is unique per run so repeated captures on a
    long-lived dev registry don't collide.
    """
    av_path = shutil.which("av")
    if av_path is None:
        return None, "not installed"
    if not _av_server_reachable():
        return None, f"registry unreachable at {_AV_REGISTRY} — start the docker compose stack"

    with tempfile.TemporaryDirectory(prefix="bench-clone-av-") as tmp:
        root = Path(tmp)
        source = root / "source"
        source.mkdir()
        _populate_fixture(source)

        def run(args, cwd):
            subprocess.run(args, cwd=cwd, check=True, capture_output=True)

        run([av_path, "init", "--mode", "local", "--yes", "--no-repl"], source)
        run([av_path, "config", "--remote-url", _AV_REGISTRY], source)
        project_name = f"bench-clone-{os.urandom(3).hex()}"
        run([av_path, "config", "--name", project_name], source)
        run([av_path, "add", "."], source)
        run([av_path, "commit", "-m", "bench"], source)
        run([av_path, "push"], source)

        clone_dest = root / "clone-workspace"
        clone_dest.mkdir()
        return time_subprocess(
            [av_path, "clone", project_name], clone_dest
        ), None


def _bench_git_lfs() -> float | None:
    git_path = shutil.which("git")
    git_lfs = shutil.which("git-lfs")
    if git_path is None or git_lfs is None:
        return None
    with tempfile.TemporaryDirectory(prefix="bench-clone-gitlfs-") as tmp:
        root = Path(tmp)
        remote = root / "remote.git"
        subprocess.run(["git", "init", "--bare", str(remote)])
        source = root / "source"
        source.mkdir()
        _populate_fixture(source)
        subprocess.run(["git", "init"], cwd=source)
        subprocess.run([git_lfs, "install", "--local"], cwd=source)
        subprocess.run([git_lfs, "track", "*.bin"], cwd=source)
        subprocess.run(["git", "config", "user.email", "bench@example.com"], cwd=source)
        subprocess.run(["git", "config", "user.name", "bench"], cwd=source)
        subprocess.run(["git", "remote", "add", "origin", str(remote)], cwd=source)
        subprocess.run(["git", "add", "."], cwd=source)
        subprocess.run(["git", "commit", "-m", "bench"], cwd=source)
        subprocess.run(["git", "push", "origin", "HEAD"], cwd=source)

        clone_dest = root / "clone"
        return time_subprocess(["git", "clone", str(remote), str(clone_dest)], root)


def _bench_dvc() -> float | None:
    dvc_path = shutil.which("dvc")
    git_path = shutil.which("git")
    if dvc_path is None or git_path is None:
        return None
    with tempfile.TemporaryDirectory(prefix="bench-clone-dvc-") as tmp:
        root = Path(tmp)
        bare = root / "remote.git"
        subprocess.run(["git", "init", "--bare", str(bare)])
        dvc_remote = root / "dvc-remote"
        dvc_remote.mkdir()
        source = root / "source"
        source.mkdir()
        _populate_fixture(source)
        subprocess.run(["git", "init"], cwd=source)
        subprocess.run([dvc_path, "init"], cwd=source)
        subprocess.run(["git", "config", "user.email", "bench@example.com"], cwd=source)
        subprocess.run(["git", "config", "user.name", "bench"], cwd=source)
        subprocess.run(["git", "remote", "add", "origin", str(bare)], cwd=source)
        subprocess.run([dvc_path, "remote", "add", "-d", "bench-remote", str(dvc_remote)], cwd=source)
        large_files = [str(p) for p in source.glob("model_*.bin")]
        subprocess.run([dvc_path, "add", *large_files], cwd=source)
        subprocess.run(["git", "add", "."], cwd=source)
        subprocess.run(["git", "commit", "-m", "bench"], cwd=source)
        subprocess.run([dvc_path, "push"], cwd=source)
        subprocess.run(["git", "push", "origin", "HEAD"], cwd=source)

        clone_dest = root / "clone"
        start_args = ["git", "clone", str(bare), str(clone_dest)]
        import time
        start = time.perf_counter()
        subprocess.run(start_args, cwd=root)
        subprocess.run([dvc_path, "pull"], cwd=clone_dest)
        return (time.perf_counter() - start) * 1000


def run(tool_order: list[str] | None = None) -> BenchmarkResult:
    tool_order = tool_order or ["av", "git-lfs", "dvc", "mlflow"]
    tools = detect_tools()
    raw = {"git-lfs": _bench_git_lfs(), "dvc": _bench_dvc()}

    values: dict[str, float | None] = {}
    statuses: dict[str, ToolStatus] = {}
    notes: dict[str, str] = {}

    av_value, av_note = _bench_av()
    if av_value is None:
        values["av"] = None
        statuses["av"] = tools.get("av").status if tools.get("av") else ToolStatus.NOT_INSTALLED
        if av_note:
            notes["av"] = av_note
    else:
        values["av"] = av_value
        statuses["av"] = ToolStatus.AVAILABLE

    for tool in ("git-lfs", "dvc"):
        if raw[tool] is None:
            values[tool] = None
            statuses[tool] = tools[tool].status
        else:
            values[tool] = raw[tool]
            statuses[tool] = ToolStatus.AVAILABLE

    values["mlflow"] = None
    statuses["mlflow"] = ToolStatus.NOT_APPLICABLE
    notes["mlflow"] = "no project-level clone/pull concept"

    row = Row(operation="clone + pull (fresh checkout)", values=values, statuses=statuses, unit="ms", notes=notes)

    return BenchmarkResult(
        name="cold_clone",
        title="Cold Clone / First Pull Time",
        description="Fresh, empty-directory checkout of a project someone else already pushed.",
        tool_order=tool_order,
        rows=[row],
    )


if __name__ == "__main__":
    from benchmarks.tool_runner import print_table
    print_table(run())
