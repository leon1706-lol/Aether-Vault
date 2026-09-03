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
    format_value,
    rate,
    result_to_markdown,
    results_to_json,
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
