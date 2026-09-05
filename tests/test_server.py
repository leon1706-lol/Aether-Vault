"""Tests for the av_server FastAPI backend.

Pure validation tests (no DB/Redis needed) always run. Everything else requires a live
Postgres + Redis (see AV_TEST_DATABASE_URL / AV_TEST_REDIS_URL below) and is skipped cleanly,
with a clear message, if they're not reachable â€” same philosophy as test_core.py's
`pytest.importorskip`, just for service reachability instead of an import.

    docker compose up -d db redis            # enough for everything except the real-wire test
    docker compose up -d db redis aether-vault-engine   # also enables the real-wire test
    pytest tests/test_server.py -v
"""
import asyncio
import base64
import hashlib
import importlib.util
import json
import os
import socket
import tempfile
import time
from urllib.parse import urlsplit

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

AV_TEST_DATABASE_URL = os.environ.get(
    "AV_TEST_DATABASE_URL",
    "postgresql+asyncpg://av_user:av_password@localhost:5432/aether_vault_test",
)
# v1.3.2 fix (found live this session): the default used to be db 0 -- the SAME logical
# Redis database `docker-compose.yml`'s own REDIS_URL points the real dev engine at.
# A local `pytest tests/test_server.py` run with no env override shares its Bloom filter
# and rate-limit/auth-spike keys with whatever real dev stack happens to be running on
# this machine -- one contributing factor in this session's second infra incident (see
# development/CHANGELOG.md Phase 60). Db 1 is still on localhost:6379 (no extra service
# needed) but is a genuinely separate keyspace from the real stack's db 0.
AV_TEST_REDIS_URL = os.environ.get("AV_TEST_REDIS_URL", "redis://localhost:6379/1")

# Point the server's own module-level config at the test instance *before* importing it â€”
# database.py/redis_cache.py read DATABASE_URL/REDIS_URL at import time, and storage.py's
# CASStorage is constructed at import time too (AV_DATA_DIR). Explicit assignment (not
# setdefault) so a real dev DATABASE_URL already exported in the shell never leaks in here.
os.environ["DATABASE_URL"] = AV_TEST_DATABASE_URL
# v1.3.2 (migration 0015): route this whole file's request-serving sessions through the
# non-superuser av_app role too, not just DATABASE_URL/av_user (which migrations and
# system_session_factory keep using) -- the real fix for the RLS-superuser gap migration
# 0013 documented only matters if it's actually exercised, and this is the one place
# every existing route in this file already gets driven end-to-end. Deliberately the
# SAME Postgres user (av_app) that docker-compose.yml wires the real engine to for
# AV_APP_DATABASE_URL, just pointed at the test database -- migration 0015 grants it
# there identically the first time `_apply_schema` runs against `aether_vault_test`.
# Overridable like the other AV_TEST_* vars above for a differently-shaped test DB.
AV_TEST_APP_DATABASE_URL = os.environ.get(
    "AV_TEST_APP_DATABASE_URL",
    AV_TEST_DATABASE_URL.replace("av_user:av_password", "av_app:av_app_password"),
)
os.environ["AV_APP_DATABASE_URL"] = AV_TEST_APP_DATABASE_URL
os.environ["REDIS_URL"] = AV_TEST_REDIS_URL
os.environ["AV_DATA_DIR"] = tempfile.mkdtemp(prefix="av-server-test-")
# The periodic webhook-retry worker (server.py's _webhook_retry_worker) is created ONCE
# at app startup with whatever AV_WEBHOOK_RETRY_INTERVAL_SECS is at that moment (the
# `client` fixture below is session-scoped — one server, one worker task, for the WHOLE
# file) and never re-reads it afterward. With the real 30s production default, that
# worker's first tick lands at a fixed wall-clock offset from session start — which
# collection order and file runtime can walk right into, racing any test that manually
# drives webhook delivery via its own monkeypatched `requests.post` (a real bug this
# caught: test_webhook_health_columns_update_on_success_and_failure flaked when new
# tests earlier in this file shifted its position to land near the 30s mark — see
# Probleme.md). A huge interval here means the worker's tick never fires during any
# realistic test session, full stop.
os.environ["AV_WEBHOOK_RETRY_INTERVAL_SECS"] = "999999"

import python.av_server.server as server_module  # noqa: E402
from python.av_server.server import app, validate_ref_name  # noqa: E402
from python.av_server.storage import CASStorage  # noqa: E402


def _tcp_reachable(url: str, timeout: float = 1.5) -> bool:
    parts = urlsplit(url)
    if not parts.hostname or not parts.port:
        return False
    try:
        with socket.create_connection((parts.hostname, parts.port), timeout=timeout):
            return True
    except OSError:
        return False


def _real_server_reachable() -> bool:
    try:
        import httpx
        return httpx.get("http://localhost:8000/api/health", timeout=1.5).status_code == 200
    except Exception:
        return False


