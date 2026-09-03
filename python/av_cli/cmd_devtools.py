"""Development-only tooling: test/benchmark suites and the README badge sync.

Bodies moved verbatim from main.py (Point-13 split). Patch-target names owned by
main.py (`_find_source_root`, `_update_readme_test_badge`) are accessed late-bound via
`_root.<name>` so test monkeypatching on the main namespace stays effective.
"""

import importlib
import re
from .core import *  # noqa: F401,F403 -- shared prelude (stdlib + helpers)
from . import main as _root



def _print_synthetic_speed_check() -> None:
    """`av test --speed` — synthetic, repeatable benchmark of av's own hot paths."""
    with tempfile.TemporaryDirectory(prefix="av-speedcheck-") as tmp:
        probes = speedcheck.run_synthetic_probes(load_config, iter_working_files, Path(tmp))

    click.secho("\n=== Speed check (synthetic fixtures) ===", bold=True, fg="cyan")
    click.echo(f"{'Probe':<40} {'Time':>10} {'Budget':>10}  Status")
    click.echo("-" * 75)
    slow_count = 0
    for label, elapsed_ms, budget_ms in probes:
        if budget_ms is not None and elapsed_ms > budget_ms:
            slow_count += 1
            status, color = "SLOW", "yellow"
        else:
            status, color = "OK", "green"
        budget_str = f"{budget_ms:.0f} ms" if budget_ms is not None else "-"
        click.secho(f"{label:<40} {elapsed_ms:>7.1f} ms {budget_str:>10}  {status}", fg=color)

    if slow_count:
        click.echo(f"\n{slow_count} of {len(probes)} probes exceeded their budget — see python/av_cli/speedcheck.py to adjust thresholds.")

    av_path = shutil.which("av")
    if av_path is None:
        click.echo("\n(av CLI not found on PATH — skipping end-to-end CLI timing.)")
        return

    with tempfile.TemporaryDirectory(prefix="av-speedcheck-cli-") as tmp:
        cli_probes = speedcheck.run_av_cli_probes(av_path, Path(tmp))

    click.secho("\n=== Speed check (av CLI, end-to-end) ===", bold=True, fg="cyan")
    click.echo(f"{'Probe':<28} {'Time':>10}")
    click.echo("-" * 39)
    for label, elapsed_ms in cli_probes:
        click.echo(f"{label:<28} {elapsed_ms:>7.1f} ms")


def _update_readme_test_badge(passed: int, failed: int) -> None:
    """Keep README.md's `tests-N%2FM passing` badge in sync with the real pytest results.

    Only called after a full, unfiltered `av test` run (no `-k`) — a scoped subset would
    overwrite the badge with a misleadingly small total otherwise.
    """
    total = passed + failed
    if total == 0:
        return  # parse failed or nothing collected — leave the badge alone rather than zero it out
    source_root = _root._find_source_root()
    readme_path = source_root / "README.md"
    if not readme_path.is_file():
        return
    text = readme_path.read_text(encoding="utf-8")
    color = "brightgreen" if failed == 0 else "red"
    pattern = re.compile(
        r'https://img\.shields\.io/badge/tests-\d+%2F\d+%20passing-[a-z]+(\?[^"]*)"\s+alt="\d+ of \d+ tests passing"'
    )

    def _replace(m: "re.Match[str]") -> str:
        return (
            f'https://img.shields.io/badge/tests-{passed}%2F{total}%20passing-{color}{m.group(1)}"'
            f' alt="{passed} of {total} tests passing"'
        )

    updated = pattern.sub(_replace, text, count=1)
    if updated != text:
        atomic_write_text(readme_path, updated)
        click.secho(f"Updated README.md test badge: {passed}/{total} passing", fg="cyan")


@click.command(name="test")
@click.option("-k", "test_filter", default=None, help="Only run tests matching this substring (forwarded to pytest -k).")
@click.option("--cov", is_flag=True, default=False, help="Run with a coverage report (--cov=python --cov-report=term-missing).")
@click.option("--webui", "run_webui", is_flag=True, default=False, help="Also run the webui/ Vitest suite (npm test) after the Python suite.")
@click.option("--speed", "speed", is_flag=True, default=False,
              help="Also run a synthetic speed benchmark of av's hot paths (and the webui/ bench suite, with --webui).")
