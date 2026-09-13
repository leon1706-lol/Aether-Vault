"""V1.6.0 (Probleme.md real bug): `/api/sync/batch-objects` awaited one `BF.EXISTS` round
trip PER hash, sequentially -- a real push's object list paid that serially before ever
reaching Postgres. `RedisCache.check_hashes_exist()` batches via `BF.MEXISTS` (chunked at
1000) instead. These are pure unit tests against a fake async Redis client (no live Redis
needed, unlike `tests/test_server.py`'s integration coverage of the same endpoint) --
exercising the actual chunking/short-circuit logic directly.
"""
import pytest

from python.av_server.redis_cache import RedisCache


class _FakeRedisClient:
    """Records every `execute_command` call and answers BF.MEXISTS/BF.EXISTS from two
    in-memory sets the test configures -- `tenant_hits`/`global_hits` -- rather than a real
    Bloom Filter (false-positive-free by construction, which is fine: these tests are about
    the batching/short-circuit LOGIC, not RedisBloom's own probabilistic behavior)."""

    def __init__(self, tenant_hits: set[str] = frozenset(), global_hits: set[str] = frozenset()):
        self.tenant_hits = tenant_hits
        self.global_hits = global_hits
        self.calls: list[tuple] = []

    async def execute_command(self, *args):
        self.calls.append(args)
        cmd, filter_name, *hashes = args
        if cmd == "BF.MADD":
            return [1] * len(hashes)
        assert cmd == "BF.MEXISTS"
        hits = self.tenant_hits if filter_name.endswith(":tenant-a") else self.global_hits
        return [1 if h in hits else 0 for h in hashes]


def _cache_with(client: _FakeRedisClient) -> RedisCache:
    cache = RedisCache.__new__(RedisCache)  # skip __init__'s real redis.from_url() call
    cache._client = client
    cache._bloom_available = True
    return cache


def test_check_hashes_exist_empty_input_makes_no_calls():
    client = _FakeRedisClient()
    cache = _cache_with(client)
    import asyncio

    result = asyncio.run(cache.check_hashes_exist([]))
    assert result == {}
    assert client.calls == []


def test_check_hashes_exist_bloom_unavailable_defaults_all_true():
    import asyncio

    cache = _cache_with(_FakeRedisClient())
    cache._bloom_available = False
    result = asyncio.run(cache.check_hashes_exist(["a", "b"]))
    assert result == {"a": True, "b": True}


def test_check_hashes_exist_shared_mode_one_call_per_chunk():
    import asyncio

    client = _FakeRedisClient(global_hits={"h1", "h3"})
    cache = _cache_with(client)
    result = asyncio.run(cache.check_hashes_exist(["h1", "h2", "h3"]))
    assert result == {"h1": True, "h2": False, "h3": True}
    assert len(client.calls) == 1  # one BF.MEXISTS for the whole (small) input, no tenant filter


def test_check_hashes_exist_chunks_at_1000():
    import asyncio

    hashes = [f"h{i}" for i in range(1500)]
    client = _FakeRedisClient(global_hits={"h0", "h1499"})
    cache = _cache_with(client)
    result = asyncio.run(cache.check_hashes_exist(hashes))
    assert result["h0"] is True
    assert result["h1499"] is True
    assert result["h750"] is False
    assert len(client.calls) == 2  # 1500 hashes -> two 1000/500 chunks
    assert len(client.calls[0][2:]) == 1000
    assert len(client.calls[1][2:]) == 500


def test_check_hashes_exist_isolated_mode_short_circuits_global_check():
    """A hash the TENANT filter already confirms must never need the global filter
    checked -- mirrors check_hash_exists()'s single-hash short-circuit, just batched."""
    import asyncio

    client = _FakeRedisClient(tenant_hits={"h1"}, global_hits={"h2"})
    cache = _cache_with(client)
    result = asyncio.run(cache.check_hashes_exist(["h1", "h2", "h3"], tenant_id="tenant-a"))
    assert result == {"h1": True, "h2": True, "h3": False}
    assert len(client.calls) == 2  # one tenant BF.MEXISTS, one global BF.MEXISTS
    tenant_call, global_call = client.calls
    assert set(tenant_call[2:]) == {"h1", "h2", "h3"}  # tenant filter checked for all
    assert set(global_call[2:]) == {"h2", "h3"}  # only the tenant-filter MISSES go to global


def test_check_hashes_exist_isolated_mode_skips_global_call_when_tenant_covers_everything():
    import asyncio

    client = _FakeRedisClient(tenant_hits={"h1", "h2"})
    cache = _cache_with(client)
    result = asyncio.run(cache.check_hashes_exist(["h1", "h2"], tenant_id="tenant-a"))
    assert result == {"h1": True, "h2": True}
    assert len(client.calls) == 1  # global BF.MEXISTS never needed


def test_add_hashes_empty_input_makes_no_calls():
    import asyncio

    client = _FakeRedisClient()
    cache = _cache_with(client)
    asyncio.run(cache.add_hashes([]))
    assert client.calls == []


def test_add_hashes_bloom_unavailable_makes_no_calls():
    import asyncio

    client = _FakeRedisClient()
    cache = _cache_with(client)
    cache._bloom_available = False
    asyncio.run(cache.add_hashes(["a", "b"]))
    assert client.calls == []


def test_add_hashes_chunks_at_1000():
    import asyncio

    hashes = [f"h{i}" for i in range(1500)]
    client = _FakeRedisClient()
    cache = _cache_with(client)
    asyncio.run(cache.add_hashes(hashes))
    assert len(client.calls) == 2  # 1500 -> 1000 + 500
    assert client.calls[0][0] == "BF.MADD"
    assert client.calls[0][1] == "av:hash_filter"
    assert len(client.calls[0][2:]) == 1000
    assert len(client.calls[1][2:]) == 500


def test_add_hashes_uses_tenant_filter_name():
    import asyncio

    client = _FakeRedisClient()
    cache = _cache_with(client)
    asyncio.run(cache.add_hashes(["h1"], tenant_id="tenant-a"))
    assert client.calls[0][1] == "av:hash_filter:tenant-a"


def test_add_hashes_error_is_swallowed_not_raised():
    import asyncio

    class _BoomClient:
        async def execute_command(self, *args):
            raise ConnectionError("simulated redis outage")

    cache = _cache_with(_BoomClient())
    asyncio.run(cache.add_hashes(["a", "b"]))  # must not raise


def test_check_hashes_exist_error_defaults_all_true():
    import asyncio

    class _BoomClient:
        async def execute_command(self, *args):
            raise ConnectionError("simulated redis outage")

    cache = _cache_with(_BoomClient())
    result = asyncio.run(cache.check_hashes_exist(["a", "b"]))
    assert result == {"a": True, "b": True}
