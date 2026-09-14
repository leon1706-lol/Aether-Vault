"""Shared infra for the `av benchmark` suite: tool detection, result rating, and table
printing. Used by every bench_*.py script and the `av benchmark` Click command.

A tool not on PATH is labeled NOT_INSTALLED; a benchmark whose primitive doesn't map onto
a tool is labeled NOT_APPLICABLE -- never guessed at.
"""

import datetime
import os
import platform
import shutil
import statistics
import subprocess
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable, TypeVar

T = TypeVar("T")

TOOL_WHICH_NAMES = {"av": "av", "git-lfs": "git-lfs", "dvc": "dvc", "mlflow": "mlflow"}
ALL_TOOLS = ["av", "git-lfs", "dvc", "mlflow"]
COMPETITOR_TOOLS = ["git-lfs", "dvc", "mlflow"]


class ToolStatus(Enum):
    AVAILABLE = "available"
    NOT_INSTALLED = "not installed"
    NOT_APPLICABLE = "N/A"
    FAILED = "failed"  # distinct from NOT_INSTALLED: a reachable tool whose operation itself failed


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


#: Peak-RSS samples recorded by `time_subprocess(..., rss_key=...)`, drained per key by
#: `pop_rss_median()` when a benchmark fills its `Row.rss_mb`. Process-global on purpose:
#: the `_bench_av()` helpers that time subprocesses return only floats, and threading a
#: second return value through every one of them would touch every benchmark for no gain.
_RSS_SAMPLES: dict[str, list[float]] = {}


def record_rss(key: str, peak_mb: float | None) -> None:
    if peak_mb is not None:
        _RSS_SAMPLES.setdefault(key, []).append(peak_mb)


def pop_rss_median(key: str) -> float | None:
    samples = _RSS_SAMPLES.pop(key, [])
    return round(statistics.median(samples), 1) if samples else None


def time_subprocess(args: list[str], cwd: Path, *, repeat: int = 1, env: dict | None = None,
                    rss_key: str | None = None) -> float:
    """Times a subprocess call in milliseconds, as the median of `repeat` runs (default 1 =
    a single timing, unchanged from before). Deliberately no `capture_output=True` -- matches
    speedcheck.run_av_cli_probes's calling convention, which a test mock depends on. `env`
    merges over the current process environment for every run (e.g. AV_NO_DAEMON=1 for a
    `--no-daemon` capture). `rss_key` additionally records each run's peak RSS (whole
    process tree, via `av_cli.sysres`) under that key for `pop_rss_median()` -- the timing
    path and return value are unchanged when it is None, which is what the test doubles for
    `subprocess.run` rely on."""
    # `env` only added to the subprocess.run() kwargs when actually given -- plain omission
    # is equivalent to `env=None` for a real subprocess (both mean "inherit the current
    # environment"), and some test doubles don't expect an `env=` keyword at all.
    run_kwargs: dict = {"cwd": cwd}
    if env:
        run_kwargs["env"] = {**os.environ, **env}
    samples = []
    for _ in range(max(1, repeat)):
        if rss_key is not None:
            from av_cli import sysres

            measured = sysres.run_measured(args, **run_kwargs)
            samples.append(measured.elapsed_ms)
            record_rss(rss_key, measured.peak_rss_mb)
            continue
        start = time.perf_counter()
        subprocess.run(args, **run_kwargs)
        samples.append((time.perf_counter() - start) * 1000)
    return statistics.median(samples)


def time_call(fn: Callable[[], object], *, repeat: int = 1) -> float:
    """In-process equivalent of time_subprocess -- median of `repeat` calls to a zero-arg
    callable, in milliseconds."""
    samples = []
    for _ in range(max(1, repeat)):
        start = time.perf_counter()
        fn()
        samples.append((time.perf_counter() - start) * 1000)
    return statistics.median(samples)


