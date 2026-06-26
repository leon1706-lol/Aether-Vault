"""Dev-only benchmark: times init/add/commit on the same synthetic fixture across
Aether-Vault, Git LFS, and DVC, and prints a Markdown table for README.md.

Not part of `av test --speed` / `av doctor --speed` (those track internal regressions on
fixed fixtures); this is for manually regenerating the README's "Benchmark Comparison"
section against whatever's actually installed. Tools that aren't on PATH are skipped
and shown as "not installed" rather than guessed at.

Usage: python scripts/run_benchmark_comparison.py
"""

import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))
from av_cli import speedcheck  # noqa: E402 — needs the sys.path tweak above

# Shared with av_cli.speedcheck.run_av_cli_probes so the av column and the internal
# "av CLI, end-to-end" section of `av test --speed` always benchmark the same fixture.
CODE_FILE_COUNT = speedcheck.CLI_CODE_FILE_COUNT
CODE_FILE_SIZE = speedcheck.CLI_CODE_FILE_SIZE
LARGE_FILE_COUNT = speedcheck.CLI_LARGE_FILE_COUNT
LARGE_FILE_SIZE = speedcheck.CLI_LARGE_FILE_SIZE
_populate_fixture = speedcheck.populate_cli_fixture


def _time_call(args: list[str], cwd: Path) -> float:
    start = time.perf_counter()
    subprocess.run(args, cwd=cwd, check=True, capture_output=True)
    return (time.perf_counter() - start) * 1000


def bench_av() -> dict[str, float] | None:
    av_path = shutil.which("av")
    if av_path is None:
        return None
    with tempfile.TemporaryDirectory(prefix="bench-av-") as tmp:
        probes = speedcheck.run_av_cli_probes(av_path, Path(tmp))
        return {"init": probes[0][1], "add": probes[1][1], "commit": probes[2][1]}


def bench_git_lfs() -> dict[str, float] | None:
    git_path = shutil.which("git")
    if git_path is None or shutil.which("git-lfs") is None:
        return None
    with tempfile.TemporaryDirectory(prefix="bench-gitlfs-") as tmp:
        root = Path(tmp)
        _populate_fixture(root)
        init_ms = _time_call([git_path, "init"], root)
        init_ms += _time_call([git_path, "lfs", "install", "--local"], root)
        init_ms += _time_call([git_path, "lfs", "track", "*.bin"], root)
        subprocess.run([git_path, "config", "user.email", "bench@example.com"], cwd=root, check=True, capture_output=True)
        subprocess.run([git_path, "config", "user.name", "bench"], cwd=root, check=True, capture_output=True)
        add_ms = _time_call([git_path, "add", "."], root)
        commit_ms = _time_call([git_path, "commit", "-m", "bench"], root)
        return {"init": init_ms, "add": add_ms, "commit": commit_ms}


def bench_dvc() -> dict[str, float] | None:
    dvc_path = shutil.which("dvc")
    git_path = shutil.which("git")
    if dvc_path is None or git_path is None:
        return None
    with tempfile.TemporaryDirectory(prefix="bench-dvc-") as tmp:
        root = Path(tmp)
        _populate_fixture(root)
        init_ms = _time_call([git_path, "init"], root)
        init_ms += _time_call([dvc_path, "init"], root)
        subprocess.run([git_path, "config", "user.email", "bench@example.com"], cwd=root, check=True, capture_output=True)
        subprocess.run([git_path, "config", "user.name", "bench"], cwd=root, check=True, capture_output=True)
        large_files = [str(p) for p in root.glob("model_*.bin")]
        add_ms = _time_call([dvc_path, "add", *large_files], root)
        add_ms += _time_call([git_path, "add", "."], root)
        commit_ms = _time_call([git_path, "commit", "-m", "bench"], root)
        return {"init": init_ms, "add": add_ms, "commit": commit_ms}


def _fmt(results: dict[str, float] | None, op: str) -> str:
    if results is None:
        return "not installed"
    return f"{results[op]:.1f} ms"


def main() -> None:
    av_results = bench_av()
    git_lfs_results = bench_git_lfs()
    dvc_results = bench_dvc()

    total_size_mb = (CODE_FILE_COUNT * CODE_FILE_SIZE + LARGE_FILE_COUNT * LARGE_FILE_SIZE) / (1024 * 1024)
    file_count = CODE_FILE_COUNT + LARGE_FILE_COUNT

    print(f"`av add` + `av commit` on a {file_count}-file mixed code/model fixture "
          f"({total_size_mb:.0f}MB total), vs. equivalent Git LFS and DVC operations.")
    print()
    print("| Operation | Aether-Vault | Git LFS | DVC |")
    print("|---|---:|---:|---:|")
    for op, label in [("init", "init"), ("add", f"add ({file_count} files)"), ("commit", "commit")]:
        print(f"| {label} | {_fmt(av_results, op)} | {_fmt(git_lfs_results, op)} | {_fmt(dvc_results, op)} |")


if __name__ == "__main__":
    main()
