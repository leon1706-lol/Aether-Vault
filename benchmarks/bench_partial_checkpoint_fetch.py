"""Benchmark #6 — partial-checkpoint fetch (layer-level pull).

Pushes a multi-layer checkpoint to a real remote, clears local objects (simulating a fresh
machine), then times fetching just *one* layer vs the whole checkpoint. None of the three
competitors have sub-file granularity, so their "single layer" cell is N/A; they only get
a real number in the "whole checkpoint" row, which av also reports for a baseline
comparison. av's own numbers go through the real `av fetch`/`av fetch --layer` CLI
subprocess (not a direct `VaultClient` call) so av pays process startup exactly like the
competitors' `git lfs pull`/`dvc pull` do.
"""

import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

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
    from av_cli import speedcheck

    av_path = shutil.which("av")
    if av_path is None:
        return {"layer": None, "whole": None}

    with tempfile.TemporaryDirectory(prefix="bench-fetch-av-", ignore_cleanup_errors=True) as tmp:
        root = Path(tmp)
        subprocess.run([av_path, "init", "--mode", "local", "--yes", "--no-repl"], cwd=root,
                       env={**os.environ, "AV_NO_UPDATE_CHECK": "1"})
        subprocess.run([av_path, "config", "1"], cwd=root)
        _make_checkpoint(root / "model.safetensors")
        subprocess.run([av_path, "add", "model.safetensors"], cwd=root)
        subprocess.run([av_path, "commit", "-m", "bench", "--no-upload"], cwd=root)
        push_result = subprocess.run([av_path, "push"], cwd=root)

        idx = Index(root)
        entry = idx.get_entry("model.safetensors")
        layers = entry.get("layers", [])
        if push_result.returncode != 0 or not layers:
            return {"layer": None, "whole": None}

        # `layers` is sorted by absolute file offset, and split_and_hash_safetensors always
        # inserts a "__header__" pseudo-layer (the length-prefix + JSON header, a few
        # hundred bytes) at offset 0 -- it always sorts first. Fetching THAT as "one layer"
        # would report a misleadingly tiny/fast number with nothing to do with real tensor
        # data; pick an actual named data layer instead.
        data_layer = next((l for l in layers if l["name"] != "__header__"), layers[0])

        speedcheck.await_daemon(av_path, root)  # untimed setup already triggered auto-spawn

        # Wipe local objects to simulate a fresh machine that only has the remote copy.
        shutil.rmtree(root / ".av" / "objects", ignore_errors=True)
        (root / ".av" / "objects").mkdir()

        start = time.perf_counter()
        r1 = subprocess.run([av_path, "fetch", "--layer", data_layer["name"], "model.safetensors"], cwd=root)
        layer_ms = (time.perf_counter() - start) * 1000
        if r1.returncode != 0:
            layer_ms = None

        # Reset again, then time fetching the whole checkpoint (every layer) via `av fetch`
        # with no --layer restriction -- the real CLI surface for the "whole checkpoint" row.
        shutil.rmtree(root / ".av" / "objects", ignore_errors=True)
        (root / ".av" / "objects").mkdir()
        start = time.perf_counter()
        r2 = subprocess.run([av_path, "fetch", "model.safetensors"], cwd=root)
        whole_ms = (time.perf_counter() - start) * 1000
        if r2.returncode != 0:
            whole_ms = None

        return {"layer": layer_ms, "whole": whole_ms}


def _bench_git_lfs() -> float | None:
    git_path = shutil.which("git")
    git_lfs = shutil.which("git-lfs")
    if git_path is None or git_lfs is None:
        return None
    with tempfile.TemporaryDirectory(prefix="bench-fetch-gitlfs-", ignore_cleanup_errors=True) as tmp:
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
    with tempfile.TemporaryDirectory(prefix="bench-fetch-dvc-", ignore_cleanup_errors=True) as tmp:
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


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for_health(url: str, timeout_s: float = 120.0) -> bool:
    # mlflow's own CLI has serious import/startup latency on this reference machine --
    # even `mlflow --help` took well over 30s to return -- so `mlflow server` needs a much
    # more generous health-check window than a typical subprocess-based service would.
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"{url}/health", timeout=2) as resp:
                if resp.status == 200:
                    return True
        except (urllib.error.URLError, OSError, ValueError):
            pass
        time.sleep(0.3)
    return False


def _bench_mlflow() -> float | None:
    mlflow_bin = shutil.which("mlflow")
    if mlflow_bin is None:
        return None
    import mlflow

    root = Path(tempfile.mkdtemp(prefix="bench-fetch-mlflow-"))
    server_proc = None
    try:
        fixture_dir = root / "fixture"
        fixture_dir.mkdir()
        _make_checkpoint(fixture_dir / "model.safetensors")

        # A real `mlflow server` over HTTP -- not the local sqlite+file-store shortcut --
        # so the "whole checkpoint" row compares a network fetch to av's own real network
        # fetch (`av fetch`) and Git LFS/DVC's real `pull`, instead of a local-disk copy
        # that would always look artificially fast for reasons unrelated to fetch speed.
        port = _free_port()
        tracking_url = f"http://127.0.0.1:{port}"
        backend_uri = f"sqlite:///{root / 'mlflow.db'}"
        artifacts_dir = root / "mlartifacts"
        artifacts_dir.mkdir()
        server_proc = subprocess.Popen(
            [mlflow_bin, "server", "--host", "127.0.0.1", "--port", str(port),
             "--backend-store-uri", backend_uri,
             "--default-artifact-root", str(artifacts_dir),
             "--serve-artifacts"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        if not _wait_for_health(tracking_url):
            return None

        mlflow.set_tracking_uri(tracking_url)
        experiment_id = mlflow.create_experiment("bench-fetch")
        with mlflow.start_run(experiment_id=experiment_id) as run_obj:
            mlflow.log_artifact(str(fixture_dir / "model.safetensors"))  # HTTP upload
            run_id = run_obj.info.run_id

        download_dest = root / "downloaded"
        start = time.perf_counter()
        mlflow.artifacts.download_artifacts(  # HTTP download -- the fair comparison
            run_id=run_id, artifact_path="model.safetensors", dst_path=str(download_dest),
        )
        return (time.perf_counter() - start) * 1000
    finally:
        if server_proc is not None:
            server_proc.terminate()
            try:
                server_proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                server_proc.kill()
                server_proc.wait(timeout=5)
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
        # claim_scope="unique": no competitor has sub-file granularity at all, so this row
        # is judged on "does av have a real number", not a competitor comparison (see
        # BenchmarkResult.claim_scope's docstring, tool_runner.py). "whole checkpoint"
        # inherits the result's default "speed" scope -- it's an ordinary head-to-head.
        Row(operation="fetch single layer", values=layer_values, statuses=layer_statuses,
            unit="ms", notes=layer_notes, claim_scope="unique"),
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