def test_cmd(test_filter: str | None, cov: bool, run_webui: bool, speed: bool) -> None:
    """(Development only) Run Aether-Vault's own pytest suite from source, and optionally the
    webui/ Vitest suite too.

    Requires an editable/dev install (`pip install -e .[dev]`) — and, for --webui, `npm install`
    already run inside webui/. This is not a tool for inspecting an end user's .av/ repository;
    see `av doctor` for that.
    """
    json_mode = current_output_mode() == "json"
    source_root = _root._find_source_root()
    tests_dir = source_root / "tests"
    if not tests_dir.is_dir():
        if json_mode:
            fail(None, "validation", "av test requires a development install; run from a "
                 "git clone with `pip install -e .[dev]`", command="test")
        click.secho(
            "av test requires a development install; run from a git clone with `pip install -e .[dev]`",
            fg="red",
        )
        sys.exit(1)

    args = [sys.executable, "-m", "pytest", str(tests_dir)]
    # Force color even though stdout is about to be piped (not a real tty) for output capture
    # below — otherwise pytest auto-detects the pipe and silently drops all colorization.
    args += ["--color=yes"]
    if test_filter:
        args += ["-k", test_filter]
    if cov:
        args += ["--cov=python", "--cov-report=term-missing"]
    if speed:
        args += ["--durations=20"]

    if not json_mode:
        click.secho("=== Python test suite ===", bold=True, fg="cyan")
        click.secho(f"Running Aether-Vault's test suite (pytest {' '.join(args[3:])})...", fg="cyan")
    # Stream pytest's output live (line by line, as it would print unbuffered) while also
    # collecting it, so the final "N passed, M failed" summary can be parsed afterward to keep
    # README.md's test-count badge honest without a second, redundant pytest run. In JSON mode
    # nothing streams to stdout (an agent wants one clean envelope, not scrollback mixed with
    # it) — the full text still ends up in the envelope's data.log for anyone who wants it.
    process = subprocess.Popen(
        args, cwd=source_root, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1
    )
    output_lines: list[str] = []
    assert process.stdout is not None
    for line in process.stdout:
        if not json_mode:
            click.echo(line, nl=False)
        output_lines.append(line)
    process.wait()
    exit_code = process.returncode

    passed_n = failed_n = None
    if test_filter is None:
        # Strip ANSI escapes (forced on above via --color=yes) before parsing — color codes can
        # otherwise sit between a number and "passed"/"failed" and break the regex match.
        captured = re.sub(r"\x1b\[[0-9;]*m", "", "".join(output_lines))
        passed_match = re.search(r"(\d+) passed", captured)
        failed_match = re.search(r"(\d+) failed", captured)
        error_match = re.search(r"(\d+) error", captured)
        if passed_match:
            passed_n = int(passed_match.group(1))
            failed_n = (int(failed_match.group(1)) if failed_match else 0) + (
                int(error_match.group(1)) if error_match else 0
            )
            _root._update_readme_test_badge(passed_n, failed_n)

    webui_exit = None
    if run_webui:
        webui_dir = source_root / "webui"
        if not webui_dir.is_dir() or not (webui_dir / "package.json").exists():
            msg = ("av test --webui requires a development install with the webui/ source "
                   "present (run from a git clone, not a built wheel).")
            if json_mode:
                fail(None, "validation", msg, command="test")
            click.secho(msg, fg="red")
            sys.exit(1)

        if not json_mode:
            click.secho("\n=== Web UI test suite (webui/) ===", bold=True, fg="cyan")
        # shutil.which (not a bare "npm" argv) — on Windows, `npm` resolves to `npm.cmd`, which
        # subprocess.run(["npm", ...]) frequently fails to locate/execute even when npm is
        # genuinely installed and on PATH; resolving the full path first (as `which` does, via
        # PATHEXT) avoids a false "npm not found" on a machine that actually has it.
        npm_path = shutil.which("npm")
        if npm_path is None:
            msg = ("npm not found on PATH — install Node.js to run the webui/ Vitest suite, "
                   "or omit --webui.")
            if json_mode:
                fail(None, "validation", msg, command="test")
            click.secho(msg, fg="red")
            sys.exit(1)
        # capture_output/text only passed when actually True — an always-present kwarg
        # (even =False) would change subprocess.run's call signature versus plain text
        # mode's bare call, breaking any caller/test that mocks subprocess.run with a
        # narrower (args, cwd=None) signature (see tests/test_cli.py's webui tests).
        extra = {"capture_output": True, "text": True} if json_mode else {}
        webui_result = subprocess.run([npm_path, "test"], cwd=webui_dir, **extra)
        webui_exit = webui_result.returncode
        if webui_result.returncode != 0:
            exit_code = webui_result.returncode

        if speed:
            if not json_mode:
                click.secho("\n=== Web UI speed bench (webui/) ===", bold=True, fg="cyan")
            bench_result = subprocess.run([npm_path, "run", "bench"], cwd=webui_dir, **extra)
            if bench_result.returncode != 0:
                exit_code = bench_result.returncode

    if speed and not json_mode:
        _print_synthetic_speed_check()

    if json_mode:
        emit_json(None, "test", data={
            "exit_code": exit_code, "passed": passed_n, "failed": failed_n,
            "webui_exit_code": webui_exit, "log": "".join(output_lines),
        })
        sys.exit(exit_code)

    sys.exit(exit_code)


