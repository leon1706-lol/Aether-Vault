"""av task — curriculum task proposals (v1.3.1, RSI R2)."""
import json

import pytest
from click.testing import CliRunner

from python.av_cli.main import cli


def invoke(*args):
    return CliRunner().invoke(cli, list(args))


def invoke_json(*args):
    return CliRunner().invoke(cli, ["--output", "json", *args])


class _FakeTaskRegistry:
    def __init__(self):
        self.tasks = {}

    def create(self, body):
        tid = body.get("id") or f"task-{len(self.tasks)}"
        self.tasks[tid] = {"id": tid, "project_id": body["project_id"], "title": body["title"],
                          "description": body.get("description"),
                          "difficulty": body.get("difficulty"), "status": "proposed",
                          "created_at": "2026-01-01T00:00:00"}
        return 201, {"status": "created", "id": tid}

    def set_status(self, tid, status):
        row = self.tasks.get(tid)
        if row is None:
            return 404, {"detail": "not found"}
        row["status"] = status
        return 200, dict(row)


def _fake_client(monkeypatch, reg: _FakeTaskRegistry):
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
            params = params or {}
            rows = [t for t in reg.tasks.values() if t["project_id"] == params.get("project_id")]
            if params.get("status"):
                rows = [t for t in rows if t["status"] == params["status"]]
            return _FakeResponse(200, {"tasks": rows})

        def post(self, url, json=None):
            body = json or {}
            if url.endswith("/api/tasks"):
                status, resp = reg.create(body)
                return _FakeResponse(status, resp)
            if url.endswith("/status"):
                tid = url.split("/api/tasks/")[1].split("/status")[0]
                status, resp = reg.set_status(tid, body.get("status"))
                return _FakeResponse(status, resp)
            return _FakeResponse(404, {})

    class _FakeClient(client_module.VaultClient):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            self.session = _FakeSession()

        def server_available(self) -> bool:
            return True

    monkeypatch.setattr(client_module, "VaultClient", _FakeClient)


@pytest.fixture
def fake_tasks(monkeypatch):
    reg = _FakeTaskRegistry()
    _fake_client(monkeypatch, reg)
    return reg


def test_propose_without_server_queues(repo, unreachable_client):
    result = invoke("task", "propose", "Add a new eval")
    assert result.exit_code == 13, result.output


def test_propose_list_accept_reject(repo, fake_tasks):
    p1 = json.loads(invoke_json("task", "propose", "Task A", "--difficulty", "easy").output)["data"]
    p2 = json.loads(invoke_json("task", "propose", "Task B").output)["data"]

    listed = json.loads(invoke_json("task", "list").output)["data"]["tasks"]
    assert {t["id"] for t in listed} == {p1["id"], p2["id"]}

    accept_result = invoke_json("task", "accept", p1["id"])
    assert accept_result.exit_code == 0, accept_result.output
    assert json.loads(accept_result.output)["data"]["status"] == "accepted"

    reject_result = invoke_json("task", "reject", p2["id"])
    assert json.loads(reject_result.output)["data"]["status"] == "rejected"

    accepted_only = json.loads(invoke_json("task", "list", "--status", "accepted").output)["data"]["tasks"]
    assert [t["id"] for t in accepted_only] == [p1["id"]]


def test_accept_unknown_task_fails(repo, fake_tasks):
    result = invoke("task", "accept", "no-such-task")
    assert result.exit_code == 15, result.output
