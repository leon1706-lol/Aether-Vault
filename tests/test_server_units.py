"""Stack-free server checks for the V1.6.3 footprint work: request validation maxima
(422 before any DB is touched), the bounded in-process dicts, the RSS gauges, and the
upload cap -- none of these need Postgres/Redis, so they run in the plain `test` job."""
import asyncio
import os
import tempfile
import time

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://av_user:av_pass@127.0.0.1:1/av_units_never_connected")
os.environ.setdefault("AV_APP_DATABASE_URL", os.environ["DATABASE_URL"])
os.environ.setdefault("REDIS_URL", "redis://127.0.0.1:1/9")
os.environ.setdefault("AV_DATA_DIR", tempfile.mkdtemp(prefix="av-server-units-"))
os.environ.setdefault("AV_WEBHOOK_RETRY_INTERVAL_SECS", "999999")

from fastapi.testclient import TestClient  # noqa: E402

import python.av_server.server as server_module  # noqa: E402
from python.av_server import identity, metrics  # noqa: E402
from python.av_server.server import app  # noqa: E402


@pytest.fixture(scope="module")
def client():
    # No `with`: the lifespan (migrations, bloom init, workers) never runs -- these tests
    # must fail before any query would be issued.
    return TestClient(app)


# --- D1: every list endpoint has a maximum ------------------------------------------------

@pytest.mark.parametrize("path", [
    "/api/commits?limit=501", "/api/runs?limit=1001", "/api/refs?limit=5001", "/api/sync/refs?limit=5001",
    "/api/tasks?limit=2001", "/api/plans?limit=2001", "/api/webhooks?limit=2001",
    "/api/improvers?limit=501", "/api/scheduler/queue?limit=501",
    "/api/commits?limit=0", "/api/refs?offset=-1", "/api/events?wait=61", "/api/events?limit=1001",
])
def test_limit_outside_bounds_is_422_before_any_db_access(client, path):
    resp = client.get(path)
    assert resp.status_code == 422, (path, resp.status_code, resp.text[:200])


def test_every_list_route_declares_a_bounded_limit():
    """Any `limit` query parameter on a GET route must carry a maximum -- a new endpoint
    that forgets `le=` fails here instead of quietly returning whole tables again."""
    from fastapi.routing import APIRoute

    # audit/verify: limit=0 means "walk the whole chain" (streamed, a cursor, not a
    # materialized list). run metrics: clamped in the handler to _RUN_METRICS_MAX_LIMIT.
    exempt = {"/api/admin/audit/verify:limit", "/api/runs/{run_id}/metrics:limit"}
    unbounded = []
    for route in app.routes:
        if not isinstance(route, APIRoute) or "GET" not in route.methods:
            continue
        for param in route.dependant.query_params:
            if param.name in ("limit", "wait"):
                field_info = param.field_info
                le = None
                for meta in getattr(field_info, "metadata", []):
                    le = getattr(meta, "le", le)
                key = f"{route.path}:{param.name}"
                if le is None and key not in exempt:
                    unbounded.append(key)
    assert unbounded == [], unbounded


# --- D4: bounded in-process state -----------------------------------------------------------