def repeat_median(fn: Callable[[], T], repeat: int = 1) -> T:
    """Calls a zero-arg bench function `repeat` times -- each call is expected to be a
    fully independent run (its own fresh tempdir / fresh `git init` / fresh remote), which
    is how every `_bench_<tool>()` function in this package is already written -- and
    returns a value with the same shape as one call, with every leaf timing replaced by the
    median across runs. Stops after one call if that call already returned None (a tool
    that isn't installed doesn't fluctuate, so there's nothing to gain by re-running it).
    Works for both a single float and a `{operation: float | None}` dict return shape."""
    first = fn()
    if first is None or repeat <= 1:
        return first
    samples = [first] + [fn() for _ in range(repeat - 1)]
    real = [s for s in samples if s is not None]
    if not real:
        return None
    if isinstance(real[0], dict):
        merged: dict = {}
        for key in real[0]:
            values = [s[key] for s in real if s.get(key) is not None]
            merged[key] = statistics.median(values) if values else None
        return merged
    return statistics.median(real)


@dataclass
class Row:
    """One operation's results across tools, for a single benchmark."""
    operation: str
    values: dict[str, float | None]  # tool name -> number (None if no real number)
    statuses: dict[str, ToolStatus]
    unit: str = "ms"
    notes: dict[str, str] = field(default_factory=dict)  # tool name -> footnote
    # Overrides the owning BenchmarkResult.claim_scope for just this row -- e.g.
    # partial_checkpoint_fetch's "fetch single layer" row is a unique-capability domain even
    # though "fetch whole checkpoint" in the same result is an ordinary speed domain. None
    # (the common case) means "inherit the result's own claim_scope".
    claim_scope: str | None = None
    # tool name -> peak RSS (MB) of the timed subprocess tree, when a benchmark measured it
    # (V1.6.3). Empty for tools/rows without a measurement; rendered as an extra column only
    # when at least one row in the result carries a number.
    rss_mb: dict[str, float | None] = field(default_factory=dict)


#: A benchmark's place in the "faster in every published domain" claim (todo.md's V1.6.0
#: brief): "speed"/"efficiency" rows must be GOOD or OK for the claim to hold; "unique" rows
#: (no competitor primitive exists at all) just need a real number; "internal" rows (no fair
#: competitor exists today -- concurrent_push, gc_throughput) are excluded from the claim
#: outright rather than silently counted as a pass.
CLAIM_SCOPES = ("speed", "efficiency", "unique", "internal")


@dataclass
class BenchmarkResult:
    name: str  # short name, e.g. "hashing_throughput" — derived from bench_<name>.py
    title: str
    description: str
    tool_order: list[str]
    rows: list[Row]
    claim_scope: str = "speed"  # one of CLAIM_SCOPES; see its docstring

    def __post_init__(self) -> None:
        if self.claim_scope not in CLAIM_SCOPES:
            raise ValueError(f"claim_scope must be one of {CLAIM_SCOPES}, got {self.claim_scope!r}")


# av must be this many times better/worse than the best real competitor number to earn a
# GOOD/BAD verdict instead of OK -- single-run timings are too noisy for a smaller margin.
VERDICT_THRESHOLD = 1.5


def rate(av_value: float | None, competitor_values: dict[str, float | None]) -> str:
    """Rates Aether's number against the best real competitor number (lower is better for
    every metric here). No real competitor number to compare against -> "ok"."""
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


def _has_rss(result: BenchmarkResult) -> bool:
    return any(v is not None for row in result.rows for v in row.rss_mb.values())


def format_rss(value: float | None) -> str:
    return "—" if value is None else f"{value:,.0f} MB"


def print_table(result: BenchmarkResult, echo=print) -> None:
    echo(f"\n=== {result.title} ===")
    echo(result.description)
    if result.claim_scope == "internal":
        echo("(internal-only — excluded from the every-domain claim; see METHODOLOGY_NOTES)")
    col_w = 16
    with_rss = _has_rss(result)
    header = f"{'Operation':<28}" + "".join(f"{t:>{col_w}}" for t in result.tool_order) + f"{'Verdict':>10}"
    if with_rss:
        header += f"{'av peak RSS':>14}"
    echo(header)
    echo("-" * len(header))
    footnotes: dict[str, str] = {}
    for row in result.rows:
        verdict = _verdict_for_row(row, result.tool_order)
        cells = "".join(
            f"{format_value(row.values.get(t), row.statuses.get(t, ToolStatus.NOT_INSTALLED), row.unit):>{col_w}}"
            for t in result.tool_order
        )
        rss = f"{format_rss(row.rss_mb.get('av')):>14}" if with_rss else ""
        echo(f"{row.operation:<28}{cells}{verdict.upper():>10}{rss}")
        for t, note in row.notes.items():
            if note:
                footnotes[t] = note
    for t, note in footnotes.items():
        echo(f"* {t}: {note}")


