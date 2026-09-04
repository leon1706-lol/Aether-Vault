"""av plan / av budget — experiment planner objects + compute/storage/step quotas
(v1.3.1, RSI R3: todo.md D.16/D.17)."""
import json

import pytest
from click.testing import CliRunner

from python.av_cli.main import cli


def invoke(*args):
    return CliRunner().invoke(cli, list(args))


def invoke_json(*args):
    return CliRunner().invoke(cli, ["--output", "json", *args])


def _fake_client(monkeypatch, get_map=None, post_map=None):
    """`get_map`/`post_map`: {url_suffix: (status, body)}, matched by `url.endswith()`."""
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

        def upload_object(self, file_path, sha256_hash, known_missing=False) -> bool:
            return True

        def download_object(self, sha256_hash, dest_path) -> bool:
            return False

    monkeypatch.setattr(client_module, "VaultClient", _FakeClient)


# ---------------------------------------------------------------------------
# av plan
# ---------------------------------------------------------------------------

def test_plan_validate_pure_local_no_network(repo, tmp_path):
    doc = {"hypotheses": ["a"], "ablations": [], "budget": {}, "stop_rules": []}
    f = tmp_path / "plan.json"
    f.write_text(json.dumps(doc), encoding="utf-8")
    result = invoke_json("plan", "validate", str(f))
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["data"]["valid"] is True


def test_plan_validate_reports_missing_keys(repo, tmp_path):
    f = tmp_path / "plan.json"
    f.write_text(json.dumps({"hypotheses": []}), encoding="utf-8")
    result = invoke_json("plan", "validate", str(f))
    data = json.loads(result.output)["data"]
    assert data["valid"] is False
    assert any("ablations" in p for p in data["problems"])


def test_plan_validate_rejects_bad_json(repo, tmp_path):
    f = tmp_path / "plan.json"
    f.write_text("not json", encoding="utf-8")
    result = invoke("plan", "validate", str(f))
    assert result.exit_code == 15, result.output


def test_plan_create_without_server_queues(repo, tmp_path, unreachable_client):
    f = tmp_path / "plan.json"
    f.write_text(json.dumps({"hypotheses": []}), encoding="utf-8")
    result = invoke("plan", "create", str(f))
    assert result.exit_code == 13, result.output


def test_plan_create_show_attach(repo, monkeypatch, tmp_path):
    _fake_client(monkeypatch,
                get_map={"/api/plans/plan-1": (200, {"id": "plan-1", "project_id": "p",
                                                     "object_id": "a" * 64, "created_at": "t"})},
                post_map={"/api/plans": (201, {"status": "created", "id": "plan-1"}),
                         "/plan": (200, {"status": "linked", "run_id": "r1", "plan_id": "plan-1"})})
    f = tmp_path / "plan.json"
    f.write_text(json.dumps({"hypotheses": ["h1"]}), encoding="utf-8")

    create_result = invoke_json("plan", "create", str(f))
    assert create_result.exit_code == 0, create_result.output
    assert json.loads(create_result.output)["data"]["id"] == "plan-1"

    show_result = invoke_json("plan", "show", "plan-1")
    assert show_result.exit_code == 0, show_result.output

    attach_result = invoke_json("plan", "attach", "plan-1", "--run", "r1")
    assert attach_result.exit_code == 0, attach_result.output
    assert json.loads(attach_result.output)["data"]["plan_id"] == "plan-1"


# ---------------------------------------------------------------------------
# av budget
# ---------------------------------------------------------------------------

def test_budget_set_without_server_queues(repo, unreachable_client):
    result = invoke("budget", "set", "run-1", "--compute-seconds", "100")
    assert result.exit_code == 13, result.output


def test_budget_set_requires_at_least_one_limit(repo, monkeypatch):
    _fake_client(monkeypatch)
    result = invoke("budget", "set", "run-1")
    assert result.exit_code == 15, result.output


def test_budget_set_and_show(repo, monkeypatch):
    body = {"id": "b1", "project_id": "p", "scope": "run", "scope_ref": "run-1",
           "compute_seconds_limit": 100.0, "storage_bytes_limit": None, "step_limit": None,
           "compute_seconds_used": 0.0, "storage_bytes_used": 0, "steps_used": 0,
           "created_at": "t", "updated_at": "t"}
    _fake_client(monkeypatch,
                get_map={"/api/budgets/b1": (200, body)},
                post_map={"/api/budgets": (201, {"status": "created", "id": "b1"})})
    set_result = invoke_json("budget", "set", "run-1", "--compute-seconds", "100")
    assert set_result.exit_code == 0, set_result.output

    show_result = invoke_json("budget", "show", "b1")
    assert show_result.exit_code == 0, show_result.output
    assert json.loads(show_result.output)["data"]["compute_seconds_limit"] == 100.0


def test_budget_attach(repo, monkeypatch):
    _fake_client(monkeypatch, post_map={
        "/budget": (200, {"status": "linked", "run_id": "r1", "budget_id": "b1"})})
    result = invoke_json("budget", "attach", "b1", "--run", "r1")
    assert result.exit_code == 0, result.output


def test_budget_consume_within_limit(repo, monkeypatch):
    _fake_client(monkeypatch, post_map={"/consume": (200, {
        "id": "b1", "project_id": "p", "scope": "run", "scope_ref": "run-1",
        "compute_seconds_limit": 100.0, "storage_bytes_limit": None, "step_limit": None,
        "compute_seconds_used": 5.0, "storage_bytes_used": 0, "steps_used": 0,
        "exhausted": False, "exceeded_dims": [],
    })})
    result = invoke_json("budget", "consume", "b1", "--compute-seconds", "5")
    assert result.exit_code == 0, result.output


def test_budget_consume_exhausted(repo, monkeypatch):
    _fake_client(monkeypatch, post_map={"/consume": (200, {
        "id": "b1", "project_id": "p", "scope": "run", "scope_ref": "run-1",
        "compute_seconds_limit": 10.0, "storage_bytes_limit": None, "step_limit": None,
        "compute_seconds_used": 20.0, "storage_bytes_used": 0, "steps_used": 0,
        "exhausted": True, "exceeded_dims": ["compute_seconds"],
    })})
    result = invoke_json("budget", "consume", "b1", "--compute-seconds", "20")
    assert result.exit_code == 17, result.output
    env = json.loads(result.output)
    assert env["ok"] is False
    assert env["error"]["code"] == "budget_exhausted"


def test_budget_unknown_id_fails_cleanly(repo, monkeypatch):
    _fake_client(monkeypatch)  # everything 404s
    result = invoke("budget", "show", "no-such-budget")
    assert result.exit_code == 15, result.output
