"""Token scopes (v1.3.1): `_scopes_for_identity` and the `require_scope()` dependency.

Same stack-free layering as tests/test_auth_users.py, which this file deliberately
does NOT duplicate — `_parse_auth_users`'s scopes-parsing additions are unit-tested here;
`_resolve_identity`'s own behavior (unchanged by this feature) stays in that file.

Two layers:
1. `_parse_auth_users` / `_scopes_for_identity` — pure unit tests against monkeypatched
   module globals, no server, no database.
2. `require_scope()`'s dependency — exercised directly (it's a plain async callable) against
   a stub Request and a stub AsyncSession whose `add`/`commit` are no-ops, so the
   allow/deny/audit logic is proven without Postgres. The live end-to-end 403 (a real
   route gated by `require_scope`) lands in tests/test_server.py once R1/R2 wire one up.
"""
import asyncio
import os
import tempfile
from types import SimpleNamespace

# Same import-time env pinning as test_auth_users.py — database.py/redis_cache.py/
# storage.py read their config at import time.
os.environ["DATABASE_URL"] = os.environ.get(
    "AV_TEST_DATABASE_URL",
    "postgresql+asyncpg://av_user:av_password@localhost:5432/aether_vault_test",
)
os.environ["REDIS_URL"] = os.environ.get("AV_TEST_REDIS_URL", "redis://localhost:6379/0")
os.environ["AV_DATA_DIR"] = tempfile.mkdtemp(prefix="av-scopes-test-")

import pytest  # noqa: E402
from fastapi import HTTPException  # noqa: E402

import python.av_server.server as server_module  # noqa: E402
from python.av_server.server import _parse_auth_users, _scopes_for_identity  # noqa: E402


# ---------------------------------------------------------------------------
# _parse_auth_users — scopes are additive, never forced onto pre-existing shapes
# ---------------------------------------------------------------------------

def test_parse_omits_scopes_key_when_not_specified():
    # Byte-for-byte the pre-v1.3.1 shape when the raw payload never mentioned scopes —
    # this is the compatibility invariant test_auth_users.py's exact-dict-equality
    # assertions depend on staying true.
    assert _parse_auth_users('{"alice": "tok-a"}') == {
        "alice": {"token": "tok-a", "expires_at": None},
    }
    assert _parse_auth_users(
        '{"alice": {"token": "tok-a", "expires_at": "2020-01-01T00:00:00+00:00"}}'
    ) == {"alice": {"token": "tok-a", "expires_at": "2020-01-01T00:00:00+00:00"}}


def test_parse_accepts_scopes_list():
    parsed = _parse_auth_users('{"alice": {"token": "tok-a", "scopes": ["read", "write"]}}')
    assert parsed == {"alice": {"token": "tok-a", "expires_at": None, "scopes": ["read", "write"]}}


def test_parse_dedupes_and_sorts_scopes():
    parsed = _parse_auth_users('{"alice": {"token": "tok-a", "scopes": ["write", "read", "write"]}}')
    assert parsed["alice"]["scopes"] == ["read", "write"]


def test_parse_ignores_empty_scopes_list():
    # An empty list is treated the same as "not specified" — omitted from the entry
    # rather than persisted as a scopes list that would deny everything.
    parsed = _parse_auth_users('{"alice": {"token": "tok-a", "scopes": []}}')
    assert parsed == {"alice": {"token": "tok-a", "expires_at": None}}


def test_parse_strips_blank_scope_entries():
    parsed = _parse_auth_users('{"alice": {"token": "tok-a", "scopes": ["read", "  ", ""]}}')
    assert parsed["alice"]["scopes"] == ["read"]


# ---------------------------------------------------------------------------
# _scopes_for_identity
# ---------------------------------------------------------------------------

@pytest.fixture
def auth_state(monkeypatch):
    monkeypatch.setattr(server_module, "AV_API_TOKEN", "")
    monkeypatch.setattr(server_module, "_AUTH_USERS", {})


def test_owner_is_always_unrestricted(auth_state):
    assert _scopes_for_identity("owner") == ["*"]


def test_unscoped_dict_entry_defaults_to_wildcard(auth_state):
    server_module._AUTH_USERS = {"alice": {"token": "tok-a", "expires_at": None}}
    assert _scopes_for_identity("alice") == ["*"]


def test_bare_string_entry_defaults_to_wildcard(auth_state):
    # Every legacy/monkeypatched-in-tests bare-string entry (test_auth_users.py's own
    # style) must resolve to unrestricted, not to an empty/deny-everything scope set.
    server_module._AUTH_USERS = {"alice": "tok-a"}
    assert _scopes_for_identity("alice") == ["*"]


def test_scoped_entry_returns_its_scopes(auth_state):
    server_module._AUTH_USERS = {"alice": {"token": "tok-a", "expires_at": None,
                                            "scopes": ["eval:write"]}}
    assert _scopes_for_identity("alice") == ["eval:write"]


def test_unknown_identity_defaults_to_wildcard(auth_state):
    # Anonymous mode's request.state.scopes is never set at all; None reaching here
    # (e.g. a caller passing a username that isn't in _AUTH_USERS) fails open to "*",
    # matching require_scope()'s own additive-by-default contract.
    assert _scopes_for_identity(None) == ["*"]
    assert _scopes_for_identity("nobody") == ["*"]


# ---------------------------------------------------------------------------
# require_scope() — exercised directly against a stub Request/AsyncSession, no Postgres
# ---------------------------------------------------------------------------

class _StubSession:
    def __init__(self):
        self.added = []
        self.committed = False

    def add(self, row):
        self.added.append(row)

    async def commit(self):
        self.committed = True


def _fake_request(scopes=None, username=None, path="/api/eval/suites"):
    state = SimpleNamespace(scopes=scopes, username=username)
    return SimpleNamespace(state=state, url=SimpleNamespace(path=path))


def test_wildcard_scope_passes():
    dep = server_module.require_scope("eval:write")
    db = _StubSession()
    asyncio.run(dep(_fake_request(scopes=["*"]), db))
    assert not db.added and not db.committed


def test_matching_scope_passes():
    dep = server_module.require_scope("eval:write")
    db = _StubSession()
    asyncio.run(dep(_fake_request(scopes=["eval:write", "read"]), db))
    assert not db.added and not db.committed


def test_missing_scope_denies_with_403_and_audits():
    dep = server_module.require_scope("eval:write")
    db = _StubSession()
    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(dep(_fake_request(scopes=["read"], username="trainer-bot"), db))
    assert excinfo.value.status_code == 403
    assert excinfo.value.detail["error"] == "scope_denied"
    assert excinfo.value.detail["required_scope"] == "eval:write"
    assert db.committed  # the audit row's transaction must land even on a denial
    assert len(db.added) == 1
    row = db.added[0]
    assert row.action == "scope.denied"
    assert row.status_code == 403
    assert row.username == "trainer-bot"


def test_absent_scopes_state_defaults_to_wildcard():
    # Mirrors Anonymous mode: request.state.scopes was never set at all.
    dep = server_module.require_scope("eval:write")
    db = _StubSession()
    asyncio.run(dep(_fake_request(scopes=None), db))
    assert not db.added and not db.committed
