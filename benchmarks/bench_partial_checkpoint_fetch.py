"""Benchmark #6 — partial-checkpoint fetch (layer-level pull).

Pushes a multi-layer checkpoint to a real remote, clears local objects (simulating a fresh
machine), then times fetching just *one* layer vs the whole checkpoint. None of the three
competitors have sub-file granularity — pulling always means the whole file — so their
"single layer" cell is N/A; they only get a real number in the "whole checkpoint" row,
which av also reports for an honest baseline comparison.

Uses `av_cli.client.VaultClient` directly against a real reachable `av_server` (no mocking)
— if no server is reachable, av's rows fall back to N/A with a clear note rather than a
fabricated number, same as every other tool-missing case in this suite.
"""

import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from av_cli.client import VaultClient  # noqa: E402
from av_cli.index import Index  # noqa: E402

from benchmarks import fixtures  # noqa: E402
from benchmarks.tool_runner import (  # noqa: E402
    BenchmarkResult,
    Row,
    ToolStatus,
    detect_tools,
)

LAYER_COUNT = 4
LAYER_SIZE_MB = 5


def _make_checkpoint(path: Path) -> None:
    layer_size = LAYER_SIZE_MB * 1024 * 1024
    layers = {f"layer_{i}": bytes([i]) * layer_size for i in range(LAYER_COUNT)}
    fixtures.make_safetensors(path, layers)


def _bench_av() -> dict[str, float | None]:
    av_path = shutil.which("av")
    if av_path is None:
        return {"layer": None, "whole": None}

    with tempfile.TemporaryDirectory(prefix="bench-fetch-av-") as tmp:
        root = Path(tmp)
        subprocess.run([av_path, "init", "--mode", "local", "--yes", "--no-repl"], cwd=root)
        subprocess.run([av_path, "config", "1"], cwd=root)
        _make_checkpoint(root / "model.safetensors")
        subprocess.run([av_path, "add", "model.safetensors"], cwd=root)
        subprocess.run([av_path, "commit", "-m", "bench"], cwd=root)
        push_result = subprocess.run([av_path, "push"], cwd=root)

        idx = Index(root)
        entry = idx.get_entry("model.safetensors")
        layers = entry.get("layers", [])
        if push_result.returncode != 0 or not layers:
            return {"layer": None, "whole": None}

        client = VaultClient()
        if not client.server_available():
            return {"layer": None, "whole": None}

        # Wipe local objects to simulate a fresh machine that only has the remote copy.
        shutil.rmtree(root / ".av" / "objects", ignore_errors=True)
        (root / ".av" / "objects").mkdir()

        dest = root / "_layer_fetch.bin"
        start = time.perf_counter()
        client.download_object(layers[0]["hash"], dest)
        layer_ms = (time.perf_counter() - start) * 1000

        # Reset again, then time fetching *every* layer — the whole-checkpoint equivalent
        # av itself would need if reassembling the file from scratch on a fresh machine.
        shutil.rmtree(root / ".av" / "objects", ignore_errors=True)
        (root / ".av" / "objects").mkdir()
        start = time.perf_counter()
        for layer in layers:
            client.download_object(layer["hash"], root / f"_{layer['hash']}.bin")
        whole_ms = (time.perf_counter() - start) * 1000

        return {"layer": layer_ms, "whole": whole_ms}


def _bench_git_lfs() -> float | None:
    git_path = shutil.which("git")
    git_lfs = shutil.which("git-lfs")
    if git_path is None or git_lfs is None:
        return None
    with tempfile.TemporaryDirectory(prefix="bench-fetch-gitlfs-") as tmp:
        root = Path(tmp)
        remote = root / "remote.git"
        subprocess.run(["git", "init", "--bare", str(remote)])
        source = root / "source"
        source.mkdir()
        _make_checkpoint(source / "model.safetensors")
        subprocess.run(["git", "init"], cwd=source)
        subprocess.run([git_lfs, "install", "--local"], cwd=source)
        subprocess.run([git_lfs, "track", "*.safetensors"], cwd=source)
        subprocess.run(["git", "config", "user.email", "bench@example.com"], cwd=source)
        subprocess.run(["git", "config", "user.name", "bench"], cwd=source)
        subprocess.run(["git", "remote", "add", "origin", str(remote)], cwd=source)
        subprocess.run(["git", "add", "."], cwd=source)
        subprocess.run(["git", "commit", "-m", "bench"], cwd=source)
        subprocess.run(["git", "push", "origin", "HEAD"], cwd=source)

        clone_dest = root / "clone"
        subprocess.run(["git", "clone", str(remote), str(clone_dest)])
        start = time.perf_counter()
        subprocess.run([git_lfs, "pull"], cwd=clone_dest)
        return (time.perf_counter() - start) * 1000


