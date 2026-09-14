"""V1.6.3: `av registry export/restore` never hold an object whole in RAM -- downloads go
through the streaming, hash-verified `VaultClient.download_object`, uploads hand an open
file to `requests`."""
import hashlib
import json
import os

import pytest
from click.testing import CliRunner

from python.av_cli import cmd_registry
from python.av_cli.main import cli


class _Resp:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


class _FakeSession:
    def __init__(self, routes):
        self.routes = routes
        self.gets = []
        self.posts = []

    def get(self, url, **kwargs):
        self.gets.append((url, kwargs))
        path = url.split("http://fake", 1)[1]
        for prefix, payload in self.routes.items():
            if path.startswith(prefix):
                return _Resp(200, payload)
        return _Resp(404, None)

    def post(self, url, data=None, **kwargs):
        self.posts.append((url, data, kwargs))
        return _Resp(201, None)

    def put(self, url, **kwargs):
        return _Resp(200, None)


class _FakeClient:
    server_url = "http://fake"

    def __init__(self, routes, objects: dict[str, bytes]):
        self.session = _FakeSession(routes)
        self.objects = objects
        self.downloads = []

    def server_available(self):
        return True

    def download_object(self, sha256_hash, dest_path):
        self.downloads.append(sha256_hash)
        data = self.objects.get(sha256_hash)
        if data is None:
            return False
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        dest_path.write_bytes(data)
        return True


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert CliRunner().invoke(cli, ["init", "--mode", "local", "--yes", "--no-repl"]).exit_code == 0
    return tmp_path


def _fake_registry(monkeypatch):
    blob = os.urandom(4096)
    h = hashlib.sha256(blob).hexdigest()
    commit = {"hash": "c" * 64, "parent_hash": None, "timestamp": "2026-01-01T00:00:00", "message": "m",
              "tree": {"model.bin": {"hash": h, "size": len(blob), "type": "artifact", "layers": [], "chunks": []}}}
    routes = {
        "/api/commits": {"commits": [commit], "next_offset": None},
        "/api/refs": {"proj/main": "c" * 64},
        "/api/runs": {"runs": []},
    }
    client = _FakeClient(routes, {h: blob})
    monkeypatch.setattr(cmd_registry, "_client", lambda repo_root: client)
    return client, h, blob


def test_export_streams_objects_through_download_object(repo, monkeypatch, tmp_path):
    client, h, blob = _fake_registry(monkeypatch)
    out = tmp_path / "archive"
    res = CliRunner().invoke(cli, ["--output", "json", "registry", "export", str(out)])
    assert res.exit_code == 0, res.output
    data = json.loads(res.output)["data"]
    assert data["objects_ok"] == 1 and data["objects_failed"] == 0
    assert client.downloads == [h]
    # No whole-body GET for an object ever happened.
    assert not any("/api/objects/" in url for url, _ in client.session.gets)
    assert (out / "objects" / h[:2] / h[2:]).read_bytes() == blob


def test_restore_uploads_shards_as_open_files(repo, monkeypatch, tmp_path):
    client, h, blob = _fake_registry(monkeypatch)
    out = tmp_path / "archive"
    assert CliRunner().invoke(cli, ["--output", "json", "registry", "export", str(out)]).exit_code == 0

    res = CliRunner().invoke(cli, ["--output", "json", "registry", "restore", str(out)])
    assert res.exit_code == 0, res.output
    object_posts = [(url, data) for url, data, _ in client.session.posts if "/api/objects/" in url]
    assert len(object_posts) == 1
    url, data = object_posts[0]
    assert url.endswith(h)
    assert hasattr(data, "read"), "shard must be streamed as a file object, not read into bytes"


def test_restore_skips_a_corrupt_shard_without_reading_it_whole(repo, monkeypatch, tmp_path):
    client, h, blob = _fake_registry(monkeypatch)
    out = tmp_path / "archive"
    assert CliRunner().invoke(cli, ["--output", "json", "registry", "export", str(out)]).exit_code == 0
    (out / "objects" / h[:2] / h[2:]).write_bytes(b"tampered")
    res = CliRunner().invoke(cli, ["--output", "json", "registry", "restore", str(out)])
    assert res.exit_code == 0, res.output
    assert not any("/api/objects/" in url for url, _, _ in client.session.posts)
    assert json.loads(res.output)["data"]["failed"] == 1


def test_sha256_streamed_matches_hashlib(tmp_path):
    p = tmp_path / "x.bin"
    payload = os.urandom(3 * 1024 * 1024 + 17)
    p.write_bytes(payload)
    assert cmd_registry._sha256_streamed(p) == hashlib.sha256(payload).hexdigest()
