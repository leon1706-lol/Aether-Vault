"""av sandbox / av tools manifest / av replay-actions — the CLI layer over the sandbox
drivers + tool manifests + action log (v1.3.1, RSI R5)."""
import json
import sys

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
            return _FakeResponse(200, {"status": "ok"})

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
# av sandbox run — uses the REAL local driver (no server needed for execution itself)
# ---------------------------------------------------------------------------

def test_sandbox_run_local_succeeds(repo):
    result = invoke_json("sandbox", "run", "--driver", "local", "--",
                        sys.executable, "-c", "print('ok')")
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)["data"]
    assert data["state"] == "succeeded"


def test_sandbox_run_local_failure_exits_validation(repo):
    result = invoke("sandbox", "run", "--driver", "local", "--",
                    sys.executable, "-c", "import sys; sys.exit(1)")
    assert result.exit_code == 15, result.output


def test_sandbox_run_rejects_mount_outside_manifest(repo, tmp_path):
    from python.av_cli.sandbox.manifest import save_manifest

    save_manifest(repo, "imp-1", {"writable_paths": ["/allowed/*"]})
    result = invoke("sandbox", "run", "--driver", "local", "--improver", "imp-1",
                    "--mount", "/forbidden:/data:rw", "--", "echo", "hi")
    assert result.exit_code == 15, result.output
    assert "manifest violation" in result.output.lower()


def test_sandbox_run_allows_mount_matching_manifest(repo):
    from python.av_cli.sandbox.manifest import save_manifest

    save_manifest(repo, "imp-2", {"writable_paths": ["/allowed/*"]})
    result = invoke_json("sandbox", "run", "--driver", "local", "--improver", "imp-2",
                        "--mount", "/allowed/data:/data:rw", "--", sys.executable, "-c", "print('x')")
    assert result.exit_code == 0, result.output


def test_sandbox_run_rejects_malformed_mount(repo):
    result = invoke("sandbox", "run", "--driver", "local", "--mount", "not-valid",
                    "--", "echo", "hi")
    assert result.exit_code == 15, result.output


# ---------------------------------------------------------------------------
# av sandbox status/cancel/logs — driver plumbing (local, real subprocess)
# ---------------------------------------------------------------------------

def test_sandbox_status_and_logs_after_run(repo, monkeypatch):
    _fake_client(monkeypatch)
    run_result = invoke_json("sandbox", "run", "--driver", "local", "--job-id", "myjob",
                            "--", sys.executable, "-c", "print('hello world')")
    assert run_result.exit_code == 0, run_result.output

    status_result = invoke_json("sandbox", "status", "myjob", "--driver", "local")
    assert json.loads(status_result.output)["data"]["state"] == "succeeded"

    logs_result = invoke_json("sandbox", "logs", "myjob", "--driver", "local")
    assert "hello world" in json.loads(logs_result.output)["data"]["output"]


def test_sandbox_cancel_already_finished_job(repo):
    invoke("sandbox", "run", "--driver", "local", "--job-id", "job-done",
          "--", sys.executable, "-c", "print('x')")
    result = invoke_json("sandbox", "cancel", "job-done", "--driver", "local")
    assert json.loads(result.output)["data"]["cancelled"] is False


# ---------------------------------------------------------------------------
# av sandbox queue
# ---------------------------------------------------------------------------

def test_sandbox_queue_without_server_queues(repo, unreachable_client):
    result = invoke("sandbox", "queue")
    assert result.exit_code == 13, result.output


def test_sandbox_queue_lists_jobs(repo, monkeypatch):
    _fake_client(monkeypatch, get_map={"/api/sandbox/jobs": (200, {"jobs": [
        {"id": "j1", "driver": "local", "state": "succeeded"}]})})
    result = invoke_json("sandbox", "queue")
    jobs = json.loads(result.output)["data"]["jobs"]
    assert jobs[0]["id"] == "j1"


# ---------------------------------------------------------------------------
# av tools manifest
# ---------------------------------------------------------------------------

def test_tools_manifest_show_defaults_restrictive(repo):
    result = invoke_json("tools", "manifest", "show", "no-such-improver")
    data = json.loads(result.output)["data"]
    assert data["network"] == "none"
    assert data["gpu"] is False


def test_tools_manifest_set_and_show(repo):
    set_result = invoke_json("tools", "manifest", "set", "imp-1",
                            "--writable-path", "/data/*", "--network", "bridge", "--gpu")
    assert set_result.exit_code == 0, set_result.output

    show_result = invoke_json("tools", "manifest", "show", "imp-1")
    data = json.loads(show_result.output)["data"]
    assert data["writable_paths"] == ["/data/*"]
    assert data["network"] == "bridge"
    assert data["gpu"] is True


