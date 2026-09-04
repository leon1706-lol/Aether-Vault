"""av freeze / the freeze_guard() kill-switch (v1.3.1).

`freeze_guard()` is called explicitly from the promotion/self-edit gate commands only
(av promote, av improver register/propose/apply, av policy pack publish) — never from a
blanket per-invocation hook (see cmd_freeze.py's module docstring for why). These tests
fake the registry response the same way tests/test_exit_codes.py's `_fake_registry_client`
does, so the frozen/unfrozen DECISION is exercised without a live server; the actual
POST /api/freeze/{project_id} route (scope enforcement, hash-chain-free but audited state
transition) is proven live in tests/test_server.py.
"""
import json

from click.testing import CliRunner

from python.av_cli.main import cli


def invoke(*args):
    return CliRunner().invoke(cli, list(args))


def invoke_json(*args):
    return CliRunner().invoke(cli, ["--output", "json", *args])


def _fake_client(monkeypatch, get_body=None, get_status=200, post_status=200, post_body=None):
    import python.av_cli.client as client_module

    class _FakeResponse:
        def __init__(self, status_code, body):
            self.status_code = status_code
            self._body = body or {}

        def json(self):
            return self._body

        @property
        def text(self):
            return json.dumps(self._body)

    class _FakeSession:
        def get(self, url, params=None, timeout=None):
            return _FakeResponse(get_status, get_body)

        def post(self, url, json=None):
            return _FakeResponse(post_status, post_body)

    class _FakeClient(client_module.VaultClient):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            self.session = _FakeSession()

        def server_available(self) -> bool:
            return True

    monkeypatch.setattr(client_module, "VaultClient", _FakeClient)


def test_status_reports_not_frozen(repo, monkeypatch):
    _fake_client(monkeypatch, get_body={"project_id": "p", "frozen": False, "reason": None})
    result = invoke_json("freeze", "status")
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["data"]["frozen"] is False


def test_status_reports_frozen_with_reason(repo, monkeypatch):
    _fake_client(monkeypatch, get_body={"project_id": "p", "frozen": True, "reason": "audit"})
    result = invoke_json("freeze", "status")
    env = json.loads(result.output)
    assert env["data"]["frozen"] is True
    assert env["data"]["reason"] == "audit"


def test_status_fails_open_when_unreachable(repo):
    """No server in the test sandbox — project_frozen() must fail OPEN (not frozen),
    never raise, matching the documented "must not become a new offline-resilience
    hazard" contract."""
    result = invoke_json("freeze", "status")
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["data"]["frozen"] is False


def test_freeze_on_succeeds_with_admin_scope(repo, monkeypatch):
    _fake_client(monkeypatch, post_body={"project_id": "p", "frozen": True, "reason": "test"})
    result = invoke_json("freeze", "on", "--reason", "test")
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["data"]["frozen"] is True


def test_freeze_on_without_reachable_server_queues(repo, unreachable_client):
    result = invoke("freeze", "on")
    assert result.exit_code == 13, result.output  # unreachable_queued


def test_freeze_off_succeeds(repo, monkeypatch):
    _fake_client(monkeypatch, post_body={"project_id": "p", "frozen": False, "reason": None})
    result = invoke_json("freeze", "off")
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["data"]["frozen"] is False


def test_bare_freeze_shows_status(repo, monkeypatch):
    """`av freeze` with no subcommand is `invoke_without_command=True` -> status."""
    _fake_client(monkeypatch, get_body={"project_id": "p", "frozen": False, "reason": None})
    result = invoke("freeze")
    assert result.exit_code == 0, result.output
    assert "not frozen" in result.output.lower()


def test_promote_blocked_while_frozen(repo, monkeypatch):
    _fake_client(monkeypatch, get_body={"project_id": "p", "frozen": True, "reason": "incident"})
    (repo / "m.txt").write_text("v1")
    invoke("add", "m.txt")
    invoke("commit", "-m", "baseline", "--no-upload")

    result = invoke("promote", "--into", "main")
    assert result.exit_code == 18, result.output


def test_promote_dry_run_is_not_blocked_by_freeze(repo, monkeypatch):
    """Dry-run's documented contract ("touches nothing either way") means it must not
    even reach the freeze check — proven by never faking a reachable client at all: if
    freeze_guard() were reached it would try a real socket and this test would need it."""
    (repo / "m.txt").write_text("v1")
    invoke("add", "m.txt")
    invoke("commit", "-m", "baseline", "--no-upload")

    result = invoke_json("promote", "--into", "main", "--dry-run")
    assert result.exit_code == 0, result.output


def test_improver_register_blocked_while_frozen(repo, monkeypatch):
    _fake_client(monkeypatch, get_body={"project_id": "p", "frozen": True, "reason": "incident"})
    result = invoke("improver", "register")
    assert result.exit_code == 18, result.output


def test_incident_rollback_freezes_then_rolls_back(repo, monkeypatch):
    _fake_client(monkeypatch, post_body={"project_id": "p", "frozen": True, "reason": "incident rollback"})
    # Seed a rollback target so improver_rollback() has somewhere to go.
    (repo / ".av" / "improver").mkdir(parents=True, exist_ok=True)
    (repo / ".av" / "improver" / "last_good").write_text("improver-abc", encoding="utf-8")

    result = invoke_json("incident", "rollback")
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["data"]["frozen"] is True
    assert (repo / ".av" / "improver" / "current").read_text(encoding="utf-8").strip() == "improver-abc"
