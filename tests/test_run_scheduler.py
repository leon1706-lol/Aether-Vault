"""av run stop / av run branch-policy / av run auto-stop-check / av scheduler queue
(v1.3.1, RSI R3: todo.md D.18-D.20)."""
import json

import pytest
from click.testing import CliRunner

from python.av_cli.main import cli


def invoke(*args):
    return CliRunner().invoke(cli, list(args))


def invoke_json(*args):
    return CliRunner().invoke(cli, ["--output", "json", *args])


def _fake_client(monkeypatch, get_map=None, post_map=None):
    import python.av_cli.client as client_module

    class _FakeResponse:
        def __init__(self, status_code, body):
            self.status_code = status_code
            self._body = body

        def json(self):
            return self._body

        @property
        def text(self):
            return json.dumps(self._body)

    class _FakeSession:
        def get(self, url, params=None, timeout=None):
            for suffix, (status, body) in (get_map or {}).items():
                if url.endswith(suffix):
                    return _FakeResponse(status, body)
            return _FakeResponse(404, {})

        def post(self, url, json=None):
            for suffix, (status, body) in (post_map or {}).items():
                if url.endswith(suffix):
                    return _FakeResponse(status, body)
            return _FakeResponse(404, {})

    class _FakeClient(client_module.VaultClient):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            self.session = _FakeSession()

        def server_available(self) -> bool:
            return True

    monkeypatch.setattr(client_module, "VaultClient", _FakeClient)


# ---------------------------------------------------------------------------
# av run stop
# ---------------------------------------------------------------------------

def test_stop_without_server_queues(repo, unreachable_client):
    result = invoke("run", "stop", "r1")
    assert result.exit_code == 13, result.output


def test_stop_reports_reason(repo, monkeypatch):
    _fake_client(monkeypatch, post_map={
        "/stop": (200, {"status": "stopped", "id": "r1", "stop_reason": "plateau"})})
    result = invoke_json("run", "stop", "r1", "--reason", "plateau")
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["data"]["stop_reason"] == "plateau"


# ---------------------------------------------------------------------------
# av run branch-policy
# ---------------------------------------------------------------------------

def test_branch_policy_set_requires_a_rule(repo):
    result = invoke("run", "branch-policy", "set")
    assert result.exit_code == 15, result.output


def test_branch_policy_set_rejects_malformed_rule(repo):
    result = invoke("run", "branch-policy", "set", "--branch-if", "val_loss wat 0.5")
    assert result.exit_code == 15, result.output


def test_branch_policy_set_and_show(repo):
    invoke("run", "branch-policy", "set", "--branch-if", "val_loss < 0.3",
          "--abandon-if", "val_loss > 2.0")
    result = invoke_json("run", "branch-policy", "show")
    data = json.loads(result.output)["data"]
    assert data["branch_if"] == {"metric": "val_loss", "op": "<", "threshold": 0.3}
    assert data["abandon_if"] == {"metric": "val_loss", "op": ">", "threshold": 2.0}


def test_branch_policy_check_without_policy_fails(repo, monkeypatch):
    _fake_client(monkeypatch, get_map={"/api/runs/r1": (200, {"metrics_summary": {}})})
    result = invoke("run", "branch-policy", "check", "r1")
    assert result.exit_code == 15, result.output


def test_branch_policy_check_recommends_abandon(repo, monkeypatch):
    invoke("run", "branch-policy", "set", "--abandon-if", "val_loss > 2.0",
          "--branch-if", "val_loss < 0.3")
    _fake_client(monkeypatch, get_map={
        "/api/runs/r1": (200, {"metrics_summary": {"val_loss": 3.0}})})
    result = invoke_json("run", "branch-policy", "check", "r1")
    data = json.loads(result.output)["data"]
    assert data["recommendation"] == "abandon"


