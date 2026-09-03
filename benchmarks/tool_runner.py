"""Shared infra for the `av benchmark` suite: tool detection, result rating, and table
printing. Used by every bench_*.py script and the `av benchmark` Click command.

Mirrors the "skip and label, never fabricate" pattern already established by
scripts/run_benchmark_comparison.py and av_cli.speedcheck's --speed diagnostics: a tool
that isn't on PATH gets labeled NOT_INSTALLED, and a benchmark whose primitive simply
doesn't map onto a given tool gets labeled NOT_APPLICABLE (set explicitly per-benchmark,
never inferred), instead of guessing at a number for either case.
"""

import datetime
import os
import platform
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
    # v1.3.0 (Probleme.md): distinct from NOT_INSTALLED. A benchmark that reached a real,
    # reachable tool/server but had the operation itself fail (e.g. a connection reset under
    # concurrent load) must say so honestly rather than claim the tool "wasn't installed" —
    # that was actively misleading on a real capture where av was installed and the server
    # was up the whole time.
    FAILED = "failed"


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
    label = "N/A" if status == ToolStatus.NOT_APPLICABLE else status.value
    return label + (f" ({note})" if with_note and note else "")


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


# Narrative, not data-derived — explains *why* certain cells are N/A or non-monotonic, not
# something that changes between captures. Kept as a hand-edited constant (update by hand
# when methodology genuinely changes) but always included by render_doc_header()'s caller,
# so a `--markdown` run never again silently drops this section the way a bare-tables-only
# write used to (the staleness that motivated this whole module).
METHODOLOGY_NOTES = """## Methodology notes (resolved open questions)

- **Hashing throughput, MLflow column:** MLflow has no exposed file-hashing primitive
  comparable to `dvc add`/`git lfs clean`/`av`'s hasher — `log_artifact()` copies/uploads a
  file but doesn't expose a hash step a caller can time independently. Marked N/A rather than
  approximated, so as not to misrepresent what MLflow actually does.
- **Concurrent push, competitor columns:** Aether has a real multi-tenant FastAPI server
  (Postgres+Redis-backed) that N clients push to concurrently. DVC and Git LFS push to a
  remote with no app-server tier (concurrency there is filesystem/object-store writes, not
  server contention), and MLflow's tracking server maps onto a different workflow entirely.
  Rather than approximate three non-equivalent setups, v1 scopes this to an Aether-only load
  test; the other three columns are N/A.
- **GC throughput, competitor columns:** same reasoning as concurrent push — `av gc` is a
  remote-CAS-server operation with no equivalent in Git LFS/DVC/MLflow's storage models, so
  all three competitor columns are N/A rather than approximated.
- **Cold clone, `av` column:** `av clone <project>` has existed since v1.1.1 — this note
  used to say the command didn't exist at all; that was true when this benchmark suite was
  first built and is stale now. The measured number here is a real, live `av clone` against
  a running registry (`benchmarks/bench_cold_clone.py`), timing exactly what Git LFS's
  `git clone` + `git lfs pull` and DVC's `git clone` + `dvc pull` measure for their own
  columns — a second machine materializing a fresh copy of a project someone else pushed.
- **Partial-checkpoint fetch, "fetch whole checkpoint" row:** MLflow's number here is a
  local-filesystem artifact store, not a network round trip like av/Git LFS's real HTTP
  fetch or DVC's local-dir remote pull — faster for that reason, not because MLflow's actual
  remote-artifact-store fetch path would be.

"""


def _tool_version(which_path: str | None, version_args: list[str]) -> str:
    """Runs `<tool> <version_args>` and extracts a bare X.Y.Z version number from whichever
    of stdout/stderr has output — each tool's raw banner is noisy (e.g. git-lfs's includes
    platform/go-toolchain info), so a regex pulls out just the number to match the doc's
    existing "git-lfs 3.7.1, dvc 3.67.1, ..." style rather than dumping the whole banner."""
    if which_path is None:
        return "not installed"
    import re
    try:
        result = subprocess.run([which_path, *version_args], capture_output=True, text=True, timeout=10)
        raw = (result.stdout or result.stderr).strip()
        match = re.search(r"\d+\.\d+\.\d+", raw)
        return match.group(0) if match else (raw.splitlines()[0] if raw else "unknown version")
    except Exception:
        return "unknown version"


