"""Benchmark #1 — hashing throughput at scale.

Times each tool's real hashing primitive over the same single large file, at several
sizes ("small tier": 10/50/100/200MB — see fixtures.HASH_BENCH_FILE_SIZES_MB for why not
literal GB). Headline framing: "Nx faster at every size tested," not a specific GB claim.

- av: `hash_file_safe()` in-process (the exact function `av add` calls).
- git-lfs: `git lfs clean` — the real filter git-lfs invokes on `git add` to hash+pointer a
  file, run directly via stdin/stdout redirection (its actual calling convention).
- dvc: `dvc add <file>` — the real primitive; hashes the file as part of staging it.
- mlflow: N/A — MLflow has no exposed file-hashing primitive comparable to the other
  three (`log_artifact()` copies/uploads a file, it doesn't expose a content hash step a
  caller can time independently). Approximating it via `log_artifact()` would misrepresent
  what MLflow actually does, so this benchmark marks it N/A rather than guess.
"""

import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # repo root, for `import benchmarks`

from av_cli.main import hash_file_safe  # noqa: E402 — av_cli is pip-installed editable, no path hack needed

from benchmarks import fixtures  # noqa: E402
from benchmarks.tool_runner import (  # noqa: E402
    BenchmarkResult,
    Row,
    ToolStatus,
    detect_tools,
)


def _time_av(path: Path) -> float:
    start = time.perf_counter()
    hash_file_safe(str(path))
    return (time.perf_counter() - start) * 1000


def _time_git_lfs(git_lfs_path: str, repo: Path, path: Path) -> float | None:
    rel = path.name
    start = time.perf_counter()
    with open(path, "rb") as src, open(repo / "pointer.out", "wb") as dst:
        result = subprocess.run([git_lfs_path, "clean", "--", rel], cwd=repo, stdin=src, stdout=dst)
    elapsed = (time.perf_counter() - start) * 1000
    return elapsed if result.returncode == 0 else None


def _time_dvc(dvc_path: str, repo: Path, path: Path) -> float | None:
    start = time.perf_counter()
    result = subprocess.run([dvc_path, "add", path.name], cwd=repo)
    elapsed = (time.perf_counter() - start) * 1000
    return elapsed if result.returncode == 0 else None


def run(tool_order: list[str] | None = None) -> BenchmarkResult:
    tools = detect_tools()
    tool_order = tool_order or ["av", "git-lfs", "dvc", "mlflow"]
    rows: list[Row] = []

    with tempfile.TemporaryDirectory(prefix="bench-hashing-") as tmp:
        root = Path(tmp)
        git_lfs_repo = root / "git-lfs-repo"
        dvc_repo = root / "dvc-repo"

        if tools["git-lfs"].status == ToolStatus.AVAILABLE and shutil.which("git"):
            git_lfs_repo.mkdir()
            subprocess.run(["git", "init"], cwd=git_lfs_repo)
            subprocess.run([tools["git-lfs"].path, "install", "--local"], cwd=git_lfs_repo)

        if tools["dvc"].status == ToolStatus.AVAILABLE and shutil.which("git"):
            dvc_repo.mkdir()
            subprocess.run(["git", "init"], cwd=dvc_repo)
            subprocess.run([tools["dvc"].path, "init"], cwd=dvc_repo)

        for size_mb in fixtures.HASH_BENCH_FILE_SIZES_MB:
            values: dict[str, float | None] = {}
            statuses: dict[str, ToolStatus] = {}
            notes: dict[str, str] = {}

            av_file = root / f"av_{size_mb}mb.bin"
            fixtures.make_large_file(av_file, size_mb)
            values["av"] = _time_av(av_file)
            statuses["av"] = ToolStatus.AVAILABLE

            if tools["git-lfs"].status == ToolStatus.AVAILABLE:
                f = git_lfs_repo / f"{size_mb}mb.bin"
                fixtures.make_large_file(f, size_mb)
                values["git-lfs"] = _time_git_lfs(tools["git-lfs"].path, git_lfs_repo, f)
                statuses["git-lfs"] = ToolStatus.AVAILABLE
            else:
                values["git-lfs"] = None
                statuses["git-lfs"] = ToolStatus.NOT_INSTALLED

            if tools["dvc"].status == ToolStatus.AVAILABLE:
                f = dvc_repo / f"{size_mb}mb.bin"
                fixtures.make_large_file(f, size_mb)
                values["dvc"] = _time_dvc(tools["dvc"].path, dvc_repo, f)
                statuses["dvc"] = ToolStatus.AVAILABLE
            else:
                values["dvc"] = None
                statuses["dvc"] = ToolStatus.NOT_INSTALLED

            values["mlflow"] = None
            statuses["mlflow"] = ToolStatus.NOT_APPLICABLE
            notes["mlflow"] = "no exposed file-hashing primitive"

            rows.append(Row(
                operation=f"hash {size_mb}MB file",
                values=values,
                statuses=statuses,
                unit="ms",
                notes=notes,
            ))

    return BenchmarkResult(
        name="hashing_throughput",
        title="Hashing Throughput at Scale",
        description=(
            "SHA-256 (or each tool's equivalent content hash) over a single file at "
            "increasing sizes. MLflow is N/A — log_artifact() isn't a hashing primitive."
        ),
        tool_order=tool_order,
        rows=rows,
    )


if __name__ == "__main__":
    from benchmarks.tool_runner import print_table
    print_table(run())