def result_to_markdown(result: BenchmarkResult) -> str:
    description = result.description
    if result.claim_scope == "internal":
        description += " **(internal-only — excluded from the every-domain claim.)**"
    lines = [f"## {result.title}", "", description, ""]
    with_rss = _has_rss(result)
    header = "| Operation | " + " | ".join(result.tool_order) + " | Verdict |" + (" av peak RSS |" if with_rss else "")
    sep = "|---|" + "---:|" * len(result.tool_order) + "---|" + ("---:|" if with_rss else "")
    lines += [header, sep]
    for row in result.rows:
        verdict = _verdict_for_row(row, result.tool_order)
        cells = " | ".join(
            format_value(row.values.get(t), row.statuses.get(t, ToolStatus.NOT_INSTALLED), row.unit, row.notes.get(t), with_note=True)
            for t in result.tool_order
        )
        rss = f" {format_rss(row.rss_mb.get('av'))} |" if with_rss else ""
        lines.append(f"| {row.operation} | {cells} | {verdict.upper()} |{rss}")
    lines.append("")
    return "\n".join(lines)


# Narrative, not data-derived -- explains *why* certain cells are N/A or non-monotonic.
# Hand-edited; update when methodology genuinely changes.
METHODOLOGY_NOTES = """## Methodology notes (resolved open questions)

- **av numbers run with the product default: native launcher + auto-spawned daemon.** Each
  av benchmark does an untimed warm-up `av` invocation as part of its setup and waits (up to
  15s) for `av daemon status` to report running before timing anything — mirroring how a
  user's *second* command in a session behaves, the same way Git LFS's own long-running
  `filter-process` is already warm by the time `git add` is timed. `av benchmark
  --no-daemon` (or `AV_NO_DAEMON=1` in the calling shell) captures the cold, no-daemon
  numbers instead — published separately in `development/BENCHMARKS-cold.md` — and the
  **av daemon:** line above states which mode produced this file. Git LFS and DVC have no
  comparable warm-process primitive of their own to match against; each runs with its own
  defaults.
- **Commit + Push Latency, "commit" row is local-only; "push" row carries the real upload.**
  `av commit`'s default behavior — uploading synchronously — is unchanged by this note; the
  *benchmark* times `av commit --no-upload` for the commit row specifically so it measures
  the same thing DVC's and Git LFS's own "commit" numbers do (neither ever touches a
  network on commit — their upload is a separate `dvc push`/already happened in `git add`'s
  LFS clean filter). The real upload — batch-check, parallel object upload, push_commit,
  ref update — is what the "push" row measures, directly comparable to `dvc push`.
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
- **Partial-checkpoint fetch, "fetch whole checkpoint" row:** MLflow's number now comes
  from a real `mlflow server` over HTTP (`--serve-artifacts`), not its local-filesystem
  artifact-store shortcut — `log_artifact`/`download_artifacts` are genuine network round
  trips, the same fairness bar av (`av fetch`), Git LFS (`git lfs pull`), and DVC
  (`dvc pull`) are already held to. av's own numbers go through the real `av fetch`/
  `av fetch --layer` CLI, paying process startup like every competitor's own subprocess.
  "fetch single layer" is scoped `claim_scope="unique"` (see `tool_runner.BenchmarkResult`)
  since no competitor has sub-file granularity to compare against at all.

"""


def _row_pass(row: Row, result: "BenchmarkResult") -> bool | None:
    """Whether one row satisfies its own claim scope (the row's own `claim_scope`,
    falling back to the owning result's). Returns None for a row scoped "internal" --
    excluded from the claim rather than counted either way."""
    scope = row.claim_scope or result.claim_scope
    if scope == "internal":
        return None
    if scope == "unique":
        return row.values.get("av") is not None
    return _verdict_for_row(row, result.tool_order) in ("good", "ok")


