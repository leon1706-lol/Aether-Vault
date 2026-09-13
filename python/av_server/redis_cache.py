import logging
import os

import redis.asyncio as redis

logger = logging.getLogger("av_server.cache")

REDIS_URL: str = os.getenv("REDIS_URL", "redis://redis:6379/0")
FILTER_NAME: str = "av:hash_filter"


def _filter_name(tenant_id: str | None) -> str:
    """`tenant_id=None` (default `shared` isolation mode) is the exact global filter
    name this class has always used. A real tenant_id (`AV_CAS_ISOLATION=isolated`)
    gets its OWN filter, so one tenant's upload volume never grows another's
    false-positive rate."""
    return FILTER_NAME if tenant_id is None else f"{FILTER_NAME}:{tenant_id}"


class RedisCache:
    """
    Thin async wrapper around a RedisBloom Bloom Filter used to short-circuit
    duplicate-upload checks without hitting PostgreSQL for every object.

    Semantics:
    - check_hash_exists() returns False  → hash DEFINITELY not stored  (skip DB)
    - check_hash_exists() returns True   → hash MIGHT be stored (verify with DB)

    If the RedisBloom module is unavailable the class degrades gracefully:
    all existence checks fall back to True (always hit the DB).
    """

    def __init__(self) -> None:
        self._client: redis.Redis = redis.from_url(REDIS_URL, decode_responses=True)
        self._bloom_available: bool = True  # optimistic; set to False on first failure

    async def init_filter(self, tenant_id: str | None = None) -> None:
        """Reserve a Bloom Filter with 1 M capacity and 0.1 % error rate. Server startup
        reserves the GLOBAL filter (no tenant_id); a per-tenant filter is reserved
        lazily on that tenant's first upload under isolated mode."""
        name = _filter_name(tenant_id)
        try:
            exists = await self._client.execute_command("EXISTS", name)
            if not exists:
                await self._client.execute_command("BF.RESERVE", name, "0.001", "1000000")
                logger.info("Initialized Bloom Filter '%s'", name)
        except Exception as exc:
            logger.warning("RedisBloom unavailable, falling back to DB-only checks: %s", exc)
            self._bloom_available = False

    async def add_hash(self, sha256_hash: str, tenant_id: str | None = None) -> None:
        """Add a hash to the Bloom Filter after a successful upload."""
        if not self._bloom_available:
            return
        try:
            await self._client.execute_command("BF.ADD", _filter_name(tenant_id), sha256_hash)
        except Exception as exc:
            logger.error("Failed to add hash to Bloom Filter: %s", exc)

    async def check_hash_exists(self, sha256_hash: str, tenant_id: str | None = None) -> bool:
        """Return True if the hash *might* exist (possible false positive). `tenant_id`
        given (isolated mode): checks that tenant's own filter first, then falls back to
        the GLOBAL filter too, so an object uploaded before switching to isolated mode
        isn't missed -- checking both is a harmless over-approximation since the DB
        query that follows always does the definitive check."""
        if not self._bloom_available:
            return True  # fallback: always verify with DB
        try:
            if tenant_id is not None:
                tenant_hit = await self._client.execute_command(
                    "BF.EXISTS", _filter_name(tenant_id), sha256_hash
                )
                if tenant_hit == 1:
                    return True
            result = await self._client.execute_command("BF.EXISTS", FILTER_NAME, sha256_hash)
            return result == 1
        except Exception as exc:
            logger.error("Bloom Filter check failed, defaulting to True: %s", exc)
            return True

    async def add_hashes(self, hashes: list[str], tenant_id: str | None = None) -> None:
        """Batch form of `add_hash()` -- `BF.MADD` in 1000-hash chunks instead of one
        `BF.ADD` round trip per hash. Used by GC's post-sweep Bloom Filter rebuild
        (`server.py::run_gc`), which used to re-add every surviving hash one at a time --
        a real, measured cost on this project's own GC benchmark. GC only calls this at
        all when the sweep actually deleted something; an unchanged alive set is already
        correctly represented by the incremental `add_hash()` calls every upload already
        makes, so a full reset+rebuild would be wasted work, not just slower than
        necessary -- see `run_gc`'s own comment."""
        if not hashes or not self._bloom_available:
            return
        try:
            name = _filter_name(tenant_id)
            CHUNK = 1000
            for i in range(0, len(hashes), CHUNK):
                chunk = hashes[i:i + CHUNK]
                await self._client.execute_command("BF.MADD", name, *chunk)
        except Exception as exc:
            logger.error("Failed to batch-add hashes to Bloom Filter: %s", exc)

    async def check_hashes_exist(self, hashes: list[str], tenant_id: str | None = None) -> dict[str, bool]:
        """Batch form of `check_hash_exists()` -- V1.6.0 (Probleme.md real bug): the
        registry's `/api/sync/batch-objects` endpoint (the "does the remote already have
        these objects" check every `av push`/`av fetch` makes) awaited one `BF.EXISTS`
        round trip PER HASH, sequentially -- a 500-object push paid 500 serial Redis round
        trips before ever touching Postgres. `BF.MEXISTS` checks an entire chunk (capped at
        1000 -- RedisBloom itself has no hard limit, but an unbounded single command against
        a push of unknown size is its own risk) in one round trip. Preserves the single-hash
        method's exact short-circuit semantics for isolated mode: a hash the TENANT's own
        filter already confirms never needs the global filter checked at all -- so isolated
        mode costs up to 2 round trips per chunk (tenant, then only the tenant-chunk's
        misses against global) rather than doubling every call. Same fail-open contract:
        Bloom Filter unavailable or any error -> every hash reports `True` (verify with DB),
        never a false "definitely not stored."
        """
        if not hashes:
            return {}
        if not self._bloom_available:
            return {h: True for h in hashes}
        result = {h: False for h in hashes}
        try:
            CHUNK = 1000
            for i in range(0, len(hashes), CHUNK):
                chunk = hashes[i:i + CHUNK]
                remaining = chunk
                if tenant_id is not None:
                    tenant_hits = await self._client.execute_command(
                        "BF.MEXISTS", _filter_name(tenant_id), *chunk
                    )
                    hit_set = {h for h, hit in zip(chunk, tenant_hits) if hit == 1}
                    for h in hit_set:
                        result[h] = True
                    remaining = [h for h in chunk if h not in hit_set]
                if remaining:
                    global_hits = await self._client.execute_command(
                        "BF.MEXISTS", FILTER_NAME, *remaining
                    )
                    for h, hit in zip(remaining, global_hits):
                        if hit == 1:
                            result[h] = True
            return result
        except Exception as exc:
            logger.error("Batch Bloom Filter check failed, defaulting to True for all: %s", exc)
            return {h: True for h in hashes}

    async def ping(self) -> None:
        """Raw connectivity check for /api/ready. Deliberately does NOT fail open -- a
        connection error propagates as an exception, unlike check_hash_exists(), whose
        own True-on-error default would otherwise make a downed Redis look healthy."""
        await self._client.ping()

    async def reset_filter(self, tenant_id: str | None = None) -> None:
        """Delete the existing Bloom Filter (called before GC rebuild). `tenant_id=None`
        resets the GLOBAL filter only."""
        name = _filter_name(tenant_id)
        try:
            await self._client.delete(name)
            logger.info("Bloom Filter '%s' deleted", name)
        except Exception as exc:
            logger.error("Failed to reset Bloom Filter: %s", exc)


# Module-level singleton shared across all requests.
cache = RedisCache()