def test_auth_failure_window_prunes_stale_hosts(monkeypatch):
    monkeypatch.setattr(server_module, "_AUTH_SPIKE_BACKEND", "memory")
    monkeypatch.setattr(server_module, "AV_ANOMALY_AUTH_SPIKE_WINDOW_SECS", 10.0)
    monkeypatch.setattr(server_module, "AV_ANOMALY_AUTH_SPIKE_THRESHOLD", 100)
    monkeypatch.setattr(server_module, "_AUTH_FAILURE_PRUNE_EVERY", 4)
    server_module._AUTH_FAILURE_WINDOW.clear()
    clock = [1000.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    for host in ("a", "b", "c"):
        asyncio.run(server_module._note_auth_failure(host))
    assert set(server_module._AUTH_FAILURE_WINDOW) == {"a", "b", "c"}
    clock[0] += 60.0  # every entry is now older than the window
    asyncio.run(server_module._note_auth_failure("d"))  # 4th call -> prune fires
    assert set(server_module._AUTH_FAILURE_WINDOW) == {"d"}


def test_auth_failure_window_is_hard_capped(monkeypatch):
    monkeypatch.setattr(server_module, "_AUTH_SPIKE_BACKEND", "memory")
    monkeypatch.setattr(server_module, "AV_ANOMALY_AUTH_SPIKE_THRESHOLD", 100)
    monkeypatch.setattr(server_module, "_AUTH_FAILURE_MAX_KEYS", 50)
    server_module._AUTH_FAILURE_WINDOW.clear()
    for i in range(400):
        asyncio.run(server_module._note_auth_failure(f"host-{i}"))
    assert len(server_module._AUTH_FAILURE_WINDOW) <= 51


def test_auth_failure_burst_leaves_no_residue(monkeypatch):
    monkeypatch.setattr(server_module, "_AUTH_SPIKE_BACKEND", "memory")
    monkeypatch.setattr(server_module, "AV_ANOMALY_AUTH_SPIKE_THRESHOLD", 3)
    server_module._AUTH_FAILURE_WINDOW.clear()
    assert asyncio.run(server_module._note_auth_failure("x")) is False
    assert asyncio.run(server_module._note_auth_failure("x")) is False
    assert asyncio.run(server_module._note_auth_failure("x")) is True
    assert "x" not in server_module._AUTH_FAILURE_WINDOW


def test_principal_cache_is_bounded(monkeypatch):
    monkeypatch.setattr(identity, "AUTH_CACHE_MAX_ENTRIES", 100)
    identity._principal_cache.clear()
    for i in range(1100):
        identity._cache_put(f"hash-{i}", None)
    assert len(identity._principal_cache) <= 100
    # The newest insert is always retained.
    assert "hash-1099" in identity._principal_cache
    identity._principal_cache.clear()


def test_principal_cache_evicts_expired_before_live(monkeypatch):
    monkeypatch.setattr(identity, "AUTH_CACHE_MAX_ENTRIES", 3)
    identity._principal_cache.clear()
    identity._principal_cache["expired"] = (time.monotonic() - 1, None)
    identity._cache_put("live-1", None)
    identity._cache_put("live-2", None)
    identity._cache_put("live-3", None)
    assert "expired" not in identity._principal_cache
    assert {"live-1", "live-2", "live-3"} == set(identity._principal_cache)
    identity._principal_cache.clear()


def test_unrouted_requests_use_a_constant_metrics_label(client):
    metrics.reset()
    for i in range(3):
        client.get(f"/api/does-not-exist-{i}")
    text = metrics.render_prometheus_text()
    assert 'path="unmatched"' in text
    assert "does-not-exist" not in text
    metrics.reset()


# --- D8: RSS gauges -----------------------------------------------------------------------------

def test_render_includes_rss_gauges_only_when_given():
    text = metrics.render_prometheus_text()
    assert "av_process_rss_bytes" not in text
    text = metrics.render_prometheus_text(process_rss_bytes=123456, process_peak_rss_bytes=234567)
    assert "av_process_rss_bytes 123456" in text
    assert "av_process_peak_rss_bytes 234567" in text


def test_server_sysres_probe_is_real():
    from python.av_server import sysres

    rss = sysres.current_rss_bytes()
    assert rss is not None and rss > 10 * 1024 * 1024
    assert sysres.peak_rss_bytes() >= rss * 0.99


# --- D5: upload cap (pure pieces; the endpoint-level 413 is covered live in test_server.py) ---

def test_limited_stream_raises_past_the_cap():
    async def gen():
        for _ in range(5):
            yield b"x" * 100

    async def consume():
        out = []
        async for chunk in server_module._limited_stream(gen(), 250):
            out.append(chunk)
        return out

    with pytest.raises(server_module._UploadTooLarge):
        asyncio.run(consume())

    async def consume_partial():
        out = []
        try:
            async for chunk in server_module._limited_stream(gen(), 250):
                out.append(chunk)
        except server_module._UploadTooLarge:
            pass
        return out

    assert len(asyncio.run(consume_partial())) == 2  # two chunks delivered, the third crossed the cap


def test_upload_cap_defaults_to_unlimited():
    assert server_module.MAX_UPLOAD_BYTES == 0
