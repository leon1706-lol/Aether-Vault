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
from sqlalchemy import text

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
from python.av_server.database import engine  # noqa: E402
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
    async with engine.begin() as conn:
        await conn.execute(text("TRUNCATE objects, trees, commits, refs CASCADE"))


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


@pytest.fixture
def db(client):
    """The TestClient, plus a guarantee that tables are truncated after this test runs."""
    yield client
    asyncio.run(_truncate_all())


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

@pytest.mark.skipif(
    not _real_server_reachable(),
    reason="Live aether-vault-server not reachable on :8000; run "
    "`docker compose up -d db redis aether-vault-server`",
)
def test_cli_commit_pushes_to_a_live_server(tmp_path, monkeypatch):
    from click.testing import CliRunner

    from python.av_cli.main import cli

    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    runner.invoke(cli, ["init"])
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
