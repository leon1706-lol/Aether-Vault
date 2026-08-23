"""Per-user access tokens (`AV_AUTH_USERS`) + auth compatibility invariants (v1.1.8).

Three layers, ordered by stack-freeness:

1. Parse/validate units for `_parse_auth_users` — always run.
2. Identity resolution (`_resolve_identity`) against monkeypatched module globals — always
   run. This is where the compatibility invariants live: the owner shared secret still maps
   to "owner", unknown tokens still fail closed.
3. Middleware rejection/exemption behavior through a lifespan-less TestClient — always run,
   because every assertion here resolves BEFORE any route touches the database.

The live attribution round-trip (authenticated push stamps the username as author) needs
Postgres + Redis and lives in tests/test_server.py behind the usual reachability skip —
it debuts on CI exactly like the rest of the live-path suite.
"""
import os
import tempfile

# Same import-time env pinning as tests/test_server.py — database.py/redis_cache.py/
# storage.py read their config at import time. Explicit assignment so a real dev shell's
# exported DATABASE_URL never leaks in. When the full suite runs, whichever module imports
# python.av_server.server first fixes these values for the process; this file sorts before
# tests/test_server.py alphabetically, so its values win — identical defaults, no conflict.
os.environ["DATABASE_URL"] = os.environ.get(
    "AV_TEST_DATABASE_URL",
    "postgresql+asyncpg://av_user:av_password@localhost:5432/aether_vault_test",
)
os.environ["REDIS_URL"] = os.environ.get("AV_TEST_REDIS_URL", "redis://localhost:6379/0")
os.environ["AV_DATA_DIR"] = tempfile.mkdtemp(prefix="av-auth-users-test-")

import pytest  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import python.av_server.server as server_module  # noqa: E402
from python.av_server.server import _parse_auth_users, _resolve_identity  # noqa: E402


# ---------------------------------------------------------------------------
# _parse_auth_users — pure validation, always run
# ---------------------------------------------------------------------------

def test_parse_accepts_a_valid_map():
    assert _parse_auth_users('{"alice": "tok-a", "bob": "tok-b"}') == {
        "alice": "tok-a",
        "bob": "tok-b",
    }


def test_parse_empty_and_unset_are_anonymous():
    # None / "" / whitespace-only must all mean "no per-user tokens configured" — the
    # compose default interpolates an empty string, which must behave like absence.
    assert _parse_auth_users(None) == {}
    assert _parse_auth_users("") == {}
    assert _parse_auth_users("   ") == {}


def test_parse_invalid_json_fails_startup_loudly():
    # A silently ignored map would look exactly like Anonymous mode — the one failure
    # mode worse than a crash. Startup must refuse instead.
    with pytest.raises(RuntimeError, match="not valid JSON"):
        _parse_auth_users('{"alice": tok-a}')  # unquoted value


def test_parse_non_object_rejected():
    with pytest.raises(RuntimeError, match="JSON object"):
        _parse_auth_users('["alice", "bob"]')
    with pytest.raises(RuntimeError, match="JSON object"):
        _parse_auth_users('"just a string"')


@pytest.mark.parametrize("payload", ['{"": "tok"}', '{"  ": "tok"}', '{"alice": ""}', '{"alice": "   "}'])
def test_parse_empty_username_or_token_rejected(payload):
    with pytest.raises(RuntimeError, match="non-empty"):
        _parse_auth_users(payload)


def test_parse_strips_and_coerces_to_str():
    # Values arrive from an env var as text, but a hand-written JSON map could use numbers
    # — compare_digest(str, int) raises TypeError, so everything normalizes to str.
    assert _parse_auth_users('{" alice ": 12345}') == {"alice": "12345"}


# ---------------------------------------------------------------------------
# _resolve_identity — compatibility invariants, always run
# ---------------------------------------------------------------------------

@pytest.fixture
def auth_state(monkeypatch):
    """Both credential sources empty (= Anonymous), restorable per test."""
    monkeypatch.setattr(server_module, "AV_API_TOKEN", "")
    monkeypatch.setattr(server_module, "_AUTH_USERS", {})


def test_owner_shared_secret_resolves_to_owner(auth_state):
    server_module.AV_API_TOKEN = "owner-secret"
    assert _resolve_identity("owner-secret") == "owner"


def test_user_token_resolves_to_its_username(auth_state):
    server_module._AUTH_USERS = {"alice": "tok-a", "bob": "tok-b"}
    assert _resolve_identity("tok-a") == "alice"
    assert _resolve_identity("tok-b") == "bob"


def test_unknown_token_fails_closed(auth_state):
    server_module.AV_API_TOKEN = "owner-secret"
    server_module._AUTH_USERS = {"alice": "tok-a"}
    assert _resolve_identity("wrong") is None
    assert _resolve_identity("") is None


def test_owner_wins_when_both_sources_match(auth_state):
    # Degenerate but legal: the same string configured in both sources. The shared secret
    # is checked first, so attribution stays stable across configurations.
    server_module.AV_API_TOKEN = "same-token"
    server_module._AUTH_USERS = {"alice": "same-token"}
    assert _resolve_identity("same-token") == "owner"