def test_branch_policy_check_recommends_continue_when_nothing_matches(repo, monkeypatch):
    invoke("run", "branch-policy", "set", "--abandon-if", "val_loss > 2.0")
    _fake_client(monkeypatch, get_map={
        "/api/runs/r1": (200, {"metrics_summary": {"val_loss": 1.0}})})
    result = invoke_json("run", "branch-policy", "check", "r1")
    assert json.loads(result.output)["data"]["recommendation"] == "continue"


# ---------------------------------------------------------------------------
# av run auto-stop-check
# ---------------------------------------------------------------------------

def _metrics_page(values, metric="val_loss"):
    return {"points": [{"metrics": {metric: v}} for v in values], "next_cursor": None}


def test_auto_stop_check_no_condition(repo, monkeypatch):
    _fake_client(monkeypatch, get_map={
        "/metrics": (200, _metrics_page([1.0, 0.8, 0.6, 0.4, 0.2]))})
    result = invoke_json("run", "auto-stop-check", "r1", "--metric", "val_loss")
    data = json.loads(result.output)["data"]
    assert data["triggered"] is None


def test_auto_stop_check_detects_plateau(repo, monkeypatch):
    _fake_client(monkeypatch, get_map={
        "/metrics": (200, _metrics_page([1.0, 0.5, 0.5, 0.5, 0.5, 0.5, 0.5]))})
    result = invoke_json("run", "auto-stop-check", "r1", "--metric", "val_loss", "--patience", "5")
    data = json.loads(result.output)["data"]
    assert data["triggered"] == "plateau"


def test_auto_stop_check_detects_divergence(repo, monkeypatch):
    _fake_client(monkeypatch, get_map={
        "/metrics": (200, _metrics_page([1.0, 0.8, 0.6, 50.0]))})
    result = invoke_json("run", "auto-stop-check", "r1", "--metric", "val_loss")
    data = json.loads(result.output)["data"]
    assert data["triggered"] == "divergence"


def test_auto_stop_check_detects_nan(repo, monkeypatch):
    _fake_client(monkeypatch, get_map={
        "/metrics": (200, _metrics_page([1.0, 0.5, float("nan")]))})
    result = invoke_json("run", "auto-stop-check", "r1", "--metric", "val_loss")
    data = json.loads(result.output)["data"]
    assert data["triggered"] == "nan"


def test_auto_stop_check_can_actually_stop(repo, monkeypatch):
    _fake_client(monkeypatch,
                get_map={"/metrics": (200, _metrics_page([1.0, 0.8, 0.6, 500.0]))},
                post_map={"/stop": (200, {"status": "stopped"})})
    result = invoke_json("run", "auto-stop-check", "r1", "--metric", "val_loss", "--stop")
    data = json.loads(result.output)["data"]
    assert data["triggered"] == "divergence"
    assert data["stopped"] is True


def test_auto_stop_check_maximize_direction(repo, monkeypatch):
    """acc going DOWN a lot should divergence-trigger when maximizing."""
    _fake_client(monkeypatch, get_map={
        "/metrics": (200, _metrics_page([0.9, 0.85, 0.8, -10.0], metric="acc"))})
    result = invoke_json("run", "auto-stop-check", "r1", "--metric", "acc", "--maximize")
    data = json.loads(result.output)["data"]
    assert data["triggered"] == "divergence"


# ---------------------------------------------------------------------------
# av scheduler queue
# ---------------------------------------------------------------------------

def test_scheduler_queue_without_server_queues(repo, unreachable_client):
    result = invoke("scheduler", "queue")
    assert result.exit_code == 13, result.output


def test_scheduler_queue_lists_running(repo, monkeypatch):
    _fake_client(monkeypatch, get_map={
        "/api/scheduler/queue": (200, {"queue": [{"id": "r1", "name": "exp-1",
                                                  "metrics_summary": {"loss": 0.1}}]})})
    result = invoke_json("scheduler", "queue")
    data = json.loads(result.output)["data"]
    assert len(data["queue"]) == 1
    assert data["queue"][0]["id"] == "r1"