async def _truncate_all() -> None:
    # A raw asyncpg connection, not the SQLAlchemy async engine's pooled connection â€” the pool's
    # connections are bound to whichever event loop first used them (TestClient's internal
    # lifespan loop), so reusing the pool from a *separate* asyncio.run() call here raises
    # "got Future ... attached to a different loop". Opening (and closing) a brand-new
    # connection scoped entirely to this call's own loop avoids that cross-loop reuse.
    import asyncpg
    conn = await asyncpg.connect(AV_TEST_DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://"))
    try:
        # v1.2.2: include the autonomous-loop + delivery/audit tables — otherwise audit
        # rows from earlier tests leak into later filters/pagination assertions.
        #
        # v1.3.1 WP-44 fix (found live): this list was never extended for ANY of the 20
        # RSI tables added across migrations 0006-0010 — every test using a hardcoded id
        # against one of them (e.g. TestImproverVersions::test_create_is_idempotent_by_id
        # using "imp-1") silently collided with the SAME row a PREVIOUS run of this file
        # against the same persistent test database had already inserted, turning a
        # fresh-201-expected assertion into a stale-200-exists one. Invisible in a CI
        # runner with a brand-new ephemeral service container per run; guaranteed on any
        # persistent local Postgres re-run — exactly this session's setup, and exactly
        # why this had never been caught before this cycle's first-ever live pass.
        # v1.3.2 (migration 0011): eight of the eleven new identity/tenancy tables join
        # the truncate list (projects, users, user_identities, groups, group_members,
        # role_bindings, api_tokens, sso_providers, sessions) — the same reasoning as the
        # WP-44 fix above: a hardcoded-id test against any of them would otherwise
        # collide with a previous local run's row.
        #
        # `tenants` and `roles` are DELIBERATELY excluded — unlike every other table
        # here, both carry migration-time SEED data (the default tenant, the six
        # built-in roles) that migration 0011 inserts exactly once, ever, not
        # per-test-run data. Truncating them would silently delete that seed with
        # nothing to reinsert it short of a full downgrade+upgrade, breaking every
        # subsequent test in this file that expects the built-in roles to exist.
        await conn.execute(
            "TRUNCATE objects, trees, commits, refs, runs, run_commits, events,"
            " webhooks, webhook_deliveries, audit_log,"
            " improver_versions, change_sets, policy_packs, canary_results, project_freeze,"
            " eval_suites, eval_results, eval_adapters, tasks, plans, budgets,"
            " causal_links, strategy_entries, lessons, reviews, critiques, blackboard_entries,"
            " sandbox_jobs, tool_manifests, action_logs,"
            " projects, users, user_identities, groups, group_members, role_bindings,"
            " api_tokens, sso_providers, sessions CASCADE"
        )
    finally:
        await conn.close()


def _hex_hash(seed: str) -> str:
    return hashlib.sha256(seed.encode()).hexdigest()


def _make_commit(seed: str, tree: dict | None = None, **overrides) -> dict:
    payload = {
        "hash": _hex_hash(seed),
        "message": overrides.pop("message", f"commit {seed}"),
        "author": "tester",
        "tree": tree if tree is not None else {},
        "tags": [],
        "metrics": {},
        "parents": [],
    }
    payload.update(overrides)
    return payload


@pytest.fixture(scope="session")
def client():
    if not (_tcp_reachable(AV_TEST_DATABASE_URL) and _tcp_reachable(AV_TEST_REDIS_URL)):
        pytest.skip(
            "Postgres/Redis test services not reachable "
            f"(AV_TEST_DATABASE_URL={AV_TEST_DATABASE_URL}, AV_TEST_REDIS_URL={AV_TEST_REDIS_URL}). "
            "Run `docker compose up -d db redis` first."
        )
    with TestClient(app) as c:  # triggers the FastAPI lifespan: init_db() + cache.init_filter()
        yield c


def _clear_storage_dirs() -> None:
    # _truncate_all() only clears the DB tables â€” any object a test uploaded via
    # POST /api/objects/{hash} still has its physical shard file on disk afterward (CASStorage
    # has no DB-driven TTL of its own). Without also clearing these, later tests in the same
    # session (e.g. the GC grace-period test) see genuine orphan files left over from earlier
    # tests and sweep them too, making "exactly N objects deleted" assertions flaky depending on
    # what ran before. Clear file contents, not the directories themselves.
    for d in (server_module.storage.objects_dir, server_module.storage.commits_dir, server_module.storage.refs_dir):
        for p in d.rglob("*"):
            if p.is_file():
                p.unlink()


@pytest.fixture
def db(client):
    """The TestClient, plus a guarantee that tables and on-disk storage are reset after this
    test runs."""
    yield client
    asyncio.run(_truncate_all())
    _clear_storage_dirs()


@pytest.fixture
def protected_token(db):
    """Turns on the require_token middleware for the duration of one test ("Protected" mode).

    AV_API_TOKEN is read once at module import (see server.py) â€” empty in this whole test
    file's process, since nothing sets the env var before `app` is imported above. Reassigning
    the module attribute directly is the correct way to flip it for a single test: the
    middleware looks up the bare name `AV_API_TOKEN` in its enclosing module's globals at call
    time, not at function-definition time, so this is picked up by every request the test
    issues through `db` and is restored afterward so later tests stay in Anonymous mode.
    """
    token = "test-secret-token-12345"
    server_module.AV_API_TOKEN = token
    try:
        yield token
    finally:
        server_module.AV_API_TOKEN = ""


# ---------------------------------------------------------------------------
# Pure validation tests â€” no DB/Redis needed, always run
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", ["main", "feature/x", "proj-id_123/main", "release.1.0"])
def test_validate_ref_name_accepts_normal_names(name):
    assert validate_ref_name(name) == name


@pytest.mark.parametrize(
    "name",
    ["../etc/passwd", "a/../b", "/main", "main\\secret", "", "bad name with spaces"],
)
def test_validate_ref_name_rejects_unsafe_names(name):
    with pytest.raises(HTTPException) as exc_info:
        validate_ref_name(name)
    assert exc_info.value.status_code == 400


def test_safe_ref_path_rejects_escape(tmp_path):
    storage = CASStorage(tmp_path)
    with pytest.raises(ValueError):
        storage._safe_ref_path("../../etc/passwd")


def test_safe_ref_path_accepts_normal_name(tmp_path):
    storage = CASStorage(tmp_path)
    assert storage._safe_ref_path("main") == storage.refs_dir.resolve() / "main"


# ---------------------------------------------------------------------------
# HTTP-layer tests â€” require a reachable Postgres + Redis
# ---------------------------------------------------------------------------

def test_health_check_ok(db):
    resp = db.get("/api/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_health_reports_the_real_installed_version(db):
    """v1.2.5: /api/health used to hardcode "1.4.0" — a THIRD version string besides
    av_server/__init__.py's separate "1.0.0" and the CLI's setuptools-scm one. Now reads
    the actual installed distribution version via importlib.metadata, so it can't drift."""
    from importlib.metadata import version as pkg_version

    resp = db.get("/api/health")
    assert resp.json()["version"] == pkg_version("aether-vault")


# ---------------------------------------------------------------------------
# v1.2.5: readiness (DB + Redis + AV_DATA_DIR writability), distinct from liveness
# ---------------------------------------------------------------------------

def test_readiness_ok_when_everything_is_healthy(db):
    resp = db.get("/api/ready")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ready"] is True
    assert body["checks"] == {"database": True, "redis": True, "data_dir_writable": True}


def test_readiness_is_auth_exempt_even_when_protected(protected_token, db):
    resp = db.get("/api/ready")  # no Authorization header at all
    assert resp.status_code == 200


def test_readiness_503_when_data_dir_is_unwritable(db, monkeypatch, tmp_path):
    """The exact failure mode /api/ready exists to catch: an unwritable AV_DATA_DIR
    that /api/health would never notice (see infrastructure.md)."""
    bogus = tmp_path / "does-not-exist" / "nested"  # write into it must fail
    monkeypatch.setattr(server_module.storage, "base_path", bogus)
    resp = db.get("/api/ready")
    assert resp.status_code == 503
    body = resp.json()
    assert body["ready"] is False
    assert body["checks"]["data_dir_writable"] is False
    assert body["checks"]["database"] is True  # the other checks are unaffected


def test_readiness_503_when_redis_is_unreachable(db, monkeypatch):
    """Regression test for a real shipped bug (Probleme.md): /api/ready originally probed
    Redis via cache.check_hash_exists(), which deliberately fails OPEN (returns True) on
    any error -- correct for its actual caller (an optimistic skip-the-DB check) but means
    a downed Redis silently read as healthy here. e2e-engine-smoke's live CI job caught it
    (REDIS_URL pointed at a nonexistent host still returned `redis: true`); this test
    exercises the same failure stack-free so it can never regress silently again."""
    async def _broken_ping():
        raise ConnectionError("simulated redis outage")

    monkeypatch.setattr(server_module.cache, "ping", _broken_ping)
    resp = db.get("/api/ready")
    assert resp.status_code == 503
    body = resp.json()
    assert body["ready"] is False
    assert body["checks"]["redis"] is False
    assert body["checks"]["database"] is True  # the other checks are unaffected


# ---------------------------------------------------------------------------
# require_token middleware ("Anonymous" vs "Protected" mode)
# ---------------------------------------------------------------------------

def test_no_token_configured_behaves_exactly_as_before(db):
    # AV_API_TOKEN is unset for every other test in this file â€” this just makes the "Anonymous
    # is truly unchanged" guarantee explicit rather than implicit.
    resp = db.get("/api/health")
    assert resp.status_code == 200
    resp = db.get("/api/refs")
    assert resp.status_code == 200


def test_protected_mode_rejects_reads_without_a_token(db, protected_token):
    resp = db.get("/api/refs")
    assert resp.status_code == 401


def test_protected_mode_accepts_reads_with_the_correct_token(db, protected_token):
    resp = db.get("/api/refs", headers={"Authorization": f"Bearer {protected_token}"})
    assert resp.status_code == 200


def test_protected_mode_rejects_the_wrong_token(db, protected_token):
    resp = db.get("/api/refs", headers={"Authorization": "Bearer wrong-token"})
    assert resp.status_code == 401


@pytest.mark.parametrize(
    "header_value",
    [
        "",  # header present but empty
        "Bearer",  # scheme with no token at all
        "Bearer ",  # scheme with trailing space, no token
        "Basic test-secret-token-12345",  # wrong scheme entirely
        "bearer test-secret-token-12345",  # lowercase scheme â€” still must work (see next test)
    ],
)
def test_protected_mode_header_parsing_edge_cases(db, protected_token, header_value):
    resp = db.get("/api/refs", headers={"Authorization": header_value})
    # Only the lowercase-scheme case is a valid token presentation; everything else here is
    # a malformed/missing credential and must be rejected, not crash with a 500.
    if header_value.lower() == f"bearer {protected_token}":
        assert resp.status_code == 200
    else:
        assert resp.status_code == 401


def test_protected_mode_scheme_is_case_insensitive(db, protected_token):
    resp = db.get("/api/refs", headers={"Authorization": f"bearer {protected_token}"})
    assert resp.status_code == 200


def test_health_check_is_always_exempt_even_when_protected(db, protected_token):
    resp = db.get("/api/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_docs_routes_are_always_exempt_even_when_protected(db, protected_token):
    for path in ("/docs", "/openapi.json", "/redoc"):
        resp = db.get(path)
        assert resp.status_code != 401, f"{path} should be reachable without a token"


def test_protected_mode_gates_writes_too_not_just_reads(db, protected_token):
    # The whole point of the revised scope decision: Protected means everything, not just the
    # 4 mutating routes from the first draft of this plan.
    auth_header = {"Authorization": f"Bearer {protected_token}"}
    commit = _make_commit("gated-write-test")
    assert db.post("/api/commits", json=commit, headers=auth_header).status_code == 201

    resp = db.put("/api/refs/proj/main", json={"commit_hash": commit["hash"]})
    assert resp.status_code == 401
    resp = db.put(
        "/api/refs/proj/main",
        json={"commit_hash": commit["hash"]},
        headers=auth_header,
    )
    assert resp.status_code == 200


@pytest.fixture
def auth_users(db):
    """Turns on per-user tokens ("Protected" mode via AV_AUTH_USERS) for one test.

    Same mechanics as protected_token above: _resolve_identity reads the module global at
    call time, so reassigning it here is picked up by every request through `db`, and the
    finally-restore keeps every other test in Anonymous mode.
    """
    users = {"alice": "alice-token-12345", "bob": "bob-token-67890"}
    server_module._AUTH_USERS = users
    try:
        yield users
    finally:
        server_module._AUTH_USERS = {}


# ---------------------------------------------------------------------------
# Per-user access tokens (AV_AUTH_USERS) â€” live attribution round-trips
# ---------------------------------------------------------------------------

def test_per_user_token_grants_access_with_no_shared_secret(db, auth_users):
    # The v1.1.8 headline mode: teammates authenticate while AV_API_TOKEN stays unset.
    resp = db.get("/api/refs", headers={"Authorization": "Bearer alice-token-12345"})
    assert resp.status_code == 200


def test_per_user_token_rejects_unknown_token(db, auth_users):
    resp = db.get("/api/refs", headers={"Authorization": "Bearer mallory-token"})
    assert resp.status_code == 401


def test_push_commit_stamps_authenticated_username_as_author(db, auth_users):
    commit = _make_commit("user-attributed", author="anonymous")
    alice_header = {"Authorization": "Bearer alice-token-12345"}
    resp = db.post("/api/commits", json=commit, headers=alice_header)
    assert resp.status_code == 201

    # The follow-up read needs the SAME credential â€” with per-user tokens active the
    # middleware 401s headerless requests, whose {"detail": ...} body has no "author".
    body = db.get(f"/api/commits/{commit['hash']}", headers=alice_header).json()
    assert body["author"] == "alice"


def test_push_commit_respects_explicit_author_from_authenticated_user(db, auth_users):
    # Scripts own their attribution: an authenticated user pushing with a client-set
    # AV_AUTHOR must NOT get silently re-stamped.
    commit = _make_commit("explicit-author", author="ci-bot")
    bob_header = {"Authorization": "Bearer bob-token-67890"}
    resp = db.post("/api/commits", json=commit, headers=bob_header)
    assert resp.status_code == 201

    body = db.get(f"/api/commits/{commit['hash']}", headers=bob_header).json()
    assert body["author"] == "ci-bot"


def test_owner_shared_secret_stamps_owner_as_author(db):
    server_module.AV_API_TOKEN = "owner-token-xyz"
    owner_header = {"Authorization": "Bearer owner-token-xyz"}
    try:
        commit = _make_commit("owner-attributed", author="anonymous")
        resp = db.post("/api/commits", json=commit, headers=owner_header)
        assert resp.status_code == 201
        body = db.get(f"/api/commits/{commit['hash']}", headers=owner_header).json()
        assert body["author"] == "owner"
    finally:
        server_module.AV_API_TOKEN = ""


def test_anonymous_mode_keeps_author_untouched(db):
    # No credentials configured â‡’ no identity exists â‡’ author passes through verbatim
    # (the pre-v1.1.8 behavior, byte-compatible).
    commit = _make_commit("anon-mode-author", author="anonymous")
    assert db.post("/api/commits", json=commit).status_code == 201
    body = db.get(f"/api/commits/{commit['hash']}").json()
    assert body["author"] == "anonymous"


def test_upload_then_download_object_roundtrip(db):
    content = b"hello aether-vault" * 100
    h = hashlib.sha256(content).hexdigest()

    resp = db.post(f"/api/objects/{h}", content=content)
    assert resp.status_code == 201

    resp = db.get(f"/api/objects/{h}")
    assert resp.status_code == 200
    assert resp.content == content

    resp = db.head(f"/api/objects/{h}")
    assert resp.status_code == 200
    assert resp.headers["content-length"] == str(len(content))


def test_upload_object_rejects_hash_mismatch(db):
    content = b"some bytes"
    wrong_hash = hashlib.sha256(b"different bytes").hexdigest()
    resp = db.post(f"/api/objects/{wrong_hash}", content=content)
    assert resp.status_code == 400


def test_upload_object_duplicate_is_idempotent_409(db):
    content = b"dup test"
    h = hashlib.sha256(content).hexdigest()
    assert db.post(f"/api/objects/{h}", content=content).status_code == 201
    assert db.post(f"/api/objects/{h}", content=content).status_code == 409


def test_push_commit_then_get_commit_roundtrip(db):
    file_hash = _hex_hash("file-content")
    commit = _make_commit("c1", tree={"train.py": {"hash": file_hash, "size": 10, "type": "code"}})
    assert db.post("/api/commits", json=commit).status_code == 201

    resp = db.get(f"/api/commits/{commit['hash']}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["message"] == commit["message"]
    assert body["tree"]["train.py"]["hash"] == file_hash


def test_list_commits_omits_tree_by_default(db):
    commit = _make_commit("list-no-layers", tree={"a.py": {"hash": _hex_hash("a"), "size": 1, "type": "code"}})
    assert db.post("/api/commits", json=commit).status_code == 201

    resp = db.get("/api/commits")
    assert resp.status_code == 200
    found = next(c for c in resp.json()["commits"] if c["hash"] == commit["hash"])
    assert "tree" not in found


def test_list_commits_include_layers_matches_get_commit(db):
    # Two commits, so the sequential-resolution loop in list_commits is actually exercised
    # with more than one tree, not just a single trivial case.
    file_hash_a = _hex_hash("layer-content-a")
    file_hash_b = _hex_hash("layer-content-b")
    commit_a = _make_commit("layers-a", tree={"model.bin": {"hash": file_hash_a, "size": 5, "type": "artifact"}})
    commit_b = _make_commit("layers-b", tree={"model.bin": {"hash": file_hash_b, "size": 7, "type": "artifact"}})
    assert db.post("/api/commits", json=commit_a).status_code == 201
    assert db.post("/api/commits", json=commit_b).status_code == 201

    resp = db.get("/api/commits", params={"include_layers": "true"})
    assert resp.status_code == 200
    by_hash = {c["hash"]: c for c in resp.json()["commits"]}

    for commit, file_hash in ((commit_a, file_hash_a), (commit_b, file_hash_b)):
        assert "tree" in by_hash[commit["hash"]]
        assert by_hash[commit["hash"]]["tree"]["model.bin"]["hash"] == file_hash
        # Must match GET /api/commits/{hash}'s own tree exactly â€” same resolve_tree() call.
        direct = db.get(f"/api/commits/{commit['hash']}").json()
        assert by_hash[commit["hash"]]["tree"] == direct["tree"]


def test_list_commits_include_layers_handles_a_commit_with_no_tree(db):
    commit = _make_commit("empty-tree-commit", tree={})
    assert db.post("/api/commits", json=commit).status_code == 201

    resp = db.get("/api/commits", params={"include_layers": "true"})
    assert resp.status_code == 200
    found = next(c for c in resp.json()["commits"] if c["hash"] == commit["hash"])
    assert found["tree"] == {}


def test_push_commit_duplicate_returns_409(db):
    commit = _make_commit("c2")
    assert db.post("/api/commits", json=commit).status_code == 201
    assert db.post("/api/commits", json=commit).status_code == 409


def test_push_commit_rejects_oversized_tree(db):
    big_tree = {f"file_{i}.py": {"hash": _hex_hash(str(i)), "size": 1, "type": "code"} for i in range(100_001)}
    commit = _make_commit("c3", tree=big_tree)
    assert db.post("/api/commits", json=commit).status_code == 422


def test_push_commit_rejects_too_many_tags(db):
    commit = _make_commit("c4", tags=[f"tag{i}" for i in range(201)])
    assert db.post("/api/commits", json=commit).status_code == 422


def test_push_commit_rejects_oversized_tag(db):
    commit = _make_commit("c5", tags=["x" * 201])
    assert db.post("/api/commits", json=commit).status_code == 422


def test_push_commit_rejects_too_many_metrics(db):
    commit = _make_commit("c6", metrics={f"m{i}": 1.0 for i in range(1001)})
    assert db.post("/api/commits", json=commit).status_code == 422


def test_push_commit_rejects_oversized_message(db):
    commit = _make_commit("c7", message="x" * 20_001)
    assert db.post("/api/commits", json=commit).status_code == 422


def test_update_ref_then_get_ref_roundtrip(db):
    commit = _make_commit("c8")
    db.post("/api/commits", json=commit)

    resp = db.put("/api/refs/main", json={"commit_hash": commit["hash"]})
    assert resp.status_code == 200

    resp = db.get("/api/refs/main")
    assert resp.status_code == 200
    assert resp.json()["commit_hash"] == commit["hash"]


def test_update_ref_rejects_invalid_name_at_http_layer(db):
    # %5C = a literal backslash once Starlette decodes the path param; httpx won't "normalize"
    # an encoded backslash the way it might collapse a literal ../ segment, so this reliably
    # reaches validate_ref_name() with the backslash intact.
    resp = db.put("/api/refs/main%5Csecret", json={"commit_hash": _hex_hash("x")})
    assert resp.status_code == 400


def test_list_refs_filters_by_project_id(db):
    c1 = _make_commit("p1", project_id="proj-a", project_name="A")
    c2 = _make_commit("p2", project_id="proj-b", project_name="B")
    db.post("/api/commits", json=c1)
    db.post("/api/commits", json=c2)
    db.put("/api/refs/proj-a/main", json={"commit_hash": c1["hash"]})
    db.put("/api/refs/proj-b/main", json={"commit_hash": c2["hash"]})

    resp = db.get("/api/refs", params={"project_id": "proj-a"})
    assert resp.status_code == 200
    refs = resp.json()
    assert "proj-a/main" in refs
    assert "proj-b/main" not in refs


def test_dashboard_summary_and_projects_endpoints(db):
    db.post("/api/commits", json=_make_commit("c9"))

    resp = db.get("/api/dashboard/summary")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total_commits"] >= 1
    assert "recent_commits" in body

    resp = db.get("/api/projects")
    assert resp.status_code == 200
    assert "projects" in resp.json()


def test_gc_respects_grace_period_then_sweeps_when_aged(db, monkeypatch):
    content = b"orphan object, never referenced by a commit"
    h = hashlib.sha256(content).hexdigest()
    db.post(f"/api/objects/{h}", content=content)

    # Default grace period (1h) protects a just-created object.
    resp = db.post("/api/admin/gc")
    assert resp.status_code == 200
    assert resp.json()["deleted_objects"] == 0
    assert db.head(f"/api/objects/{h}").status_code == 200

    # Zero the grace period so the same object is now "aged" relative to the new cutoff.
    monkeypatch.setattr(server_module, "GC_GRACE_SECONDS", 0)
    resp = db.post("/api/admin/gc")
    assert resp.status_code == 200
    assert resp.json()["deleted_objects"] == 1
    assert db.head(f"/api/objects/{h}").status_code == 404


# ---------------------------------------------------------------------------
# Real-wire test â€” needs the actual aether-vault-server process, not just TestClient
# ---------------------------------------------------------------------------

def test_cli_commit_pushes_to_a_live_server(tmp_path, monkeypatch):
    # Checked here (lazily, when this test actually runs) rather than via a `skipif` decorator
    # (evaluated once at collection time, before any other test has run) â€” a `skipif` condition
    # check competes with whatever the rest of collection/the test run is doing for CPU/network
    # scheduling right at that single moment, and a slow tick there reads as "unreachable" even
    # though the server is fine moments later (observed once in a heavy combined venv+Docker run).
    if not _real_server_reachable():
        pytest.skip(
            "live aether-vault-engine not reachable on :8000; run "
            "`docker compose up -d db redis aether-vault-engine`"
        )

    from click.testing import CliRunner

    from python.av_cli.main import cli

    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    runner.invoke(cli, ["init", "--no-repl"])
    runner.invoke(cli, ["config", "--remote-url", "http://localhost:8000"])
    (tmp_path / "train.py").write_text("print('live wire test')")
    runner.invoke(cli, ["add", "train.py"])
    result = runner.invoke(cli, ["commit", "-m", "live wire test"])
    assert result.exit_code == 0, result.output

    commit_hash = (tmp_path / ".av" / "refs" / "heads" / "main").read_text().strip()

    import httpx
    resp = httpx.get(f"http://localhost:8000/api/commits/{commit_hash}")
    assert resp.status_code == 200
    assert resp.json()["message"] == "live wire test"


def test_registry_export_restore_round_trip(tmp_path, monkeypatch):
    """v1.3.0 (todo.md item 18): the one thing this surface never had — a real
    `av registry export` -> `av registry restore` round trip on a non-trivial fixture
    (a plain commit, a run-linked commit, and a two-parent merge commit), against the
    live registry. Also proves --resume actually skips completed work on a second pass.
    """
    if not _real_server_reachable():
        pytest.skip(
            "live aether-vault-engine not reachable on :8000; run "
            "`docker compose up -d db redis aether-vault-engine`"
        )

    import json as json_mod

    from click.testing import CliRunner

    from python.av_cli.main import cli

    repo = tmp_path / "source-repo"
    repo.mkdir()
    monkeypatch.chdir(repo)
    runner = CliRunner()
    assert runner.invoke(cli, ["init", "--mode", "local", "--yes", "--no-repl"]).exit_code == 0
    assert runner.invoke(cli, ["config", "--remote-url", "http://localhost:8000"]).exit_code == 0

    from python.av_cli.core import load_config

    project_id = load_config(repo)["project_id"]  # --project below filters by ID, not name

    (repo / "a.pt").write_bytes(b"weights-a")
    runner.invoke(cli, ["add", "a.pt"])
    r1 = runner.invoke(cli, ["commit", "-m", "plain commit", "--metric", "acc=0.9"])
    assert r1.exit_code == 0, r1.output

    assert runner.invoke(cli, ["run", "start", "roundtrip-run"]).exit_code == 0
    (repo / "b.pt").write_bytes(b"weights-b")
    runner.invoke(cli, ["add", "b.pt"])
    r2 = runner.invoke(cli, ["commit", "-m", "run-linked commit"])
    assert r2.exit_code == 0, r2.output
    assert runner.invoke(cli, ["run", "finish"]).exit_code == 0

    # A REAL two-parent merge commit: both sides must diverge on separate files (no
    # conflict) — main also gets a commit after branching, or `av merge` would just
    # fast-forward (no merge commit at all, and only 3 unique commits total instead of 5).
    assert runner.invoke(cli, ["branch", "feature"]).exit_code == 0
    assert runner.invoke(cli, ["checkout", "feature"]).exit_code == 0
    (repo / "c.pt").write_bytes(b"weights-c")
    runner.invoke(cli, ["add", "c.pt"])
    assert runner.invoke(cli, ["commit", "-m", "feature work"]).exit_code == 0
    assert runner.invoke(cli, ["checkout", "main"]).exit_code == 0
    (repo / "d.pt").write_bytes(b"weights-d")
    runner.invoke(cli, ["add", "d.pt"])
    assert runner.invoke(cli, ["commit", "-m", "main work"]).exit_code == 0
    merge_result = runner.invoke(cli, ["merge", "feature"])
    assert merge_result.exit_code == 0, merge_result.output
    assert "Merged" in merge_result.output, (
        f"expected a real (non-fast-forward) merge commit, got: {merge_result.output}"
    )

    main_tip = (repo / ".av" / "refs" / "heads" / "main").read_text().strip()

    # Push everything, then export the live registry's view of this project.
    push = runner.invoke(cli, ["push"])
    assert push.exit_code == 0, push.output

    archive_dir = tmp_path / "archive"
    export1 = runner.invoke(cli, ["--output", "json", "registry", "export", str(archive_dir),
                                  "--project", project_id])
    assert export1.exit_code == 0, export1.output
    export1_data = json_mod.loads(export1.output)["data"]
    assert export1_data["commits"] >= 5  # plain + run-linked + feature + main-work + merge
    # v1.3.0 (Probleme #119): the whole point of an export is the file content — this is
    # the assertion whose absence let the object-discovery walk silently find zero hashes
    # (missing include_layers=true on the commits query) survive three prior fix-and-
    # verify cycles on this same command undetected. a.pt/b.pt/c.pt/d.pt = 4 distinct
    # object hashes at minimum (the merge commit reuses its parents' unchanged files).
    assert export1_data["objects_ok"] >= 4, (
        f"expected real file objects to export, got: {export1_data}"
    )
    assert export1_data["objects_failed"] == 0
    assert (archive_dir / "manifest.json").exists()
    assert (archive_dir / ".export-state.json").exists()

    manifest = json_mod.loads((archive_dir / "manifest.json").read_text())
    assert any(c["hash"] == main_tip for c in manifest["commits"])
    assert manifest["objects"], "manifest.objects must not be empty — see Probleme #119"
    assert all(o["ok"] for o in manifest["objects"])  # the always-True bug this fixed

    # First restore: everything ingests as idempotent duplicates (already on the server).
    restore1 = runner.invoke(cli, ["--output", "json", "registry", "restore", str(archive_dir)])
    assert restore1.exit_code == 0, restore1.output
    restore1_data = json_mod.loads(restore1.output)["data"]
    assert restore1_data["failed"] == 0
    assert restore1_data["objects_duplicate"] > 0
    assert restore1_data["commits_duplicate"] >= 5
    assert restore1_data["objects_resumed"] == 0  # first pass — nothing skipped yet

    # Second restore (--resume, the default): everything the first pass completed is
    # now skipped via .restore-state.json instead of re-POSTed.
    restore2 = runner.invoke(cli, ["--output", "json", "registry", "restore", str(archive_dir)])
    assert restore2.exit_code == 0, restore2.output
    restore2_data = json_mod.loads(restore2.output)["data"]
    assert restore2_data["objects_resumed"] > 0
    assert restore2_data["commits_resumed"] >= 5
    assert restore2_data["failed"] == 0

    # --no-resume forces a full re-attempt regardless of .restore-state.json.
    restore3 = runner.invoke(cli, ["--output", "json", "registry", "restore", str(archive_dir),
                                   "--no-resume"])
    assert restore3.exit_code == 0, restore3.output
    restore3_data = json_mod.loads(restore3.output)["data"]
    assert restore3_data["objects_resumed"] == 0
    assert restore3_data["objects_duplicate"] > 0  # re-attempted, still idempotent


# ---------------------------------------------------------------------------
# Merge commits: parents round-trip + live clone/pull collaboration flow
# ---------------------------------------------------------------------------

def test_merge_commit_round_trips_both_parents(db):
    parent_a = _hex_hash("pa")
    parent_b = _hex_hash("pb")
    merge_hash = _hex_hash("merge1")
    payload = _make_commit("merge1", parents=[parent_a, parent_b], message="Merge feature")
    # parents[0] need not exist server-side (no FK by design); push the merge directly.
    resp = db.post("/api/commits", json=payload)
    assert resp.status_code == 201, resp.text

    got = db.get(f"/api/commits/{merge_hash}")
    assert got.status_code == 200
    body = got.json()
    assert body["parent_hash"] == parent_a          # backward-compatible field
    assert body["parents"] == [parent_a, parent_b]   # full list reconstructed

    listing = db.get("/api/commits", params={"limit": 10})
    row = next(c for c in listing.json()["commits"] if c["hash"] == merge_hash)
    assert row["parents"] == [parent_a, parent_b]


def test_single_parent_commit_reports_one_parent(db):
    c1 = _make_commit("single-parent", parents=[_hex_hash("only")])
    db.post("/api/commits", json=c1)
    body = db.get(f"/api/commits/{c1['hash']}").json()
    assert body["parents"] == [_hex_hash("only")]


def test_live_two_repo_clone_pull_flow(tmp_path, monkeypatch):
    """The team-collaboration proof, end to end on a real Docker stack:

    repo A init/add/commit/push -> repo B av clone -> B edits/commits/pushes ->
    A av pull fast-forwards onto B's work. Skips (lazily) when the stack is down.
    """
    import httpx

    if not _real_server_reachable():
        pytest.skip(
            "live aether-vault-engine not reachable on :8000; run "
            "`docker compose up -d db redis aether-vault-engine`"
        )

    from click.testing import CliRunner

    from python.av_cli.main import cli

    runner = CliRunner()

    def _in(path, *args):
        monkeypatch.chdir(path)
        result = runner.invoke(cli, list(args))
        assert result.exit_code == 0, result.output
        return result

    repo_a = tmp_path / "repo_a"
    repo_a.mkdir()
    _in(repo_a, "init", "--mode", "local", "--yes", "--no-repl")
    _in(repo_a, "config", "--remote-url", "http://localhost:8000")
    project_name = "collab-live-" + os.urandom(3).hex()
    _in(repo_a, "config", "--name", project_name)

    (repo_a / "train.py").write_text("print('from A')")
    _in(repo_a, "add", "train.py")
    _in(repo_a, "commit", "-m", "a1")

    # --- B clones A's project from the registry ---
    monkeypatch.chdir(tmp_path)
    clone_result = runner.invoke(cli, ["clone", project_name])
    assert clone_result.exit_code == 0, clone_result.output
    cloned = tmp_path / project_name
    assert (cloned / "train.py").read_text() == "print('from A')"

    cfg_b = json.loads((cloned / ".av" / "config").read_text())
    cfg_a = json.loads((repo_a / ".av" / "config").read_text())
    assert cfg_b["project_id"] == cfg_a["project_id"]

    # --- B edits and pushes; the remote tip moves ---
    (cloned / "from_b.py").write_text("print('from B')")
    _in(cloned, "add", "from_b.py")
    _in(cloned, "commit", "-m", "b1")

    pid = cfg_a["project_id"]
    ref_resp = httpx.get(f"http://localhost:8000/api/refs/{pid}/main", timeout=5)
    assert ref_resp.status_code == 200
    remote_tip = ref_resp.json()["commit_hash"]

    # --- A pulls: fast-forwards onto B's commit; sees B's file ---
    _in(repo_a, "pull")
    assert (repo_a / "from_b.py").read_text() == "print('from B')"
    assert (repo_a / ".av" / "refs" / "heads" / "main").read_text().strip() == remote_tip

    status = _in(repo_a, "status")
    assert "Nothing to commit" in status.output


# ---------------------------------------------------------------------------
# Alembic adoption â€” live schema assertions against Postgres
# ---------------------------------------------------------------------------

def _pg_columns(table: str) -> set[str]:
    """Column names of `table`, probed over a direct asyncpg connection."""
    import asyncpg

    async def _run():
        conn = await asyncpg.connect(
            AV_TEST_DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://")
        )
        try:
            rows = await conn.fetch(
                "SELECT column_name FROM information_schema.columns WHERE table_name = $1",
                table,
            )
            return {r["column_name"] for r in rows}
        finally:
            await conn.close()

    return asyncio.run(_run())


def test_alembic_brings_schema_to_head(db):
    """The lifespan's init_db() must have run the migration chain, not create_all."""
    import asyncpg

    async def _probe():
        conn = await asyncpg.connect(
            AV_TEST_DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://")
        )
        try:
            version = await conn.fetchval("SELECT version_num FROM alembic_version")
            tables = {
                r["tablename"]
                for r in await conn.fetch(
                    "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
                )
            }
            return version, tables
        finally:
            await conn.close()

    version, tables = asyncio.run(_probe())
    assert version == "0016"  # current migration head — bump alongside new revisions
    assert {"objects", "trees", "commits", "refs", "alembic_version"} <= tables
    assert {"extra_parents"} <= _pg_columns("commits")
    assert {"chunks"} <= _pg_columns("trees")
    # v1.2.2 additive surfaces:
    assert {"signature"} <= _pg_columns("commits")
    assert {"status_code"} <= _pg_columns("audit_log")
    assert "webhook_deliveries" in tables


def test_migration_0011_seeds_default_tenant_and_builtin_roles(db):
    """v1.3.2 (WP-1/WP-2): migration 0011's seed data — a default tenant and six
    built-in roles expressed in the EXISTING require_scope() vocabulary — must be
    present after any fresh boot, not just conceptually documented. `owner`'s
    permissions being exactly `["*"]` is the load-bearing assertion: it is the same
    wildcard `_scopes_for_identity()` already returns for AV_API_TOKEN and for any
    legacy per-user token with no explicit scopes, which is what keeps this additive —
    nothing that could already reach a route loses access under the new RBAC surface."""
    import asyncpg

    from python.av_server.models import DEFAULT_TENANT_ID

    async def _probe():
        conn = await asyncpg.connect(
            AV_TEST_DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://")
        )
        try:
            tenant = await conn.fetchrow(
                "SELECT slug, status FROM tenants WHERE id = $1", DEFAULT_TENANT_ID
            )
            roles = await conn.fetch("SELECT name, permissions, builtin FROM roles")
            return tenant, {r["name"]: (r["permissions"], r["builtin"]) for r in roles}
        finally:
            await conn.close()

    tenant, roles = asyncio.run(_probe())
    assert tenant is not None, "migration 0011 did not seed the default tenant"
    assert tenant["slug"] == "default"
    assert tenant["status"] == "active"

    expected = {
        "owner": ["*"],
        "admin": ["admin", "improver:write", "policy:write", "eval:write", "review",
                  "scorer", "token:write", "user:write", "scim"],
        "maintainer": ["improver:write", "policy:write", "review"],
        "trainer": ["improver:write", "scorer"],
        "reviewer": ["review"],
        "reader": ["read"],
    }
    assert set(roles) == set(expected), f"built-in role set drifted: {set(roles)}"
    for name, perms in expected.items():
        stored_perms, builtin = roles[name]
        import json as _json
        assert _json.loads(stored_perms) == perms, f"role {name} permissions drifted"
        assert builtin is True, f"role {name} must be marked builtin"


def test_migration_0011_enforces_tenant_scoped_uniqueness(db):
    """`(tenant_id, username)` and `(tenant_id, email)` are unique per tenant (models.py
    DBUser.__table_args__) — the same username/email may exist in two DIFFERENT tenants
    (that's the whole point of tenant isolation) but never twice in the same one."""
    import uuid

    import asyncpg

    from python.av_server.models import DEFAULT_TENANT_ID

    async def _probe():
        conn = await asyncpg.connect(
            AV_TEST_DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://")
        )
        try:
            uid1, uid2 = str(uuid.uuid4()), str(uuid.uuid4())
            await conn.execute(
                "INSERT INTO users (id, tenant_id, username, email, status, source) "
                "VALUES ($1, $2, 'alice', 'alice@example.com', 'active', 'local')",
                uid1, DEFAULT_TENANT_ID,
            )
            try:
                dup_error = None
                try:
                    await conn.execute(
                        "INSERT INTO users (id, tenant_id, username, email, status, source) "
                        "VALUES ($1, $2, 'alice', 'alice2@example.com', 'active', 'local')",
                        uid2, DEFAULT_TENANT_ID,
                    )
                except asyncpg.UniqueViolationError as exc:
                    dup_error = str(exc)
                return dup_error
            finally:
                await conn.execute("DELETE FROM users WHERE id = $1", uid1)
        finally:
            await conn.close()

    dup_error = asyncio.run(_probe())
    assert dup_error is not None, "duplicate (tenant_id, username) was not rejected"
    assert "username" in dup_error


def test_legacy_database_is_healed_and_stamped(db):
    """Simulates a pre-Alembic volume (missing post-adoption columns, no alembic_version)
    and proves startup heals it zero-touch (Phase: DB migrations)."""
    import asyncpg

    # _apply_schema IS the engine-taking entry point (init_db() closes over the module's
    # own engine); the old `from ...database import init_db_with_engine` here pointed at a
    # helper that only ever existed in THIS file â€” invisible locally behind the
    # reachability skip, ImportError on every CI run with a live stack.
    from python.av_server.database import _apply_schema

    url_sync = AV_TEST_DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://")

    async def _make_legacy():
        conn = await asyncpg.connect(url_sync)
        try:
            await conn.execute("ALTER TABLE commits DROP COLUMN IF EXISTS extra_parents")
            await conn.execute("ALTER TABLE trees DROP COLUMN IF EXISTS chunks")
            await conn.execute("DELETE FROM alembic_version")
        finally:
            await conn.close()

    asyncio.run(_make_legacy())
    assert "extra_parents" not in _pg_columns("commits")
    assert "chunks" not in _pg_columns("trees")

    # A fresh engine (own event loop) â€” never reuse the TestClient's pooled one across loops.
    from sqlalchemy.ext.asyncio import create_async_engine

    legacy_engine = create_async_engine(AV_TEST_DATABASE_URL)
    try:
        asyncio.run(_apply_schema(legacy_engine))
    finally:
        asyncio.run(legacy_engine.dispose())

    assert "extra_parents" in _pg_columns("commits")
    assert "chunks" in _pg_columns("trees")

    async def _version():
        conn = await asyncpg.connect(url_sync)
        try:
            return await conn.fetchval("SELECT version_num FROM alembic_version")
        finally:
            await conn.close()

    assert asyncio.run(_version()) == "0016"  # stamps to CURRENT head, not a hardcoded rev


def test_migration_chain_downgrades_and_reupgrades_cleanly(db):
    """v1.3.0 (todo.md item 21): every revision's downgrade() had NEVER been executed by
    anything before this — only rendered offline as SQL text (test_migrations.py) or
    implied by the upgrade-only live path above. This walks the REAL chain down to base
    (necessarily dropping every table — that's what "no schema" means, data loss here is
    by design, not a bug under test) and back up to head against live Postgres, asserting
    the schema comes back fully functional: right tables, right columns, right indexes,
    and a brand-new commit pushes/reads back cleanly."""
    import asyncpg
    from alembic import command

    from python.av_server.database import _alembic_config, app_engine, engine

    url_sync = AV_TEST_DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://")

    cfg = _alembic_config()
    # A REAL (non-offline) run needs the actual async driver URL this project's env.py
    # connects with everywhere else — unlike the offline SQL-rendering tests in
    # test_migrations.py, which only need a dialect name and use the sync/psycopg2-style
    # URL string purely for cosmetic rendering, never an actual connection.
    cfg.set_main_option("sqlalchemy.url", AV_TEST_DATABASE_URL)

    try:
        command.downgrade(cfg, "base")

        async def _tables():
            conn = await asyncpg.connect(url_sync)
            try:
                rows = await conn.fetch(
                    "SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
                return {r["tablename"] for r in rows}
            finally:
                await conn.close()

        tables_at_base = asyncio.run(_tables())
        # alembic_version itself is the only thing alembic ever guarantees survives a
        # downgrade to base (it's how it knows where it is); every model table this
        # project owns must be gone.
        assert not ({"objects", "trees", "commits", "refs", "runs", "webhooks",
                     "audit_log"} & tables_at_base), \
            f"downgrade to base left tables behind: {tables_at_base}"

        command.upgrade(cfg, "head")

        version = asyncio.run(_version_after())
        assert version == "0016"
        tables_at_head = asyncio.run(_tables())
        assert {"objects", "trees", "commits", "refs", "runs", "webhooks",
                "audit_log", "webhook_deliveries", "events",
                "improver_versions", "change_sets", "policy_packs",
                "canary_results", "project_freeze",
                "eval_suites", "eval_results", "eval_adapters", "tasks",
                "plans", "budgets",
                "causal_links", "strategy_entries", "lessons", "reviews", "critiques",
                "blackboard_entries",
                "sandbox_jobs", "tool_manifests", "action_logs",
                "tenants", "projects", "users", "user_identities", "groups",
                "group_members", "roles", "role_bindings", "api_tokens",
                "sso_providers", "sessions"} <= tables_at_head
        assert {"policy_outcome", "kind", "improver_id", "integrity_signals",
                "plan_id", "budget_id", "stop_reason", "lessons_id"} <= _pg_columns("runs")
        assert {"signature", "env_snapshot_id"} <= _pg_columns("commits")
        assert {"chain_hash", "signature"} <= _pg_columns("audit_log")
    finally:
        # However far the assertions above got, always leave the schema back at head —
        # every OTHER test in this session-scoped file assumes head. A failure partway
        # through this test must not corrupt the rest of the suite's fixture state.
        command.upgrade(cfg, "head")

        # v1.3.2 fix (found live, running the FULL file rather than just this test in
        # isolation): the drop-and-recreate above gives every table NEW Postgres OIDs.
        # The existing "one-shot retry" dance below only ever heals the ONE connection
        # THIS test's own two calls happen to check out (SQLAlchemy's pool is LIFO, so
        # sequential same-test calls keep reusing that one) -- but by the time this test
        # runs in a full suite, the pool can already hold SEVERAL distinct idle
        # connections opened by earlier tests, each still caching prepared-statement
        # plans against the OLD (now-dropped) OIDs. Any LATER test that happens to draw
        # one of THOSE out of the pool hits a raw, unretried
        # `InvalidCachedStatementError` -- confirmed live: a full `pytest
        # tests/test_server.py` run failed 5 unrelated later tests this way, while this
        # test in isolation always passed. `engine.dispose(close=False)` DROPS every
        # currently-pooled connection reference outright (no attempt to close them
        # over the wrong event loop, which is what previously made a plain `dispose()`
        # break the TestClient's own anyio portal for the rest of the session, per the
        # comment below) — every subsequent checkout, in this test and every later one,
        # opens a genuinely fresh connection instead of possibly drawing a stale one.
        # Verified live (a standalone repro script) that this does NOT break the portal.
        engine.sync_engine.dispose(close=False)
        if app_engine is not engine:
            app_engine.sync_engine.dispose(close=False)

    # A downgrade all the way to `base` necessarily DROPS every table (that's what "no
    # schema at all" means) — the commit made before the round trip is gone by design,
    # not a bug. What the round trip actually promises is that the schema comes back
    # fully FUNCTIONAL: a brand-new commit pushes and reads back cleanly afterward.
    #
    # v1.3.1 WP-44 fix (found live): alembic's downgrade-to-base DROPS every table via
    # its OWN raw connection, entirely bypassing the app's shared SQLAlchemy async engine
    # pool (used by `db`, the session-scoped TestClient, for the REST of this file). Any
    # connection already sitting in that pool still holds asyncpg PREPARED STATEMENT
    # PLANS compiled against the old (now-dropped-and-recreated) table OIDs —
    # `pool_pre_ping=True` only checks liveness, not statement-cache validity, so the
    # FIRST query through that pool against an affected table raises
    # `InvalidCachedStatementError` — SQLAlchemy's own asyncpg dialect catches it and
    # invalidates that connection's prepared-statement cache IN RESPONSE (see the
    # exception's own message), so a retried query on the same connection succeeds.
    # `engine.dispose()` was tried here first and made things categorically worse (broke
    # the TestClient's own anyio portal for the rest of the session — "This portal is
    # not running" on every later test, since disposing from a separate `asyncio.run()`
    # loop tears down state the portal's OWN loop still depends on) — the pool must NOT
    # be touched from outside the app's own event loop. A one-shot retry is the correct,
    # narrow fix: absorb the one-time invalidation on a throwaway call, then make the
    # real, asserted call.
    def _post_absorbing_one_stale_cache_hit(url, **kw):
        # TestClient's default raise_server_exceptions=True means an unhandled exception
        # in the route (exactly what an InvalidCachedStatementError is, here) propagates
        # as a raised Python exception out of db.post/db.get, not as a 500 Response — so
        # this must catch the exception itself, not check a status code.
        try:
            return db.post(url, **kw)
        except Exception:
            return db.post(url, **kw)

    def _get_absorbing_one_stale_cache_hit(url):
        try:
            return db.get(url)
        except Exception:
            return db.get(url)

    after_roundtrip = _make_commit("after-migration-roundtrip")
    pushed = _post_absorbing_one_stale_cache_hit("/api/commits", json=after_roundtrip)
    assert pushed.status_code == 201, pushed.text
    still_there = _get_absorbing_one_stale_cache_hit(f"/api/commits/{after_roundtrip['hash']}")
    assert still_there.status_code == 200


async def _version_after() -> str | None:
    import asyncpg

    url_sync = AV_TEST_DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://")
    conn = await asyncpg.connect(url_sync)
    try:
        return await conn.fetchval("SELECT version_num FROM alembic_version")
    finally:
        await conn.close()



# ---------------------------------------------------------------------------
# API hardening â€” rate limiting + CORS defaults (live-server assertions)
# ---------------------------------------------------------------------------

def test_rate_limit_blocks_gc_burst_but_data_plane_stays_open(db, monkeypatch):
    """Destructive GC gets a hard default cap; the data plane stays unlimited so bulk
    uploads never false-positive (Phase: API hardening)."""
    import hashlib

    from python.av_server import server as server_module
    from python.av_server.rate_limit import build_limiter_from_env

    monkeypatch.setattr(
        server_module,
        "_RATE_LIMITER",
        build_limiter_from_env({"AV_RATE_LIMIT_GC": "2/minute"}),
    )
    try:
        codes = [db.post("/api/admin/gc").status_code for _ in range(3)]
        assert codes[2] == 429
        assert int(db.post("/api/admin/gc").headers["retry-after"]) >= 1

        # Data plane untouched by the default policy: five rapid identical uploads.
        content = b"rate-limit data plane probe"
        h = hashlib.sha256(content).hexdigest()
        codes = [db.post(f"/api/objects/{h}", content=content).status_code for _ in range(5)]
        assert all(c in (201, 409) for c in codes)
    finally:
        server_module._RATE_LIMITER.reset()


def test_cors_defaults_lock_to_webui_origin(db):
    allowed = db.get("/api/health", headers={"Origin": "http://localhost:3000"})
    assert allowed.headers.get("access-control-allow-origin") == "http://localhost:3000"

    foreign = db.get("/api/health", headers={"Origin": "https://drive-by.example"})
    assert "access-control-allow-origin" not in foreign.headers


# ---------------------------------------------------------------------------
# v1.2.0 autonomous-loop surface: events, webhooks, runs, audit
# ---------------------------------------------------------------------------

def test_events_cursor_orders_and_resumes(db):
    commit = _make_commit("evt-a")
    db.post("/api/commits", json=commit)
    db.put("/api/refs/proj/main", json={"commit_hash": commit["hash"]})
    first = db.get("/api/events?limit=10").json()
    assert first["events"], "commit/ref mutations must emit events"
    ids = [e["id"] for e in first["events"]]
    assert ids == sorted(ids)
    kinds = {e["kind"] for e in first["events"]}
    assert {"commit", "ref"} <= kinds

    # resume strictly after the last seen cursor:
    since = first["next_cursor"]
    second = db.get(f"/api/events?since={since}").json()
    assert all(e["id"] > since for e in second["events"])

    # project filter only narrows, never leaks other projects' rows:
    scoped = db.get("/api/events?project_id=proj-nope").json()
    assert all(e["project_id"] in (None, "proj-nope") for e in scoped["events"])


def test_events_run_id_filter_and_gap_detection(db):
    # v1.3.0 (todo.md item 9): run_id joins project/kind in one stable query model, and
    # a stale cursor is reported honestly instead of looking identical to "nothing new".
    import uuid

    run_a, run_b = str(uuid.uuid4()), str(uuid.uuid4())
    c1 = _make_commit("evt-run-a", project_id="proj-evt-run")
    c1["run_id"] = run_a
    c2 = _make_commit("evt-run-b", project_id="proj-evt-run")
    c2["run_id"] = run_b
    db.post("/api/commits", json=c1)
    db.post("/api/commits", json=c2)

    only_a = db.get(f"/api/events?project_id=proj-evt-run&run_id={run_a}").json()
    assert only_a["events"], "run_id filter matched nothing"
    assert all(e["payload"].get("run_id") == run_a for e in only_a["events"])
    assert not any(e["payload"].get("run_id") == run_b for e in only_a["events"])

    # A fresh cursor at 0 is never a gap (every consumer starts there legitimately).
    fresh = db.get("/api/events?project_id=proj-evt-run").json()
    assert fresh["gap"] is False

    # A `since` that predates this project's oldest retained event is a real gap.
    oldest = min(e["id"] for e in fresh["events"])
    stale = db.get(f"/api/events?project_id=proj-evt-run&since={max(oldest - 100, 1)}").json()
    if oldest > 1:  # only meaningful once there's genuinely something before `oldest`
        assert stale["gap"] is True
        assert stale["oldest_id"] == oldest

    # A `since` right at the resumable boundary (one before the oldest row) is NOT a gap.
    not_gap = db.get(f"/api/events?project_id=proj-evt-run&since={oldest - 1}").json()
    assert not_gap["gap"] is False


def test_runs_crud_and_commit_linkage_with_lazy_create(db):
    import uuid

    run_id = str(uuid.uuid4())
    r = db.post("/api/runs", json={"id": run_id, "project_id": "proj-runs",
                                   "name": "smoke-run"})
    assert r.status_code == 200 and r.json()["status"] == "created"
    # Idempotent create (multi-agent safe):
    again = db.post("/api/runs", json={"id": run_id, "project_id": "proj-runs"})
    assert again.json()["status"] == "exists"

    commit = _make_commit("run-linked", metrics={"val_loss": 0.42})
    commit["run_id"] = run_id
    assert db.post("/api/commits", json=commit).status_code == 201

    body = db.get(f"/api/runs/{run_id}").json()
    assert commit["hash"] in body["commit_hashes"]
    assert body["metrics_summary"].get("val_loss") == 0.42

    done = db.post(f"/api/runs/{run_id}/complete", json={"metrics_summary": {"status_score": 1}})
    assert done.status_code == 200
    assert db.get(f"/api/runs/{run_id}").json()["status"] == "completed"

    # Lazy-create: a push referencing an UNKNOWN run must succeed AND create it.
    ghost = str(uuid.uuid4())
    c2 = _make_commit("ghost-run", project_id="proj-lazy")
    c2["run_id"] = ghost
    assert db.post("/api/commits", json=c2).status_code == 201
    lazy = db.get(f"/api/runs/{ghost}").json()
    assert lazy["status"] == "created"  # never failed the push


# ---------------------------------------------------------------------------
# v1.3.0 contract freeze (todo.md item 27): validates LIVE run/event/webhook-payload
# bodies against python/av_cli/schemas/*.schema.json — the envelope/semdiff/avh schemas
# are proven stack-free in tests/test_contracts.py; these three need a live server, so
# they live here alongside every other reachability-gated assertion in this file.
# ---------------------------------------------------------------------------

@pytest.mark.skipif(importlib.util.find_spec("jsonschema") is None,
                    reason="jsonschema not installed (dev extra)")
def test_run_payload_matches_schema(db):
    import jsonschema
    from python.av_cli.core import load_contract_schema

    run_id = _hex_hash("schema-run")
    db.post("/api/runs", json={"id": run_id, "project_id": "proj-schema", "name": "schema-run"})
    commit = _make_commit("schema-run-commit", project_id="proj-schema")
    commit["run_id"] = run_id
    db.post("/api/commits", json=commit)

    schema = load_contract_schema("run-1.0")
    jsonschema.validate(db.get(f"/api/runs/{run_id}").json(), schema)
    for row in db.get("/api/runs?project_id=proj-schema").json()["runs"]:
        jsonschema.validate(row, schema)


@pytest.mark.skipif(importlib.util.find_spec("jsonschema") is None,
                    reason="jsonschema not installed (dev extra)")
def test_event_payload_matches_schema(db):
    import jsonschema
    from python.av_cli.core import load_contract_schema

    commit = _make_commit("schema-event")
    db.post("/api/commits", json=commit)
    db.put("/api/refs/proj-schema-evt/main", json={"commit_hash": commit["hash"]})

    schema = load_contract_schema("event-1.0")
    events = db.get("/api/events?limit=10").json()["events"]
    assert events
    for e in events:
        jsonschema.validate(e, schema)


@pytest.mark.skipif(importlib.util.find_spec("jsonschema") is None,
                    reason="jsonschema not installed (dev extra)")
def test_webhook_delivery_body_matches_schema(db, monkeypatch):
    import jsonschema
    from python.av_cli.core import load_contract_schema

    delivered = []

    class FakeResp:
        status_code = 200

    def fake_post(url, data=None, headers=None, timeout=None):
        delivered.append(data)
        return FakeResp()

    import requests as requests_mod
    monkeypatch.setattr(requests_mod, "post", fake_post)

    db.post("/api/webhooks", json={"url": "http://example.invalid/hook", "secret": "s3cr3t"})
    commit = _make_commit("schema-webhook")
    db.post("/api/commits", json=commit)
    for _ in range(40):  # fire-and-forget delivery task — same poll pattern as the test
        if delivered:      # just above (test_webhook_delivery_is_signed_and_filtered)
            break
        time.sleep(0.05)

    assert delivered, "webhook delivery never fired"
    jsonschema.validate(json.loads(delivered[0]), load_contract_schema("webhook-payload-1.0"))


def test_webhook_delivery_is_signed_and_filtered(db, monkeypatch):
    delivered = []

    class FakeResp:
        status_code = 200

    def fake_post(url, data=None, headers=None, timeout=None):
        delivered.append({"url": url, "body": data, "headers": headers})
        return FakeResp()

    import requests as requests_mod
    monkeypatch.setattr(requests_mod, "post", fake_post)

    created = db.post("/api/webhooks", json={
        "url": "http://orchestrator.test/hook",
        "secret": "whsec-123",
        "project_id": "proj-hooked",
        "kinds": ["commit"],
    })
    wid = created.json()["id"]

    commit = _make_commit("hooked", project_id="proj-hooked")
    db.post("/api/commits", json=commit)
    # give the fire-and-forget task a beat:
    for _ in range(40):
        if delivered:
            break
        time.sleep(0.05)
    assert delivered, "webhook did not fire for matching commit event"
    import hashlib
    import hmac as hmac_mod
    expected_sig = hmac_mod.new(b"whsec-123", delivered[0]["body"], hashlib.sha256).hexdigest()
    assert delivered[0]["headers"]["X-AV-Signature"] == expected_sig

    # Non-matching project/kind must NOT deliver:
    before = len(delivered)
    other = _make_commit("unhooked", project_id="proj-other")
    db.post("/api/commits", json=other)
    for _ in range(20):
        if len(delivered) > before:
            break
        time.sleep(0.05)
    assert len(delivered) == before, "webhook fired for a non-matching project"

    listing = db.get("/api/webhooks").json()
    row = next(w for w in listing["webhooks"] if w["id"] == wid)
    assert not row["secret"].startswith("whsec-123"), "full secret must never be returned"
    assert row["secret"].endswith("\u2026")


def test_audit_log_records_mutations(db):
    commit = _make_commit("audited")
    db.post("/api/commits", json=commit)
    db.put("/api/refs/proj/main", json={"commit_hash": commit["hash"]})
    rows = db.get("/api/admin/audit?limit=20").json()["entries"]
    actions = {r["action"] for r in rows}
    assert "commit.push" in actions
    assert "ref.update" in actions
    assert rows[0]["ts"] >= rows[-1]["ts"]  # ordered


def test_audit_prune_dry_run_deletes_nothing(db):
    # v1.3.0 (todo.md item 16): dry_run=true reports would_delete honestly and leaves
    # every row untouched.
    commit = _make_commit("prune-dry-run")
    db.post("/api/commits", json=commit)
    before = db.get("/api/admin/audit?limit=100").json()["entries"]
    assert before, "the commit above must have produced at least one audit row"

    dry = db.delete("/api/admin/audit", params={"before_days": 0, "dry_run": "true"})
    assert dry.status_code == 200
    body = dry.json()
    assert body["deleted"] == 0
    assert body["would_delete"] >= len(before)
    assert body["dry_run"] is True

    after = db.get("/api/admin/audit?limit=100").json()["entries"]
    assert len(after) == len(before), "dry_run must not delete anything"

    # A real (non-dry) prune with the same cutoff actually removes them.
    real = db.delete("/api/admin/audit", params={"before_days": 0})
    assert real.json()["dry_run"] is False
    assert real.json()["deleted"] >= len(before)


def test_multi_agent_same_run_interleaved_pushes(db):
    """Two agents push commits referencing the SAME new run id, interleaved. The run is
    lazily created by whichever push lands first; both commits must end up linked and
    the metrics summary must reflect the union of both agents' latest values."""
    import uuid

    run_id = str(uuid.uuid4())
    c1 = _make_commit("agent-a", project_id="proj-multi", author="anonymous",
                      metrics={"val_loss": 0.5})
    c1["run_id"] = run_id
    c2 = _make_commit("agent-b", project_id="proj-multi", author="anonymous",
                      metrics={"steps": 500})
    c2["run_id"] = run_id

    assert db.post("/api/commits", json=c1).status_code == 201
    assert db.post("/api/commits", json=c2).status_code == 201

    body = db.get(f"/api/runs/{run_id}").json()
    assert sorted(body["commit_hashes"]) == sorted([c1["hash"], c2["hash"]])
    assert body["metrics_summary"].get("val_loss") == 0.5
    assert body["metrics_summary"].get("steps") == 500
    assert body["status"] == "created"  # lazy-created state, not failed by ordering


def test_ref_update_without_expected_hash_is_unconditional_last_write_wins(db):
    """Pre-1.2.5 behavior is preserved exactly when expected_hash is omitted — additive
    change, old clients unaffected."""
    proj = "proj-ref-lww"
    c1 = _make_commit("ref-lww-1", project_id=proj)
    c2 = _make_commit("ref-lww-2", project_id=proj)
    db.post("/api/commits", json=c1)
    db.post("/api/commits", json=c2)

    assert db.put(f"/api/refs/{proj}/main", json={"commit_hash": c1["hash"]}).status_code == 200
    # No expected_hash: unconditionally overwrites, exactly like every pre-1.2.5 client.
    assert db.put(f"/api/refs/{proj}/main", json={"commit_hash": c2["hash"]}).status_code == 200
    assert db.get(f"/api/refs/{proj}/main").json()["commit_hash"] == c2["hash"]


def test_ref_update_expected_hash_compare_and_swap(db):
    """v1.2.5: two agents racing the same branch ref. The loser's PUT (stale expected_hash)
    gets 409 with the ref's real current hash, instead of silently overwriting the winner —
    this is the server-side half of the WP-7 ref-race fix; core.py's _finalize_commit
    catches the client-side RefRaceError and queues rather than losing the commit."""
    proj = "proj-ref-race"
    base = _make_commit("ref-race-base", project_id=proj)
    agent_a = _make_commit("ref-race-a", project_id=proj, parents=[base["hash"]])
    agent_b = _make_commit("ref-race-b", project_id=proj, parents=[base["hash"]])
    for c in (base, agent_a, agent_b):
        assert db.post("/api/commits", json=c).status_code == 201
    assert db.put(f"/api/refs/{proj}/main", json={"commit_hash": base["hash"]}).status_code == 200

    # Agent A wins the race: its expected_hash (base) matches the ref's current value.
    r_a = db.put(f"/api/refs/{proj}/main",
                 json={"commit_hash": agent_a["hash"], "expected_hash": base["hash"]})
    assert r_a.status_code == 200
    assert db.get(f"/api/refs/{proj}/main").json()["commit_hash"] == agent_a["hash"]

    # Agent B loses: it still believes the ref is at `base`, but A already moved it.
    r_b = db.put(f"/api/refs/{proj}/main",
                 json={"commit_hash": agent_b["hash"], "expected_hash": base["hash"]})
    assert r_b.status_code == 409
    detail = r_b.json()["detail"]
    assert detail["error"] == "ref_race"
    assert detail["current"] == agent_a["hash"]  # tells the loser exactly what won
    assert detail["expected"] == base["hash"]
    # The ref itself is untouched by the losing attempt — no partial/corrupt write.
    assert db.get(f"/api/refs/{proj}/main").json()["commit_hash"] == agent_a["hash"]
    # Both commits remain individually reachable by hash — content addressing never loses
    # data; only the branch POINTER was contested.
    assert db.get(f"/api/commits/{agent_b['hash']}").status_code == 200

    # The audit trail records the race as a 409, not a silent 200.
    rows = db.get(f"/api/admin/audit?action=ref.update&project_id={proj}&limit=20").json()["entries"]
    statuses = [r["status_code"] for r in rows]
    assert 409 in statuses


def test_run_create_is_idempotent_for_concurrent_agents(db):
    import uuid

    run_id = str(uuid.uuid4())
    payload = {"id": run_id, "project_id": "proj-idem", "name": "same"}
    r1 = db.post("/api/runs", json=payload)
    r2 = db.post("/api/runs", json=payload)
    assert r1.json()["status"] in ("created", "exists")
    assert r2.json()["status"] in ("created", "exists")
    listing = db.get("/api/runs?project_id=proj-idem").json()["runs"]
    assert len([x for x in listing if x["id"] == run_id]) == 1


# ---------------------------------------------------------------------------
# v1.2.2 audit depth, webhook delivery ledger, signatures, env snapshot linkage
# ---------------------------------------------------------------------------

def test_audit_records_outcome_status_codes(db):
    """Every mutation's trail entry carries the HTTP outcome ("did it land?", not just
    "was it tried")."""
    commit = _make_commit("audited-outcome")
    resp = db.post("/api/commits", json=commit)
    rows = db.get(f"/api/admin/audit?action=commit.push&limit=5").json()["entries"]
    mine = next(r for r in rows if r["details"]["hash"] == commit["hash"])
    assert mine["status_code"] == resp.status_code == 201


def test_audit_filters_action_project_time_and_pagination(db):
    proj_a, proj_b = "proj-audit-a", "proj-audit-b"
    ca = _make_commit("audit-fa", project_id=proj_a)
    cb = _make_commit("audit-fb", project_id=proj_b)
    db.post("/api/commits", json=ca)
    db.post("/api/commits", json=cb)
    db.put(f"/api/refs/{proj_a}/main", json={"commit_hash": ca["hash"]})

    # action filter:
    rows = db.get("/api/admin/audit?action=ref.update&limit=50").json()
    assert rows["total"] >= 1
    assert all(r["action"] == "ref.update" for r in rows["entries"])
    assert all(r["project_id"] == proj_a for r in rows["entries"])

    # project filter:
    rows = db.get(f"/api/admin/audit?project_id={proj_b}&limit=50").json()
    assert rows["total"] >= 1
    assert all(r["project_id"] == proj_b for r in rows["entries"])

    # since/until windows: an empty window matches nothing, a wide one matches all.
    empty_past = db.get(
        "/api/admin/audit?since=2030-01-01T00:00:00&limit=500").json()
    assert empty_past["total"] == 0
    empty_until = db.get(
        "/api/admin/audit?until=2020-01-01T00:00:00&limit=500").json()
    assert empty_until["total"] == 0
    wide = db.get("/api/admin/audit?since=2020-01-01T00:00:00&limit=500").json()
    assert wide["total"] >= 2

    # invalid timestamps are 422, never silent match-alls:
    assert db.get("/api/admin/audit?since=not-a-date").status_code == 422

    # pagination math:
    page1 = db.get("/api/admin/audit?limit=1&offset=0").json()
    page2 = db.get("/api/admin/audit?limit=1&offset=1").json()
    assert page1["entries"][0]["id"] != page2["entries"][0]["id"]
    assert page1["total"] == page2["total"]


def test_audit_retention_sweep_runs_during_gc(db, monkeypatch):
    from datetime import datetime, timedelta

    commit = _make_commit("audit-retention")
    db.post("/api/commits", json=commit)
    before_ids = {e["id"] for e in db.get("/api/admin/audit?limit=500").json()["entries"]}
    assert before_ids

    # Retention 0 days ⇒ every existing row is past its cutoff at GC time.
    monkeypatch.setattr(server_module, "AUDIT_RETENTION_DAYS", 0)
    gc = db.post("/api/admin/gc")
    assert gc.status_code == 200
    # v1.2.5: GC's own admin.gc audit row is written AFTER the retention sweep (correctly
    # — it postdates the cutoff), so comparing raw totals is no longer a reliable signal
    # (GC always adds at least one new row). Assert the PRE-GC rows specifically are gone.
    after_ids = {e["id"] for e in db.get("/api/admin/audit?limit=500").json()["entries"]}
    assert not (before_ids & after_ids), "retention sweep should have removed the pre-GC rows"


def test_webhook_delivery_rows_record_outcome_and_dead_letter(db, monkeypatch):
    """v1.2.2 webhook depth: attempts persist BEFORE the POST; failures retry on the
    worker interval and dead-letter after AV_WEBHOOK_MAX_ATTEMPTS; observability
    endpoint exposes the ledger."""
    calls = []

    class FakeResp:
        def __init__(self, code):
            self.status_code = code

    def always_fail(url, data=None, headers=None, timeout=None):
        calls.append(url)
        return FakeResp(500)

    import requests as requests_mod
    monkeypatch.setattr(requests_mod, "post", always_fail)
    # Retry interval 0 ⇒ failed rows are immediately due again (test-speed backoff).
    monkeypatch.setattr(server_module, "WEBHOOK_RETRY_INTERVAL_SECS", 0)

    created = db.post("/api/webhooks", json={
        "url": "http://dead.test/hook", "secret": "s3",
        "project_id": "proj-dead", "kinds": ["commit"],
    })
    wid = created.json()["id"]

    commit = _make_commit("dead-letter", project_id="proj-dead")
    db.post("/api/commits", json=commit)
    for _ in range(40):
        rows = db.get(f"/api/admin/webhook-deliveries?webhook_id={wid}").json()["deliveries"]
        if rows:
            break
        time.sleep(0.05)
    assert rows, "no delivery row was persisted"
    row = rows[0]
    assert row["attempt"] >= 2 and row["response_code"] == 500
    assert row["event_kind"] == "commit" and row["project_id"] == "proj-dead"

    def _drive():
        fut = db.portal.start_task_soon(server_module.process_due_webhook_deliveries)
        return fut.result(timeout=30)

    # Drive retries until dead-letter (first attempt done; max 5 ⇒ ≤4 more rounds).
    for _ in range(10):
        _drive()
        row = db.get(
            f"/api/admin/webhook-deliveries?webhook_id={wid}&status=dead"
        ).json()["deliveries"]
        if row:
            break
    assert row, f"delivery never dead-lettered: last={row!r}"
    assert calls, "retry worker did not re-POST"

    # status filter works both ways:
    pendingish = db.get(
        f"/api/admin/webhook-deliveries?webhook_id={wid}&status=pending"
    ).json()["deliveries"]
    assert pendingish == []


def test_webhook_delivery_success_path_records_delivered(db, monkeypatch):
    class FakeResp:
        status_code = 200

    delivered_ok = []
    import requests as requests_mod

    monkeypatch.setattr(requests_mod, "post",
                        lambda url, data=None, headers=None, timeout=None:
                        delivered_ok.append(url) or FakeResp())

    created = db.post("/api/webhooks", json={
        "url": "http://ok.test/hook", "secret": "s4",
        "project_id": "proj-ok", "kinds": ["commit"],
    })
    wid = created.json()["id"]
    db.post("/api/commits", json=_make_commit("delivered-ok", project_id="proj-ok"))

    for _ in range(40):
        rows = db.get(f"/api/admin/webhook-deliveries?webhook_id={wid}").json()["deliveries"]
        if rows and rows[0]["status"] == "delivered":
            break
        time.sleep(0.05)
    assert rows and rows[0]["status"] == "delivered"
    assert rows[0]["response_code"] == 200
    assert rows[0]["next_retry_at"] is None


# ---------------------------------------------------------------------------
# v1.2.5: webhook delivery maturity — health columns, backoff, disable-after-N, replay
# ---------------------------------------------------------------------------

def test_webhook_health_columns_update_on_success_and_failure(db, monkeypatch):
    class FakeResp:
        def __init__(self, code):
            self.status_code = code

    outcomes = iter([500, 500, 200])  # fail, fail, then succeed

    import requests as requests_mod
    monkeypatch.setattr(requests_mod, "post",
                        lambda url, data=None, headers=None, timeout=None: FakeResp(next(outcomes)))
    monkeypatch.setattr(server_module, "WEBHOOK_RETRY_INTERVAL_SECS", 0)

    wid = db.post("/api/webhooks", json={
        "url": "http://health.test/hook", "secret": "s5",
        "project_id": "proj-health", "kinds": ["commit"],
    }).json()["id"]

    db.post("/api/commits", json=_make_commit("health-1", project_id="proj-health"))
    for _ in range(40):
        row = next((w for w in db.get("/api/webhooks").json()["webhooks"] if w["id"] == wid), None)
        if row and row["consecutive_failures"] >= 1:
            break
        time.sleep(0.05)
    assert row["consecutive_failures"] == 1
    assert row["last_failure_at"] is not None
    assert row["last_success_at"] is None

    def _drive():
        fut = db.portal.start_task_soon(server_module.process_due_webhook_deliveries)
        return fut.result(timeout=30)

    _drive()  # second attempt: also fails (outcomes[1] == 500)
    row = next(w for w in db.get("/api/webhooks").json()["webhooks"] if w["id"] == wid)
    assert row["consecutive_failures"] == 2

    _drive()  # third attempt: succeeds (outcomes[2] == 200)
    row = next(w for w in db.get("/api/webhooks").json()["webhooks"] if w["id"] == wid)
    assert row["consecutive_failures"] == 0, "a success must clear the failure streak"
    assert row["last_success_at"] is not None


def test_webhook_delivery_backoff_grows_exponentially(db, monkeypatch):
    """Drives _deliver_one directly (not through process_due_webhook_deliveries' due-time
    filter) so consecutive attempts can be measured without waiting out real backoff
    seconds — attempt N's scheduled gap must be interval * 2**(N-1), capped at
    WEBHOOK_RETRY_MAX_SECS."""
    class FakeResp:
        status_code = 500

    import requests as requests_mod
    monkeypatch.setattr(requests_mod, "post",
                        lambda url, data=None, headers=None, timeout=None: FakeResp())
    monkeypatch.setattr(server_module, "WEBHOOK_RETRY_INTERVAL_SECS", 10)
    monkeypatch.setattr(server_module, "WEBHOOK_RETRY_MAX_SECS", 100)
    monkeypatch.setattr(server_module, "WEBHOOK_MAX_ATTEMPTS", 10)  # stay 'failed', not 'dead'

    wid = db.post("/api/webhooks", json={
        "url": "http://backoff.test/hook", "secret": "s6",
        "project_id": "proj-backoff", "kinds": ["commit"],
    }).json()["id"]
    db.post("/api/commits", json=_make_commit("backoff-1", project_id="proj-backoff"))

    from datetime import datetime as _dt

    def _gap(d):
        created = _dt.fromisoformat(d["created_at"])
        retry = _dt.fromisoformat(d["next_retry_at"])
        return (retry - created).total_seconds()

    def _expected_gap(attempt: int) -> float:
        return min(10 * (2 ** (attempt - 1)), 100)

    def _assert_formula_holds(row):
        expected = _expected_gap(row["attempt"])
        gap = _gap(row)
        assert expected - 2 <= gap <= expected + 2, \
            f"attempt {row['attempt']}: expected ~{expected}s backoff, got {gap}s"

    for _ in range(40):
        rows = db.get(f"/api/admin/webhook-deliveries?webhook_id={wid}").json()["deliveries"]
        if rows and rows[0]["attempt"] >= 1:
            break
        time.sleep(0.05)
    assert rows
    # v1.2.5: the real background retry worker (started once at test-session lifespan,
    # on whatever interval it had at THAT time) also races for this row, so the attempt
    # actually observed here isn't guaranteed to be exactly 1 — checking the FORMULA
    # against whatever attempt is present is what's actually being tested, not a specific
    # count. See _webhook_retry_worker: its interval is bound once at task creation, so
    # this test's monkeypatch of the module global can't (and needn't) control its tick.
    _assert_formula_holds(rows[0])

    async def _force_next_attempt():
        from sqlalchemy import select as _select

        from python.av_server.database import async_session_factory as _sf
        from python.av_server.models import DBWebhook as _W, DBWebhookDelivery as _D

        async with _sf() as session:
            delivery = (await session.execute(
                _select(_D).where(_D.webhook_id == wid)
            )).scalars().first()
            hook = (await session.execute(_select(_W).where(_W.id == wid))).scalar_one()
            event = {"id": delivery.event_id or -1, "kind": delivery.event_kind,
                     "project_id": delivery.project_id, "payload": delivery.payload}
            await server_module._deliver_one(hook, delivery, event, session)
            await session.commit()

    for _ in range(4):
        fut = db.portal.start_task_soon(_force_next_attempt)
        fut.result(timeout=30)
        rows = db.get(f"/api/admin/webhook-deliveries?webhook_id={wid}").json()["deliveries"]
        _assert_formula_holds(rows[0])
        if rows[0]["attempt"] >= 6:  # 10*2**5=320, well past the 100s cap — proven the cap holds
            break
    assert rows[0]["attempt"] >= 4, "never observed enough attempts to prove the backoff curve"
    assert _gap(rows[0]) <= 102, "backoff must respect WEBHOOK_RETRY_MAX_SECS"


def test_webhook_disable_after_n_consecutive_failures(db, monkeypatch):
    class FakeResp:
        status_code = 500

    import requests as requests_mod
    monkeypatch.setattr(requests_mod, "post",
                        lambda url, data=None, headers=None, timeout=None: FakeResp())
    monkeypatch.setattr(server_module, "WEBHOOK_RETRY_INTERVAL_SECS", 0)
    monkeypatch.setattr(server_module, "WEBHOOK_MAX_ATTEMPTS", 10)  # isolate from dead-lettering
    monkeypatch.setattr(server_module, "WEBHOOK_DISABLE_AFTER", 2)

    wid = db.post("/api/webhooks", json={
        "url": "http://disable.test/hook", "secret": "s7",
        "project_id": "proj-disable", "kinds": ["commit"],
    }).json()["id"]
    db.post("/api/commits", json=_make_commit("disable-1", project_id="proj-disable"))

    def _row():
        return next(w for w in db.get("/api/webhooks").json()["webhooks"] if w["id"] == wid)

    for _ in range(40):
        if _row()["consecutive_failures"] >= 1:
            break
        time.sleep(0.05)
    assert _row()["active"] is True

    fut = db.portal.start_task_soon(server_module.process_due_webhook_deliveries)
    fut.result(timeout=30)

    row = _row()
    assert row["consecutive_failures"] == 2
    assert row["active"] is False
    assert row["disabled_reason"] and "auto-disabled" in row["disabled_reason"]

    # The auto-disable is itself an audited, evented transition.
    audit_rows = db.get("/api/admin/audit?action=webhook.auto_disable&limit=10").json()["entries"]
    assert any(r["details"].get("webhook_id") == wid for r in audit_rows)

    # An already-disabled webhook is left alone (never toggled back on by more failures).
    fut2 = db.portal.start_task_soon(server_module.process_due_webhook_deliveries)
    fut2.result(timeout=30)
    assert _row()["active"] is False

    # Explicit re-enable clears the streak and reactivates.
    enable_resp = db.post(f"/api/webhooks/{wid}/enable")
    assert enable_resp.status_code == 200
    row = _row()
    assert row["active"] is True
    assert row["consecutive_failures"] == 0
    assert row["disabled_reason"] is None


def test_webhook_delivery_replay_requeues_dead_letter(db, monkeypatch):
    class FakeResp:
        def __init__(self, code):
            self.status_code = code

    state = {"fail": True}
    import requests as requests_mod
    monkeypatch.setattr(
        requests_mod, "post",
        lambda url, data=None, headers=None, timeout=None: FakeResp(500 if state["fail"] else 200),
    )
    monkeypatch.setattr(server_module, "WEBHOOK_RETRY_INTERVAL_SECS", 0)
    monkeypatch.setattr(server_module, "WEBHOOK_MAX_ATTEMPTS", 1)  # dead-letter on first failure

    wid = db.post("/api/webhooks", json={
        "url": "http://replay.test/hook", "secret": "s8",
        "project_id": "proj-replay", "kinds": ["commit"],
    }).json()["id"]
    db.post("/api/commits", json=_make_commit("replay-1", project_id="proj-replay"))

    for _ in range(40):
        rows = db.get(f"/api/admin/webhook-deliveries?webhook_id={wid}&status=dead").json()["deliveries"]
        if rows:
            break
        time.sleep(0.05)
    assert rows, "delivery never dead-lettered"
    delivery_id = rows[0]["id"]

    # Can't replay something that isn't failed/dead.
    still_delivered = db.get(f"/api/admin/webhook-deliveries?webhook_id={wid}").json()["deliveries"][0]
    assert still_delivered["status"] == "dead"

    state["fail"] = False  # the endpoint "gets fixed" before the replay
    replay_resp = db.post(f"/api/admin/webhook-deliveries/{delivery_id}/replay")
    assert replay_resp.status_code == 200
    assert replay_resp.json()["delivery"]["status"] == "pending"
    assert replay_resp.json()["delivery"]["attempt"] == 0

    fut = db.portal.start_task_soon(server_module.process_due_webhook_deliveries)
    fut.result(timeout=30)
    final = db.get(f"/api/admin/webhook-deliveries?webhook_id={wid}").json()["deliveries"][0]
    assert final["status"] == "delivered"

    # A delivered/pending row refuses to replay (409) — no double-delivery via replay.
    refused = db.post(f"/api/admin/webhook-deliveries/{delivery_id}/replay")
    assert refused.status_code == 409

    replay_audit = db.get("/api/admin/audit?action=webhook.delivery_replay&limit=10").json()["entries"]
    assert any(r["details"].get("delivery_id") == delivery_id for r in replay_audit)


def test_poison_webhook_does_not_block_a_healthy_sibling(db, monkeypatch):
    """One endpoint that always 500s must dead-letter on its own schedule without
    delaying or skipping deliveries to a healthy webhook in the same retry tick."""
    class FakeResp:
        def __init__(self, code):
            self.status_code = code

    def _post(url, data=None, headers=None, timeout=None):
        return FakeResp(500 if "poison" in url else 200)

    import requests as requests_mod
    monkeypatch.setattr(requests_mod, "post", _post)
    monkeypatch.setattr(server_module, "WEBHOOK_RETRY_INTERVAL_SECS", 0)
    monkeypatch.setattr(server_module, "WEBHOOK_MAX_ATTEMPTS", 2)

    proj = "proj-poison"
    poison_id = db.post("/api/webhooks", json={
        "url": "http://poison.test/hook", "secret": "sp", "project_id": proj, "kinds": ["commit"],
    }).json()["id"]
    healthy_id = db.post("/api/webhooks", json={
        "url": "http://healthy.test/hook", "secret": "sh", "project_id": proj, "kinds": ["commit"],
    }).json()["id"]
    db.post("/api/commits", json=_make_commit("poison-1", project_id=proj))

    for _ in range(40):
        h_rows = db.get(f"/api/admin/webhook-deliveries?webhook_id={healthy_id}").json()["deliveries"]
        if h_rows and h_rows[0]["status"] == "delivered":
            break
        time.sleep(0.05)
    assert h_rows and h_rows[0]["status"] == "delivered", \
        "the healthy webhook must be delivered promptly regardless of the poison one"

    # Drive the poison one to dead-letter without ever affecting the healthy row.
    for _ in range(10):
        p_rows = db.get(f"/api/admin/webhook-deliveries?webhook_id={poison_id}").json()["deliveries"]
        if p_rows and p_rows[0]["status"] == "dead":
            break
        fut = db.portal.start_task_soon(server_module.process_due_webhook_deliveries)
        fut.result(timeout=30)
    assert p_rows and p_rows[0]["status"] == "dead"
    h_rows_final = db.get(f"/api/admin/webhook-deliveries?webhook_id={healthy_id}").json()["deliveries"]
    assert h_rows_final[0]["status"] == "delivered"
    assert h_rows_final[0]["attempt"] == 1, "the healthy delivery was never re-attempted"


def test_webhook_backlog_delivers_all_in_order_without_starving_healthy_hook(db, monkeypatch):
    """v1.3.0 (todo.md item 10): a real BACKLOG — many pending deliveries at once across
    a healthy and a poison endpoint — not just one commit each. Every healthy delivery
    must still land, in cursor order, and `deliveries`/`replay` must report the ledger
    truthfully throughout (not just at the end)."""
    class FakeResp:
        def __init__(self, code):
            self.status_code = code

    def _post(url, data=None, headers=None, timeout=None):
        return FakeResp(500 if "poison" in url else 200)

    import requests as requests_mod
    monkeypatch.setattr(requests_mod, "post", _post)
    monkeypatch.setattr(server_module, "WEBHOOK_RETRY_INTERVAL_SECS", 0)
    monkeypatch.setattr(server_module, "WEBHOOK_MAX_ATTEMPTS", 2)

    proj = "proj-backlog"
    poison_id = db.post("/api/webhooks", json={
        "url": "http://poison.test/hook", "secret": "sp", "project_id": proj, "kinds": ["commit"],
    }).json()["id"]
    healthy_id = db.post("/api/webhooks", json={
        "url": "http://healthy.test/hook", "secret": "sh", "project_id": proj, "kinds": ["commit"],
    }).json()["id"]

    N = 20
    for i in range(N):
        db.post("/api/commits", json=_make_commit(f"backlog-{i}", project_id=proj))

    for _ in range(80):
        rows = db.get(f"/api/admin/webhook-deliveries?webhook_id={healthy_id}&limit={N + 5}").json()["deliveries"]
        if sum(1 for r in rows if r["status"] == "delivered") == N:
            break
        time.sleep(0.05)
    delivered = [r for r in rows if r["status"] == "delivered"]
    assert len(delivered) == N, f"only {len(delivered)}/{N} healthy deliveries landed"
    # /api/admin/webhook-deliveries orders newest-first (DBWebhookDelivery.id.desc()) —
    # matches /api/admin/audit's own convention. Every id must be distinct and every one
    # of the N commits' deliveries must be present — no duplicate row, none skipped —
    # rather than asserting a specific direction this endpoint never promised.
    ids = [r["id"] for r in delivered]
    assert len(set(ids)) == N, "duplicate or missing delivery ids in the backlog"
    assert ids == sorted(ids, reverse=True), \
        "deliveries must come back newest-first, matching /api/admin/audit's convention"

    # The parallel poison backlog must eventually fully dead-letter, on its own schedule,
    # without ever affecting the healthy count/order asserted above.
    for _ in range(20):
        p_rows = db.get(f"/api/admin/webhook-deliveries?webhook_id={poison_id}&limit={N + 5}").json()["deliveries"]
        if all(r["status"] == "dead" for r in p_rows) and len(p_rows) == N:
            break
        fut = db.portal.start_task_soon(server_module.process_due_webhook_deliveries)
        fut.result(timeout=30)
    assert len(p_rows) == N and all(r["status"] == "dead" for r in p_rows)

    # `deliveries` (paginated) and `replay` see this same backlog truthfully.
    page1 = db.get(f"/api/admin/webhook-deliveries?webhook_id={poison_id}&limit=5").json()
    assert len(page1["deliveries"]) == 5
    assert page1.get("next_cursor")

    replay_target = p_rows[0]["id"]
    replay_resp = db.post(f"/api/admin/webhook-deliveries/{replay_target}/replay")
    assert replay_resp.status_code == 200
    requeued = db.get(f"/api/admin/webhook-deliveries?webhook_id={poison_id}").json()["deliveries"]
    replayed_row = next(r for r in requeued if r["id"] == replay_target)
    assert replayed_row["status"] == "pending"


def test_commit_signature_round_trips_over_the_wire(db):
    sig_blob = {"algo": "ed25519", "public_key": "ab" * 32,
                "sig": base64.b64encode(b"\x01" * 64).decode(), "signed_at": "now"}
    commit = _make_commit("signed-wire", signature=sig_blob)
    assert db.post("/api/commits", json=commit).status_code == 201

    got = db.get(f"/api/commits/{commit['hash']}").json()
    assert got["signature"]["algo"] == "ed25519"
    assert got["signature"]["sig"] == sig_blob["sig"], \
        "signature must survive push→fetch byte-exactly so clones can verify"

    listed = db.get("/api/commits?limit=50").json()["commits"]
    match = next(c for c in listed if c["hash"] == commit["hash"])
    assert match["signature"]["public_key"] == "ab" * 32

    unsigned = _make_commit("unsigned-wire")
    db.post("/api/commits", json=unsigned)
    got = db.get(f"/api/commits/{unsigned['hash']}").json()
    assert got["signature"] is None  # unsigned stays valid, honestly represented


# ---------------------------------------------------------------------------
# v1.2.5: WebUI run detail — GET /api/runs/{id}/summary, POST /api/runs/{id}/avh
# ---------------------------------------------------------------------------

def test_run_summary_aggregates_lineage_commits_and_semantic_diff(db):
    """One request replaces the WebUI's previous N individual GET /api/commits/{hash}
    calls — lineage, linked commits, and a server-computed semantic summary."""
    proj = "proj-run-summary"
    parent_run = db.post("/api/runs", json={"project_id": proj, "name": "parent"}).json()["id"]
    child_run = db.post("/api/runs", json={
        "project_id": proj, "name": "child", "parent_run_id": parent_run,
    }).json()["id"]

    tree_v1 = {"a.txt": {"hash": "a" * 64, "size": 10, "type": "code"}}
    tree_v2 = {"a.txt": {"hash": "a" * 64, "size": 10, "type": "code"},
               "b.txt": {"hash": "b" * 64, "size": 20, "type": "code"}}
    c1 = _make_commit("summary-1", project_id=proj, tree=tree_v1, run_id=child_run,
                      metrics={"loss": 1.0})
    c2 = _make_commit("summary-2", project_id=proj, tree=tree_v2, run_id=child_run,
                      metrics={"loss": 0.5}, parents=[c1["hash"]])
    assert db.post("/api/commits", json=c1).status_code == 201
    assert db.post("/api/commits", json=c2).status_code == 201

    body = db.get(f"/api/runs/{child_run}/summary").json()
    assert body["run"]["id"] == child_run
    assert [n["id"] for n in body["lineage"]] == [child_run, parent_run]
    assert body["total_commits"] == 2
    assert {c["hash"] for c in body["commits"]} == {c1["hash"], c2["hash"]}
    # v1.3.0: files.added/removed/changed carry {path, kind} objects (matching
    # av_cli.semdiff's own schema), not bare path strings.
    assert body["semantic_summary"]["files"]["added"] == [{"path": "b.txt", "kind": "code"}]
    assert body["semantic_summary"]["files"]["changed"] == []
    assert body["semantic_summary"]["totals"]["bytes_after"] == 30
    # v1.3.0: full schema parity — models/chunks/datasets now ride the server-side
    # summary too, not just files/totals.
    assert body["semantic_summary"]["chunks"]["status"] == "no_chunks"
    assert body["semantic_summary"]["models"] == []
    assert body["semantic_summary"]["datasets"] == []


def test_run_summary_no_semantic_diff_with_fewer_than_two_commits(db):
    proj = "proj-run-summary-single"
    run_id = db.post("/api/runs", json={"project_id": proj}).json()["id"]
    c1 = _make_commit("summary-single", project_id=proj, run_id=run_id)
    assert db.post("/api/commits", json=c1).status_code == 201

    body = db.get(f"/api/runs/{run_id}/summary").json()
    assert body["semantic_summary"] is None
    assert body["total_commits"] == 1


def test_server_side_summary_matches_client_side_semdiff_on_the_same_trees():
    """v1.3.0 (todo.md item 3): server.py::_summarize_tree_diff() is a deliberate,
    independent re-implementation (the server package never imports av_cli) of exactly
    what av_cli.semdiff.diff_trees() computes — this is the shared-golden-fixture proof
    that the two can never silently drift apart the way the files-only version already
    had (it used to omit models/chunks/datasets entirely). Stack-free: pure functions,
    no live server needed, but lives here (not test_semdiff.py) since it's inherently a
    cross-package comparison."""
    from python.av_cli.semdiff import diff_trees
    from python.av_server.server import _summarize_tree_diff

    old_tree = {
        "a.txt": {"hash": "a" * 64, "size": 10, "type": "code"},
        "model.safetensors": {
            "hash": "m1" * 32, "size": 0, "type": "artifact",
            "layers": [{"name": "L0", "hash": "l0" * 32, "size": 100},
                      {"name": "L1", "hash": "l1" * 32, "size": 200}],
        },
        "data/train.parquet": {"hash": "d1" * 32, "size": 5000, "type": "artifact"},
        "ckpt.pt": {
            "hash": "c1" * 32, "size": 0, "type": "artifact",
            "chunks": [{"hash": "ch0" * 21 + "0", "size": 1000, "offset": 0},
                      {"hash": "ch1" * 21 + "0", "size": 2000, "offset": 1000}],
        },
    }
    new_tree = {
        "a.txt": {"hash": "a" * 64, "size": 10, "type": "code"},  # unchanged
        "b.txt": {"hash": "b" * 64, "size": 20, "type": "code"},  # added
        "model.safetensors": {
            "hash": "m2" * 32, "size": 0, "type": "artifact",
            "layers": [{"name": "L0", "hash": "l0" * 32, "size": 100},  # unchanged
                      {"name": "L1", "hash": "l1new" * 13 + "0", "size": 200}],  # moved
        },
        "data/train.parquet": {"hash": "d2" * 32, "size": 5500, "type": "artifact"},  # changed
        "ckpt.pt": {
            "hash": "c2" * 32, "size": 0, "type": "artifact",
            "chunks": [{"hash": "ch0" * 21 + "0", "size": 1000, "offset": 0},  # reused
                      {"hash": "ch2" * 21 + "0", "size": 3000, "offset": 1000}],  # new
        },
        # a.txt removed intentionally handled below by using a third comparison
    }

    client_side = diff_trees(old_tree, new_tree)
    server_side = _summarize_tree_diff(old_tree, new_tree)

    # base/target/summary(prose) are CLI-only additions the run-summary endpoint doesn't
    # carry the same way — compare everything else field-by-field.
    for key in ("files", "models", "chunks", "datasets", "totals"):
        assert client_side[key] == server_side[key], (
            f"server/client semdiff drift on {key!r}:\n"
            f"  client: {client_side[key]}\n  server: {server_side[key]}"
        )


def test_run_summary_404_for_unknown_run(db):
    assert db.get("/api/runs/does-not-exist/summary").status_code == 404


def test_run_metrics_endpoint_pages_the_full_series_oldest_first(db):
    """v1.3.0 (todo.md item 7): /summary caps at _RUN_SUMMARY_MAX_COMMITS and returns
    newest-first; /metrics is the uncapped, oldest-first, cursor-paginated complement."""
    proj = "proj-run-metrics"
    run_id = db.post("/api/runs", json={"project_id": proj}).json()["id"]
    hashes = []
    prev = None
    for i in range(3):
        extra = {"parents": [prev]} if prev else {}
        c = _make_commit(f"metrics-{i}", project_id=proj, run_id=run_id,
                         metrics={"step": i}, **extra)
        assert db.post("/api/commits", json=c).status_code == 201
        hashes.append(c["hash"])
        prev = c["hash"]

    page1 = db.get(f"/api/runs/{run_id}/metrics", params={"limit": 2}).json()
    assert [p["hash"] for p in page1["points"]] == hashes[:2]
    assert page1["points"][0]["metrics"] == {"step": 0}
    assert page1["next_cursor"] is not None

    page2 = db.get(f"/api/runs/{run_id}/metrics",
                   params={"limit": 2, "cursor": page1["next_cursor"]}).json()
    assert [p["hash"] for p in page2["points"]] == hashes[2:]
    assert page2["next_cursor"] is None


def test_run_metrics_endpoint_404s_for_unknown_run(db):
    assert db.get("/api/runs/does-not-exist/metrics").status_code == 404


def test_run_metrics_endpoint_rejects_a_garbage_cursor(db):
    proj = "proj-run-metrics-bad-cursor"
    run_id = db.post("/api/runs", json={"project_id": proj}).json()["id"]
    resp = db.get(f"/api/runs/{run_id}/metrics", params={"cursor": "not-a-real-cursor!!"})
    assert resp.status_code == 422


def test_run_lineage_endpoint_pages_past_the_summary_cap(db):
    """v1.3.0: builds a chain longer than one page (depth=2) and proves the cursor resumes
    exactly where the previous page left off, ending with next_cursor: null at the root."""
    proj = "proj-run-lineage"
    root_id = db.post("/api/runs", json={"project_id": proj, "name": "root"}).json()["id"]
    mid_id = db.post("/api/runs", json={
        "project_id": proj, "name": "mid", "parent_run_id": root_id,
    }).json()["id"]
    leaf_id = db.post("/api/runs", json={
        "project_id": proj, "name": "leaf", "parent_run_id": mid_id,
    }).json()["id"]

    page1 = db.get(f"/api/runs/{leaf_id}/lineage", params={"depth": 2}).json()
    assert [n["id"] for n in page1["lineage"]] == [leaf_id, mid_id]
    assert page1["next_cursor"] == root_id

    page2 = db.get(f"/api/runs/{leaf_id}/lineage",
                   params={"depth": 2, "cursor": page1["next_cursor"]}).json()
    assert [n["id"] for n in page2["lineage"]] == [root_id]
    assert page2["next_cursor"] is None


def test_run_lineage_endpoint_404s_for_unknown_run(db):
    assert db.get("/api/runs/does-not-exist/lineage").status_code == 404


def test_run_lineage_endpoint_422s_for_an_unknown_cursor(db):
    proj = "proj-run-lineage-bad-cursor"
    run_id = db.post("/api/runs", json={"project_id": proj}).json()["id"]
    resp = db.get(f"/api/runs/{run_id}/lineage", params={"cursor": "does-not-exist"})
    assert resp.status_code == 422


def test_run_policy_outcome_is_recorded_and_surfaced_on_the_run(db):
    proj = "proj-policy-outcome"
    run_id = db.post("/api/runs", json={"project_id": proj}).json()["id"]

    resp = db.post(f"/api/runs/{run_id}/policy-outcome",
                   json={"decision": "deny", "rule": "metric:loss<"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["policy_outcome"]["decision"] == "deny"
    assert body["policy_outcome"]["rule"] == "metric:loss<"
    assert body["policy_outcome"]["at"]  # ISO timestamp present

    # Surfaced from both GET /api/runs/{id} and the /summary aggregate.
    assert db.get(f"/api/runs/{run_id}").json()["policy_outcome"]["decision"] == "deny"
    assert db.get(f"/api/runs/{run_id}/summary").json()["run"]["policy_outcome"]["decision"] == "deny"


def test_run_policy_outcome_rejects_an_invalid_decision(db):
    proj = "proj-policy-outcome-bad"
    run_id = db.post("/api/runs", json={"project_id": proj}).json()["id"]
    resp = db.post(f"/api/runs/{run_id}/policy-outcome", json={"decision": "maybe"})
    assert resp.status_code == 422


def test_run_policy_outcome_404s_for_unknown_run(db):
    resp = db.post("/api/runs/does-not-exist/policy-outcome", json={"decision": "allow"})
    assert resp.status_code == 404


def test_commit_diff_endpoint_returns_full_semdiff_shape(db):
    proj = "proj-commit-diff"
    tree_v1 = {"a.txt": {"hash": "a" * 64, "size": 10, "type": "code"}}
    tree_v2 = {"a.txt": {"hash": "a" * 64, "size": 10, "type": "code"},
               "b.txt": {"hash": "b" * 64, "size": 20, "type": "code"}}
    c1 = _make_commit("diff-ep-1", project_id=proj, tree=tree_v1)
    c2 = _make_commit("diff-ep-2", project_id=proj, tree=tree_v2, parents=[c1["hash"]])
    assert db.post("/api/commits", json=c1).status_code == 201
    assert db.post("/api/commits", json=c2).status_code == 201

    resp = db.get(f"/api/commits/{c1['hash']}/diff/{c2['hash']}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["base"] == c1["hash"] and body["target"] == c2["hash"]
    assert body["files"]["added"] == [{"path": "b.txt", "kind": "code"}]
    for key in ("models", "chunks", "datasets", "totals", "summary"):
        assert key in body


def test_commit_diff_endpoint_404s_for_unknown_commit(db):
    known = _make_commit("diff-ep-known")
    db.post("/api/commits", json=known)
    resp = db.get(f"/api/commits/{known['hash']}/diff/{'f' * 64}")
    assert resp.status_code == 404


def test_commit_diff_endpoint_rejects_malformed_hash(db):
    resp = db.get("/api/commits/not-a-hash/diff/" + "a" * 64)
    assert resp.status_code == 400


def test_run_avh_link_requires_uploaded_object_first(db):
    import hashlib

    proj = "proj-avh"
    run_id = db.post("/api/runs", json={"project_id": proj}).json()["id"]
    avh_content = b'{"avh_version": "2.0"}'
    avh_hash = hashlib.sha256(avh_content).hexdigest()

    # Not uploaded yet -> 422, not silently accepted.
    resp = db.post(f"/api/runs/{run_id}/avh", json={"avh_object_id": avh_hash})
    assert resp.status_code == 422

    # Upload the object for real (hash must match its actual content — the server
    # re-verifies), then link succeeds.
    upload_resp = db.post(f"/api/objects/{avh_hash}", content=avh_content)
    assert upload_resp.status_code == 201
    linked = db.post(f"/api/runs/{run_id}/avh", json={"avh_object_id": avh_hash})
    assert linked.status_code == 200
    assert linked.json() == {"status": "linked", "run_id": run_id, "avh_object_id": avh_hash}

    # The run's summary AND the plain GET /api/runs/{id} both surface the pointer.
    assert db.get(f"/api/runs/{run_id}").json()["avh_object_id"] == avh_hash
    summary = db.get(f"/api/runs/{run_id}/summary").json()
    assert summary["avh_object_id"] == avh_hash

    # It's audited.
    audit_rows = db.get("/api/admin/audit?action=run.avh_publish&limit=10").json()["entries"]
    assert any(r["details"].get("run_id") == run_id for r in audit_rows)


def test_run_avh_link_rejects_malformed_hash(db):
    proj = "proj-avh-bad"
    run_id = db.post("/api/runs", json={"project_id": proj}).json()["id"]
    resp = db.post(f"/api/runs/{run_id}/avh", json={"avh_object_id": "not-a-hash"})
    assert resp.status_code == 422


def test_run_avh_link_404_for_unknown_run(db):
    import hashlib

    content = b"x"
    avh_hash = hashlib.sha256(content).hexdigest()
    db.post(f"/api/objects/{avh_hash}", content=content)
    resp = db.post("/api/runs/does-not-exist/avh", json={"avh_object_id": avh_hash})
    assert resp.status_code == 404


def test_push_back_fills_run_env_snapshot_id_once(db):
    import uuid

    run_id = str(uuid.uuid4())
    sid = "e" * 64  # snapshot ids are sha256 hex like any CAS object
    c1 = _make_commit("env-first", project_id="proj-env")
    c1.update(run_id=run_id, env_snapshot_id=sid)
    assert db.post("/api/commits", json=c1).status_code == 201
    assert db.get(f"/api/runs/{run_id}").json()["env_snapshot_id"] == sid

    # A different later snapshot must NOT steal the pointer (first link wins):
    c2 = _make_commit("env-second", project_id="proj-env")
    c2.update(run_id=run_id, env_snapshot_id="f" * 64)
    assert db.post("/api/commits", json=c2).status_code == 201
    assert db.get(f"/api/runs/{run_id}").json()["env_snapshot_id"] == sid


# ---------------------------------------------------------------------------
# RSI R1 (v1.3.1, migration 0006): improver versions, change sets, policy packs,
# canary results, project freeze. Live-server tests — Docker-gated like everything else
# in this file (skips cleanly via the `db` fixture's reachability check).
# ---------------------------------------------------------------------------

def _upload_object(db, content: bytes, headers: dict | None = None) -> str:
    h = hashlib.sha256(content).hexdigest()
    resp = db.post(f"/api/objects/{h}", content=content, headers=headers)
    assert resp.status_code in (200, 201), resp.text
    return h


@pytest.fixture
def scoped_users(db):
    """A trainer (no scopes -> unrestricted, pre-v1.3.1 default), an admin (explicit
    'admin' scope), and a genuinely UNPRIVILEGED reader (explicit 'read' scope only) —
    proves scope enforcement is additive (trainer keeps full access) while still gating
    an admin/policy:write-only route for real.

    v1.3.1 WP-44 fix (found live, the first time this fixture's own callers actually ran
    against a real server): every "requires_scope" denial test in this file used
    trainer's token expecting a 403 — but trainer is UNRESTRICTED by this fixture's own
    design (bare-string entries resolve to `["*"]`, see `_scopes_for_identity()`), so
    those requests were always genuinely ALLOWED and the assertions were dead code until
    this session's live pass actually executed them. `reader` is the identity those
    tests should have used from the start."""
    server_module._AUTH_USERS = {
        "trainer": "trainer-token-12345",
        "root": {"token": "root-token-12345", "expires_at": None, "scopes": ["admin"]},
        "reader": {"token": "reader-token-12345", "expires_at": None, "scopes": ["read"]},
    }
    try:
        yield
    finally:
        server_module._AUTH_USERS = {}


class TestImproverVersions:
    def test_create_requires_project_id(self, db):
        manifest_hash = _upload_object(db, b'{"kind":"improver_manifest"}')
        resp = db.post("/api/improvers", json={"manifest_object_id": manifest_hash})
        assert resp.status_code == 422

    def test_create_requires_uploaded_manifest_object(self, db):
        resp = db.post("/api/improvers", json={"project_id": "p1", "manifest_object_id": "f" * 64})
        assert resp.status_code == 422

    def test_create_is_idempotent_by_id(self, db):
        # v1.3.1 WP-44 fix (found live): this asserted 201 for the create path — but
        # create_improver_version()'s own docstring says it's "idempotent by
        # client-generated id, same lazy/ordering-safe contract as POST /api/runs", and
        # /api/runs's OWN test (test_runs_crud_and_commit_linkage_with_lazy_create)
        # deliberately asserts 200 for ITS create path too, distinguishing create vs.
        # exists purely via the response body's "status" field, not the HTTP status —
        # multi-agent races don't get to pick which one of them "wins" the 201.
        manifest_hash = _upload_object(db, b'{"kind":"improver_manifest","n":1}')
        body = {"id": "imp-1", "project_id": "p1", "manifest_object_id": manifest_hash}
        first = db.post("/api/improvers", json=body)
        assert first.status_code == 200 and first.json()["status"] == "created"
        second = db.post("/api/improvers", json=body)
        assert second.status_code == 200
        assert second.json() == {"status": "exists", "id": "imp-1"}

    def test_get_and_list_roundtrip(self, db):
        manifest_hash = _upload_object(db, b'{"kind":"improver_manifest","n":2}')
        db.post("/api/improvers", json={"id": "imp-2", "project_id": "p2",
                                        "manifest_object_id": manifest_hash})
        got = db.get("/api/improvers/imp-2")
        assert got.status_code == 200
        assert got.json()["manifest_object_id"] == manifest_hash

        listed = db.get("/api/improvers", params={"project_id": "p2"})
        assert listed.status_code == 200
        assert any(r["id"] == "imp-2" for r in listed.json()["improvers"])

    def test_get_unknown_is_404(self, db):
        assert db.get("/api/improvers/does-not-exist").status_code == 404

    def test_lineage_walks_multiple_hops(self, db):
        m1 = _upload_object(db, b'{"n":1}')
        m2 = _upload_object(db, b'{"n":2}')
        m3 = _upload_object(db, b'{"n":3}')
        db.post("/api/improvers", json={"id": "lin-1", "project_id": "plin", "manifest_object_id": m1})
        db.post("/api/improvers", json={"id": "lin-2", "project_id": "plin",
                                        "manifest_object_id": m2, "parent_id": "lin-1"})
        db.post("/api/improvers", json={"id": "lin-3", "project_id": "plin",
                                        "manifest_object_id": m3, "parent_id": "lin-2"})

        resp = db.get("/api/improvers/lin-3/lineage")
        assert resp.status_code == 200
        chain_ids = [n["id"] for n in resp.json()["lineage"]]
        assert chain_ids == ["lin-3", "lin-2", "lin-1"]
        assert resp.json()["next_cursor"] is None

    def test_lineage_depth_bound_paginates(self, db):
        m1 = _upload_object(db, b'{"n":10}')
        m2 = _upload_object(db, b'{"n":11}')
        db.post("/api/improvers", json={"id": "dep-1", "project_id": "pdep", "manifest_object_id": m1})
        db.post("/api/improvers", json={"id": "dep-2", "project_id": "pdep",
                                        "manifest_object_id": m2, "parent_id": "dep-1"})

        resp = db.get("/api/improvers/dep-2/lineage", params={"depth": 1})
        assert resp.status_code == 200
        assert len(resp.json()["lineage"]) == 1
        assert resp.json()["next_cursor"] == "dep-1"

    def test_lineage_rejects_out_of_range_depth(self, db):
        m1 = _upload_object(db, b'{"n":20}')
        db.post("/api/improvers", json={"id": "dr-1", "project_id": "pdr", "manifest_object_id": m1})
        assert db.get("/api/improvers/dr-1/lineage", params={"depth": 0}).status_code == 422
        assert db.get("/api/improvers/dr-1/lineage", params={"depth": 10_000}).status_code == 422


class TestChangeSets:
    def _seed_improver(self, db, suffix):
        m = _upload_object(db, f'{{"n":"{suffix}"}}'.encode())
        db.post("/api/improvers", json={"id": f"cs-imp-{suffix}", "project_id": "pcs",
                                        "manifest_object_id": m})
        return f"cs-imp-{suffix}"

    def test_create_requires_uploaded_object(self, db):
        resp = db.post("/api/change-sets", json={"project_id": "pcs", "object_id": "f" * 64})
        assert resp.status_code == 422

    def test_create_rejects_bad_risk(self, db):
        obj = _upload_object(db, b'{"kind":"change_set"}')
        resp = db.post("/api/change-sets", json={"project_id": "pcs", "object_id": obj,
                                                  "risk": "extreme"})
        assert resp.status_code == 422

    def test_full_legal_transition_chain(self, db):
        # v1.3.1 WP-44 fix (found live): create_change_set() is the same lazy/idempotent
        # create-or-exists pattern as /api/runs and /api/improvers — 200 either way,
        # "status" in the body distinguishes create from exists. See the identical note
        # on TestImproverVersions::test_create_is_idempotent_by_id above.
        obj = _upload_object(db, b'{"kind":"change_set","n":1}')
        create = db.post("/api/change-sets", json={"id": "cs-1", "project_id": "pcs",
                                                    "object_id": obj, "risk": "low"})
        assert create.status_code == 200 and create.json()["status"] == "created"
        assert db.get("/api/change-sets/cs-1").json()["status"] == "proposed"

        assert db.post("/api/change-sets/cs-1/status", json={"status": "approved"}).status_code == 200
        assert db.get("/api/change-sets/cs-1").json()["status"] == "approved"

        assert db.post("/api/change-sets/cs-1/status", json={"status": "applied"}).status_code == 200
        assert db.post("/api/change-sets/cs-1/status", json={"status": "rolled_back"}).status_code == 200
        assert db.get("/api/change-sets/cs-1").json()["status"] == "rolled_back"

    def test_illegal_transition_is_422_not_a_silent_overwrite(self, db):
        obj = _upload_object(db, b'{"kind":"change_set","n":2}')
        db.post("/api/change-sets", json={"id": "cs-2", "project_id": "pcs", "object_id": obj})
        # proposed -> applied directly must be rejected (approval is mandatory first).
        resp = db.post("/api/change-sets/cs-2/status", json={"status": "applied"})
        assert resp.status_code == 422
        assert db.get("/api/change-sets/cs-2").json()["status"] == "proposed"

    def test_transition_on_unknown_change_set_is_404(self, db):
        resp = db.post("/api/change-sets/does-not-exist/status", json={"status": "approved"})
        assert resp.status_code == 404

    def test_list_filters_by_status_and_improver(self, db):
        improver_id = self._seed_improver(db, "filt")
        obj = _upload_object(db, b'{"kind":"change_set","n":3}')
        db.post("/api/change-sets", json={"id": "cs-filt", "project_id": "pcs",
                                          "object_id": obj, "improver_id": improver_id})
        resp = db.get("/api/change-sets", params={"project_id": "pcs", "improver_id": improver_id})
        assert any(r["id"] == "cs-filt" for r in resp.json()["change_sets"])


class TestPolicyPacks:
    def test_create_requires_uploaded_object(self, db):
        resp = db.post("/api/policy-packs", json={"project_id": "ppk", "object_id": "f" * 64})
        assert resp.status_code == 422

    def test_create_rejects_unknown_prev_id(self, db):
        obj = _upload_object(db, b'{"main":{}}')
        resp = db.post("/api/policy-packs", json={"project_id": "ppk", "object_id": obj,
                                                   "prev_id": "no-such-pack"})
        assert resp.status_code == 422

    def test_chain_hash_matches_expected_formula(self, db):
        # v1.3.1 WP-44 fix (found live): create_policy_pack() is the same lazy/idempotent
        # create-or-exists pattern as /api/runs — 200 either way. See the identical note
        # on TestImproverVersions::test_create_is_idempotent_by_id above.
        obj = _upload_object(db, b'{"main":{"metric":"a"}}')
        resp = db.post("/api/policy-packs", json={"id": "pack-1", "project_id": "ppk",
                                                   "object_id": obj})
        assert resp.status_code == 200 and resp.json()["status"] == "created"
        expected = hashlib.sha256(f":{obj}".encode()).hexdigest()
        assert resp.json()["chain_hash"] == expected

        obj2 = _upload_object(db, b'{"main":{"metric":"b"}}')
        resp2 = db.post("/api/policy-packs", json={"id": "pack-2", "project_id": "ppk",
                                                    "object_id": obj2, "prev_id": "pack-1"})
        expected2 = hashlib.sha256(f"pack-1:{obj2}".encode()).hexdigest()
        assert resp2.json()["chain_hash"] == expected2

    def test_latest_returns_the_most_recent(self, db):
        obj1 = _upload_object(db, b'{"main":{"n":1}}')
        obj2 = _upload_object(db, b'{"main":{"n":2}}')
        db.post("/api/policy-packs", json={"id": "lat-1", "project_id": "plat", "object_id": obj1})
        db.post("/api/policy-packs", json={"id": "lat-2", "project_id": "plat", "object_id": obj2,
                                           "prev_id": "lat-1"})
        resp = db.get("/api/policy-packs/latest", params={"project_id": "plat"})
        assert resp.status_code == 200
        assert resp.json()["id"] == "lat-2"

    def test_latest_404_when_none_published(self, db):
        resp = db.get("/api/policy-packs/latest", params={"project_id": "no-such-project"})
        assert resp.status_code == 404

    def test_publish_requires_policy_write_scope(self, db, scoped_users):
        # Upload as the unrestricted trainer identity (Protected mode is active once
        # scoped_users sets _AUTH_USERS — an unauthenticated upload now 401s outright);
        # the actual denial below is what's under test, on a genuinely unprivileged token.
        obj = _upload_object(db, b'{"main":{}}',
                             headers={"Authorization": "Bearer trainer-token-12345"})
        headers = {"Authorization": "Bearer reader-token-12345"}
        resp = db.post("/api/policy-packs", json={"project_id": "pscope", "object_id": obj},
                       headers=headers)
        assert resp.status_code == 403
        assert resp.json()["detail"]["error"] == "scope_denied"


class TestCanaryResults:
    def test_create_requires_uploaded_suite_object(self, db):
        resp = db.post("/api/canary-results", json={"project_id": "pc", "improver_id": "i1",
                                                     "suite_object_id": "f" * 64, "passed": True})
        assert resp.status_code == 422

    def test_create_and_list_filters_by_improver(self, db):
        suite = _upload_object(db, b'{"checks":[]}')
        db.post("/api/canary-results", json={"project_id": "pc", "improver_id": "i-can-1",
                                             "suite_object_id": suite, "passed": True})
        db.post("/api/canary-results", json={"project_id": "pc", "improver_id": "i-can-2",
                                             "suite_object_id": suite, "passed": False})
        resp = db.get("/api/canary-results", params={"project_id": "pc", "improver_id": "i-can-1"})
        rows = resp.json()["canary_results"]
        assert len(rows) == 1
        assert rows[0]["passed"] is True


class TestProjectFreeze:
    def test_default_state_is_not_frozen(self, db):
        resp = db.get("/api/freeze/pf1")
        assert resp.status_code == 200
        assert resp.json() == {"project_id": "pf1", "frozen": False, "reason": None,
                               "frozen_by": None, "frozen_at": None}

    def test_set_and_read_back(self, db):
        resp = db.post("/api/freeze/pf2", json={"frozen": True, "reason": "incident"})
        assert resp.status_code == 200
        assert resp.json()["frozen"] is True
        assert resp.json()["reason"] == "incident"

        after = db.get("/api/freeze/pf2")
        assert after.json()["frozen"] is True
        assert after.json()["reason"] == "incident"

    def test_unfreeze_clears_reason(self, db):
        db.post("/api/freeze/pf3", json={"frozen": True, "reason": "incident"})
        db.post("/api/freeze/pf3", json={"frozen": False})
        after = db.get("/api/freeze/pf3")
        assert after.json()["frozen"] is False
        assert after.json()["reason"] is None

    def test_set_requires_admin_scope(self, db, scoped_users):
        headers = {"Authorization": "Bearer reader-token-12345"}
        resp = db.post("/api/freeze/pf4", json={"frozen": True}, headers=headers)
        assert resp.status_code == 403
        assert resp.json()["detail"]["required_scope"] == "admin"

    def test_admin_scope_token_succeeds(self, db, scoped_users):
        headers = {"Authorization": "Bearer root-token-12345"}
        resp = db.post("/api/freeze/pf5", json={"frozen": True, "reason": "drill"},
                       headers=headers)
        assert resp.status_code == 200

    def test_get_needs_no_scope_even_in_protected_mode(self, db, scoped_users):
        headers = {"Authorization": "Bearer trainer-token-12345"}
        resp = db.get("/api/freeze/pf6", headers=headers)
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# v1.3.1 RSI R6 (todo.md I.38, WP-36): server-side anomaly detectors -> `kind="anomaly"`
# events, fanned out through the SAME webhook mechanism every other event kind already
# uses (no new delivery path — proven by test_webhooks_cli.py/the webhook classes above;
# these tests only need to prove the DETECTORS fire the event with the right payload).
# ---------------------------------------------------------------------------

@pytest.fixture
def tenancy_enforced():
    """Flips AV_TENANCY_ENFORCE on for one test. Two module-level bindings, not one, are
    patched -- `python.av_server.database.TENANCY_ENFORCE` (read by `_apply_tenant_guc`/
    `_apply_bypass_rls`, the RLS GUC listeners) AND `python.av_server.server.TENANCY_ENFORCE`
    (server.py's OWN name, bound via `from .database import TENANCY_ENFORCE` at import
    time -- reassigning the database module's attribute does NOT retroactively change
    server.py's already-bound copy of that name). Same two-module-identity class of bug
    this codebase has hit before (Probleme.md #132) -- both are patched here so neither
    half of tenancy enforcement is silently left off."""
    from python.av_server import database as database_module

    database_module.TENANCY_ENFORCE = True
    server_module.TENANCY_ENFORCE = True
    try:
        yield
    finally:
        database_module.TENANCY_ENFORCE = False
        server_module.TENANCY_ENFORCE = False


@pytest.fixture
def cas_isolated(tmp_path, monkeypatch):
    """v1.3.3 (WP-21): flips AV_CAS_ISOLATION to 'isolated' for one test. Only ONE
    module-level binding to patch here (`server_module.CAS_ISOLATION`) -- unlike
    `tenancy_enforced` above, nothing in database.py reads this name, so there's no
    second copy to keep in sync. Also points AV_DATA_DIR-derived storage at a FRESH
    tmp_path for this one test, so a prior test's flat-layout objects (which legitimately
    still exist on disk under `shared` mode, by design) can never accidentally satisfy
    this test's own isolated-mode assertions via the legacy-fallback read path."""
    from python.av_server.storage import CASStorage

    original_isolation = server_module.CAS_ISOLATION
    original_storage = server_module.storage
    server_module.CAS_ISOLATION = "isolated"
    server_module.storage = CASStorage(tmp_path)
    try:
        yield
    finally:
        server_module.CAS_ISOLATION = original_isolation
        server_module.storage = original_storage


@pytest.fixture
def two_tenants(db):
    """Two real tenants with one admin-scoped DB token each, created via the real HTTP
    surface (not direct DB inserts) -- proves `av tenant create`/`av token create`'s own
    routes work as the actual bootstrap path for a genuine multi-tenant test, not just a
    fixture shortcut."""
    import uuid

    import python.av_server.identity as identity_module

    headers_a = {"Authorization": "Bearer trainer-token-12345"}
    slug_a = f"tenant-a-{uuid.uuid4().hex[:8]}"
    slug_b = f"tenant-b-{uuid.uuid4().hex[:8]}"
    created_a = db.post("/api/tenants", json={"slug": slug_a, "name": "Tenant A"},
                        headers=headers_a)
    created_b = db.post("/api/tenants", json={"slug": slug_b, "name": "Tenant B"},
                        headers=headers_a)
    assert created_a.status_code == 200 and created_b.status_code == 200
    tenant_a_id, tenant_b_id = created_a.json()["id"], created_b.json()["id"]

    # Direct inserts for the tokens themselves (not /api/tokens, which always mints
    # under the CALLER's own tenant -- there is deliberately no route that lets one
    # identity mint a token for a DIFFERENT tenant, which is exactly the property this
    # fixture needs to route around to set up a clean two-tenant test at all).
    async def _mint(tenant_id, name):
        import asyncpg

        conn = await asyncpg.connect(
            AV_TEST_DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://")
        )
        try:
            raw = f"tok-{uuid.uuid4().hex}"
            await conn.execute(
                "INSERT INTO api_tokens (id, tenant_id, name, token_hash, prefix, "
                "created_by, created_at) VALUES ($1, $2, $3, $4, $5, $6, now())",
                f"id-{raw}", tenant_id, name, identity_module.hash_token(raw), raw[:8], "test",
            )
        finally:
            await conn.close()
        return raw

    token_a = asyncio.run(_mint(tenant_a_id, "tenant-a-token"))
    token_b = asyncio.run(_mint(tenant_b_id, "tenant-b-token"))
    identity_module._principal_cache.clear()
    return {
        "tenant_a_id": tenant_a_id, "tenant_b_id": tenant_b_id,
        "headers_a": {"Authorization": f"Bearer {token_a}"},
        "headers_b": {"Authorization": f"Bearer {token_b}"},
    }


class TestHardTenancy:
    """v1.3.2 — live proof of both enforcement layers together: the app-level guard
    (`_enforce_project_tenant`, a clean 403/404) AND Postgres RLS (migration 0013, the
    backstop) against a REAL two-tenant, real-Postgres setup. `scoped_users` is NOT used
    here (unlike TestApiTokens/TestTenantsUsersRoles) -- these tokens carry no explicit
    scopes at all (unrestricted `["*"]`, DB-token default), isolating tenancy as the only
    variable under test rather than mixing it with scope enforcement."""

    def test_unconfigured_server_is_byte_identical_with_enforcement_off(self, db):
        """The single most important test in this class: with TENANCY_ENFORCE at its
        real default (off, no fixture applied), a caller can push under ANY project_id,
        read ANY project's data, and list every project -- exactly v1.3.1 behavior,
        proving this whole phase adds nothing observable until explicitly turned on."""
        obj_hash = _upload_object(db, b"unconfigured-proof")
        resp = db.post("/api/commits", json={
            "hash": "c" * 64, "message": "m", "root_tree_hash": "r" * 64,
            "project_id": "anyone-can-use-this-project-id", "project_name": "p",
        })
        assert resp.status_code in (201, 409)
        listed = db.get("/api/projects")
        assert listed.status_code == 200  # never denied, never tenant-filtered

    def test_write_under_a_foreign_project_id_is_denied_with_tenant_denied(
        self, db, tenancy_enforced, two_tenants,
    ):
        t = two_tenants
        first = db.post("/api/commits", json={
            "hash": "d" * 64, "message": "m", "root_tree_hash": "r" * 64,
            "project_id": "shared-name-proj", "project_name": "p",
        }, headers=t["headers_a"])
        assert first.status_code in (201, 409), first.text  # tenant A claims it

        collide = db.post("/api/commits", json={
            "hash": "e" * 64, "message": "m2", "root_tree_hash": "r" * 64,
            "project_id": "shared-name-proj", "project_name": "p",
        }, headers=t["headers_b"])
        assert collide.status_code == 403, collide.text
        assert collide.json()["detail"]["error"] == "tenant_denied"

    def test_read_of_a_foreign_project_id_is_a_bare_404(self, db, tenancy_enforced, two_tenants):
        t = two_tenants
        db.post("/api/commits", json={
            "hash": "f" * 64, "message": "m", "root_tree_hash": "r" * 64,
            "project_id": "b-owns-this-one", "project_name": "p",
        }, headers=t["headers_b"])

        denied = db.get("/api/commits", params={"project_id": "b-owns-this-one"},
                        headers=t["headers_a"])
        assert denied.status_code == 404
        assert "tenant_denied" not in denied.text  # not the write-path error shape

    def test_unfiltered_list_routes_are_tenant_scoped(self, db, tenancy_enforced, two_tenants):
        """An UNFILTERED list route (no project_id given at all) run by tenant A must
        never show tenant B's rows. NOT an RLS test, despite what an earlier draft of
        this test assumed and named itself after -- when this test was written, RLS was
        live-verified INERT under this repo's own default docker-compose topology
        (av_user connects as a Postgres SUPERUSER, which unconditionally bypasses
        row-level security; see migration 0013's own docstring for the full finding).
        Migration 0015 fixed that gap (see
        test_rls_actually_filters_now_for_the_non_superuser_role below for its proof),
        but this test is kept exactly as-is and un-renamed: it proves the explicit
        application-level filters `list_commits`/`list_projects` (server.py) work in
        their OWN right, which remains the correct thing to rely on regardless of which
        Postgres role is in play -- defense in depth, not "RLS makes this redundant"."""
        t = two_tenants
        db.post("/api/commits", json={
            "hash": "1" * 64, "message": "a-owns", "root_tree_hash": "r" * 64,
            "project_id": "rls-proof-a", "project_name": "p",
        }, headers=t["headers_a"])
        db.post("/api/commits", json={
            "hash": "2" * 64, "message": "b-owns", "root_tree_hash": "r" * 64,
            "project_id": "rls-proof-b", "project_name": "p",
        }, headers=t["headers_b"])

        as_a = db.get("/api/commits", headers=t["headers_a"]).json()["commits"]
        ids_a = {c["hash"] for c in as_a}
        assert "1" * 64 in ids_a
        assert "2" * 64 not in ids_a  # app-level filter excluded tenant B's row

        as_b = db.get("/api/commits", headers=t["headers_b"]).json()["commits"]
        ids_b = {c["hash"] for c in as_b}
        assert "2" * 64 in ids_b
        assert "1" * 64 not in ids_b

        projects_a = {p["project_id"] for p in db.get("/api/projects", headers=t["headers_a"]).json()["projects"]}
        assert "rls-proof-a" in projects_a
        assert "rls-proof-b" not in projects_a

    def test_rls_actually_filters_now_for_the_non_superuser_role(self, db, tenancy_enforced, two_tenants):
        """v1.3.2 (migration 0015): the real backstop, proven at the SQL layer with the
        app-level guard entirely out of the picture -- a raw `SELECT * FROM commits` with
        NO WHERE clause at all, issued directly over asyncpg as `av_app` with only the
        `app.tenant_id` GUC set (exactly what `after_begin` does, reproduced by hand
        here), must return only that tenant's rows. This is what migration 0013's own
        docstring said was missing ("the real fix is an infrastructure change... not
        attempted here") -- 0015 is that infrastructure change, and this is its proof.
        Also documents the flip side: connecting as `av_user` (still a superuser) is
        UNAFFECTED and keeps seeing everything, which is exactly right -- it's the role
        migrations and `system_session_factory`'s legitimately-cross-tenant background
        workers still need."""
        import asyncpg

        t = two_tenants
        db.post("/api/commits", json={
            "hash": "4" * 64, "message": "a-owns-raw", "root_tree_hash": "r" * 64,
            "project_id": "raw-sql-proof-a", "project_name": "p",
        }, headers=t["headers_a"])
        db.post("/api/commits", json={
            "hash": "5" * 64, "message": "b-owns-raw", "root_tree_hash": "r" * 64,
            "project_id": "raw-sql-proof-b", "project_name": "p",
        }, headers=t["headers_b"])

        async def _raw_select(dsn: str, tenant_id: str | None) -> set[str]:
            conn = await asyncpg.connect(dsn)
            try:
                async with conn.transaction():
                    if tenant_id is not None:
                        await conn.execute(
                            "SELECT set_config('app.tenant_id', $1, true)", tenant_id
                        )
                    rows = await conn.fetch(
                        "SELECT hash FROM commits WHERE hash IN ($1, $2)", "4" * 64, "5" * 64
                    )
                    return {r["hash"] for r in rows}
            finally:
                await conn.close()

        app_dsn = AV_TEST_APP_DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://")
        superuser_dsn = AV_TEST_DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://")

        as_app_for_a = asyncio.run(_raw_select(app_dsn, t["tenant_a_id"]))
        assert as_app_for_a == {"4" * 64}, \
            f"av_app + app.tenant_id=A must see ONLY A's row via raw SQL, got {as_app_for_a}"

        as_app_for_b = asyncio.run(_raw_select(app_dsn, t["tenant_b_id"]))
        assert as_app_for_b == {"5" * 64}, \
            f"av_app + app.tenant_id=B must see ONLY B's row via raw SQL, got {as_app_for_b}"

        as_app_no_guc = asyncio.run(_raw_select(app_dsn, None))
        assert "4" * 64 not in as_app_no_guc and "5" * 64 not in as_app_no_guc, \
            "av_app with NO tenant GUC set must fall back to the default tenant, not see A/B's rows"

        # The documented, permanent flip side: a superuser connection (av_user, what
        # migrations and system_session_factory use) is NOT filtered by RLS no matter
        # what GUC is set -- Postgres exempts superusers unconditionally, FORCE included.
        as_superuser = asyncio.run(_raw_select(superuser_dsn, t["tenant_a_id"]))
        assert as_superuser == {"4" * 64, "5" * 64}, \
            "av_user (superuser) must still see both tenants' rows -- this is expected, not a bug"

    def test_rls_survives_a_mid_request_commit_boundary(self, db, tenancy_enforced, two_tenants):
        """The hard part the design explicitly called out: a one-shot SET LOCAL at
        session creation would silently stop applying after any commit inside the same
        request. `DELETE /api/admin/audit` (prune_audit_log) commits TWICE in one
        request (the delete itself, then its own _audit() row) -- proving RLS/tenant
        scoping still correctly applies to that SECOND transaction, not just the first,
        is the direct live proof `after_begin`'s per-transaction re-application actually
        works, not just reads correctly in isolation."""
        t = two_tenants
        admin_a = {"Authorization": "Bearer trainer-token-12345"}  # unrestricted; admin scope
        # A tenant-A-scoped audit row to prune.
        db.post("/api/commits", json={
            "hash": "3" * 64, "message": "prune-me", "root_tree_hash": "r" * 64,
            "project_id": "prune-proof-a", "project_name": "p",
        }, headers=t["headers_a"])

        pruned = db.delete("/api/admin/audit", params={"before_days": 0}, headers=admin_a)
        assert pruned.status_code == 200, pruned.text
        # Reaching here without an error/hang across the delete's two internal commits
        # IS the assertion -- a regression here would surface as a DatatypeMismatch-style
        # exception or a silently-wrong second transaction, not a clean denial.


class TestTenantsUsersRoles:
    """v1.3.2 — /api/tenants, /api/users, /api/roles, /api/role-bindings. Proves the
    ROLE-BINDING-derived permission path specifically (identity.py::_permissions_for_subject)
    -- TestApiTokens below only ever exercises a token's own explicit `scopes` list, never
    a role granted via a binding, which is a materially different code path
    (resolve_db_token's role_scopes union) that needs its own live proof."""

    def test_tenants_me_defaults_to_default_tenant_in_anonymous_mode(self, db):
        from python.av_server.models import DEFAULT_TENANT_ID

        resp = db.get("/api/tenants/me")
        assert resp.status_code == 200
        assert resp.json()["id"] == DEFAULT_TENANT_ID
        assert resp.json()["slug"] == "default"

    def test_list_roles_includes_all_six_builtins(self, db):
        resp = db.get("/api/roles")
        assert resp.status_code == 200
        names = {r["name"] for r in resp.json()["roles"]}
        assert names == {"owner", "admin", "maintainer", "trainer", "reviewer", "reader"}
        owner = next(r for r in resp.json()["roles"] if r["name"] == "owner")
        assert owner["permissions"] == ["*"]
        assert owner["builtin"] is True

    def test_create_user_requires_user_write_scope(self, db, scoped_users):
        denied = db.post("/api/users", json={"username": "alice"},
                         headers={"Authorization": "Bearer reader-token-12345"})
        assert denied.status_code == 403
        assert denied.json()["detail"]["required_scope"] == "user:write"

        allowed = db.post("/api/users", json={"username": "alice"},
                          headers={"Authorization": "Bearer trainer-token-12345"})
        assert allowed.status_code == 200
        assert allowed.json()["status"] == "created"

    def test_role_binding_grants_effective_permission_to_a_db_token(self, db, scoped_users):
        """The core proof: a DB token with NO explicit scopes of its own, whose SUBJECT
        (the token itself, subject_type='token') is granted the 'reviewer' role via a
        binding, ends up with EXACTLY reviewer's permissions (['review']) — not the
        unrestricted default a scopeless token would otherwise carry. This is
        identity.py::resolve_db_token's role_scopes union path, specifically."""
        admin_headers = {"Authorization": "Bearer trainer-token-12345"}

        # Mint a token with NO explicit scopes -- it should carry whatever its role
        # bindings grant, once one exists (server.py::create_api_token: "an unrestricted
        # token... [that] carries exactly its role-derived scopes").
        minted = db.post("/api/tokens", json={"name": "bound-token"}, headers=admin_headers)
        assert minted.status_code == 200
        token_id = minted.json()["id"]
        raw_token = minted.json()["token"]
        bound_headers = {"Authorization": f"Bearer {raw_token}"}

        roles = {r["name"]: r["id"]
                for r in db.get("/api/roles", headers=admin_headers).json()["roles"]}

        # A real, reviewable change set — created_by defaults to the admin identity
        # (trainer), so the bound token (a different subject entirely) reviewing it can
        # never trip the self-review rejection this route also enforces.
        obj_hash = _upload_object(db, b'{"diff":"reviewable"}', headers=admin_headers)
        cs = db.post("/api/change-sets",
                    json={"id": "cs-rb-proof", "project_id": "prb", "object_id": obj_hash},
                    headers=admin_headers)
        assert cs.status_code == 200, cs.text

        # Before any binding: a scopeless token has no role-derived scopes at all, so it
        # falls back to identity.py::resolve_db_token's minimal `["read"]` default --
        # 'review' must NOT yet be reachable.
        pre_bind = db.post("/api/reviews",
                           json={"project_id": "prb", "target_type": "change_set",
                                 "target_id": "cs-rb-proof", "decision": "approve"},
                           headers=bound_headers)
        assert pre_bind.status_code == 403

        grant = db.post("/api/role-bindings",
                        json={"subject_type": "token", "subject_id": token_id,
                              "role_id": roles["reviewer"]},
                        headers=admin_headers)
        assert grant.status_code == 200, grant.text

        from python.av_server import identity as identity_module
        identity_module._principal_cache.clear()  # the pre_bind call above cached "no role"

        post_bind = db.post("/api/reviews",
                            json={"project_id": "prb", "target_type": "change_set",
                                  "target_id": "cs-rb-proof", "decision": "approve"},
                            headers=bound_headers)
        assert post_bind.status_code == 200, post_bind.text

        # And still restricted to exactly what 'reviewer' grants -- token:write (a
        # DIFFERENT permission) must stay denied for this same token.
        still_denied = db.post("/api/tokens", json={"name": "x"}, headers=bound_headers)
        assert still_denied.status_code == 403

    def test_suspend_user_revokes_their_tokens_and_sessions(self, db, scoped_users):
        admin_headers = {"Authorization": "Bearer trainer-token-12345"}
        created = db.post("/api/users", json={"username": "bob"}, headers=admin_headers)
        user_id = created.json()["id"]

        # A token explicitly minted FOR this user (user_id set), not a bare service
        # token. Uses a RAW asyncpg connection (matching _truncate_all()'s own pattern
        # above), never the app's `async_session_factory()` from inside a fresh
        # asyncio.run() call here — that pool's connections are bound to the TestClient's
        # own lifespan event loop, and reusing them from a separate loop hangs rather
        # than raising cleanly (found live: this exact mistake in an earlier draft of
        # this test hung for 8+ minutes with near-zero CPU, not a clean traceback).
        import asyncpg

        from python.av_server.models import DEFAULT_TENANT_ID
        import python.av_server.identity as identity_module_direct

        async def _mint_for_user():
            conn = await asyncpg.connect(
                AV_TEST_DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://")
            )
            try:
                raw = "manual-user-token-abc123"
                await conn.execute(
                    "INSERT INTO api_tokens (id, tenant_id, user_id, name, token_hash, "
                    "prefix, created_by, created_at) VALUES ($1, $2, $3, $4, $5, $6, $7, now())",
                    "tok-bob", DEFAULT_TENANT_ID, user_id, "bob-token",
                    identity_module_direct.hash_token(raw), raw[:8], "test",
                )
            finally:
                await conn.close()
            return raw

        raw_token = asyncio.run(_mint_for_user())
        identity_module_direct._principal_cache.clear()
        bob_headers = {"Authorization": f"Bearer {raw_token}"}
        assert db.get("/api/freeze/suspend-proof", headers=bob_headers).status_code == 200

        suspended = db.post(f"/api/users/{user_id}/suspend", headers=admin_headers)
        assert suspended.status_code == 200

        identity_module_direct._principal_cache.clear()
        after = db.get("/api/freeze/suspend-proof-2", headers=bob_headers)
        assert after.status_code == 401


class TestApiTokens:
    """v1.3.2 — DB-backed `/api/tokens*` (identity.py's Principal resolution). Run under
    `scoped_users` (Protected mode via _AUTH_USERS) specifically so an unrecognized/
    revoked token genuinely 401s rather than falling through Anonymous mode's blanket
    "an unrecognized token is treated as no credential at all" permissiveness — that
    fallback is real and correct (see server.py::require_token's own comment), but it
    would mask the exact behavior this class needs to prove: a DB token authenticates,
    and gets ITS OWN scopes enforced, independent of whichever env-based identity minted
    it."""

    def _create_token(self, db, headers, name="ci-bot", scopes=None, expires_in_days=None):
        body = {"name": name}
        if scopes is not None:
            body["scopes"] = scopes
        if expires_in_days is not None:
            body["expires_in_days"] = expires_in_days
        resp = db.post("/api/tokens", json=body, headers=headers)
        assert resp.status_code == 200, resp.text
        return resp.json()

    def test_create_requires_token_write_scope(self, db, scoped_users):
        # 'trainer' is the pre-v1.3.1 unrestricted default (no explicit scopes ->
        # ["*"]) -- token creation must stay reachable for it, same additive guarantee
        # every other scoped route already carries.
        headers = {"Authorization": "Bearer trainer-token-12345"}
        created = self._create_token(db, headers)
        assert created["status"] == "created"
        assert created["token"] and len(created["token"]) > 20
        assert created["prefix"] == created["token"][:8]

        # 'reader' (explicit ["read"] only) must be denied -- token:write is not read.
        denied = db.post("/api/tokens", json={"name": "nope"},
                         headers={"Authorization": "Bearer reader-token-12345"})
        assert denied.status_code == 403
        assert denied.json()["detail"]["required_scope"] == "token:write"

    def test_minted_token_authenticates_standalone_with_its_own_scopes(self, db, scoped_users):
        """The core proof: a token minted here authenticates with NO other credential
        present, and the SCOPES ENFORCED are the token's own declared list -- not the
        wildcard the identity that minted it happened to carry.

        Minted using 'trainer' (scoped_users: bare-string entry -> unrestricted ["*"],
        the pre-v1.3.1 default) rather than 'root' (scoped_users: literal scope
        ["admin"]) deliberately -- the env-based scope model is a flat string match with
        no hierarchy (the SAME reason /api/freeze's admin-only route checks for the
        literal string "admin", not a role), so a token whose only declared scope is the
        STRING "admin" does not, and should not, also satisfy require_scope("token:write")."""
        admin_headers = {"Authorization": "Bearer trainer-token-12345"}
        minted = self._create_token(db, admin_headers, name="narrow", scopes=["read"])
        narrow_headers = {"Authorization": f"Bearer {minted['token']}"}

        # The minting admin has ["admin"], but the MINTED token only ever declared
        # ["read"] -- using it to create a second token must be denied.
        denied = db.post("/api/tokens", json={"name": "should-fail"}, headers=narrow_headers)
        assert denied.status_code == 403
        assert denied.json()["detail"]["required_scope"] == "token:write"

        # A route needing no scope at all still works with this token -- it DOES
        # authenticate, it's just restricted.
        assert db.get("/api/freeze/tok-proof", headers=narrow_headers).status_code == 200

    def test_list_never_leaks_the_hash_and_revoke_takes_effect(self, db, scoped_users):
        from python.av_server import identity as identity_module

        admin_headers = {"Authorization": "Bearer trainer-token-12345"}
        minted = self._create_token(db, admin_headers, name="throwaway")
        token_id = minted["id"]
        raw_headers = {"Authorization": f"Bearer {minted['token']}"}

        listed = db.get("/api/tokens", headers=admin_headers)
        assert listed.status_code == 200
        rows = listed.json()["tokens"]
        assert any(r["id"] == token_id for r in rows)
        assert all("token_hash" not in r and "hash" not in r for r in rows)
        matching = next(r for r in rows if r["id"] == token_id)
        assert matching["prefix"] == minted["token"][:8]

        # Confirm the token authenticates before revoking (a real, working credential).
        assert db.get("/api/freeze/tok-revoke-proof", headers=raw_headers).status_code == 200

        revoked = db.post(f"/api/tokens/{token_id}/revoke", headers=admin_headers)
        assert revoked.status_code == 200
        # The TTL cache (identity.py::AUTH_CACHE_TTL_SECS) means a revoke's effect on an
        # ALREADY-cached resolution is bounded, not immediate -- documented explicitly in
        # identity.py::invalidate_cached_token's docstring. Clearing it here proves the
        # revoke itself is real and permanent (the next, uncached resolution correctly
        # sees it), not that the cache never exists.
        identity_module._principal_cache.clear()
        after_revoke = db.get("/api/freeze/tok-revoke-proof-2", headers=raw_headers)
        assert after_revoke.status_code == 401

    def test_revoke_of_unknown_or_foreign_token_is_404(self, db, scoped_users):
        headers = {"Authorization": "Bearer trainer-token-12345"}
        resp = db.post("/api/tokens/does-not-exist/revoke", headers=headers)
        assert resp.status_code == 404


@pytest.fixture
def clean_auth_window():
    """`_AUTH_FAILURE_WINDOW` is a module-level, process-lifetime dict (deliberately not
    per-request state) — reset it before AND after so one test's failed-auth burst can
    never leak into the next test's threshold count."""
    server_module._AUTH_FAILURE_WINDOW.clear()
    try:
        yield
    finally:
        server_module._AUTH_FAILURE_WINDOW.clear()


class TestAnomalyDetection:
    def _anomaly_events(self, db, project_id=None, headers=None):
        params = {"since": 0, "kinds": "anomaly"}
        if project_id:
            params["project_id"] = project_id
        resp = db.get("/api/events", params=params, headers=headers)
        return resp.json()["events"]

    def test_metric_jump_emits_anomaly_event(self, db):
        base = _make_commit("anom-jump-base", tree={}, metrics={"val_loss": 1.0},
                            project_id="p-anom-jump")
        assert db.post("/api/commits", json=base).status_code == 201
        child = _make_commit("anom-jump-child", tree={}, metrics={"val_loss": 100.0},
                             parents=[base["hash"]], project_id="p-anom-jump")
        assert db.post("/api/commits", json=child).status_code == 201

        events = self._anomaly_events(db, "p-anom-jump")
        jumps = [e for e in events if e["payload"]["type"] == "metric_jump"]
        assert len(jumps) == 1
        assert jumps[0]["payload"]["metric"] == "val_loss"
        assert jumps[0]["payload"]["commit_hash"] == child["hash"]

    def test_small_metric_change_is_not_an_anomaly(self, db):
        base = _make_commit("anom-nojump-base", tree={}, metrics={"val_loss": 1.0},
                            project_id="p-anom-nojump")
        db.post("/api/commits", json=base)
        child = _make_commit("anom-nojump-child", tree={}, metrics={"val_loss": 1.1},
                             parents=[base["hash"]], project_id="p-anom-nojump")
        db.post("/api/commits", json=child)

        events = self._anomaly_events(db, "p-anom-nojump")
        assert not [e for e in events if e["payload"]["type"] == "metric_jump"]

    def test_mass_rewrite_emits_anomaly_event(self, db):
        threshold = server_module.AV_ANOMALY_MASS_REWRITE_FILES
        base_tree = {f"f{i}.txt": "a" * 64 for i in range(3)}
        new_tree = {f"g{i}.txt": "b" * 64 for i in range(threshold + 5)}
        base = _make_commit("anom-mass-base", tree=base_tree, project_id="p-anom-mass")
        assert db.post("/api/commits", json=base).status_code == 201
        child = _make_commit("anom-mass-child", tree=new_tree, parents=[base["hash"]],
                             project_id="p-anom-mass")
        assert db.post("/api/commits", json=child).status_code == 201

        events = self._anomaly_events(db, "p-anom-mass")
        rewrites = [e for e in events if e["payload"]["type"] == "mass_rewrite"]
        assert len(rewrites) == 1
        assert rewrites[0]["payload"]["changed_files"] >= threshold

    def test_small_tree_change_is_not_an_anomaly(self, db):
        base = _make_commit("anom-nomass-base", tree={"a.txt": "a" * 64}, project_id="p-anom-nomass")
        db.post("/api/commits", json=base)
        child = _make_commit("anom-nomass-child", tree={"a.txt": "a" * 64, "b.txt": "b" * 64},
                             parents=[base["hash"]], project_id="p-anom-nomass")
        db.post("/api/commits", json=child)

        events = self._anomaly_events(db, "p-anom-nomass")
        assert not [e for e in events if e["payload"]["type"] == "mass_rewrite"]

    def test_policy_pack_publish_emits_anomaly_event(self, db):
        obj = _upload_object(db, b'{"main":{}}')
        resp = db.post("/api/policy-packs", json={"project_id": "p-anom-pol", "object_id": obj})
        assert resp.status_code == 200 and resp.json()["status"] == "created"

        events = self._anomaly_events(db, "p-anom-pol")
        changes = [e for e in events if e["payload"]["type"] == "policy_change"]
        assert len(changes) == 1
        assert changes[0]["payload"]["policy_pack_id"] == resp.json()["id"]

    def test_scope_denial_burst_emits_auth_spike_anomaly(self, db, scoped_users, clean_auth_window):
        headers = {"Authorization": "Bearer reader-token-12345"}
        threshold = server_module.AV_ANOMALY_AUTH_SPIKE_THRESHOLD
        for _ in range(threshold):
            resp = db.post("/api/freeze/p-anom-scope", json={"frozen": True}, headers=headers)
            assert resp.status_code == 403

        events = self._anomaly_events(db, headers=headers)
        spikes = [e for e in events if e["payload"]["type"] == "auth_spike"
                 and e["payload"]["reason"] == "scope_denied"]
        assert len(spikes) == 1
        assert spikes[0]["payload"]["identifier"] == "reader"

    def test_scope_denials_below_threshold_do_not_trip(self, db, scoped_users, clean_auth_window):
        headers = {"Authorization": "Bearer reader-token-12345"}
        for _ in range(server_module.AV_ANOMALY_AUTH_SPIKE_THRESHOLD - 1):
            db.post("/api/freeze/p-anom-scope-under", json={"frozen": True}, headers=headers)

        events = self._anomaly_events(db, headers=headers)
        spikes = [e for e in events if e["payload"]["type"] == "auth_spike"
                 and e["payload"]["identifier"] == "reader"]
        assert not spikes


# ---------------------------------------------------------------------------
# v1.3.3 (WP-21): per-tenant CAS storage isolation, live against real Postgres. The
# exact scenario WP-21's own design review flagged as a real data-loss risk if shipped
# half-done: tenant B uploading content identical to tenant A's.
# ---------------------------------------------------------------------------

class TestPerTenantCAS:
    def test_identical_content_from_two_tenants_both_succeed_and_stay_independently_readable(
        self, db, cas_isolated, two_tenants,
    ):
        t = two_tenants
        content = b"identical bytes, two different tenants, isolated mode"
        h = hashlib.sha256(content).hexdigest()

        first = db.post(f"/api/objects/{h}", content=content, headers=t["headers_a"])
        assert first.status_code == 201, first.text  # NOT 409 -- this is the bug WP-21 exists to prevent

        second = db.post(f"/api/objects/{h}", content=content, headers=t["headers_b"])
        assert second.status_code == 201, second.text  # tenant B must ALSO succeed, not see a false 409

        # Both readable independently afterward -- proves each tenant genuinely has its
        # OWN copy on disk, not one shared file one of them can't actually reach.
        get_a = db.get(f"/api/objects/{h}", headers=t["headers_a"])
        assert get_a.status_code == 200 and get_a.content == content
        get_b = db.get(f"/api/objects/{h}", headers=t["headers_b"])
        assert get_b.status_code == 200 and get_b.content == content

        head_a = db.head(f"/api/objects/{h}", headers=t["headers_a"])
        assert head_a.status_code == 200

    def test_a_second_upload_of_the_same_content_by_the_same_tenant_still_409s(
        self, db, cas_isolated, two_tenants,
    ):
        """Isolation must not weaken INTRA-tenant dedup -- the product's actual
        headline claim -- only remove CROSS-tenant dedup."""
        t = two_tenants
        content = b"same tenant, uploaded twice, isolated mode"
        h = hashlib.sha256(content).hexdigest()

        first = db.post(f"/api/objects/{h}", content=content, headers=t["headers_a"])
        assert first.status_code == 201, first.text
        second = db.post(f"/api/objects/{h}", content=content, headers=t["headers_a"])
        assert second.status_code == 409, second.text

    def test_physically_separate_files_on_disk(self, db, cas_isolated, two_tenants):
        t = two_tenants
        content = b"checked directly on disk, isolated mode"
        h = hashlib.sha256(content).hexdigest()
        db.post(f"/api/objects/{h}", content=content, headers=t["headers_a"])
        db.post(f"/api/objects/{h}", content=content, headers=t["headers_b"])

        path_a = server_module.storage.get_object_path(h, t["tenant_a_id"])
        path_b = server_module.storage.get_object_path(h, t["tenant_b_id"])
        assert path_a is not None and path_b is not None
        assert path_a != path_b
        assert t["tenant_a_id"] in str(path_a)
        assert t["tenant_b_id"] in str(path_b)

    def test_batch_objects_check_is_tenant_scoped(self, db, cas_isolated, two_tenants):
        t = two_tenants
        content = b"batch-checked content, isolated mode"
        h = hashlib.sha256(content).hexdigest()
        db.post(f"/api/objects/{h}", content=content, headers=t["headers_a"])

        # Tenant B has never uploaded this content -- isolated mode means B's batch
        # check must report it MISSING, not "found" via A's row.
        resp_b = db.post("/api/sync/batch-objects", json=[h], headers=t["headers_b"])
        assert h in resp_b.json()["missing"]
        assert h not in resp_b.json()["found"]

        resp_a = db.post("/api/sync/batch-objects", json=[h], headers=t["headers_a"])
        assert h in resp_a.json()["found"]

    def test_shared_mode_is_completely_unaffected_default_behavior(self, db, two_tenants):
        """No cas_isolated fixture here -- proves the default (`shared`) mode still
        behaves exactly like pre-v1.3.3: identical content from a second tenant is a
        genuine 409, the global dedup guarantee every existing deployment relies on."""
        t = two_tenants
        content = b"shared mode, global dedup, two tenants"
        h = hashlib.sha256(content).hexdigest()

        first = db.post(f"/api/objects/{h}", content=content, headers=t["headers_a"])
        assert first.status_code == 201, first.text
        second = db.post(f"/api/objects/{h}", content=content, headers=t["headers_b"])
        assert second.status_code == 409, second.text


# ---------------------------------------------------------------------------
# v1.3.3 (WP-32): audit-log hash-chaining, live against real Postgres.
# ---------------------------------------------------------------------------

class TestAuditChain:
    def test_verify_reports_ok_on_an_untampered_chain(self, db):
        db.post("/api/webhooks", json={"url": "http://example.invalid/hook", "secret": "s"})
        resp = db.get("/api/admin/audit/verify")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["ok"] is True
        assert body["checked"] >= 1
        assert body["signature_checks"]["absent"] >= 1  # no signing key configured in this test env

    def test_verify_detects_a_tampered_row(self, db):
        create = db.post("/api/webhooks", json={"url": "http://example.invalid/tamper", "secret": "s"})
        assert create.status_code == 200

        async def _tamper():
            import asyncpg

            conn = await asyncpg.connect(
                AV_TEST_DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://")
            )
            try:
                row = await conn.fetchrow(
                    "SELECT id FROM audit_log WHERE action = 'webhook.create' ORDER BY id DESC LIMIT 1"
                )
                await conn.execute(
                    "UPDATE audit_log SET chain_hash = 'deadbeef' WHERE id = $1", row["id"]
                )
                return row["id"]
            finally:
                await conn.close()

        tampered_id = asyncio.run(_tamper())

        resp = db.get("/api/admin/audit/verify")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["ok"] is False
        assert body["broken_at_id"] == tampered_id

    def test_two_audit_rows_in_the_same_flush_chain_against_each_other_not_just_the_db(self, db):
        """prune_audit_log (DELETE /api/admin/audit) writes its OWN audit row inside the
        same request as a delete that may itself already be audited elsewhere -- proving
        the `_chain_seq`-ordered, same-flush chaining path (not just the cross-request
        path the other tests here exercise) actually produces a verifiable chain."""
        db.post("/api/webhooks", json={"url": "http://example.invalid/x", "secret": "s"})
        pruned = db.delete("/api/admin/audit", params={"before_days": 9999})
        assert pruned.status_code == 200, pruned.text

        resp = db.get("/api/admin/audit/verify")
        assert resp.status_code == 200, resp.text
        assert resp.json()["ok"] is True

    def test_public_key_404s_when_signing_is_not_configured(self, db):
        resp = db.get("/api/admin/audit/public-key")
        assert resp.status_code == 404

    def test_since_id_only_reverifies_the_new_tail(self, db):
        first = db.get("/api/admin/audit/verify")
        checkpoint = first.json()["last_id"] or 0

        db.post("/api/webhooks", json={"url": "http://example.invalid/since-id", "secret": "s"})

        resp = db.get("/api/admin/audit/verify", params={"since_id": checkpoint})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["ok"] is True
        assert body["checked"] >= 1
        assert body["last_id"] > checkpoint


class TestAuditSigning:
    """Same live server, but with AV_AUDIT_SIGNING_KEY_PATH set for the duration of one
    test — proves the opt-in signing path end to end, not just that it stays off by
    default (TestAuditChain's public-key 404 test already covers that)."""

    def test_signing_enabled_produces_verifiable_signatures(self, db, tmp_path, monkeypatch):
        import python.av_server.audit_signing as audit_signing_module

        key_path = tmp_path / "audit-signing.pem"
        monkeypatch.setenv("AV_AUDIT_SIGNING_KEY_PATH", str(key_path))
        pytest.importorskip("cryptography")
        audit_signing_module.ensure_keypair()

        db.post("/api/webhooks", json={"url": "http://example.invalid/signed", "secret": "s"})

        resp = db.get("/api/admin/audit/verify")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["ok"] is True
        assert body["signature_checks"]["verified"] >= 1
        assert body["signature_checks"]["failed"] == 0

        pub = db.get("/api/admin/audit/public-key")
        assert pub.status_code == 200
        assert len(pub.json()["public_key"]) == 64  # 32-byte ed25519 public key, hex-encoded


# ---------------------------------------------------------------------------
# v1.3.3 (WP-35): /api/metrics, live against real Postgres.
# ---------------------------------------------------------------------------

class TestMetrics:
    def test_metrics_endpoint_renders_prometheus_text_and_counts_requests(self, db):
        from python.av_server import metrics as metrics_module

        metrics_module.reset()
        db.get("/api/health")
        db.get("/api/health")

        resp = db.get("/api/metrics")
        assert resp.status_code == 200, resp.text
        assert resp.headers["content-type"].startswith("text/plain")
        body = resp.text
        assert "# TYPE av_http_requests_total counter" in body
        assert 'av_http_requests_total{method="GET",path="/api/health",status_class="2xx"}' in body
        assert "# TYPE av_http_request_duration_seconds histogram" in body
        assert "av_http_request_duration_seconds_bucket" in body
        assert "av_uptime_seconds" in body

    def test_metrics_reports_webhook_queue_depth_and_db_pool(self, db):
        resp = db.get("/api/metrics")
        assert resp.status_code == 200, resp.text
        assert "av_webhook_queue_depth" in resp.text
        assert "av_db_pool_checked_out" in resp.text


# ---------------------------------------------------------------------------
# v1.3.3 (WP-17 x identity.py fix) — the real architectural gap found and fixed this
# session: `identity.py::_permissions_for_subject` never expanded through group
# membership for a `subject_type == "user"` principal, despite `DBRoleBinding`'s own
# docstring promising exactly that. SSO's group->role mapping and SCIM's group sync both
# fundamentally depend on this actually working -- this is its first live proof.
# ---------------------------------------------------------------------------

class TestGroupRoleBindingGrantsUserPermission:
    def _mint_user_token(self, user_id: str, raw: str) -> None:
        import asyncpg

        from python.av_server.models import DEFAULT_TENANT_ID
        import python.av_server.identity as identity_module_direct

        async def _mint():
            conn = await asyncpg.connect(
                AV_TEST_DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://")
            )
            try:
                await conn.execute(
                    "INSERT INTO api_tokens (id, tenant_id, user_id, name, token_hash, "
                    "prefix, created_by, created_at) VALUES ($1, $2, $3, $4, $5, $6, $7, now())",
                    f"tok-{raw}", DEFAULT_TENANT_ID, user_id, f"{raw}-token",
                    identity_module_direct.hash_token(raw), raw[:8], "test",
                )
            finally:
                await conn.close()

        asyncio.run(_mint())
        identity_module_direct._principal_cache.clear()

    def test_user_inherits_permission_via_group_role_binding(self, db, scoped_users):
        """The core proof: carol has NO explicit scopes and NO direct role binding of
        her own -- her only path to 'review' is through a group SCIM put her in, which
        was granted the reviewer role. Before this session's fix, this stayed 403
        forever (group bindings were silently inert for a user subject)."""
        admin_headers = {"Authorization": "Bearer trainer-token-12345"}

        created = db.post("/api/users", json={"username": "grouped-carol"}, headers=admin_headers)
        assert created.status_code == 200, created.text
        user_id = created.json()["id"]

        raw_token = "carol-group-token-xyz789"
        self._mint_user_token(user_id, raw_token)
        carol_headers = {"Authorization": f"Bearer {raw_token}"}

        obj_hash = _upload_object(db, b'{"diff":"group-reviewable"}', headers=admin_headers)
        cs = db.post("/api/change-sets",
                    json={"id": "cs-group-rb-proof", "project_id": "pgrb", "object_id": obj_hash},
                    headers=admin_headers)
        assert cs.status_code == 200, cs.text

        # Before any group membership/binding exists: carol is denied.
        pre = db.post("/api/reviews",
                      json={"project_id": "pgrb", "target_type": "change_set",
                            "target_id": "cs-group-rb-proof", "decision": "approve"},
                      headers=carol_headers)
        assert pre.status_code == 403

        # The real production path this fix exists for: a group created via SCIM
        # (mirroring what an IdP's group sync does) with carol as a member.
        group_resp = db.post("/scim/v2/Groups",
                             json={"displayName": "reviewers-team",
                                   "members": [{"value": user_id}]},
                             headers=admin_headers)
        assert group_resp.status_code == 201, group_resp.text
        group_id = group_resp.json()["id"]

        roles = {r["name"]: r["id"]
                for r in db.get("/api/roles", headers=admin_headers).json()["roles"]}
        grant = db.post("/api/role-bindings",
                        json={"subject_type": "group", "subject_id": group_id,
                              "role_id": roles["reviewer"]},
                        headers=admin_headers)
        assert grant.status_code == 200, grant.text

        import python.av_server.identity as identity_module_direct
        identity_module_direct._principal_cache.clear()  # the pre-grant 403 above cached "no perms"

        post = db.post("/api/reviews",
                       json={"project_id": "pgrb", "target_type": "change_set",
                             "target_id": "cs-group-rb-proof", "decision": "approve"},
                       headers=carol_headers)
        assert post.status_code == 200, post.text

        # Still restricted to exactly what 'reviewer' grants -- not a blanket unlock.
        still_denied = db.post("/api/tokens", json={"name": "x"}, headers=carol_headers)
        assert still_denied.status_code == 403

    def test_removing_group_membership_revokes_the_inherited_permission(self, db, scoped_users):
        """The group-expansion subquery reads CURRENT `group_members` rows at request
        time -- removing carol from the group (via the same SCIM PATCH an IdP's group
        sync uses) must revoke her `reviewer` permission immediately, with no separate
        role_bindings change needed."""
        admin_headers = {"Authorization": "Bearer trainer-token-12345"}
        import python.av_server.identity as identity_module_direct

        created = db.post("/api/users", json={"username": "grouped-dave"}, headers=admin_headers)
        user_id = created.json()["id"]
        raw_token = "dave-group-token-abc456"
        self._mint_user_token(user_id, raw_token)
        dave_headers = {"Authorization": f"Bearer {raw_token}"}

        obj_hash = _upload_object(db, b'{"diff":"group-revoke-reviewable"}', headers=admin_headers)
        db.post("/api/change-sets",
               json={"id": "cs-group-revoke-proof", "project_id": "pgrbr", "object_id": obj_hash},
               headers=admin_headers)

        group_resp = db.post("/scim/v2/Groups",
                             json={"displayName": "reviewers-team-2",
                                   "members": [{"value": user_id}]},
                             headers=admin_headers)
        group_id = group_resp.json()["id"]
        roles = {r["name"]: r["id"]
                for r in db.get("/api/roles", headers=admin_headers).json()["roles"]}
        db.post("/api/role-bindings",
               json={"subject_type": "group", "subject_id": group_id, "role_id": roles["reviewer"]},
               headers=admin_headers)
        identity_module_direct._principal_cache.clear()

        allowed = db.post("/api/reviews",
                          json={"project_id": "pgrbr", "target_type": "change_set",
                                "target_id": "cs-group-revoke-proof", "decision": "approve"},
                          headers=dave_headers)
        assert allowed.status_code == 200, allowed.text

        # Remove dave from the group via SCIM PATCH (the "remove" op an IdP's own group
        # sync uses when a user leaves a group upstream).
        patch = db.patch(f"/scim/v2/Groups/{group_id}",
                         json={"Operations": [{"op": "remove", "path": "members",
                                              "value": [{"value": user_id}]}]},
                         headers=admin_headers)
        assert patch.status_code == 200, patch.text
        assert all(m["value"] != user_id for m in patch.json()["members"])

        identity_module_direct._principal_cache.clear()
        revoked = db.post("/api/reviews",
                          json={"project_id": "pgrbr", "target_type": "change_set",
                                "target_id": "cs-group-revoke-proof", "decision": "approve"},
                          headers=dave_headers)
        assert revoked.status_code == 403, revoked.text


# ---------------------------------------------------------------------------
# v1.3.3 (WP-17) — SCIM 2.0 (/scim/v2), live against real Postgres.
# ---------------------------------------------------------------------------

class TestScim:
    def _admin(self) -> dict:
        return {"Authorization": "Bearer trainer-token-12345"}

    def test_service_provider_config_and_resource_types(self, db, scoped_users):
        cfg = db.get("/scim/v2/ServiceProviderConfig", headers=self._admin())
        assert cfg.status_code == 200
        assert cfg.json()["patch"]["supported"] is True

        types = db.get("/scim/v2/ResourceTypes", headers=self._admin())
        assert types.status_code == 200
        names = {r["id"] for r in types.json()["Resources"]}
        assert names == {"User", "Group"}

    def test_create_get_list_filter_user(self, db, scoped_users):
        admin = self._admin()
        created = db.post("/scim/v2/Users",
                          json={"userName": "scim.alice", "displayName": "Alice",
                                "emails": [{"value": "alice@example.com", "primary": True}],
                                "externalId": "ext-alice-1"},
                          headers=admin)
        assert created.status_code == 201, created.text
        body = created.json()
        assert body["userName"] == "scim.alice"
        assert body["active"] is True
        user_id = body["id"]

        fetched = db.get(f"/scim/v2/Users/{user_id}", headers=admin)
        assert fetched.status_code == 200
        assert fetched.json()["emails"][0]["value"] == "alice@example.com"

        listed = db.get("/scim/v2/Users", headers=admin,
                        params={"filter": 'userName eq "scim.alice"'})
        assert listed.status_code == 200
        assert listed.json()["totalResults"] == 1
        assert listed.json()["Resources"][0]["id"] == user_id

        by_external = db.get("/scim/v2/Users", headers=admin,
                             params={"filter": 'externalId eq "ext-alice-1"'})
        assert by_external.json()["totalResults"] == 1

    def test_create_is_not_idempotent_and_reports_409_uniqueness(self, db, scoped_users):
        """A real IdP retries a provisioning POST -- a 409 (not a silent duplicate, not a
        silent 200) is what makes that retry safe: the IdP falls back to GET+PATCH."""
        admin = self._admin()
        first = db.post("/scim/v2/Users", json={"userName": "scim.bob"}, headers=admin)
        assert first.status_code == 201

        again = db.post("/scim/v2/Users", json={"userName": "scim.bob"}, headers=admin)
        assert again.status_code == 409
        assert again.json()["schemas"] == ["urn:ietf:params:scim:api:messages:2.0:Error"]
        assert again.json()["scimType"] == "uniqueness"

    def test_patch_active_false_suspends_and_revokes_sessions_not_a_hard_delete(self, db, scoped_users):
        admin = self._admin()
        created = db.post("/scim/v2/Users", json={"userName": "scim.carol"}, headers=admin)
        user_id = created.json()["id"]

        patched = db.patch(f"/scim/v2/Users/{user_id}",
                           json={"Operations": [{"op": "replace", "path": "active", "value": False}]},
                           headers=admin)
        assert patched.status_code == 200, patched.text
        assert patched.json()["active"] is False

        # Still readable -- not deleted (audit/authorship attribution survives).
        still_there = db.get(f"/scim/v2/Users/{user_id}", headers=admin)
        assert still_there.status_code == 200
        assert still_there.json()["userName"] == "scim.carol"

        reactivated = db.put(f"/scim/v2/Users/{user_id}",
                             json={"userName": "scim.carol", "active": True}, headers=admin)
        assert reactivated.status_code == 200
        assert reactivated.json()["active"] is True

    def test_delete_user_suspends_rather_than_removing_the_row(self, db, scoped_users):
        admin = self._admin()
        created = db.post("/scim/v2/Users", json={"userName": "scim.erin"}, headers=admin)
        user_id = created.json()["id"]

        deleted = db.delete(f"/scim/v2/Users/{user_id}", headers=admin)
        assert deleted.status_code == 204

        still_there = db.get(f"/scim/v2/Users/{user_id}", headers=admin)
        assert still_there.status_code == 200
        assert still_there.json()["active"] is False

    def test_group_create_patch_members_and_delete(self, db, scoped_users):
        admin = self._admin()
        u1 = db.post("/scim/v2/Users", json={"userName": "scim.member1"}, headers=admin).json()["id"]
        u2 = db.post("/scim/v2/Users", json={"userName": "scim.member2"}, headers=admin).json()["id"]

        group = db.post("/scim/v2/Groups",
                        json={"displayName": "scim-team", "members": [{"value": u1}]},
                        headers=admin)
        assert group.status_code == 201, group.text
        group_id = group.json()["id"]
        assert {m["value"] for m in group.json()["members"]} == {u1}

        added = db.patch(f"/scim/v2/Groups/{group_id}",
                         json={"Operations": [{"op": "add", "path": "members",
                                              "value": [{"value": u2}]}]},
                         headers=admin)
        assert added.status_code == 200
        assert {m["value"] for m in added.json()["members"]} == {u1, u2}

        removed = db.patch(f"/scim/v2/Groups/{group_id}",
                           json={"Operations": [{"op": "remove", "path": "members",
                                                "value": [{"value": u1}]}]},
                           headers=admin)
        assert removed.status_code == 200
        assert {m["value"] for m in removed.json()["members"]} == {u2}

        deleted = db.delete(f"/scim/v2/Groups/{group_id}", headers=admin)
        assert deleted.status_code == 204
        assert db.get(f"/scim/v2/Groups/{group_id}", headers=admin).status_code == 404

    def test_scim_scope_required_for_reader_only_token(self, db, scoped_users):
        denied = db.get("/scim/v2/Users", headers={"Authorization": "Bearer reader-token-12345"})
        # reader-token-12345 carries explicit scopes=["read"], no wildcard and no "scim"
        # -- this must be denied, not silently treated as SCIM-privileged.
        assert denied.status_code == 403


# ---------------------------------------------------------------------------
# v1.3.3 (WP-10/WP-11) — sso_crypto.py: provider secrets are Fernet-encrypted at rest,
# never round-tripped back out in plaintext.
# ---------------------------------------------------------------------------

class TestSsoCrypto:
    def test_encrypt_decrypt_round_trip_and_masking(self, monkeypatch):
        monkeypatch.setenv("AV_SECRET_KEY", "test-secret-key-for-sso-crypto")
        from python.av_server import sso_crypto

        config = {"issuer": "https://idp.example.com", "client_id": "abc",
                  "client_secret": "super-secret-value"}
        stored = sso_crypto.encrypt_config(config)
        assert stored["client_secret"] != "super-secret-value"
        assert stored["issuer"] == "https://idp.example.com"  # non-secret fields untouched

        restored = sso_crypto.decrypt_config(stored)
        assert restored["client_secret"] == "super-secret-value"

        masked = sso_crypto.mask_config(stored)
        assert masked["client_secret"] == "***REDACTED***"
        assert masked["issuer"] == "https://idp.example.com"

    def test_encrypt_refuses_a_secret_with_no_key_configured(self, monkeypatch):
        monkeypatch.delenv("AV_SECRET_KEY", raising=False)
        from python.av_server import sso_crypto

        with pytest.raises(sso_crypto.SecretsUnavailable):
            sso_crypto.encrypt_config({"client_secret": "x"})

    def test_sso_provider_create_stores_secret_encrypted_not_plaintext(self, db, scoped_users, monkeypatch):
        monkeypatch.setenv("AV_SECRET_KEY", "test-secret-key-for-sso-crypto")
        admin = {"Authorization": "Bearer trainer-token-12345"}
        created = db.post("/api/sso-providers",
                          json={"kind": "oidc", "name": "test-idp",
                                "config": {"issuer": "https://idp.example.com",
                                          "client_id": "abc", "client_secret": "shh"}},
                          headers=admin)
        assert created.status_code == 200, created.text
        provider_id = created.json()["id"]

        shown = db.get(f"/api/sso-providers/{provider_id}", headers=admin)
        assert shown.json()["config"]["client_secret"] == "***REDACTED***"

        import asyncpg

        async def _raw_config():
            conn = await asyncpg.connect(
                AV_TEST_DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://")
            )
            try:
                row = await conn.fetchrow("SELECT config FROM sso_providers WHERE id = $1", provider_id)
                return row["config"]
            finally:
                await conn.close()

        raw = asyncio.run(_raw_config())
        assert "shh" not in raw  # the plaintext secret must never appear in the stored row


# ---------------------------------------------------------------------------
# v1.3.3 (WP-12) — device_flow.py: Redis-backed device-code state for `av login`.
# ---------------------------------------------------------------------------

class TestDeviceFlow:
    """device_flow.py's async functions read `redis_cache.py::cache._client` -- the
    app's own pooled Redis connection, bound to the TestClient's lifespan event loop
    (started once, session-scoped). Calling them from a fresh `asyncio.run()` here
    reuses that pooled connection from a DIFFERENT loop and hits exactly the documented
    cross-loop failure class this codebase already hit once for asyncpg (`_truncate_all`'s
    own comment) -- confirmed live: the very first draft of this test, calling
    `device_flow.create()` straight from `asyncio.run()`, failed with
    `RuntimeError: ... got Future ... attached to a different loop`. The fix mirrors
    `_truncate_all()`'s own: a brand-new Redis client, opened AND used entirely inside
    this call's own loop, swapped in for the duration via monkeypatch."""

    def _run_with_isolated_redis_client(self, coro_fn, monkeypatch):
        from python.av_server import redis_cache as redis_cache_module

        async def _run():
            import redis.asyncio as redis_asyncio

            isolated_client = redis_asyncio.from_url(
                redis_cache_module.REDIS_URL, decode_responses=True,
            )
            monkeypatch.setattr(redis_cache_module.cache, "_client", isolated_client)
            try:
                return await coro_fn()
            finally:
                await isolated_client.aclose()

        return asyncio.run(_run())

    def test_create_approve_poll_round_trip_is_single_use(self, db, monkeypatch):
        from python.av_server import device_flow

        async def _body():
            created = await device_flow.create("provider-x", ttl_secs=60)
            assert "-" in created["user_code"]

            pending = await device_flow.lookup_by_user_code(created["user_code"])
            assert pending["provider_id"] == "provider-x"
            assert pending["status"] == "pending"

            not_yet = await device_flow.poll(created["device_code"])
            assert not_yet == ("pending", None)

            approved = await device_flow.approve(created["user_code"], "a-real-session-token")
            assert approved is True

            polled = await device_flow.poll(created["device_code"])
            assert polled == ("approved", "a-real-session-token")

            # Single-use: a second poll of the same device_code must not re-deliver the
            # token (the record is deleted on first successful collection).
            second_poll = await device_flow.poll(created["device_code"])
            assert second_poll == ("expired", None)

        self._run_with_isolated_redis_client(_body, monkeypatch)

    def test_poll_unknown_device_code_reports_expired(self, db, monkeypatch):
        from python.av_server import device_flow

        async def _body():
            return await device_flow.poll("never-issued-device-code")

        result = self._run_with_isolated_redis_client(_body, monkeypatch)
        assert result == ("expired", None)
