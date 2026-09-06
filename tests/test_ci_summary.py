"""v1.3.4 (todo.md item 29/31): scripts/ci_summary.py's pure logic — budget loading,
duration math, and table rendering — unit-tested independently of the GitHub API, same
factoring rationale as tests/test_release_gate.py's own module docstring.
"""
import importlib.util
import sys
from pathlib import Path

_SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "ci_summary.py"
_spec = importlib.util.spec_from_file_location("ci_summary", _SCRIPT_PATH)
cs = importlib.util.module_from_spec(_spec)
sys.modules["ci_summary"] = cs
_spec.loader.exec_module(cs)


def test_load_budgets_reads_the_named_workflow_section(tmp_path):
    (tmp_path / ".github").mkdir()
    (tmp_path / ".github" / "ci-budgets.yml").write_text(
        "workflows:\n  tests.yml:\n    test: 22\n    lint-workflows: 5\n  security.yml:\n    gitleaks: 6\n",
        encoding="utf-8",
    )
    budgets = cs.load_budgets(tmp_path, "tests.yml")
    assert budgets == {"test": 22, "lint-workflows": 5}


def test_load_budgets_returns_empty_dict_when_file_missing(tmp_path):
    assert cs.load_budgets(tmp_path, "tests.yml") == {}


def test_job_duration_minutes_computes_from_timestamps():
    job = {"started_at": "2026-09-06T10:00:00Z", "completed_at": "2026-09-06T10:07:30Z"}
    assert cs.job_duration_minutes(job) == 7.5


def test_job_duration_minutes_none_when_not_completed():
    job = {"started_at": "2026-09-06T10:00:00Z", "completed_at": None}
    assert cs.job_duration_minutes(job) is None


def test_render_summary_flags_an_overrun():
    jobs = [
        {"name": "lint-workflows", "conclusion": "success",
         "started_at": "2026-09-06T10:00:00Z", "completed_at": "2026-09-06T10:08:00Z"},
    ]
    table, warnings = cs.render_summary(jobs, {"lint-workflows": 5})
    assert "lint-workflows" in table
    assert "+3.0m over" in table
    assert len(warnings) == 1
    assert "lint-workflows" in warnings[0]


def test_render_summary_matches_a_matrix_job_by_base_id():
    jobs = [
        {"name": "test (3.14)", "conclusion": "success",
         "started_at": "2026-09-06T10:00:00Z", "completed_at": "2026-09-06T10:10:00Z"},
    ]
    table, warnings = cs.render_summary(jobs, {"test": 22})
    assert "22m" in table
    assert not warnings   # 10m against a 22m budget -- under, no warning


def test_render_summary_handles_no_budget_entry_gracefully():
    jobs = [
        {"name": "some-new-job", "conclusion": "success",
         "started_at": "2026-09-06T10:00:00Z", "completed_at": "2026-09-06T10:05:00Z"},
    ]
    table, warnings = cs.render_summary(jobs, {})
    assert "some-new-job" in table
    assert not warnings


def test_render_summary_shows_skipped_and_failed_conclusions():
    jobs = [
        {"name": "preview-env", "conclusion": "skipped"},
        {"name": "ha-drill", "conclusion": "failure",
         "started_at": "2026-09-06T10:00:00Z", "completed_at": "2026-09-06T10:12:00Z"},
    ]
    table, _ = cs.render_summary(jobs, {"ha-drill": 10})
    assert "⏭️ skipped" in table
    assert "❌ failure" in table
