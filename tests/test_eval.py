"""av eval — task/eval registry, blind scoring, external adapters (v1.3.1, RSI R2).

Fake-registry technique shared with tests/test_improver.py and tests/test_policy_pack.py.
Real server-side enforcement (the `scorer`/`eval:write` scope checks, the frozen-suite
409, blind-result redaction) is proven live in tests/test_server.py.
"""
import json

import pytest
from click.testing import CliRunner

from python.av_cli.main import cli


def invoke(*args):
    return CliRunner().invoke(cli, list(args))


def invoke_json(*args):
    return CliRunner().invoke(cli, ["--output", "json", *args])


class _FakeEvalRegistry:
    def __init__(self):
        self.suites = {}
        self.results = {}
        self.adapters = {}
        self.objects = set()
        self._next_result = 1

    def create_suite(self, body):
        sid = body.get("id") or f"suite-{len(self.suites)}"
        if sid in self.suites:
            return 200, {"status": "exists", "id": sid}
        self.suites[sid] = {"id": sid, "project_id": body["project_id"],
                            "object_id": body["object_id"], "name": body.get("name"),
                            "frozen": False, "blind": bool(body.get("blind")),
                            "created_at": "2026-01-01T00:00:00"}
        return 201, {"status": "created", "id": sid}

    def create_result(self, body):
        suite = self.suites.get(body["suite_id"])
        if suite is None:
            return 422, {"detail": f"unknown suite {body['suite_id']}"}
        rid = self._next_result
        self._next_result += 1
        row = {"id": rid, "project_id": body["project_id"], "suite_id": body["suite_id"],
              "run_id": body.get("run_id"), "score": body.get("score"), "details": None,
              "revealed": not suite["blind"], "scored_by": None,
              "created_at": "2026-01-01T00:00:00"}
        self.results[rid] = row
        return 201, {"status": "recorded", "id": rid, "revealed": row["revealed"]}

    def reveal(self, rid):
        row = self.results.get(rid)
        if row is None:
            return 404, {"detail": "not found"}
        row["revealed"] = True
        return 200, dict(row)


def _fake_client(monkeypatch, reg: _FakeEvalRegistry, forbidden_scopes=()):
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
            if "/api/eval/suites/" in url:
                sid = url.rsplit("/", 1)[-1]
                row = reg.suites.get(sid)
                return _FakeResponse(200, row) if row else _FakeResponse(404, {})
            if url.endswith("/api/eval/suites"):
                rows = [r for r in reg.suites.values() if r["project_id"] == params.get("project_id")]
                return _FakeResponse(200, {"suites": rows})
            if url.endswith("/api/eval/results"):
                rows = [r for r in reg.results.values()]
                return _FakeResponse(200, {"eval_results": rows})
            if url.endswith("/api/eval/adapters"):
                rows = [r for r in reg.adapters.values() if r["project_id"] == params.get("project_id")]
                return _FakeResponse(200, {"adapters": rows})
            if url.endswith("/api/tasks"):
                return _FakeResponse(200, {"tasks": []})
            return _FakeResponse(404, {})

        def post(self, url, json=None):
            body = json or {}
            if "eval:write" in forbidden_scopes and (url.endswith("/api/eval/suites")
                                                      or url.endswith("/freeze")):
                return _FakeResponse(403, {"detail": {"error": "scope_denied", "required_scope": "eval:write"}})
            if "scorer" in forbidden_scopes and (url.endswith("/api/eval/results")
                                                  or url.endswith("/reveal")):
                return _FakeResponse(403, {"detail": {"error": "scope_denied", "required_scope": "scorer"}})
            if url.endswith("/api/eval/suites"):
                status, resp = reg.create_suite(body)
                return _FakeResponse(status, resp)
            if url.endswith("/freeze"):
                sid = url.split("/api/eval/suites/")[1].split("/freeze")[0]
                reg.suites[sid]["frozen"] = True
                return _FakeResponse(200, reg.suites[sid])
            if url.endswith("/api/eval/results"):
                status, resp = reg.create_result(body)
                return _FakeResponse(status, resp)
            if url.endswith("/reveal"):
                rid = int(url.split("/api/eval/results/")[1].split("/reveal")[0])
                status, resp = reg.reveal(rid)
                return _FakeResponse(status, resp)
            if url.endswith("/api/eval/adapters"):
                aid = f"adapter-{len(reg.adapters)}"
                reg.adapters[aid] = {"id": aid, **body}
                return _FakeResponse(201, {"status": "created", "id": aid})
            return _FakeResponse(404, {})

        def upload_object(self, *a, **k):
            return True

    class _FakeClient(client_module.VaultClient):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            self.session = _FakeSession()

        def server_available(self) -> bool:
            return True

        def upload_object(self, file_path, sha256_hash, known_missing=False) -> bool:
            reg.objects.add(sha256_hash)
            return True

    monkeypatch.setattr(client_module, "VaultClient", _FakeClient)


