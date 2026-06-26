"""Benchmark #5 — cold clone / first pull time.

Real finding while building this benchmark: `av` has no `clone`/`pull` command at all
(confirmed via `av --help` — only init/add/status/commit/branch/checkout/push/etc. exist).
Aether's sync model today is push-only from a single local working repo; there is no flow
for a *second* machine to materialize a fresh working copy of a project someone else
already pushed. That's a real product gap, not a benchmark artifact — av's column here is
N/A with that footnote, not a fabricated number. (Tracked as a roadmap item, not a
Probleme.md bug — nothing is broken, the feature just doesn't exist yet.)

Git LFS and DVC get real numbers: push the fixture once to a local bare/dir remote (setup,
not timed), then time a fresh `git clone` + `git lfs pull` / `git clone` + `dvc pull` into an
empty directory. MLflow is also N/A — it has no project-level clone/pull concept (artifacts
are fetched per-run via `download_artifacts`, not a wholesale "give me this project" verb).
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
    time_subprocess,
)

_populate_fixture = speedcheck.populate_cli_fixture


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

    values["av"] = None
    statuses["av"] = ToolStatus.NOT_APPLICABLE
    notes["av"] = "no clone/pull command exists yet — push-only sync today"

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