def render_claim_summary(results: list["BenchmarkResult"]) -> str:
    """Machine-derived "Claim status" table -- one row per benchmark that has at least one
    non-internal row, plus a final every-domain line. A "speed"/"efficiency" row passes when
    it rates GOOD or OK against the best real competitor; a "unique" row passes when it
    carries a real av number (no competitor primitive to compare against by definition). A
    benchmark passes only if every one of its non-internal rows does. Rows/benchmarks scoped
    "internal" (no fair competitor exists at all -- concurrent_push, gc_throughput) are
    listed separately and never count toward the claim either way -- this is what keeps the
    claim honest instead of a hand-maintained line that can drift from the tables below it."""
    lines = ["## Claim status", "", "| # | Benchmark | Scope | Status |", "|---|---|---|---|"]
    all_pass = True
    n = 0
    fully_internal = []
    for result in results:
        row_verdicts = [v for v in (_row_pass(row, result) for row in result.rows) if v is not None]
        if not row_verdicts:
            fully_internal.append(result.title)
            continue
        n += 1
        passed = all(row_verdicts)
        all_pass = all_pass and passed
        scope_label = result.claim_scope if len({r.claim_scope or result.claim_scope for r in result.rows}) == 1 else "mixed"
        lines.append(f"| {n} | {result.title} | {scope_label} | {'PASS' if passed else 'FAIL'} |")

    if fully_internal:
        lines.append("")
        lines.append(f"Internal-only (excluded from the claim): {', '.join(fully_internal)}.")

    lines.append("")
    lines.append(f"**Faster in every published domain: {'YES' if all_pass else 'NO'}**")
    lines.append("")
    return "\n".join(lines)


def _tool_version(which_path: str | None, version_args: list[str]) -> str:
    """Runs `<tool> <version_args>` and extracts a bare X.Y.Z version number from whichever
    of stdout/stderr has output -- each tool's raw banner is noisy, so a regex pulls out
    just the number."""
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
    """Best-effort version string per tool for the doc header's Captured line. `av`'s own
    version comes from its installed package metadata, since it has no `--version` flag."""
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
    """Best-effort, dependency-free (no psutil) total RAM. Returns "unknown" rather than
    a wrong guess when the platform-specific path isn't available."""
    try:
        from av_cli import sysres

        total_mb = sysres.total_ram_mb()
        if total_mb:
            return f"{total_mb / 1024:.0f} GB"
    except Exception:
        pass
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
        # /proc/meminfo exists on Linux; macOS needs sysctl instead -- try both.
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
    """CPU model, core count, RAM, and OS -- the variables that make a "hash 10MB file"
    timing mean something different on two different machines."""
    cpu = platform.processor() or platform.machine() or "unknown"
    return f"""## Reference machine

| | |
|---|---|
| CPU | {cpu} ({os.cpu_count() or "?"} logical cores) |
| RAM | {_total_ram_gb()} |
| OS | {platform.system()} {platform.release()} ({platform.machine()}) |
| Python | {platform.python_version()} |

"""


def daemon_mode_label() -> str:
    """Whether this process would launch av subprocesses with the daemon fast-path
    available (the product default) or suppressed. Benchmarks call this to detect an
    `AV_NO_DAEMON` leaking in from the calling shell -- capturing "official" numbers with
    the daemon accidentally off would understate av and is exactly the kind of methodology
    drift this module exists to prevent silently."""
    return "off (AV_NO_DAEMON set)" if os.environ.get("AV_NO_DAEMON") else "warm (default)"


