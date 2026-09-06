"""Tests for python/av_cli/client.py — VaultClient's batch-existence check, the
known_missing fast path on upload_object (added to cut commit latency, see
development/Probleme.md and BENCHMARKS.md #3), and the "Protected" mode token handling."""
import pytest

from python.av_cli.client import AuthenticationError, VaultClient


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


# ---------------------------------------------------------------------------
# "Protected" mode — token header + 401 handling
# ---------------------------------------------------------------------------

def test_constructing_with_a_token_sets_the_authorization_header():
    client = VaultClient(api_token="my-secret")
    assert client.session.headers["Authorization"] == "Bearer my-secret"


def test_constructing_without_a_token_sets_no_authorization_header():
    client = VaultClient()
    assert "Authorization" not in client.session.headers


@pytest.mark.parametrize(
    "method_name, call",
    [
        ("batch_check_objects", lambda c: c.batch_check_objects(["aaa"])),
        ("push_commit", lambda c: c.push_commit({"hash": "x"})),
        ("update_ref", lambda c: c.update_ref("main", "x")),
        ("get_commit", lambda c: c.get_commit("x")),
        ("get_ref", lambda c: c.get_ref("main")),
        ("run_gc", lambda c: c.run_gc()),
        ("object_exists", lambda c: c.object_exists("x")),
    ],
)
def test_methods_raise_authentication_error_on_401(monkeypatch, method_name, call):
    client = VaultClient()
    fake_401 = _FakeResponse(401)
    monkeypatch.setattr(client.session, "get", lambda *a, **k: fake_401)
    monkeypatch.setattr(client.session, "post", lambda *a, **k: fake_401)
    monkeypatch.setattr(client.session, "put", lambda *a, **k: fake_401)
    monkeypatch.setattr(client.session, "head", lambda *a, **k: fake_401)

    with pytest.raises(AuthenticationError):
        call(client)


def test_server_available_never_raises_authentication_error_even_on_401(monkeypatch):
    # server_available() must stay a plain bool probe (never raise) even on a 401, since
    # restart_service's readiness wait calls this with zero credentials before any token exists.
    client = VaultClient()
    monkeypatch.setattr(client.session, "get", lambda *a, **k: _FakeResponse(401))
    assert client.server_available() is False


def test_upload_object_raises_authentication_error_from_head_check(monkeypatch, tmp_path):
    client = VaultClient()
    f = tmp_path / "obj.bin"
    f.write_bytes(b"content")
    monkeypatch.setattr(client.session, "head", lambda *a, **k: _FakeResponse(401))

    with pytest.raises(AuthenticationError):
        client.upload_object(f, "deadbeef")


def test_download_object_raises_authentication_error_on_401(monkeypatch, tmp_path):
    client = VaultClient()

    class _FakeStreamResponse:
        status_code = 401
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    monkeypatch.setattr(client.session, "get", lambda *a, **k: _FakeStreamResponse())

    with pytest.raises(AuthenticationError):
        client.download_object("deadbeef", tmp_path / "out.bin")
