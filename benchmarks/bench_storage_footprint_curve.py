"""Benchmark #7 — storage footprint over N versions.

Same fine-tune-sequence mechanism as bench_safetensors_dedup.py (#2), but reports the
growth *curve* (size after each commit) instead of just the before/after total — useful
for a visual "disk usage over time" chart rather than a single headline number.
"""

import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from benchmarks.bench_safetensors_dedup import DEFAULT_STEPS, run_finetune_sequence  # noqa: E402
from benchmarks.tool_runner import BenchmarkResult, Row, ToolStatus, detect_tools  # noqa: E402


def run(tool_order: list[str] | None = None) -> BenchmarkResult:
    tool_order = tool_order or ["av", "git-lfs", "dvc", "mlflow"]
    curves: dict[str, list[int] | None] = {}
    statuses: dict[str, ToolStatus] = {}

    for tool in ["av", "git-lfs", "dvc", "mlflow"]:
        repo = Path(tempfile.mkdtemp(prefix=f"bench-curve-{tool}-"))
        handle_status = detect_tools([tool])[tool].status
        try:
            sizes = run_finetune_sequence(tool, repo, DEFAULT_STEPS)
        except Exception as exc:
            print(f"warning: {tool} sequence failed: {exc}", file=sys.stderr)
            sizes = None
        finally:
            shutil.rmtree(repo, ignore_errors=True)
        curves[tool] = sizes
        statuses[tool] = ToolStatus.AVAILABLE if sizes is not None else (
            handle_status if handle_status != ToolStatus.AVAILABLE else ToolStatus.NOT_INSTALLED
        )

    rows = []
    for step in range(DEFAULT_STEPS):
        values = {
            tool: (float(curves[tool][step]) if curves[tool] is not None else None)
            for tool in tool_order
        }
        rows.append(Row(operation=f"after commit {step + 1}", values=values, statuses=statuses, unit="bytes"))

    return BenchmarkResult(
        name="storage_footprint_curve",
        title="Storage Footprint Over N Versions",
        description=(
            f"Cumulative on-disk storage after each of {DEFAULT_STEPS} fine-tune commits "
            "(same scenario as the safetensors_dedup benchmark, reported as a growth curve)."
        ),
        tool_order=tool_order,
        rows=rows,
    )


if __name__ == "__main__":
    from benchmarks.tool_runner import print_table
    print_table(run())
