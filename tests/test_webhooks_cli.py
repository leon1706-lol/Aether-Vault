"""av webhooks CLI tests — fake VaultClient session against the stable API shapes."""
import json

import pytest
from click.testing import CliRunner

from python.av_cli.main import cli


class FakeResp:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, json=None, timeout=None):
        self.calls.append((method, url, json))
        return self.responses.pop(0)

    def get(self, url, timeout=None):
        self.calls.append(("GET", url, None))
        return self.responses.pop(0)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    res = CliRunner().invoke(cli, ["init", "--mode", "local", "--yes", "--no-repl"])
    assert res.exit_code == 0, res.output
    return tmp_path


def _fake_client(monkeypatch, responses):
    from python.av_cli import cmd_webhooks

    sess = FakeSession(responses)

    class FC:
        server_url = "http://localhost:8000"
        session = sess  # noqa: A001 — attribute name mirrors VaultClient's

    monkeypatch.setattr(cmd_webhooks, "_client", lambda repo_root: FC())
    return sess


def test_webhook_add_posts_payload_and_prints_id(repo, monkeypatch):
    session = _fake_client(monkeypatch, [FakeResp(200, {"id": "abcd1234", "url": "http://x", "active": True})])
    res = CliRunner().invoke(cli, ["webhooks", "add", "http://x/hook", "--secret", "s3",
                                   "--project", "p1", "--kind", "commit"])
    assert res.exit_code == 0, res.output
    method, url, body = session.calls[0]
    assert (method, url) == ("POST", "http://localhost:8000/api/webhooks")
    assert body == {"url": "http://x/hook", "secret": "s3",
                    "project_id": "p1", "kinds": ["commit"]}
    assert "abcd12" in res.output


def test_webhook_add_validation_failure_surfaces_detail(repo, monkeypatch):
    session = _fake_client(monkeypatch, [FakeResp(422, {"detail": "url must be http(s)"})])
    res = CliRunner().invoke(cli, ["webhooks", "add", "ftp://bad", "--secret", "s"])
    assert res.exit_code != 0
    assert "url must be http(s)" in res.output
    assert session.calls[0][1].endswith("/api/webhooks")


def test_webhook_list_masks_and_shows_state(repo, monkeypatch):
    session = _fake_client(monkeypatch, [FakeResp(200, {"webhooks": [
        {"id": "abcd1234", "url": "http://x/hook", "project_id": None,
         "kinds": ["commit"], "active": True, "secret": "s3…"}
    ]})])
    res = CliRunner().invoke(cli, ["webhooks", "list"])
    assert res.exit_code == 0, res.output
    assert "[active]" in res.output and "abcd12" in res.output
    assert "s3…" in res.output


def test_webhook_remove_404_maps_to_clean_error(repo, monkeypatch):
    session = _fake_client(monkeypatch, [FakeResp(404, {"detail": "Webhook not found"})])
    res = CliRunner().invoke(cli, ["webhooks", "remove", "nope1234"])
    assert res.exit_code != 0
    assert "No such webhook" in res.output


def test_webhook_test_delivers(repo, monkeypatch):
    session = _fake_client(monkeypatch, [FakeResp(200, {"status": "delivered"}),
                                          FakeResp(200, {"status": "delivered"})])
    res = CliRunner().invoke(cli, ["--output", "json", "webhooks", "test", "abcd1234"])
    env = json.loads(res.output)
    assert env["data"]["delivered"] is True
    assert session.calls[0][:2] == ("POST", "http://localhost:8000/api/webhooks/abcd1234/test")