def test_user_mode_works_with_no_shared_secret_at_all(auth_state):
    # The new mode this whole feature exists for: teammates authenticate while the owner
    # never sets AV_API_TOKEN.
    server_module._AUTH_USERS = {"carol": "tok-c"}
    assert _resolve_identity("tok-c") == "carol"
    assert _resolve_identity("owner-secret") is None


def test_prefix_tokens_do_not_match(auth_state):
    # compare_digest is exact-match; a token that merely prefixes another must not leak in.
    server_module.AV_API_TOKEN = "owner-secret-long"
    server_module._AUTH_USERS = {"alice": "owner-secret"}
    assert _resolve_identity("owner-secret") == "alice"
    assert _resolve_identity("owner-secret-lon") is None


# ---------------------------------------------------------------------------
# require_token middleware — stack-free via the PRODUCTION middleware on a probe app
# ---------------------------------------------------------------------------
# Mounting server_module.require_token (the exact decorated production middleware) onto a
# tiny DB-free FastAPI app proves BOTH rejection and acceptance without Postgres: a valid
# token gets 200 from the probe route; an invalid one gets the real 401 shape. No lifespan
# runs anywhere here, so nothing ever touches the database.

@pytest.fixture(scope="module")
def probe_client():
    from starlette.middleware.base import BaseHTTPMiddleware

    probe = FastAPI()

    @probe.get("/api/refs")
    def _refs_stub():
        return {"reached": True}

    @probe.get("/api/health")
    def _health_stub():
        return {"status": "ok", "version": "probe"}

    # Same exemption semantics as production: the middleware checks raw path strings, so
    # stubbing the same paths exercises the identical branch.
    probe.add_middleware(BaseHTTPMiddleware, dispatch=server_module.require_token)
    return TestClient(probe)


def _refs(probe_client, **kwargs):
    return probe_client.get("/api/refs", **kwargs)


def test_middleware_rejects_without_header_in_users_only_mode(probe_client, auth_state):
    server_module._AUTH_USERS = {"alice": "tok-a"}
    assert _refs(probe_client).status_code == 401


def test_middleware_rejects_wrong_and_unknown_tokens_in_users_only_mode(probe_client, auth_state):
    server_module._AUTH_USERS = {"alice": "tok-a"}
    assert _refs(probe_client, headers={"Authorization": "Bearer nope"}).status_code == 401
    # Bearer-shaped but matching no source:
    assert _refs(probe_client, headers={"Authorization": "Bearer tok-b"}).status_code == 401


def test_middleware_accepts_valid_user_token_in_users_only_mode(probe_client, auth_state):
    server_module._AUTH_USERS = {"alice": "tok-a"}
    resp = _refs(probe_client, headers={"Authorization": "Bearer tok-a"})
    assert resp.status_code == 200
    assert resp.json() == {"reached": True}


def test_middleware_rejects_garbage_schemes_in_users_only_mode(probe_client, auth_state):
    server_module._AUTH_USERS = {"alice": "tok-a"}
    for header_value in ("", "Bearer", "Bearer ", "Basic tok-a"):
        resp = _refs(probe_client, headers={"Authorization": header_value})
        assert resp.status_code == 401, header_value


def test_scheme_is_case_insensitive_for_user_tokens_too(probe_client, auth_state):
    # Parity with the legacy single-token mode's documented behavior.
    server_module._AUTH_USERS = {"alice": "tok-a"}
    assert _refs(probe_client, headers={"Authorization": "bearer tok-a"}).status_code == 200


def test_health_stays_exempt_in_users_only_mode(probe_client, auth_state):
    server_module._AUTH_USERS = {"alice": "tok-a"}
    resp = probe_client.get("/api/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_anonymous_mode_untouched_by_the_new_code_path(probe_client, auth_state):
    # Compatibility invariant: both sources empty ⇒ requests reach routes with NO
    # Authorization header at all, exactly as every pre-v1.1.8 client sends them.
    assert _refs(probe_client).status_code == 200


def test_legacy_single_token_mode_unchanged(probe_client, auth_state):
    # Compatibility invariant: the pre-v1.1.8 configuration (only AV_API_TOKEN set) must
    # accept/reject exactly as before — no user-map code path may loosen or tighten it.
    server_module.AV_API_TOKEN = "legacy-owner-token"
    assert _refs(probe_client).status_code == 401
    assert _refs(probe_client, headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert (
        _refs(probe_client, headers={"Authorization": "Bearer legacy-owner-token"}).status_code
        == 200
    )


def test_combined_mode_accepts_both_credential_shapes(probe_client, auth_state):
    server_module.AV_API_TOKEN = "owner-secret"
    server_module._AUTH_USERS = {"alice": "tok-a"}
    assert _refs(probe_client, headers={"Authorization": "Bearer owner-secret"}).status_code == 200
    assert _refs(probe_client, headers={"Authorization": "Bearer tok-a"}).status_code == 200
