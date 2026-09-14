"""`av_cli.lowmem_tests` -- the file-per-subprocess runner that makes the full suite
runnable on a memory-constrained box (V1.6.3). Exercised against a tiny fake test tree
with REAL pytest subprocesses (no mocking of the thing being verified)."""
import json
from pathlib import Path

from python.av_cli import lowmem_tests


def _fake_suite(root: Path) -> Path:
    tests = root / "tests"
    tests.mkdir()
    (tests / "test_alpha.py").write_text(
        "def test_one():\n    assert True\n\ndef test_two():\n    assert True\n", encoding="utf-8")
    (tests / "test_beta.py").write_text(
        "import pytest\n\ndef test_ok():\n    assert True\n\n@pytest.mark.skip(reason='x')\ndef test_skipped():\n    pass\n",
        encoding="utf-8")
    (tests / "test_gamma.py").write_text(
        "def test_boom():\n    assert 1 == 2\n\ndef test_fine():\n    assert True\n", encoding="utf-8")
    (root / "pyproject.toml").write_text("[tool.pytest.ini_options]\naddopts = '-p no:randomly'\n", encoding="utf-8")
    return tests


def test_parse_counts_reads_the_final_summary_line():
    out = "....\n\x1b[32m3 passed\x1b[0m, 1 failed, 2 skipped, 1 error in 0.12s\n"
    assert lowmem_tests.parse_counts(out) == (3, 1, 2, 1)
    assert lowmem_tests.parse_counts("no tests ran in 0.01s\n") == (0, 0, 0, 0)
    assert lowmem_tests.parse_counts("garbage") == (0, 0, 0, 0)


def test_runs_each_file_in_its_own_process_and_counts(tmp_path):
    tests = _fake_suite(tmp_path)
    lines = []
    summary = lowmem_tests.run_lowmem(tests, min_free_mb=0, state_path=tmp_path / "state.json",
                                      echo=lines.append, per_file_timeout=300)
    assert (summary.passed, summary.failed, summary.skipped, summary.errors) == (4, 1, 1, 0)
    assert not summary.ok
    statuses = {f.path: f.status for f in summary.files}
    assert statuses["tests/test_alpha.py"] == "passed"
    assert statuses["tests/test_beta.py"] == "passed"
    assert statuses["tests/test_gamma.py"] == "failed"
    assert summary.pytest_style_line() == "4 passed, 1 failed, 1 skipped"
    # Peak RSS is measured for every unit (this platform has a probe).
    assert all(f.peak_rss_mb is None or f.peak_rss_mb > 0 for f in summary.files)
    state = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert sorted(state["passed_units"]) == ["tests/test_alpha.py", "tests/test_beta.py"]


def test_resume_skips_units_that_already_passed(tmp_path):
    tests = _fake_suite(tmp_path)
    state = tmp_path / "state.json"
    lowmem_tests.run_lowmem(tests, min_free_mb=0, state_path=state, echo=lambda *_: None, per_file_timeout=300)
    summary = lowmem_tests.run_lowmem(tests, min_free_mb=0, state_path=state, echo=lambda *_: None,
                                      per_file_timeout=300)
    assert summary.resumed == 2
    assert summary.passed == 1 and summary.failed == 1  # only test_gamma re-ran
    fresh = lowmem_tests.run_lowmem(tests, min_free_mb=0, state_path=state, resume=False,
                                    echo=lambda *_: None, per_file_timeout=300)
    assert fresh.resumed == 0 and fresh.passed == 4


def test_k_filter_and_file_selection_are_forwarded(tmp_path):
    tests = _fake_suite(tmp_path)
    summary = lowmem_tests.run_lowmem(tests, min_free_mb=0, state_path=tmp_path / "s.json", resume=False,
                                      k_expr="fine or one", files=["tests/test_gamma.py", "tests/test_alpha.py"],
                                      echo=lambda *_: None, per_file_timeout=300)
    assert [f.path for f in summary.files] == ["tests/test_gamma.py", "tests/test_alpha.py"]
    assert summary.passed == 2 and summary.failed == 0 and summary.ok


def test_chunking_splits_collected_node_ids(tmp_path):
    tests = _fake_suite(tmp_path)
    lines = []
    summary = lowmem_tests.run_lowmem(tests, min_free_mb=0, state_path=tmp_path / "s.json", resume=False,
                                      chunk_size=1, files=["tests/test_alpha.py"], echo=lines.append,
                                      per_file_timeout=300)
    assert summary.files[0].chunks == 2
    assert summary.passed == 2
    assert any("test_alpha.py#0" in line for line in lines) and any("test_alpha.py#1" in line for line in lines)


def test_a_killed_interpreter_counts_as_an_error_not_a_pass(tmp_path):
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_die.py").write_text("import os\n\ndef test_x():\n    os._exit(137)\n", encoding="utf-8")
    summary = lowmem_tests.run_lowmem(tests, min_free_mb=0, state_path=tmp_path / "s.json", resume=False,
                                      echo=lambda *_: None, per_file_timeout=300)
    assert summary.errors == 1 and not summary.ok
    assert summary.files[0].status == "error"
    assert "without a summary" in summary.files[0].summary


def test_script_wrapper_parses_passthrough_args(tmp_path, monkeypatch):
    import importlib.util

    script = Path(__file__).resolve().parents[1] / "scripts" / "run_tests_lowmem.py"
    spec = importlib.util.spec_from_file_location("run_tests_lowmem", script)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    captured = {}

    def fake_run(tests_dir, **kwargs):
        captured.update(kwargs)
        return lowmem_tests.LowmemSummary(passed=1)

    monkeypatch.setattr(mod.lowmem_tests, "run_lowmem", fake_run)
    rc = mod.main(["--min-free-mb", "123", "-k", "x", "--files", "tests/a.py", "--no-resume", "--", "-x"])
    assert rc == 0
    assert captured["min_free_mb"] == 123 and captured["k_expr"] == "x"
    assert captured["files"] == ["tests/a.py"] and captured["resume"] is False
    assert captured["extra_args"] == ("-x",)
