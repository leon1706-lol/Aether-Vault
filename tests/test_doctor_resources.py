"""`av doctor --resources` (V1.6.3): the live memory picture + low-memory recommendations,
additive `resources` key in the doctor envelope."""
import json

import pytest
from click.testing import CliRunner

from python.av_cli import resources_report, sysres
from python.av_cli.main import cli


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert CliRunner().invoke(cli, ["init", "--mode", "local", "--yes", "--no-repl"]).exit_code == 0
    return tmp_path


def test_doctor_without_flag_has_resources_null(repo):
    res = CliRunner().invoke(cli, ["--output", "json", "doctor"])
    assert res.exit_code == 0, res.output
    assert json.loads(res.output)["data"]["resources"] is None


def test_doctor_resources_json_shape(repo):
    res = CliRunner().invoke(cli, ["--output", "json", "doctor", "--resources"])
    assert res.exit_code == 0, res.output
    data = json.loads(res.output)["data"]["resources"]
    assert set(data) >= {"rss_mb", "peak_rss_mb", "total_ram_mb", "available_mb", "daemon",
                         "stage_workers_effective", "stage_buffer_mb", "stage_worker_budget_mb",
                         "stage_peak_estimate_mb", "recommended_profile", "recommendations"}
    assert isinstance(data["daemon"]["running"], bool)
    assert data["rss_mb"] > 0 and data["total_ram_mb"] > 0
    assert data["stage_workers_effective"] >= 1
    assert data["stage_buffer_mb"] == 32
    assert data["stage_worker_budget_mb"] == 34
    assert data["recommended_profile"] in ("lowmem", "default")
    assert isinstance(data["recommendations"], list)


def test_doctor_resources_text_mode_prints_a_section(repo):
    res = CliRunner().invoke(cli, ["doctor", "--resources"])
    assert res.exit_code == 0, res.output
    assert "Resources" in res.output and "staging" in res.output and "profile" in res.output


def test_recommended_profile_lowmem_when_ram_small(monkeypatch, tmp_path):
    monkeypatch.setattr(sysres, "total_ram_mb", lambda: 3900.0)
    monkeypatch.setattr(sysres, "available_mb", lambda: 400.0)
    report = resources_report.build_resources_report(None)
    assert report["recommended_profile"] == "lowmem"
    assert any("AV_STAGE_WORKERS_MAX=2" in r for r in report["recommendations"])
    assert any("AV_STAGE_BUFFER_MB=8" in r for r in report["recommendations"])
    # (400 - 256) // 34 = 4 workers at most on this much free RAM
    assert report["stage_workers_effective"] <= 4


def test_recommended_profile_default_on_a_big_box(monkeypatch):
    monkeypatch.setattr(sysres, "total_ram_mb", lambda: 32000.0)
    monkeypatch.setattr(sysres, "available_mb", lambda: 20000.0)
    report = resources_report.build_resources_report(None)
    assert report["recommended_profile"] == "default"
    assert report["recommendations"] == []
    assert report["daemon"] == {"running": False, "rss_mb": None, "peak_rss_mb": None}