def detect_tool_versions() -> dict[str, str]:
    """Best-effort version string per tool for the doc header's Captured line.

    Mirrors detect_tool()'s "skip and label, never fabricate" pattern — a tool not on PATH
    reports "not installed" rather than an empty/guessed string. `av`'s own version comes
    from its installed package metadata (it has no `--version` CLI flag), not a subprocess.
    """
    tools = detect_tools()
    versions = {"av": "not installed"}
    if tools["av"].status == ToolStatus.AVAILABLE:
        try:
            from av_cli import __version__ as av_version
            versions["av"] = av_version
        except ImportError:
            versions["av"] = "unknown version"
    version_args = {
        "git-lfs": ["version"],
        "dvc": ["--version"],
        "mlflow": ["--version"],
    }
    for name in ("git-lfs", "dvc", "mlflow"):
        versions[name] = _tool_version(tools[name].path, version_args[name])
    return versions


def _git_short_sha(repo_root: Path) -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], cwd=repo_root, capture_output=True, text=True, timeout=10
        )
        sha = result.stdout.strip()
        return sha if sha else "unknown"
    except Exception:
        return "unknown"


def _total_ram_gb() -> str:
    """Best-effort, dependency-free (no psutil) total RAM — a real reference-machine fact
    every published number here should be read against, since these are single-machine
    timings (see the Caveat line). "unknown" rather than a wrong guess when the platform-
    specific path isn't available."""
    try:
        if platform.system() == "Windows":
            import ctypes

            class _MEMSTATUS(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            status = _MEMSTATUS()
            status.dwLength = ctypes.sizeof(_MEMSTATUS)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status))
            return f"{status.ullTotalPhys / (1024 ** 3):.0f} GB"
        # Linux/macOS: /proc/meminfo exists on Linux; macOS has no equivalent without a
        # subprocess (sysctl) — try both, fall back to unknown rather than guess.
        meminfo = Path("/proc/meminfo")
        if meminfo.exists():
            for line in meminfo.read_text().splitlines():
                if line.startswith("MemTotal:"):
                    kb = int(line.split()[1])
                    return f"{kb / (1024 ** 2):.0f} GB"
        result = subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True, text=True, timeout=5)
        if result.returncode == 0 and result.stdout.strip():
            return f"{int(result.stdout.strip()) / (1024 ** 3):.0f} GB"
    except Exception:
        pass
    return "unknown"


def render_machine_profile() -> str:
    """v1.3.0 (todo.md item 4): none of the numbers in this doc were reproducible before
    this existed — CPU model, core count, RAM, and OS are exactly the variables that make
    a "hash 10MB file" timing mean something different on two different machines."""
    cpu = platform.processor() or platform.machine() or "unknown"
    return f"""## Reference machine

| | |
|---|---|
| CPU | {cpu} ({os.cpu_count() or "?"} logical cores) |
| RAM | {_total_ram_gb()} |
| OS | {platform.system()} {platform.release()} ({platform.machine()}) |
| Python | {platform.python_version()} |

"""