def _bench_dvc() -> float | None:
    dvc_path = shutil.which("dvc")
    git_path = shutil.which("git")
    if dvc_path is None or git_path is None:
        return None
    with tempfile.TemporaryDirectory(prefix="bench-fetch-dvc-") as tmp:
        root = Path(tmp)
        bare = root / "remote.git"
        subprocess.run(["git", "init", "--bare", str(bare)])
        dvc_remote = root / "dvc-remote"
        dvc_remote.mkdir()
        source = root / "source"
        source.mkdir()
        _make_checkpoint(source / "model.safetensors")
        subprocess.run(["git", "init"], cwd=source)
        subprocess.run([dvc_path, "init"], cwd=source)
        subprocess.run(["git", "config", "user.email", "bench@example.com"], cwd=source)
        subprocess.run(["git", "config", "user.name", "bench"], cwd=source)
        subprocess.run(["git", "remote", "add", "origin", str(bare)], cwd=source)
        subprocess.run([dvc_path, "remote", "add", "-d", "bench-remote", str(dvc_remote)], cwd=source)
        subprocess.run([dvc_path, "add", "model.safetensors"], cwd=source)
        subprocess.run(["git", "add", "."], cwd=source)
        subprocess.run(["git", "commit", "-m", "bench"], cwd=source)
        subprocess.run([dvc_path, "push"], cwd=source)
        subprocess.run(["git", "push", "origin", "HEAD"], cwd=source)

        clone_dest = root / "clone"
        subprocess.run(["git", "clone", str(bare), str(clone_dest)])
        start = time.perf_counter()
        subprocess.run([dvc_path, "pull"], cwd=clone_dest)
        return (time.perf_counter() - start) * 1000


def _bench_mlflow() -> float | None:
    if shutil.which("mlflow") is None:
        return None
    import mlflow

    root = Path(tempfile.mkdtemp(prefix="bench-fetch-mlflow-"))
    try:
        fixture_dir = root / "fixture"
        fixture_dir.mkdir()
        _make_checkpoint(fixture_dir / "model.safetensors")
        mlflow.set_tracking_uri(f"sqlite:///{root / 'mlflow.db'}")
        experiment_id = mlflow.create_experiment("bench-fetch", artifact_location=f"file:{root / 'mlartifacts'}")
        with mlflow.start_run(experiment_id=experiment_id) as run_obj:
            mlflow.log_artifact(str(fixture_dir / "model.safetensors"))
            run_id = run_obj.info.run_id

        download_dest = root / "downloaded"
        start = time.perf_counter()
        mlflow.artifacts.download_artifacts(run_id=run_id, artifact_path="model.safetensors", dst_path=str(download_dest))
        return (time.perf_counter() - start) * 1000
    finally:
        shutil.rmtree(root, ignore_errors=True)


def run(tool_order: list[str] | None = None) -> BenchmarkResult:
    tool_order = tool_order or ["av", "git-lfs", "dvc", "mlflow"]
    tools = detect_tools()
    av_result = _bench_av()
    git_lfs_whole = _bench_git_lfs()
    dvc_whole = _bench_dvc()
    mlflow_whole = _bench_mlflow()

    no_granularity_note = "no sub-file granularity — always fetches the whole file"

    layer_values: dict[str, float | None] = {"av": av_result["layer"]}
    layer_statuses: dict[str, ToolStatus] = {"av": ToolStatus.AVAILABLE if av_result["layer"] is not None else ToolStatus.NOT_INSTALLED}
    layer_notes: dict[str, str] = {}
    for tool in ("git-lfs", "dvc", "mlflow"):
        layer_values[tool] = None
        layer_statuses[tool] = ToolStatus.NOT_APPLICABLE
        layer_notes[tool] = no_granularity_note

    whole_values: dict[str, float | None] = {"av": av_result["whole"], "git-lfs": git_lfs_whole, "dvc": dvc_whole, "mlflow": mlflow_whole}
    whole_statuses: dict[str, ToolStatus] = {
        "av": ToolStatus.AVAILABLE if av_result["whole"] is not None else ToolStatus.NOT_INSTALLED,
        "git-lfs": ToolStatus.AVAILABLE if git_lfs_whole is not None else tools["git-lfs"].status,
        "dvc": ToolStatus.AVAILABLE if dvc_whole is not None else tools["dvc"].status,
        "mlflow": ToolStatus.AVAILABLE if mlflow_whole is not None else tools["mlflow"].status,
    }

    rows = [
        Row(operation="fetch single layer", values=layer_values, statuses=layer_statuses, unit="ms", notes=layer_notes),
        Row(operation="fetch whole checkpoint", values=whole_values, statuses=whole_statuses, unit="ms"),
    ]

    return BenchmarkResult(
        name="partial_checkpoint_fetch",
        title="Partial-Checkpoint Fetch (Layer-Level Pull)",
        description=(
            f"Fetching one {LAYER_SIZE_MB}MB layer of a {LAYER_COUNT * LAYER_SIZE_MB}MB checkpoint "
            "vs the whole thing, from a real remote with local objects cleared first."
        ),
        tool_order=tool_order,
        rows=rows,
    )


if __name__ == "__main__":
    from benchmarks.tool_runner import print_table
    print_table(run())
