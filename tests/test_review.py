"""av review / av critique — the reviewer gate + structured objections (v1.3.1, RSI R4:
todo.md H.34/H.35)."""
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


def test_review_without_server_queues(repo, unreachable_client):
    result = invoke("review", "approve", "imp-1")
    assert result.exit_code == 13, result.output


def test_review_approve(repo, monkeypatch):
    _fake_client(monkeypatch, post_map={
        "/api/reviews": (201, {"status": "created", "id": "rev-1", "decision": "approve"})})
    result = invoke_json("review", "approve", "imp-1")
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["data"]["decision"] == "approve"


def test_review_denied_without_scope(repo, monkeypatch):
    _fake_client(monkeypatch, post_map={"/api/reviews": (403, {})})
    result = invoke("review", "approve", "imp-1")
    assert result.exit_code == 20, result.output


def test_review_self_review_rejected(repo, monkeypatch):
    _fake_client(monkeypatch, post_map={
        "/api/reviews": (422, {"detail": "A target's own proposer cannot review it"})})
    result = invoke("review", "approve", "imp-1")
    assert result.exit_code == 15, result.output
    assert "another identity" in result.output.lower()


def test_review_list(repo, monkeypatch):
    _fake_client(monkeypatch, get_map={
        "/api/reviews": (200, {"reviews": [{"id": "r1", "target_type": "improver",
                                            "target_id": "imp-1", "reviewer": "bob",
                                            "decision": "approve"}]})})
    result = invoke_json("review", "list")
    rows = json.loads(result.output)["data"]["reviews"]
    assert rows[0]["reviewer"] == "bob"


def test_critique_add_list_resolve(repo, monkeypatch):
    _fake_client(monkeypatch,
                get_map={"/api/critiques": (200, {"critiques": [
                    {"id": "c1", "target_type": "change_set", "target_id": "cs-1",
                     "status": "open", "objection": "untested"}]})},
                post_map={"/api/critiques": (201, {"status": "created", "id": "c1"}),
                         "/resolve": (200, {"id": "c1", "status": "resolved"})})
    add_result = invoke_json("critique", "add", "cs-1", "untested", "--target-type", "change_set")
    assert add_result.exit_code == 0, add_result.output

    list_result = invoke_json("critique", "list")
    assert json.loads(list_result.output)["data"]["critiques"][0]["status"] == "open"

    resolve_result = invoke_json("critique", "resolve", "c1", "--resolution", "added tests")
    assert resolve_result.exit_code == 0, resolve_result.output
    assert json.loads(resolve_result.output)["data"]["status"] == "resolved"


def test_critique_waive_requires_resolution_text(repo):
    result = invoke("critique", "waive", "c1")
    assert result.exit_code == 2, result.output  # click usage error: --resolution required


def test_critique_waive_denied_without_scope(repo, monkeypatch):
    _fake_client(monkeypatch, post_map={"/waive": (403, {})})
    result = invoke("critique", "waive", "c1", "--resolution", "accepted risk")
    assert result.exit_code == 20, result.output


def test_critique_finalize_already_terminal_is_validation_error(repo, monkeypatch):
    _fake_client(monkeypatch, post_map={"/resolve": (409, {"detail": "already resolved"})})
    result = invoke("critique", "resolve", "c1")
    assert result.exit_code == 15, result.output
