"""av lineage / av search / av strategy / av lessons / av blackboard (v1.3.1, RSI R4:
todo.md E.21-E.24, H.36)."""
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

        def upload_object(self, file_path, sha256_hash, known_missing=False) -> bool:
            return True

        def download_object(self, sha256_hash, dest_path) -> bool:
            return False

    monkeypatch.setattr(client_module, "VaultClient", _FakeClient)


# ---------------------------------------------------------------------------
# av lineage
# ---------------------------------------------------------------------------

def test_lineage_link_without_server_queues(repo, unreachable_client):
    result = invoke("lineage", "link", "--cause-type", "commit", "--cause", "abcd",
                    "--metric", "val_loss")
    assert result.exit_code == 13, result.output


def test_lineage_link_and_show(repo, monkeypatch):
    _fake_client(monkeypatch,
                get_map={"/api/causal-links": (200, {"causal_links": [
                    {"id": 1, "cause_ref": "abcd1234", "effect_metric": "val_loss",
                     "effect_delta": -0.2, "verified": True}]})},
                post_map={"/api/causal-links": (201, {"status": "created", "id": 1})})
    link_result = invoke_json("lineage", "link", "--cause-type", "commit", "--cause", "abcd1234",
                              "--metric", "val_loss", "--delta", "-0.2", "--verified")
    assert link_result.exit_code == 0, link_result.output

    show_result = invoke_json("lineage", "show")
    rows = json.loads(show_result.output)["data"]["causal_links"]
    assert rows[0]["verified"] is True


# ---------------------------------------------------------------------------
# av search runs
# ---------------------------------------------------------------------------

def test_search_runs_without_server_queues(repo, unreachable_client):
    result = invoke("search", "runs", "--metric", "eval_acc")
    assert result.exit_code == 13, result.output


def test_search_runs_reports_matches(repo, monkeypatch):
    _fake_client(monkeypatch, get_map={"/api/search/runs": (200, {"matches": [
        {"run_id": "r1", "parent_run_id": "r0", "metric": "eval_acc",
         "value": 0.9, "parent_value": 0.8, "delta": 0.1}]})})
    result = invoke_json("search", "runs", "--metric", "eval_acc", "--direction", "up")
    matches = json.loads(result.output)["data"]["matches"]
    assert matches[0]["run_id"] == "r1"


# ---------------------------------------------------------------------------
# av strategy
# ---------------------------------------------------------------------------

def test_strategy_add_requires_valid_json_hyperparameters(repo, monkeypatch):
    _fake_client(monkeypatch)
    result = invoke("strategy", "add", "warmup", "--outcome", "worked",
                    "--hyperparameters", "not json")
    assert result.exit_code == 15, result.output


def test_strategy_add_search_show(repo, monkeypatch):
    entry = {"id": "s1", "technique": "warmup", "outcome": "worked",
            "hyperparameters": {"lr": 0.01}, "data_mix": None, "run_ids": ["r1"]}
    _fake_client(monkeypatch,
                get_map={"/api/strategy": (200, {"entries": [entry]})},
                post_map={"/api/strategy": (201, {"status": "created", "id": "s1"})})
    add_result = invoke_json("strategy", "add", "warmup", "--outcome", "worked",
                             "--hyperparameters", '{"lr": 0.01}', "--run", "r1")
    assert add_result.exit_code == 0, add_result.output

    search_result = invoke_json("strategy", "search", "--outcome", "worked")
    assert search_result.exit_code == 0, search_result.output
    assert json.loads(search_result.output)["data"]["entries"][0]["technique"] == "warmup"

    show_result = invoke_json("strategy", "show", "s1")
    assert show_result.exit_code == 0, show_result.output

    missing_result = invoke("strategy", "show", "no-such-entry")
    assert missing_result.exit_code == 15, missing_result.output


# ---------------------------------------------------------------------------
# av lessons
# ---------------------------------------------------------------------------

def test_lessons_show_with_none_published(repo, monkeypatch):
    _fake_client(monkeypatch, get_map={"/api/lessons/latest": (404, {})})
    result = invoke_json("lessons", "show")
    assert result.exit_code == 0, result.output
    # json_envelope() normalizes a None `data` to {} (core.py's own documented contract),
    # not to a JSON null — this pins the actual, correct shape.
    assert json.loads(result.output)["data"] == {}


def test_lessons_update_and_show(repo, monkeypatch, tmp_path):
    doc = {"beliefs": ["lr 3e-4 works better than 1e-3 for this arch"]}
    f = tmp_path / "lessons.json"
    f.write_text(json.dumps(doc), encoding="utf-8")
    _fake_client(monkeypatch,
                get_map={"/api/lessons/latest": (200, {"id": "l1", "object_id": "a" * 64})},
                post_map={"/api/lessons": (201, {"status": "created", "id": "l1"})})
    update_result = invoke_json("lessons", "update", str(f))
    assert update_result.exit_code == 0, update_result.output

    show_result = invoke_json("lessons", "show")
    assert show_result.exit_code == 0, show_result.output


# ---------------------------------------------------------------------------
# av blackboard
# ---------------------------------------------------------------------------

def test_blackboard_post_malformed_evidence(repo, monkeypatch):
    _fake_client(monkeypatch)
    result = invoke("blackboard", "post", "claim text", "--evidence", "not-a-typed-ref")
    assert result.exit_code == 15, result.output


def test_blackboard_post_list_resolve(repo, monkeypatch):
    _fake_client(monkeypatch,
                get_map={"/api/blackboard": (200, {"entries": [
                    {"id": "b1", "claim": "LR schedule X helps", "status": "open",
                     "evidence": [{"type": "run", "ref": "r1"}]}]})},
                post_map={"/api/blackboard": (201, {"status": "created", "id": "b1"}),
                         "/resolve": (200, {"id": "b1", "status": "resolved"})})
    post_result = invoke_json("blackboard", "post", "LR schedule X helps", "--evidence", "run:r1")
    assert post_result.exit_code == 0, post_result.output

    list_result = invoke_json("blackboard", "list")
    assert json.loads(list_result.output)["data"]["entries"][0]["claim"] == "LR schedule X helps"

    resolve_result = invoke_json("blackboard", "resolve", "b1")
    assert json.loads(resolve_result.output)["data"]["status"] == "resolved"
