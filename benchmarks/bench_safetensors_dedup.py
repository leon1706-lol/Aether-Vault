"""Benchmark #2 — Safetensors layer-dedup storage savings.

Simulates fine-tuning only a model's classifier head across several commits (every other
layer stays byte-identical) and measures each tool's total on-disk storage after the
sequence. None of the three competitors split a checkpoint into independently-hashed
layers, so they re-store the entire file every step; Aether's
`aether_core.split_and_hash_safetensors` only re-stores the layer that actually changed.

Shared with bench_storage_footprint_curve.py: `run_finetune_sequence()` returns the size
after each step, not just the final total.
"""

import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from benchmarks import fixtures  # noqa: E402
from benchmarks.tool_runner import (  # noqa: E402
    BenchmarkResult,
    Row,
    ToolStatus,
    detect_tools,
)

DEFAULT_STEPS = fixtures.STORAGE_CURVE_COMMIT_COUNT


def _av_sequence(av_path: str, repo: Path, steps: int) -> list[int]:
    subprocess.run([av_path, "init", "--mode", "local", "--yes", "--no-repl"], cwd=repo)
    # Default LFS threshold is 50MB, well above the fixture's size, so it must be lowered
    # or `av add` would never engage layer-splitting at all.
    subprocess.run([av_path, "config", "1"], cwd=repo)
    sizes = []
    for step in range(steps):
        fixtures.make_finetune_step_safetensors(repo / "model.safetensors", step)
        subprocess.run([av_path, "add", "model.safetensors"], cwd=repo)
        subprocess.run([av_path, "commit", "-m", f"step {step}"], cwd=repo)
        sizes.append(fixtures.dir_size_bytes(repo / ".av" / "objects"))
    return sizes


def _git_lfs_sequence(git_lfs_path: str, repo: Path, steps: int) -> list[int]:
    subprocess.run(["git", "init"], cwd=repo)
    subprocess.run([git_lfs_path, "install", "--local"], cwd=repo)
    subprocess.run([git_lfs_path, "track", "*.safetensors"], cwd=repo)
    subprocess.run(["git", "config", "user.email", "bench@example.com"], cwd=repo)
    subprocess.run(["git", "config", "user.name", "bench"], cwd=repo)
    sizes = []
    for step in range(steps):
        fixtures.make_finetune_step_safetensors(repo / "model.safetensors", step)
        subprocess.run(["git", "add", "."], cwd=repo)
        subprocess.run(["git", "commit", "-m", f"step {step}"], cwd=repo)
        sizes.append(fixtures.dir_size_bytes(repo / ".git" / "lfs" / "objects"))
    return sizes


def _dvc_sequence(dvc_path: str, repo: Path, steps: int) -> list[int]:
    subprocess.run(["git", "init"], cwd=repo)
    subprocess.run([dvc_path, "init"], cwd=repo)
    subprocess.run(["git", "config", "user.email", "bench@example.com"], cwd=repo)
    subprocess.run(["git", "config", "user.name", "bench"], cwd=repo)
    sizes = []
    for step in range(steps):
        fixtures.make_finetune_step_safetensors(repo / "model.safetensors", step)
        subprocess.run([dvc_path, "add", "model.safetensors"], cwd=repo)
        subprocess.run(["git", "add", "."], cwd=repo)
        subprocess.run(["git", "commit", "-m", f"step {step}"], cwd=repo)
        sizes.append(fixtures.dir_size_bytes(repo / ".dvc" / "cache"))
    return sizes


def _mlflow_sequence(repo: Path, steps: int) -> list[int]:
    import mlflow

    # MLflow 3.x's filesystem tracking store is maintenance-mode-only, so a local sqlite
    # store is the realistic modern equivalent of "local tracking."
    artifacts_dir = repo / "mlartifacts"
    mlflow.set_tracking_uri(f"sqlite:///{repo / 'mlflow.db'}")
    experiment_id = mlflow.create_experiment("bench-safetensors-dedup", artifact_location=f"file:{artifacts_dir}")
    sizes = []
    for step in range(steps):
        path = repo / "model.safetensors"
        fixtures.make_finetune_step_safetensors(path, step)
        with mlflow.start_run(experiment_id=experiment_id):
            mlflow.log_artifact(str(path))
        sizes.append(fixtures.dir_size_bytes(artifacts_dir))
    return sizes


def run_finetune_sequence(tool: str, repo: Path, steps: int = DEFAULT_STEPS) -> list[int] | None:
    """Runs `steps` fine-tune commits for `tool` in `repo`, returns storage size in bytes
    after each step, or None if the tool isn't available."""
    tools = detect_tools([tool]) if tool != "av" else detect_tools(["av"])
    handle = tools[tool]
    if handle.status != ToolStatus.AVAILABLE:
        return None
    if tool == "av":
        return _av_sequence(handle.path, repo, steps)
    if tool == "git-lfs":
        return _git_lfs_sequence(handle.path, repo, steps)
    if tool == "dvc":
        return _dvc_sequence(handle.path, repo, steps)
    if tool == "mlflow":
        return _mlflow_sequence(repo, steps)
    raise ValueError(f"unknown tool: {tool}")


def run(tool_order: list[str] | None = None) -> BenchmarkResult:
    tool_order = tool_order or ["av", "git-lfs", "dvc", "mlflow"]
    values: dict[str, float | None] = {}
    statuses: dict[str, ToolStatus] = {}

    import tempfile

    for tool in ["av", "git-lfs", "dvc", "mlflow"]:
        # Manual mkdtemp + ignore_errors cleanup: mlflow's sqlite backend can still hold
        # the DB file open on Windows when a TemporaryDirectory's `with` block exits.
        repo = Path(tempfile.mkdtemp(prefix=f"bench-dedup-{tool}-"))
        handle_status = detect_tools([tool])[tool].status
        try:
            sizes = run_finetune_sequence(tool, repo, DEFAULT_STEPS)
        except Exception as exc:
            print(f"warning: {tool} sequence failed: {exc}", file=sys.stderr)
            sizes = None
        finally:
            shutil.rmtree(repo, ignore_errors=True)
        if sizes is None:
            values[tool] = None
            statuses[tool] = handle_status if handle_status != ToolStatus.AVAILABLE else ToolStatus.NOT_INSTALLED
        else:
            values[tool] = float(sizes[-1])
            statuses[tool] = ToolStatus.AVAILABLE

    row = Row(
        operation=f"storage after {DEFAULT_STEPS} fine-tune commits",
        values=values,
        statuses=statuses,
        unit="bytes",
    )

    return BenchmarkResult(
        name="safetensors_dedup",
        title="Safetensors Layer-Dedup Storage Savings",
        description=(
            f"Total on-disk storage after {DEFAULT_STEPS} commits, each changing only a "
            "classifier-head layer of an otherwise-unchanged checkpoint. Aether re-stores "
            "only the changed layer; the others re-store the whole file every commit."
        ),
        tool_order=tool_order,
        rows=[row],
    )


if __name__ == "__main__":
    from benchmarks.tool_runner import print_table
    print_table(run())