def test_tools_manifest_set_with_publish(repo, monkeypatch):
    _fake_client(monkeypatch, post_map={
        "/api/tool-manifests": (201, {"status": "created", "id": "tm-1"})})
    result = invoke_json("tools", "manifest", "set", "imp-1", "--writable-path", "/x/*", "--publish")
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["data"]["published_id"] == "tm-1"


def test_tools_manifest_verify_allowed(repo):
    invoke("tools", "manifest", "set", "imp-1", "--writable-path", "/data/*")
    result = invoke_json("tools", "manifest", "verify", "imp-1", "--mount", "/data/x:/data:rw",
                        "--", "echo", "hi")
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["data"]["allowed"] is True


def test_tools_manifest_verify_denied(repo):
    result = invoke("tools", "manifest", "verify", "no-such-improver", "--network", "bridge",
                    "--", "echo", "hi")
    assert result.exit_code == 15, result.output


# ---------------------------------------------------------------------------
# actionlog.py + av replay-actions
# ---------------------------------------------------------------------------

def test_log_action_appends_jsonl(repo):
    from python.av_cli.actionlog import log_action, read_actions

    log_action(repo, "proposed_change", details={"risk": "low"})
    log_action(repo, "applied_change", command=["echo", "hi"])
    actions = read_actions(repo)
    assert len(actions) == 2
    assert actions[0]["action"] == "proposed_change"
    assert actions[1]["command"] == ["echo", "hi"]


def test_read_actions_empty_when_no_log(repo):
    from python.av_cli.actionlog import read_actions

    assert read_actions(repo) == []


def test_read_actions_tolerates_corrupt_trailing_line(repo):
    from python.av_cli.actionlog import log_action, read_actions

    log_action(repo, "good_action")
    (repo / ".av" / "actions.jsonl").open("a", encoding="utf-8").write("not json\n")
    actions = read_actions(repo)
    assert len(actions) == 1


def test_replay_actions_without_server_queues(repo, unreachable_client):
    result = invoke("replay-actions", "some-log-id")
    assert result.exit_code == 13, result.output


def test_replay_actions_prints_sequence(repo, monkeypatch):
    from python.av_cli import casobj

    doc = {"kind": "action_log", "actions": [
        {"ts": "2026-01-01T00:00:00Z", "actor": "agent", "action": "proposed", "details": {}},
    ]}
    object_id = casobj.write_object(repo, doc)
    _fake_client(monkeypatch, get_map={
        "/api/action-logs/log-1": (200, {"id": "log-1", "object_id": object_id})})

    result = invoke_json("replay-actions", "log-1")
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)["data"]
    assert data["log_id"] == "log-1"
    assert len(data["actions"]) == 1


def test_replay_actions_falls_back_to_run_id_lookup(repo, monkeypatch):
    from python.av_cli import casobj

    doc = {"kind": "action_log", "actions": []}
    object_id = casobj.write_object(repo, doc)
    _fake_client(monkeypatch, get_map={
        "/api/action-logs/run-1": (404, {}),
        "/api/action-logs": (200, {"action_logs": [{"id": "log-2", "object_id": object_id}]})})

    result = invoke_json("replay-actions", "run-1")
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["data"]["log_id"] == "log-2"


def test_replay_actions_unknown_target_fails(repo, monkeypatch):
    _fake_client(monkeypatch, get_map={
        "/api/action-logs/nope": (404, {}), "/api/action-logs": (200, {"action_logs": []})})
    result = invoke("replay-actions", "nope")
    assert result.exit_code == 15, result.output


def test_replay_actions_execute_reruns_commands(repo, monkeypatch):
    from python.av_cli import casobj

    doc = {"kind": "action_log", "actions": [
        {"ts": "t", "actor": "agent", "action": "ran_check",
         "command": [sys.executable, "-c", "print('ran')"]},
    ]}
    object_id = casobj.write_object(repo, doc)
    _fake_client(monkeypatch, get_map={
        "/api/action-logs/log-3": (200, {"id": "log-3", "object_id": object_id})})

    result = invoke_json("replay-actions", "log-3", "--execute")
    assert result.exit_code == 0, result.output
    executed = json.loads(result.output)["data"]["executed"]
    assert executed[0]["state"] == "succeeded"
