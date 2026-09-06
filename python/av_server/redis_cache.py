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
