"""Benchmark #9 — garbage collection throughput.

`av gc` is a remote-CAS-server operation with no equivalent in Git LFS/DVC/MLflow's
storage models, so this is scoped Aether-only (see BENCHMARKS.md methodology); all three
competitor columns are N/A, and the whole benchmark is excluded from the "faster in every
published domain" claim (`claim_scope="internal"`) rather than silently counted as a pass.

Drives the real `av` CLI as subprocesses, then times `av gc` -- the same path a real
user's invocation would take, never the server's internal GC function directly. Note on
`repeat`: each run's `av gc` sweeps the *entire* shared registry, so with `repeat > 1` later
runs see a bigger accumulated registry than earlier ones (whatever this and every other
benchmark in the same `av benchmark` invocation has pushed) -- the median still reduces
scheduler/disk noise, but it isn't a clean "identical independent trials" repeat the way
e.g. hashing_throughput's is.
"""

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from av_cli import speedcheck  # noqa: E402
from av_cli.client import VaultClient  # noqa: E402

from benchmarks.tool_runner import (  # noqa: E402
    BenchmarkResult,
    Row,
    ToolStatus,
    repeat_median,
    time_subprocess,
)

GC_OBJECT_COUNT = 20
GC_OBJECT_SIZE_BYTES = 4096


def _bench_av() -> float | None:
    av_path = shutil.which("av")
    if av_path is None:
        return None

    client = VaultClient()
    server_up = client.server_available()
    client.close()
    if not server_up:
        return None

    with tempfile.TemporaryDirectory(prefix="bench-gc-av-", ignore_cleanup_errors=True) as tmp:
        root = Path(tmp)
        # Untimed setup fixture -- populate + init/add/commit, never via run_av_cli_probes
        # (that times an extra `av --version` this benchmark has no use for; see Probleme.md
        # on the probe-index bug that mapping-by-label, not by position, now guards against).
        speedcheck.populate_cli_fixture(root)
        subprocess.run([av_path, "init", "--mode", "local", "--yes", "--no-repl"],
                       cwd=root, env={**os.environ, "AV_NO_UPDATE_CHECK": "1"})
        subprocess.run([av_path, "add", "."], cwd=root)
        subprocess.run([av_path, "commit", "-m", "speedcheck"], cwd=root)
        speedcheck.await_daemon(av_path, root)  # untimed setup already triggered auto-spawn
        for i in range(GC_OBJECT_COUNT):
            (root / f"gc_obj_{i}.bin").write_bytes(os.urandom(GC_OBJECT_SIZE_BYTES))
        time_subprocess([av_path, "add", "."], root)
        time_subprocess([av_path, "commit", "-m", "bench gc fixture"], root)
        time_subprocess([av_path, "push"], root)
        gc_ms = time_subprocess([av_path, "gc"], root)
        subprocess.run([av_path, "daemon", "stop"], cwd=root)
        return gc_ms


def run(tool_order: list[str] | None = None, repeat: int = 1) -> BenchmarkResult:
    tool_order = tool_order or ["av", "git-lfs", "dvc", "mlflow"]

    av_value = repeat_median(_bench_av, repeat)
    values: dict[str, float | None] = {"av": av_value}
    statuses: dict[str, ToolStatus] = {
        "av": ToolStatus.AVAILABLE if av_value is not None else ToolStatus.NOT_INSTALLED
    }
    notes: dict[str, str] = {}
    if av_value is None:
        notes["av"] = "no av_server reachable, or av not on PATH"

    for tool in ("git-lfs", "dvc", "mlflow"):
        values[tool] = None
        statuses[tool] = ToolStatus.NOT_APPLICABLE
        notes[tool] = "no comparable server-side garbage-collection primitive (see BENCHMARKS.md methodology)"

    row = Row(
        operation=f"gc after {GC_OBJECT_COUNT} objects",
        values=values,
        statuses=statuses,
        unit="ms",
        notes=notes,
    )

    return BenchmarkResult(
        name="gc_throughput",
        title="Garbage Collection Throughput",
        description=(
            f"Time to run `av gc` on the remote CAS server after committing and pushing "
            f"{GC_OBJECT_COUNT} small objects from a real fixture. Aether-only — see "
            "methodology note for why the other three are N/A."
        ),
        tool_order=tool_order,
        rows=[row],
        claim_scope="internal",
    )


if __name__ == "__main__":
    from benchmarks.tool_runner import print_table
    print_table(run())
