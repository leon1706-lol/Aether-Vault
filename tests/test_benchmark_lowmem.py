"""`av benchmark --lowmem` (V1.6.3): one benchmark per fresh subprocess with a free-RAM
floor, merged through the SAME report path as a combined run -- and the dataclass
round-trip that makes that possible."""
import json
from pathlib import Path

from click.testing import CliRunner

from python.av_cli import cmd_devtools
from python.av_cli.main import cli


def test_results_full_json_round_trip_is_lossless():
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from benchmarks.tool_runner import BenchmarkResult, Row, ToolStatus, results_from_json_full, results_to_json_full

    row = Row(operation="status", values={"av": 1.0, "dvc": None},
              statuses={"av": ToolStatus.AVAILABLE, "dvc": ToolStatus.NOT_INSTALLED},
              notes={"dvc": "n/a"}, claim_scope="unique", rss_mb={"av": 41.0})
    original = [BenchmarkResult("noop_status_speed", "T", "D", ["av", "dvc"], [row], "speed")]
    assert results_from_json_full(json.loads(json.dumps(results_to_json_full(original)))) == original


def test_lowmem_runs_each_benchmark_in_a_subprocess_and_merges_one_report(tmp_path, monkeypatch):
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from benchmarks.tool_runner import BenchmarkResult, Row, ToolStatus, results_to_json_full
    from python.av_cli import sysres

    launched = []

    def fake_run_measured(args, **kwargs):
        launched.append(args)
        name = args[args.index("--only") + 1]
        dump = Path(args[args.index("--dump-results") + 1])
        row = Row(operation=f"{name} op", values={"av": 5.0}, statuses={"av": ToolStatus.AVAILABLE},
                  rss_mb={"av": 50.0})
        dump.write_text(json.dumps(results_to_json_full(
            [BenchmarkResult(name, name.title(), "d", ["av"], [row], "internal")])), encoding="utf-8")
        return sysres.MeasuredRun(0, 10.0, 77.0)

    monkeypatch.setattr(sysres, "run_measured", fake_run_measured)
    monkeypatch.setattr(sysres, "available_mb", lambda: 9999.0)
    out_md = tmp_path / "b.md"
    res = CliRunner().invoke(cli, ["benchmark", "--lowmem", "--only", "noop_status_speed",
                                   "--only", "hashing_throughput", "--vs", "dvc",
                                   "--markdown", str(out_md), "--repeat", "1"])
    assert res.exit_code == 0, res.output
    assert len(launched) == 2
    assert all("--dump-results" in a and "--repeat" in a for a in launched)
    assert "--vs" in launched[0] and "dvc" in launched[0]
    md = out_md.read_text(encoding="utf-8")
    assert md.count("# Aether-Vault Benchmarks") == 1  # header once
    assert "## Noop_Status_Speed" in md and "## Hashing_Throughput" in md
    assert "child peak RSS 77 MB" in res.output


def test_lowmem_reports_a_failed_row_when_ram_never_frees_up(monkeypatch):
    from python.av_cli import sysres

    monkeypatch.setattr(sysres, "available_mb", lambda: 100.0)
    monkeypatch.setattr(cmd_devtools.time, "sleep", lambda s: None)
    calls = []
    monkeypatch.setattr(sysres, "run_measured", lambda *a, **k: calls.append(a) or None)
    res = CliRunner().invoke(cli, ["benchmark", "--lowmem", "--only", "hashing_throughput",
                                   "--min-free-mb", "400"])
    assert res.exit_code == 0, res.output
    assert calls == []  # never launched
    assert "skipped: free RAM 100 MB stayed below --min-free-mb 400" in res.output
    assert "failed" in res.output  # a FAILED cell in the printed table, not a missing row


def test_lowmem_reports_a_failed_row_when_the_child_dies(monkeypatch):
    from python.av_cli import sysres

    monkeypatch.setattr(sysres, "available_mb", lambda: 9999.0)
    monkeypatch.setattr(sysres, "run_measured", lambda *a, **k: sysres.MeasuredRun(137, 1.0, None))
    res = CliRunner().invoke(cli, ["benchmark", "--lowmem", "--only", "hashing_throughput"])
    assert res.exit_code == 0, res.output
    assert "child process exited 137" in res.output
    assert "failed" in res.output


def test_dump_results_child_mode_writes_full_results_and_prints_nothing(tmp_path, monkeypatch):
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import importlib

    from benchmarks.tool_runner import BenchmarkResult, Row, ToolStatus

    fake_module = type("M", (), {})()
    fake_module.run = lambda tool_order, repeat: BenchmarkResult(
        "hashing_throughput", "H", "d", tool_order,
        [Row(operation="hash", values={"av": 1.5}, statuses={"av": ToolStatus.AVAILABLE})])
    monkeypatch.setattr(importlib, "import_module", lambda name: fake_module)
    dump = tmp_path / "out.json"
    res = CliRunner().invoke(cli, ["benchmark", "--only", "hashing_throughput", "--repeat", "1",
                                   "--dump-results", str(dump)])
    assert res.exit_code == 0, res.output
    assert "hash" not in res.output  # no table printed in child mode
    doc = json.loads(dump.read_text(encoding="utf-8"))
    assert doc["results"][0]["rows"][0]["values"]["av"] == 1.5