@pytest.fixture
def fake_eval(monkeypatch):
    reg = _FakeEvalRegistry()
    _fake_client(monkeypatch, reg)
    return reg


def _write_suite_file(tmp_path, checks=None):
    p = tmp_path / "suite.json"
    p.write_text(json.dumps({"checks": checks or []}), encoding="utf-8")
    return p


def test_register_without_server_queues(repo, tmp_path, unreachable_client):
    f = _write_suite_file(tmp_path)
    result = invoke("eval", "register", "core", str(f))
    assert result.exit_code == 13, result.output


def test_register_and_list(repo, fake_eval, tmp_path):
    f = _write_suite_file(tmp_path)
    result = invoke_json("eval", "register", "core", str(f), "--blind")
    assert result.exit_code == 0, result.output
    env = json.loads(result.output)
    assert env["data"]["blind"] is True

    listed = invoke_json("eval", "list")
    rows = json.loads(listed.output)["data"]["suites"]
    assert any(r["id"] == env["data"]["id"] for r in rows)


def test_freeze_then_show(repo, fake_eval, tmp_path):
    f = _write_suite_file(tmp_path)
    sid = json.loads(invoke_json("eval", "register", "core", str(f)).output)["data"]["id"]
    freeze_result = invoke_json("eval", "freeze", sid)
    assert freeze_result.exit_code == 0, freeze_result.output

    show_result = invoke_json("eval", "show", sid)
    assert json.loads(show_result.output)["data"]["frozen"] is True


def test_score_and_reveal_blind_suite(repo, fake_eval, tmp_path):
    f = _write_suite_file(tmp_path)
    sid = json.loads(invoke_json("eval", "register", "core", str(f), "--blind").output)["data"]["id"]

    score_result = invoke_json("eval", "score", sid, "--metric", "acc=0.9")
    assert score_result.exit_code == 0, score_result.output
    result_data = json.loads(score_result.output)["data"]
    assert result_data["revealed"] is False

    reveal_result = invoke_json("eval", "reveal", str(result_data["id"]))
    assert reveal_result.exit_code == 0, reveal_result.output
    assert json.loads(reveal_result.output)["data"]["revealed"] is True


def test_score_against_unknown_suite_fails(repo, fake_eval):
    result = invoke("eval", "score", "no-such-suite", "--metric", "acc=1")
    assert result.exit_code == 15, result.output


def test_score_denied_without_scorer_scope(repo, monkeypatch, tmp_path):
    reg = _FakeEvalRegistry()
    _fake_client(monkeypatch, reg, forbidden_scopes=("scorer",))
    f = _write_suite_file(tmp_path)
    sid = json.loads(invoke_json("eval", "register", "core", str(f)).output)["data"]["id"]

    result = invoke("eval", "score", sid, "--metric", "acc=0.9")
    assert result.exit_code == 20, result.output


def test_freeze_denied_without_eval_write_scope(repo, monkeypatch, tmp_path):
    reg = _FakeEvalRegistry()
    _fake_client(monkeypatch, reg, forbidden_scopes=("eval:write",))
    result = invoke("eval", "freeze", "some-suite")
    assert result.exit_code == 20, result.output


def test_adapter_add_list_and_run(repo, fake_eval, tmp_path):
    add_result = invoke_json("eval", "adapter", "add", "scorer1",
                             "--command", "python -c \"import sys,json; print(json.dumps({'ok': True}))\"")
    assert add_result.exit_code == 0, add_result.output

    list_result = invoke_json("eval", "adapter", "list")
    names = [a["name"] for a in json.loads(list_result.output)["data"]["adapters"]]
    assert "scorer1" in names

    run_result = invoke_json("eval", "adapter", "run", "scorer1")
    assert run_result.exit_code == 0, run_result.output
    assert json.loads(run_result.output)["data"]["result"] == {"ok": True}


def test_adapter_run_unknown_name_fails(repo, fake_eval):
    result = invoke("eval", "adapter", "run", "nope")
    assert result.exit_code == 15, result.output


def test_adapter_run_nonzero_exit_is_a_failed_scoring(repo, fake_eval):
    invoke("eval", "adapter", "add", "failer", "--command", "python -c \"import sys; sys.exit(1)\"")
    result = invoke("eval", "adapter", "run", "failer")
    assert result.exit_code == 15, result.output
