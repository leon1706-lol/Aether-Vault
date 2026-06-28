"""Benchmark #9 — garbage collection throughput.

`av gc` is a remote-CAS-server operation (POST /api/admin/gc) with no equivalent primitive
in Git LFS/DVC/MLflow's storage models — none of the three competitors has a server-side
garbage-collection concept comparable to Aether's CAS reclaiming dangling objects. Per the
same reasoning already applied to bench_concurrent_push.py, this is scoped Aether-only; all
three competitor columns are N/A (see BENCHMARKS.md methodology notes).

Drives the real `av` CLI as subprocesses (init/add/commit/push), then times `av gc` —
never the server's internal GC function directly, so this measures the same path a real
user's `av gc` invocation would take.
"""

import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from av_cli import speedcheck  # noqa: E402
from av_cli.client import VaultClient  # noqa: E402

from benchmarks.tool_runner import BenchmarkResult, Row, ToolStatus, time_subprocess  # noqa: E402

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

    with tempfile.TemporaryDirectory(prefix="bench-gc-av-") as tmp:
        root = Path(tmp)
        speedcheck.run_av_cli_probes(av_path, root)  # init + add + commit a small fixture
        for i in range(GC_OBJECT_COUNT):
            (root / f"gc_obj_{i}.bin").write_bytes(os.urandom(GC_OBJECT_SIZE_BYTES))
        time_subprocess([av_path, "add", "."], root)
        time_subprocess([av_path, "commit", "-m", "bench gc fixture"], root)
        time_subprocess([av_path, "push"], root)
        return time_subprocess([av_path, "gc"], root)


def run(tool_order: list[str] | None = None) -> BenchmarkResult:
    tool_order = tool_order or ["av", "git-lfs", "dvc", "mlflow"]

    av_value = _bench_av()
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
    )


if __name__ == "__main__":
    from benchmarks.tool_runner import print_table
    print_table(run())