def render_doc_header(repo_root: Path, tool_versions: dict[str, str] | None = None) -> str:
    """Generates the whole BENCHMARKS.md preamble (title, intro, Captured line, Caveat,
    Legend) so `--markdown` can write a complete, ready-to-commit file in one shot instead
    of bare tables that need the surrounding prose manually re-spliced in after every run —
    the gap that let the committed numbers drift stale from the code they're supposed to
    measure (see development/Probleme.md for the incident this fixed).
    """
    tool_versions = tool_versions if tool_versions is not None else detect_tool_versions()
    today = datetime.date.today().isoformat()
    sha = _git_short_sha(repo_root)
    versions = ", ".join(f"{name} {ver}" for name, ver in tool_versions.items() if name != "av")
    return f"""# Aether-Vault Benchmarks

Reproducible cross-tool comparison against **Git LFS**, **DVC**, and **MLflow** — generated by
`av benchmark --markdown development/BENCHMARKS.md` (see [`benchmarks/README.md`](../benchmarks/README.md)
for how to re-run it yourself). These are real, measured numbers from real subprocess/HTTP
calls to each tool — never fabricated. A tool that genuinely can't run a given benchmark
(not installed, or the benchmark's primitive doesn't map onto it) is shown as such, not
guessed at.

**Captured:** {today}, on {platform.system()}. Aether-Vault @ `{sha}`, {versions}, Python {platform.python_version()}.

**Caveat:** these are single-run, single-machine timings — disk/antivirus/OS-scheduler noise
is real. Re-run before relying on any single number for a decision. Use `av benchmark --baseline`
to track regressions across captures rather than eyeballing two snapshots of this file by hand.

{render_machine_profile()}## Legend

- **GOOD** — Aether is at least 1.5x better than the best real competitor number.
- **OK** — within 1.5x either way, or no competitor produced a real number to compare against.
- **BAD** — Aether is more than 1.5x worse than the best real competitor number.
- **N/A** — the benchmark's primitive doesn't apply to that tool at all (footnoted why).
- **not installed** — the tool wasn't found on `PATH` in the capturing environment.
- **failed** — the tool/server was reachable but the operation itself failed on this capture
  (footnoted why); re-run before treating a "failed" cell as a real regression, since it
  usually means capture-machine contention rather than a code defect.

"""


def results_to_json(results: list[BenchmarkResult]) -> dict:
    """{benchmark_name: {operation: av_value_or_None}} — a flat snapshot for --save-json,
    consumed later by compare_to_baseline() in a future run."""
    return {
        result.name: {row.operation: row.values.get("av") for row in result.rows}
        for result in results
    }


def compare_to_baseline(results: list[BenchmarkResult], baseline: dict) -> list[dict]:
    """Compares this run's `av` values against a prior results_to_json() snapshot.

    Only flags a regression where BOTH this run and the baseline have a real `av` number —
    a row that's None in either (server unreachable, tool not installed) is skipped rather
    than treated as a regression, since that's a missing-data case, not a timing signal.
    Uses the same VERDICT_THRESHOLD already used for GOOD/OK/BAD so "regression" means the
    same thing here as it does in the normal competitor-comparison verdicts.
    """
    findings = []
    for result in results:
        baseline_ops = baseline.get(result.name, {})
        for row in result.rows:
            current = row.values.get("av")
            prior = baseline_ops.get(row.operation)
            if current is None or prior is None or prior <= 0:
                continue
            ratio = current / prior
            findings.append({
                "benchmark": result.name,
                "operation": row.operation,
                "baseline": prior,
                "current": current,
                "ratio": ratio,
                "regressed": ratio > VERDICT_THRESHOLD,
            })
    return findings


def print_regression_report(findings: list[dict], echo=print) -> bool:
    """Prints a table of any row that regressed past VERDICT_THRESHOLD vs. the baseline.
    Returns True if any regression was found (caller uses this to set the exit code)."""
    regressed = [f for f in findings if f["regressed"]]
    if not findings:
        echo("\nNo comparable rows between this run and the baseline (nothing to report).")
        return False
    if not regressed:
        echo(f"\nNo regressions vs baseline ({len(findings)} row(s) compared).")
        return False
    echo(f"\n=== Regressions vs baseline ({len(regressed)} of {len(findings)} row(s)) ===")
    for f in regressed:
        echo(
            f"  [REGRESSED] {f['benchmark']} / {f['operation']}: "
            f"{f['baseline']:,.1f} -> {f['current']:,.1f} ({f['ratio']:.2f}x slower)"
        )
    return True
