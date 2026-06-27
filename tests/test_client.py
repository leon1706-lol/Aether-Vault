"""Tests for python/av_cli/client.py — VaultClient's batch-existence check and the
known_missing fast path on upload_object (added to cut commit latency, see
development/Probleme.md and BENCHMARKS.md #3)."""
from python.av_cli.client import VaultClient


class _FakeResponse:
    def __init__(self, status_code: int, json_data: dict | None = None):
        self.status_code = status_code
        self._json_data = json_data or {}

    def json(self):
        return self._json_data


def test_batch_check_objects_posts_hashes_and_returns_found_set(monkeypatch):
    client = VaultClient()
    calls = {}

    def fake_post(url, json=None, **kwargs):
        calls["url"] = url
        calls["json"] = json
        return _FakeResponse(200, {"found": ["aaa", "bbb"], "missing": ["ccc"]})

    monkeypatch.setattr(client.session, "post", fake_post)

    result = client.batch_check_objects(["aaa", "bbb", "ccc"])

    assert result == {"aaa", "bbb"}
    assert calls["url"].endswith("/api/sync/batch-objects")
    assert calls["json"] == ["aaa", "bbb", "ccc"]


def test_batch_check_objects_empty_input_skips_request(monkeypatch):
    client = VaultClient()

    def fake_post(*args, **kwargs):
        raise AssertionError("should not make a request for an empty hash list")

    monkeypatch.setattr(client.session, "post", fake_post)

    assert client.batch_check_objects([]) == set()


def test_batch_check_objects_returns_empty_set_on_non_200(monkeypatch):
    client = VaultClient()
    monkeypatch.setattr(client.session, "post", lambda *a, **k: _FakeResponse(500))

    assert client.batch_check_objects(["aaa"]) == set()


def test_upload_object_known_missing_skips_head_request(monkeypatch, tmp_path):
    client = VaultClient()
    f = tmp_path / "obj.bin"
    f.write_bytes(b"content")

    def fake_head(*args, **kwargs):
        raise AssertionError("known_missing=True must not issue a HEAD request")

    monkeypatch.setattr(client.session, "head", fake_head)
    monkeypatch.setattr(client.session, "post", lambda *a, **k: _FakeResponse(201))

    assert client.upload_object(f, "deadbeef", known_missing=True) is True


def test_upload_object_default_still_checks_head_first(monkeypatch, tmp_path):
    client = VaultClient()
    f = tmp_path / "obj.bin"
    f.write_bytes(b"content")

    monkeypatch.setattr(client.session, "head", lambda *a, **k: _FakeResponse(200))

    def fake_post(*args, **kwargs):
        raise AssertionError("should short-circuit on HEAD 200 without posting")

    monkeypatch.setattr(client.session, "post", fake_post)

    assert client.upload_object(f, "deadbeef") is True
