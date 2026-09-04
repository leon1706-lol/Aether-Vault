"""av run --kind scoring validation (todo.md F.28) and av run integrity-check (todo.md
B.10) — v1.3.1, RSI R2."""
import json

import pytest
from click.testing import CliRunner

from python.av_cli.main import cli


def invoke(*args):
    return CliRunner().invoke(cli, list(args))


def invoke_json(*args):
    return CliRunner().invoke(cli, ["--output", "json", *args])


# ---------------------------------------------------------------------------
# --kind scoring reproducibility gate
# ---------------------------------------------------------------------------

def test_scoring_run_without_snapshot_or_git_fails(repo):
    result = invoke("run", "start", "--kind", "scoring")
    assert result.exit_code == 15, result.output
    assert "env snapshot" in result.output.lower()


def test_scoring_run_with_snapshot_but_no_git_still_fails(repo):
    invoke("env", "snapshot")
    result = invoke("run", "start", "--kind", "scoring")
    assert result.exit_code == 15, result.output
    assert "code revision" in result.output.lower()


def test_scoring_run_with_both_succeeds(repo, monkeypatch):
    invoke("env", "snapshot")
    monkeypatch.setattr("python.av_cli.core.capture_code_pointer",
                        lambda repo_root: {"git_remote": None, "git_sha": "abc123", "dirty": False})
    result = invoke_json("run", "start", "--kind", "scoring")
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["data"]["kind"] == "scoring"


def test_train_run_needs_neither(repo):
    result = invoke_json("run", "start")
    assert result.exit_code == 0, result.output


# ---------------------------------------------------------------------------
# integrity-check
# ---------------------------------------------------------------------------

def _fake_client(monkeypatch, run_metrics=None, eval_rows=None, report_status=200):
    import python.av_cli.client as client_module

    class _FakeResponse:
        def __init__(self, status_code, body):
            self.status_code = status_code
            self._body = body

        def json(self):
            return self._body

    class _FakeSession:
        def get(self, url, params=None, timeout=None):
            if "/api/eval/results" in url:
                return _FakeResponse(200, {"eval_results": eval_rows or []})
            if "/api/runs/" in url:
                return _FakeResponse(200, {"id": "r1", "metrics_summary": run_metrics or {}})
            return _FakeResponse(404, {})

        def post(self, url, json=None):
            return _FakeResponse(report_status, {"status": "recorded"})

    class _FakeClient(client_module.VaultClient):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            self.session = _FakeSession()

        def server_available(self) -> bool:
            return True

    monkeypatch.setattr(client_module, "VaultClient", _FakeClient)


def test_integrity_check_no_eval_result_yet(repo, monkeypatch):
    _fake_client(monkeypatch, run_metrics={"val_loss": 0.5}, eval_rows=[])
    result = invoke_json("run", "integrity-check", "r1", "--suite", "s1")
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)["data"]
    assert data["has_eval_result"] is False
    assert data["flagged_metrics"] == []


def test_integrity_check_flags_large_gap(repo, monkeypatch):
    _fake_client(monkeypatch, run_metrics={"acc": 0.95},
                eval_rows=[{"revealed": True, "score": {"acc": 0.5}}])
    result = invoke_json("run", "integrity-check", "r1", "--suite", "s1")
    data = json.loads(result.output)["data"]
    assert "acc" in data["flagged_metrics"]
    assert data["metric_gaps"]["acc"]["train"] == 0.95
    assert data["metric_gaps"]["acc"]["eval"] == 0.5


def test_integrity_check_no_gap_when_close(repo, monkeypatch):
    _fake_client(monkeypatch, run_metrics={"acc": 0.90},
                eval_rows=[{"revealed": True, "score": {"acc": 0.89}}])
    result = invoke_json("run", "integrity-check", "r1", "--suite", "s1")
    data = json.loads(result.output)["data"]
    assert data["flagged_metrics"] == []


def test_integrity_check_ignores_unrevealed_results(repo, monkeypatch):
    _fake_client(monkeypatch, run_metrics={"acc": 0.95},
                eval_rows=[{"revealed": False, "score": {"acc": 0.1}}])
    result = invoke_json("run", "integrity-check", "r1", "--suite", "s1")
    data = json.loads(result.output)["data"]
    assert data["has_eval_result"] is False


def test_integrity_check_without_server_fails_cleanly(repo, unreachable_client):
    result = invoke("run", "integrity-check", "r1", "--suite", "s1")
    assert result.exit_code == 13, result.output


def test_integrity_check_unknown_run(repo, monkeypatch):
    import python.av_cli.client as client_module

    class _FakeResponse:
        status_code = 404

        def json(self):
            return {}

    class _FakeSession:
        def get(self, url, params=None, timeout=None):
            return _FakeResponse()

    class _FakeClient(client_module.VaultClient):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            self.session = _FakeSession()

        def server_available(self) -> bool:
            return True

    monkeypatch.setattr(client_module, "VaultClient", _FakeClient)
    result = invoke("run", "integrity-check", "no-such-run", "--suite", "s1")
    assert result.exit_code == 15, result.output