def render_doc_header(
    repo_root: Path,
    tool_versions: dict[str, str] | None = None,
    *,
    repeat: int = 1,
    daemon_mode: str | None = None,
) -> str:
    """Generates the whole BENCHMARKS.md preamble (title, intro, Captured line, Caveat,
    Legend) so `--markdown` writes a complete, ready-to-commit file in one shot instead of
    bare tables needing the surrounding prose re-spliced in by hand."""
    tool_versions = tool_versions if tool_versions is not None else detect_tool_versions()
    today = datetime.date.today().isoformat()
    sha = _git_short_sha(repo_root)
    versions = ", ".join(f"{name} {ver}" for name, ver in tool_versions.items() if name != "av")
    daemon_mode = daemon_mode if daemon_mode is not None else daemon_mode_label()
    run_note = "a single run" if repeat <= 1 else f"the median of {repeat} runs"
    return f"""# Aether-Vault Benchmarks

Reproducible cross-tool comparison against **Git LFS**, **DVC**, and **MLflow** — generated by
`av benchmark --markdown development/BENCHMARKS.md` (see [`benchmarks/README.md`](../benchmarks/README.md)
for how to re-run it yourself). These are real, measured numbers from real subprocess/HTTP
calls to each tool — never fabricated. A tool that genuinely can't run a given benchmark
(not installed, or the benchmark's primitive doesn't map onto it) is shown as such, not
guessed at.

**Captured:** {today}, on {platform.system()}. Aether-Vault @ `{sha}`, {versions}, Python {platform.python_version()}.
**av daemon:** {daemon_mode}. **Timing:** each number is {run_note}.
**Memory:** an `av peak RSS` column, where present, is the peak resident set of the timed
`av` process tree (Windows: the kernel's exact PeakWorkingSet; POSIX: sampled every 20 ms),
median across runs. With the daemon warm (the default) that tree is the launcher *client*
(~4 MB) — the resident daemon's own RSS is `av daemon status`'s `rss_mb`; a `--no-daemon`
capture measures the full in-process CLI instead. Budgets: `development/MEMORY.md`.

**Caveat:** these are single-machine timings — disk/antivirus/OS-scheduler noise is real.
Re-run before relying on any single number for a decision. Use `av benchmark --baseline`
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
    consumed later by compare_to_baseline() in a future run. Peak RSS rides along under
    the `_rss_mb` key only when some row measured it; `compare_to_baseline` walks this
    run's results (never the baseline's keys), so the underscore key is inert there."""
    doc = {
        result.name: {row.operation: row.values.get("av") for row in result.rows}
        for result in results
    }
    rss = {
        result.name: {row.operation: row.rss_mb.get("av") for row in result.rows if row.rss_mb.get("av") is not None}
        for result in results if _has_rss(result)
    }
    if rss:
        doc["_rss_mb"] = rss
    return doc


def results_to_json_full(results: list[BenchmarkResult]) -> dict:
    """Lossless dataclass → dict form (every tool's value/status/note, claim scopes, RSS),
    so `av benchmark --lowmem` can run each benchmark in its own subprocess and merge the
    results through the normal print/markdown/baseline code path."""
    return {
        "schema": "benchmark-results-1.0",
        "results": [
            {
                "name": r.name, "title": r.title, "description": r.description,
                "tool_order": list(r.tool_order), "claim_scope": r.claim_scope,
                "rows": [
                    {
                        "operation": row.operation, "unit": row.unit, "claim_scope": row.claim_scope,
                        "values": dict(row.values),
                        "statuses": {t: s.value for t, s in row.statuses.items()},
                        "notes": dict(row.notes), "rss_mb": dict(row.rss_mb),
                    }
                    for row in r.rows
                ],
            }
            for r in results
        ],
    }


def results_from_json_full(doc: dict) -> list[BenchmarkResult]:
    by_value = {s.value: s for s in ToolStatus}
    out: list[BenchmarkResult] = []
    for r in doc.get("results", []):
        rows = [
            Row(
                operation=row["operation"], values=dict(row.get("values", {})),
                statuses={t: by_value[s] for t, s in row.get("statuses", {}).items()},
                unit=row.get("unit", "ms"), notes=dict(row.get("notes", {})),
                claim_scope=row.get("claim_scope"), rss_mb=dict(row.get("rss_mb", {})),
            )
            for row in r.get("rows", [])
        ]
        out.append(BenchmarkResult(name=r["name"], title=r["title"], description=r["description"],
                                   tool_order=list(r["tool_order"]), rows=rows,
                                   claim_scope=r.get("claim_scope", "speed")))
    return out


def compare_to_baseline(results: list[BenchmarkResult], baseline: dict) -> list[dict]:
    """Compares this run's `av` values against a prior results_to_json() snapshot. Only
    flags a regression where BOTH runs have a real `av` number; uses the same
    VERDICT_THRESHOLD as the normal competitor-comparison verdicts."""
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
