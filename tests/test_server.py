"""Tests for the av_server FastAPI backend.

Pure validation tests (no DB/Redis needed) always run. Everything else requires a live
Postgres + Redis (see AV_TEST_DATABASE_URL / AV_TEST_REDIS_URL below) and is skipped cleanly,
with a clear message, if they're not reachable — same philosophy as test_core.py's
`pytest.importorskip`, just for service reachability instead of an import.

    docker compose up -d db redis            # enough for everything except the real-wire test
    docker compose up -d db redis aether-vault-server   # also enables the real-wire test
    pytest tests/test_server.py -v
"""
import asyncio
import hashlib
import os
import socket
import tempfile
from urllib.parse import urlsplit

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

AV_TEST_DATABASE_URL = os.environ.get(
    "AV_TEST_DATABASE_URL",
    "postgresql+asyncpg://av_user:av_password@localhost:5432/aether_vault_test",
)
AV_TEST_REDIS_URL = os.environ.get("AV_TEST_REDIS_URL", "redis://localhost:6379/0")

# Point the server's own module-level config at the test instance *before* importing it —
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
    # A raw asyncpg connection, not the SQLAlchemy async engine's pooled connection — the pool's
    # connections are bound to whichever event loop first used them (TestClient's internal
    # lifespan loop), so reusing the pool from a *separate* asyncio.run() call here raises
    # "got Future ... attached to a different loop". Opening (and closing) a brand-new
    # connection scoped entirely to this call's own loop avoids that cross-loop reuse.
    import asyncpg
    conn = await asyncpg.connect(AV_TEST_DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://"))
    try:
        await conn.execute("TRUNCATE objects, trees, commits, refs CASCADE")
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
    # _truncate_all() only clears the DB tables — any object a test uploaded via
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

    AV_API_TOKEN is read once at module import (see server.py) — empty in this whole test
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
# Pure validation tests — no DB/Redis needed, always run
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
# HTTP-layer tests — require a reachable Postgres + Redis
# ---------------------------------------------------------------------------

def test_health_check_ok(db):
    resp = db.get("/api/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


# ---------------------------------------------------------------------------
# require_token middleware ("Anonymous" vs "Protected" mode)
# ---------------------------------------------------------------------------

def test_no_token_configured_behaves_exactly_as_before(db):
    # AV_API_TOKEN is unset for every other test in this file — this just makes the "Anonymous
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
        "bearer test-secret-token-12345",  # lowercase scheme — still must work (see next test)
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
        # Must match GET /api/commits/{hash}'s own tree exactly — same resolve_tree() call.
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
# Real-wire test — needs the actual aether-vault-server process, not just TestClient
# ---------------------------------------------------------------------------

def test_cli_commit_pushes_to_a_live_server(tmp_path, monkeypatch):
    # Checked here (lazily, when this test actually runs) rather than via a `skipif` decorator
    # (evaluated once at collection time, before any other test has run) — a `skipif` condition
    # check competes with whatever the rest of collection/the test run is doing for CPU/network
    # scheduling right at that single moment, and a slow tick there reads as "unreachable" even
    # though the server is fine moments later (observed once in a heavy combined venv+Docker run).
    if not _real_server_reachable():
        pytest.skip(
            "Live aether-vault-server not reachable on :8000; run "
            "`docker compose up -d db redis aether-vault-server`"
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
            "Live aether-vault-server not reachable on :8000; run "
            "`docker compose up -d db redis aether-vault-server`"
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