BENCHMARK_NAMES = [
    "hashing_throughput",
    "safetensors_dedup",
    "commit_push_latency",
    "noop_status_speed",
    "cold_clone",
    "partial_checkpoint_fetch",
    "storage_footprint_curve",
    "concurrent_push",
    "gc_throughput",
]


@click.command()
@click.option("--only", "only", multiple=True,
              help=f"Only run these benchmarks by name (repeatable). Default: run all {len(BENCHMARK_NAMES)}. Names: {', '.join(BENCHMARK_NAMES)}.")
@click.option("--vs", "vs_tools", multiple=True, default=("git-lfs", "dvc", "mlflow"),
              help="Competitor tools to include (repeatable). Default: all three. Aether-Vault itself always runs.")
@click.option("--markdown", "markdown_out", type=click.Path(), default=None,
              help="Write a complete, ready-to-commit Markdown report (header/legend/methodology notes + every benchmark's table) to this path — for regenerating BENCHMARKS.md.")
@click.option("--save-json", "save_json_out", type=click.Path(), default=None,
              help="Save this run's av-only numbers as a JSON snapshot, for a future --baseline comparison.")
@click.option("--baseline", "baseline_path", type=click.Path(exists=True), default=None,
              help="Compare this run's av numbers against a prior --save-json snapshot and report any row that regressed past the 1.5x verdict threshold. Exits non-zero if any regression is found.")
def benchmark(only: tuple, vs_tools: tuple, markdown_out: str | None, save_json_out: str | None, baseline_path: str | None) -> None:
    """(Development only) Run cross-tool benchmark comparisons against DVC, Git LFS, and MLflow.

    Requires an editable/dev install (`pip install -e .[dev,benchmarks]`) — see benchmarks/README.md
    for installing DVC/MLflow as comparison targets. A tool not found on PATH is skipped and
    labeled "not installed" in the output, never given a fabricated number.
    """
    json_mode = current_output_mode() == "json"
    source_root = _root._find_source_root()
    benchmarks_dir = source_root / "benchmarks"
    if not benchmarks_dir.is_dir():
        msg = "av benchmark requires a development install; run from a git clone with `pip install -e .[dev]`"
        if json_mode:
            fail(None, "validation", msg, command="benchmark")
        click.secho(msg, fg="red")
        sys.exit(1)

    names = list(only) if only else BENCHMARK_NAMES
    invalid = [n for n in names if n not in BENCHMARK_NAMES]
    if invalid:
        msg = f"Unknown benchmark name(s): {', '.join(invalid)}. Valid names: {', '.join(BENCHMARK_NAMES)}"
        if json_mode:
            fail(None, "validation", msg, command="benchmark")
        click.secho(msg, fg="red")
        sys.exit(1)

    if str(source_root) not in sys.path:
        sys.path.insert(0, str(source_root))
    from benchmarks.tool_runner import (
        compare_to_baseline,
        print_regression_report,
        print_table,
        render_doc_header,
        result_to_markdown,
        results_to_json,
        METHODOLOGY_NOTES,
    )

    valid_competitors = {"git-lfs", "dvc", "mlflow"}
    invalid_tools = [t for t in vs_tools if t not in valid_competitors]
    if invalid_tools:
        msg = f"Unknown --vs tool(s): {', '.join(invalid_tools)}. Valid: {', '.join(sorted(valid_competitors))}"
        if json_mode:
            fail(None, "validation", msg, command="benchmark")
        click.secho(msg, fg="red")
        sys.exit(1)
    tool_order = ["av", *[t for t in vs_tools]]

    results = []
    markdown_chunks = []
    for name in names:
        module = importlib.import_module(f"benchmarks.bench_{name}")
        result = module.run(tool_order=tool_order)
        if not json_mode:
            print_table(result)
        results.append(result)
        markdown_chunks.append(result_to_markdown(result))

    if markdown_out:
        doc = render_doc_header(source_root) + METHODOLOGY_NOTES + "\n".join(markdown_chunks)
        Path(markdown_out).write_text(doc, encoding="utf-8")
        if not json_mode:
            click.echo(f"\nWrote {markdown_out}")

    if save_json_out:
        Path(save_json_out).write_text(json.dumps(results_to_json(results), indent=2), encoding="utf-8")
        if not json_mode:
            click.echo(f"Saved benchmark snapshot to {save_json_out}")

    regressed = False
    findings = None
    if baseline_path:
        baseline = json.loads(Path(baseline_path).read_text(encoding="utf-8"))
        findings = compare_to_baseline(results, baseline)
        if not json_mode:
            regressed = print_regression_report(findings)
        else:
            regressed = any(f.get("regressed") for f in findings)

    if json_mode:
        emit_json(None, "benchmark", data={
            "results": results_to_json(results), "markdown_path": markdown_out,
            "json_snapshot_path": save_json_out, "regressions": findings, "regressed": regressed,
        })
        if regressed:
            sys.exit(1)
        return

    if regressed:
        sys.exit(1)
