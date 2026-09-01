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
AV_TEST_REDIS_URL = os.environ.get("AV_TEST_REDIS_URL", "redis://localhost:6379/0")

# Point the server's own module-level config at the test instance *before* importing it â€”
# database.py/redis_cache.py read DATABASE_URL/REDIS_URL at import time, and storage.py's
# CASStorage is constructed at import time too (AV_DATA_DIR). Explicit assignment (not
# setdefault) so a real dev DATABASE_URL already exported in the shell never leaks in here.
os.environ["DATABASE_URL"] = AV_TEST_DATABASE_URL
os.environ["REDIS_URL"] = AV_TEST_REDIS_URL
os.environ["AV_DATA_DIR"] = tempfile.mkdtemp(prefix="av-server-test-")

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
        await conn.execute(
            "TRUNCATE objects, trees, commits, refs, runs, run_commits, events,"
            " webhooks, webhook_deliveries, audit_log CASCADE"
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
    assert version == "0004"  # current migration head — bump alongside new revisions
    assert {"objects", "trees", "commits", "refs", "alembic_version"} <= tables
    assert {"extra_parents"} <= _pg_columns("commits")
    assert {"chunks"} <= _pg_columns("trees")
    # v1.2.2 additive surfaces:
    assert {"signature"} <= _pg_columns("commits")
    assert {"status_code"} <= _pg_columns("audit_log")
    assert "webhook_deliveries" in tables


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

    assert asyncio.run(_version()) == "0004"  # stamps to CURRENT head, not a hardcoded rev



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
    assert body["semantic_summary"]["files"]["added"] == ["b.txt"]
    assert body["semantic_summary"]["files"]["changed"] == []
    assert body["semantic_summary"]["totals"]["bytes_after"] == 30


def test_run_summary_no_semantic_diff_with_fewer_than_two_commits(db):
    proj = "proj-run-summary-single"
    run_id = db.post("/api/runs", json={"project_id": proj}).json()["id"]
    c1 = _make_commit("summary-single", project_id=proj, run_id=run_id)
    assert db.post("/api/commits", json=c1).status_code == 201

    body = db.get(f"/api/runs/{run_id}/summary").json()
    assert body["semantic_summary"] is None
    assert body["total_commits"] == 1


def test_run_summary_404_for_unknown_run(db):
    assert db.get("/api/runs/does-not-exist/summary").status_code == 404


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
