"""Unit tests for benchmarks/tool_runner.py — the shared verdict math, table/markdown
rendering, and the regression-tracking additions (results_to_json/compare_to_baseline).
A bug in rate()'s threshold math would silently mislabel every benchmark row, so this is
pure logic worth pinning down independently of any real bench_*.py script.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from benchmarks.tool_runner import (  # noqa: E402
    BenchmarkResult,
    Row,
    ToolStatus,
    VERDICT_THRESHOLD,
    compare_to_baseline,
    daemon_mode_label,
    format_value,
    rate,
    render_claim_summary,
    repeat_median,
    result_to_markdown,
    results_to_json,
    time_call,
    time_subprocess,
)


def test_rate_returns_good_when_av_is_more_than_threshold_times_better():
    assert rate(10.0, {"git-lfs": 16.0}) == "good"  # 16 / 1.5 = 10.67 -> av (10) <= that


def test_rate_returns_bad_when_av_is_more_than_threshold_times_worse():
    assert rate(16.0, {"git-lfs": 10.0}) == "bad"  # 10 * 1.5 = 15 -> av (16) > that


def test_rate_returns_ok_exactly_at_the_threshold_boundary():
    best = 10.0
    # Exactly best/THRESHOLD and exactly best*THRESHOLD are both inside the OK band (the
    # comparisons in rate() are <=/> , so the boundary itself counts as GOOD, just past it OK).
    assert rate(best / VERDICT_THRESHOLD, {"git-lfs": best}) == "good"
    assert rate(best / VERDICT_THRESHOLD + 0.01, {"git-lfs": best}) == "ok"
    assert rate(best * VERDICT_THRESHOLD, {"git-lfs": best}) == "ok"
    assert rate(best * VERDICT_THRESHOLD + 0.01, {"git-lfs": best}) == "bad"


def test_rate_returns_ok_when_av_value_is_none():
    assert rate(None, {"git-lfs": 10.0}) == "ok"


def test_rate_returns_ok_when_no_real_competitor_data():
    assert rate(10.0, {"git-lfs": None, "dvc": None}) == "ok"


def test_rate_returns_ok_when_best_competitor_value_is_zero_or_negative():
    assert rate(10.0, {"git-lfs": 0.0}) == "ok"


def test_format_value_renders_a_real_number_with_unit():
    assert format_value(12.345, ToolStatus.AVAILABLE, "ms") == "12.3 ms"


def test_format_value_renders_not_applicable_with_note():
    out = format_value(None, ToolStatus.NOT_APPLICABLE, "ms", note="no primitive", with_note=True)
    assert out == "N/A (no primitive)"


def test_format_value_renders_not_applicable_without_note_when_with_note_false():
    out = format_value(None, ToolStatus.NOT_APPLICABLE, "ms", note="no primitive", with_note=False)
    assert out == "N/A"


def test_format_value_renders_not_installed():
    assert format_value(None, ToolStatus.NOT_INSTALLED, "ms") == "not installed"


def test_format_value_renders_failed_with_note():
    # v1.3.0 (Probleme.md): a reachable tool/server whose operation itself failed must say
    # "failed", never "not installed" — those mean different things to a reader deciding
    # whether to re-run the capture or go looking for a missing binary.
    out = format_value(None, ToolStatus.FAILED, "ms", note="connection reset", with_note=True)
    assert out == "failed (connection reset)"


def test_format_value_renders_failed_without_note_when_with_note_false():
    out = format_value(None, ToolStatus.FAILED, "ms", note="connection reset", with_note=False)
    assert out == "failed"


def _make_result(name="bench_x", op="op1", av_value=100.0, competitor_value=50.0):
    return BenchmarkResult(
        name=name,
        title="Bench X",
        description="desc",
        tool_order=["av", "git-lfs"],
        rows=[
            Row(
                operation=op,
                values={"av": av_value, "git-lfs": competitor_value},
                statuses={"av": ToolStatus.AVAILABLE, "git-lfs": ToolStatus.AVAILABLE},
            )
        ],
    )


def test_result_to_markdown_includes_title_header_and_verdict():
    md = result_to_markdown(_make_result())
    assert "## Bench X" in md
    assert "| Operation | av | git-lfs | Verdict |" in md
    assert "op1" in md
    assert "BAD" in md  # av=100 vs git-lfs=50 -> av is 2x worse -> BAD


def test_results_to_json_extracts_av_values_keyed_by_benchmark_and_operation():
    snapshot = results_to_json([_make_result(name="bench_x", op="op1", av_value=42.0)])
    assert snapshot == {"bench_x": {"op1": 42.0}}


def test_results_to_json_keeps_none_for_a_row_with_no_real_av_value():
    result = _make_result(name="bench_y", op="op1", av_value=None)
    snapshot = results_to_json([result])
    assert snapshot == {"bench_y": {"op1": None}}


def test_compare_to_baseline_flags_a_real_regression():
    current = [_make_result(name="bench_x", op="op1", av_value=200.0)]
    baseline = {"bench_x": {"op1": 100.0}}
    findings = compare_to_baseline(current, baseline)
    assert len(findings) == 1
    assert findings[0]["regressed"] is True
    assert findings[0]["ratio"] == 2.0


def test_compare_to_baseline_does_not_flag_noise_under_the_threshold():
    current = [_make_result(name="bench_x", op="op1", av_value=110.0)]
    baseline = {"bench_x": {"op1": 100.0}}
    findings = compare_to_baseline(current, baseline)
    assert findings[0]["regressed"] is False


def test_compare_to_baseline_skips_rows_missing_from_either_side():
    current = [_make_result(name="bench_x", op="op1", av_value=None)]
    baseline = {"bench_x": {"op1": 100.0}}
    assert compare_to_baseline(current, baseline) == []

    current2 = [_make_result(name="bench_x", op="op1", av_value=100.0)]
    assert compare_to_baseline(current2, {}) == []


# --- WS0.3: median-of-N ----------------------------------------------------------------

def test_repeat_median_returns_the_single_call_when_repeat_is_one():
    calls = iter([42.0])
    assert repeat_median(lambda: next(calls), repeat=1) == 42.0


def test_repeat_median_of_floats_is_the_median_across_calls():
    calls = iter([10.0, 30.0, 20.0])
    assert repeat_median(lambda: next(calls), repeat=3) == 20.0


def test_repeat_median_of_dicts_takes_the_median_per_key_independently():
    calls = iter([
        {"init": 10.0, "add": 100.0},
        {"init": 30.0, "add": 300.0},
        {"init": 20.0, "add": 200.0},
    ])
    result = repeat_median(lambda: next(calls), repeat=3)
    assert result == {"init": 20.0, "add": 200.0}


def test_repeat_median_stops_after_one_call_when_the_tool_is_not_installed():
    calls = []

    def fn():
        calls.append(1)
        return None

    assert repeat_median(fn, repeat=5) is None
    assert len(calls) == 1  # never retried a tool that isn't there


def test_repeat_median_ignores_a_none_among_otherwise_real_samples():
    calls = iter([10.0, None, 20.0])
    assert repeat_median(lambda: next(calls), repeat=3) == 15.0


def test_time_call_returns_median_of_repeat_in_process_calls(monkeypatch):
    ticks = iter([0.0, 1.0, 1.0, 4.0, 4.0, 6.0])  # elapsed seconds: 1, 3, 2 -> 1000/3000/2000 ms
    monkeypatch.setattr("benchmarks.tool_runner.time.perf_counter", lambda: next(ticks))
    assert time_call(lambda: None, repeat=3) == 2000.0  # median of [1000, 3000, 2000]


def test_time_subprocess_returns_median_of_repeat_runs(monkeypatch):
    calls = []
    monkeypatch.setattr("benchmarks.tool_runner.subprocess.run", lambda *a, **k: calls.append(k))
    ticks = iter([0.0, 1.0, 1.0, 5.0, 5.0, 6.0])  # elapsed seconds: 1, 4, 1 -> 1000/4000/1000 ms
    monkeypatch.setattr("benchmarks.tool_runner.time.perf_counter", lambda: next(ticks))
    result = time_subprocess(["av", "status"], Path("."), repeat=3, env={"X": "1"})
    assert result == 1000.0  # median of [1000, 4000, 1000]
    assert len(calls) == 3
    assert calls[0]["env"]["X"] == "1"


# --- WS0.5: daemon-mode labeling ---------------------------------------------------------

def test_daemon_mode_label_reports_warm_by_default(monkeypatch):
    monkeypatch.delenv("AV_NO_DAEMON", raising=False)
    assert daemon_mode_label() == "warm (default)"


def test_daemon_mode_label_reports_off_when_env_set(monkeypatch):
    monkeypatch.setenv("AV_NO_DAEMON", "1")
    assert "off" in daemon_mode_label()


# --- WS0.7: claim summary ----------------------------------------------------------------

def _speed_result(name, av_value, competitor_value, claim_scope="speed"):
    return BenchmarkResult(
        name=name, title=name, description="d", tool_order=["av", "git-lfs"],
        rows=[Row(operation="op", values={"av": av_value, "git-lfs": competitor_value},
                  statuses={"av": ToolStatus.AVAILABLE, "git-lfs": ToolStatus.AVAILABLE})],
        claim_scope=claim_scope,
    )


def test_claim_summary_passes_when_every_speed_row_is_good_or_ok():
    results = [_speed_result("a", av_value=10.0, competitor_value=10.0)]
    summary = render_claim_summary(results)
    assert "| 1 | a | speed | PASS |" in summary
    assert "Faster in every published domain: YES" in summary


def test_claim_summary_fails_when_a_speed_row_is_bad():
    results = [_speed_result("a", av_value=100.0, competitor_value=10.0)]
    summary = render_claim_summary(results)
    assert "| 1 | a | speed | FAIL |" in summary
    assert "Faster in every published domain: NO" in summary


def test_claim_summary_excludes_internal_benchmarks_from_the_verdict():
    bad = _speed_result("internal-one", av_value=100.0, competitor_value=10.0, claim_scope="internal")
    good = _speed_result("real-one", av_value=10.0, competitor_value=10.0)
    summary = render_claim_summary([bad, good])
    assert "Faster in every published domain: YES" in summary
    assert "Internal-only (excluded from the claim): internal-one." in summary
    assert "internal-one" not in summary.split("Internal-only")[0]


def test_claim_summary_unique_scope_passes_on_a_real_number_alone():
    result = BenchmarkResult(
        name="u", title="u", description="d", tool_order=["av"],
        rows=[Row(operation="op", values={"av": 5.0}, statuses={"av": ToolStatus.AVAILABLE})],
        claim_scope="unique",
    )
    assert "PASS" in render_claim_summary([result])


def test_claim_summary_unique_scope_fails_with_no_real_av_number():
    result = BenchmarkResult(
        name="u", title="u", description="d", tool_order=["av"],
        rows=[Row(operation="op", values={"av": None}, statuses={"av": ToolStatus.NOT_INSTALLED})],
        claim_scope="unique",
    )
    assert "FAIL" in render_claim_summary([result])
    assert "Faster in every published domain: NO" in render_claim_summary([result])


def test_claim_summary_row_level_scope_overrides_the_result_default():
    # partial_checkpoint_fetch's real shape: one "unique" row, one ordinary "speed" row.
    result = BenchmarkResult(
        name="fetch", title="Fetch", description="d", tool_order=["av", "git-lfs"],
        rows=[
            Row(operation="single layer", values={"av": 5.0}, statuses={"av": ToolStatus.AVAILABLE},
                claim_scope="unique"),
            Row(operation="whole checkpoint", values={"av": 100.0, "git-lfs": 10.0},
                statuses={"av": ToolStatus.AVAILABLE, "git-lfs": ToolStatus.AVAILABLE}),
        ],
        claim_scope="speed",
    )
    summary = render_claim_summary([result])
    assert "| 1 | Fetch | mixed | FAIL |" in summary  # the whole-checkpoint row is BAD
    assert "Faster in every published domain: NO" in summary


def test_result_to_markdown_marks_internal_benchmarks():
    result = _speed_result("a", av_value=10.0, competitor_value=10.0, claim_scope="internal")
    md = result_to_markdown(result)
    assert "internal-only" in md.lower()


# --- V1.6.3: peak-RSS capture rides along with the timings --------------------------------

def _result_with_rss(rss=None):
    row = Row(operation="status", values={"av": 10.0, "dvc": 20.0},
              statuses={"av": ToolStatus.AVAILABLE, "dvc": ToolStatus.AVAILABLE},
              rss_mb={"av": rss} if rss is not None else {})
    return BenchmarkResult(name="noop_status_speed", title="t", description="d", tool_order=["av", "dvc"], rows=[row])


def test_time_subprocess_with_rss_key_records_a_sample(monkeypatch):
    from benchmarks import tool_runner
    from av_cli import sysres  # the same module object tool_runner imports (not python.av_cli)

    recorded = []
    monkeypatch.setattr(sysres, "run_measured",
                        lambda args, **k: (recorded.append((args, k)), sysres.MeasuredRun(0, 5.0, 42.0))[1])
    tool_runner._RSS_SAMPLES.clear()
    result = time_subprocess(["av", "status"], Path("."), repeat=3, env={"X": "1"}, rss_key="k")
    assert result == 5.0
    assert len(recorded) == 3 and recorded[0][1]["env"]["X"] == "1"
    assert tool_runner.pop_rss_median("k") == 42.0
    assert tool_runner.pop_rss_median("k") is None  # drained


def test_pop_rss_median_ignores_none_samples():
    from benchmarks import tool_runner

    tool_runner.record_rss("m", 10.0)
    tool_runner.record_rss("m", None)
    tool_runner.record_rss("m", 30.0)
    assert tool_runner.pop_rss_median("m") == 20.0


def test_markdown_and_table_add_rss_column_only_when_present():
    from benchmarks.tool_runner import print_table

    plain = result_to_markdown(_result_with_rss())
    assert "av peak RSS" not in plain
    with_rss = result_to_markdown(_result_with_rss(61.4))
    assert "| av peak RSS |" in with_rss
    assert "| 61 MB |" in with_rss
    lines = []
    print_table(_result_with_rss(61.4), echo=lines.append)
    assert any("av peak RSS" in line for line in lines)
    assert any("61 MB" in line for line in lines)


def test_results_to_json_underscore_rss_key_does_not_break_compare():
    with_rss = _result_with_rss(61.4)
    snapshot = results_to_json([with_rss])
    assert snapshot["_rss_mb"] == {"noop_status_speed": {"status": 61.4}}
    assert "_rss_mb" not in results_to_json([_result_with_rss()])
    # A baseline carrying `_rss_mb` compares exactly like one without it.
    findings = compare_to_baseline([_result_with_rss()], snapshot)
    assert [f["operation"] for f in findings] == ["status"]
    assert findings[0]["ratio"] == 1.0


def test_results_full_json_round_trip():
    from benchmarks.tool_runner import results_from_json_full, results_to_json_full

    row = Row(operation="fetch single layer", values={"av": 1.5, "dvc": None},
              statuses={"av": ToolStatus.AVAILABLE, "dvc": ToolStatus.NOT_APPLICABLE},
              unit="ms", notes={"dvc": "no layer primitive"}, claim_scope="unique", rss_mb={"av": 33.0})
    original = BenchmarkResult(name="partial", title="P", description="D", tool_order=["av", "dvc"],
                               rows=[row], claim_scope="speed")
    restored = results_from_json_full(results_to_json_full([original]))
    assert restored == [original]
