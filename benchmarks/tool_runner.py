"""Shared infra for the `av benchmark` suite: tool detection, result rating, and table
printing. Used by every bench_*.py script and the `av benchmark` Click command.

Mirrors the "skip and label, never fabricate" pattern already established by
scripts/run_benchmark_comparison.py and av_cli.speedcheck's --speed diagnostics: a tool
that isn't on PATH gets labeled NOT_INSTALLED, and a benchmark whose primitive simply
doesn't map onto a given tool gets labeled NOT_APPLICABLE (set explicitly per-benchmark,
never inferred), instead of guessing at a number for either case.
"""

import shutil
import subprocess
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

TOOL_WHICH_NAMES = {"av": "av", "git-lfs": "git-lfs", "dvc": "dvc", "mlflow": "mlflow"}
ALL_TOOLS = ["av", "git-lfs", "dvc", "mlflow"]
COMPETITOR_TOOLS = ["git-lfs", "dvc", "mlflow"]


class ToolStatus(Enum):
    AVAILABLE = "available"
    NOT_INSTALLED = "not installed"
    NOT_APPLICABLE = "N/A"


@dataclass
class ToolHandle:
    name: str
    status: ToolStatus
    path: str | None = None


def detect_tool(name: str) -> ToolHandle:
    path = shutil.which(TOOL_WHICH_NAMES[name])
    return ToolHandle(name, ToolStatus.AVAILABLE if path else ToolStatus.NOT_INSTALLED, path)


def detect_tools(names: list[str] | None = None) -> dict[str, ToolHandle]:
    names = names if names is not None else ALL_TOOLS
    return {name: detect_tool(name) for name in names}


def time_subprocess(args: list[str], cwd: Path) -> float:
    """Times a subprocess call in milliseconds.

    Deliberately no `capture_output=True` — matches av_cli.speedcheck.run_av_cli_probes's
    calling convention, which tests/test_cli.py's `fake_run(args, cwd=None)` mock signature
    depends on (capture_output broke that mock once already).
    """
    start = time.perf_counter()
    subprocess.run(args, cwd=cwd)
    return (time.perf_counter() - start) * 1000


@dataclass
class Row:
    """One operation's results across tools, for a single benchmark."""
    operation: str
    values: dict[str, float | None]  # tool name -> number (None if no real number)
    statuses: dict[str, ToolStatus]
    unit: str = "ms"
    notes: dict[str, str] = field(default_factory=dict)  # tool name -> footnote


@dataclass
class BenchmarkResult:
    name: str  # short name, e.g. "hashing_throughput" — derived from bench_<name>.py
    title: str
    description: str
    tool_order: list[str]
    rows: list[Row]


# av must be this many times better/worse than the best real competitor number to earn a
# GOOD/BAD verdict instead of OK. Single-run, single-machine timings are noisy enough that
# a difference under 1.5x isn't a reliable signal either way (same caveat already on the
# README's existing Benchmark Comparison section).
VERDICT_THRESHOLD = 1.5


def rate(av_value: float | None, competitor_values: dict[str, float | None]) -> str:
    """Rates Aether's number against the best real competitor number. Lower is better for
    every metric in this suite (time or bytes — costs, not throughput rates).

    No real competitor number to compare against -> "ok" (nothing to claim a win against).
    """
    if av_value is None:
        return "ok"
    real_competitors = [v for v in competitor_values.values() if v is not None]
    if not real_competitors:
        return "ok"
    best = min(real_competitors)
    if best <= 0:
        return "ok"
    if av_value <= best / VERDICT_THRESHOLD:
        return "good"
    if av_value > best * VERDICT_THRESHOLD:
        return "bad"
    return "ok"


def format_value(value: float | None, status: ToolStatus, unit: str, note: str | None = None, with_note: bool = False) -> str:
    if value is not None:
        return f"{value:,.1f} {unit}"
    if status == ToolStatus.NOT_APPLICABLE:
        return "N/A" + (f" ({note})" if with_note and note else "")
    return status.value


def _verdict_for_row(row: Row, tool_order: list[str]) -> str:
    av_value = row.values.get("av")
    competitors = {t: row.values.get(t) for t in tool_order if t != "av"}
    return rate(av_value, competitors)


def print_table(result: BenchmarkResult, echo=print) -> None:
    echo(f"\n=== {result.title} ===")
    echo(result.description)
    col_w = 16
    header = f"{'Operation':<28}" + "".join(f"{t:>{col_w}}" for t in result.tool_order) + f"{'Verdict':>10}"
    echo(header)
    echo("-" * len(header))
    footnotes: dict[str, str] = {}
    for row in result.rows:
        verdict = _verdict_for_row(row, result.tool_order)
        cells = "".join(
            f"{format_value(row.values.get(t), row.statuses.get(t, ToolStatus.NOT_INSTALLED), row.unit):>{col_w}}"
            for t in result.tool_order
        )
        echo(f"{row.operation:<28}{cells}{verdict.upper():>10}")
        for t, note in row.notes.items():
            if note:
                footnotes[t] = note
    for t, note in footnotes.items():
        echo(f"* {t}: {note}")


def result_to_markdown(result: BenchmarkResult) -> str:
    lines = [f"## {result.title}", "", result.description, ""]
    header = "| Operation | " + " | ".join(result.tool_order) + " | Verdict |"
    sep = "|---|" + "---:|" * len(result.tool_order) + "---|"
    lines += [header, sep]
    for row in result.rows:
        verdict = _verdict_for_row(row, result.tool_order)
        cells = " | ".join(
            format_value(row.values.get(t), row.statuses.get(t, ToolStatus.NOT_INSTALLED), row.unit, row.notes.get(t), with_note=True)
            for t in result.tool_order
        )
        lines.append(f"| {row.operation} | {cells} | {verdict.upper()} |")
    lines.append("")
    return "\n".join(lines)
