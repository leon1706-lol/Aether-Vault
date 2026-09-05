import logging
import os

import redis.asyncio as redis

logger = logging.getLogger("av_server.cache")

REDIS_URL: str = os.getenv("REDIS_URL", "redis://redis:6379/0")
FILTER_NAME: str = "av:hash_filter"


def _filter_name(tenant_id: str | None) -> str:
    """v1.3.3 (WP-21, AV_CAS_ISOLATION): `tenant_id=None` (every call site under the
    default `shared` isolation mode, and every pre-v1.3.3 call site) is the exact
    global filter name this class has always used — byte-identical. A real tenant_id
    (only ever passed under `AV_CAS_ISOLATION=isolated`) gets its OWN filter, so one
    tenant's upload volume never grows another tenant's false-positive rate."""
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
        """Reserve a Bloom Filter with 1 M capacity and 0.1 % error rate. Always
        reserves the GLOBAL filter (server startup calls this with no tenant_id — the
        global filter is what `shared` mode, the default, uses exclusively); a
        per-tenant filter is reserved lazily, on that tenant's first upload under
        isolated mode, via the same call with a real tenant_id."""
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
        """Return True if the hash *might* exist (possible false positive).

        `tenant_id` given (isolated mode): checks that tenant's OWN filter first, then
        falls back to the GLOBAL filter too — an object uploaded before this
        deployment (or this tenant) switched to isolated mode lives only in the global
        filter/legacy flat storage path, and a tenant-only check would silently miss it,
        producing exactly the false "not a duplicate" the existence check exists to
        prevent. A positive from either filter means "might exist, verify with DB" —
        the DB query that follows always does the definitive check regardless, so
        checking both filters is a harmless over-approximation, not a correctness risk."""
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
        """v1.2.5: raw connectivity check for /api/ready (server.py). Deliberately does
        NOT fail open — a connection error propagates to the caller as an exception.
        check_hash_exists() looks like a connectivity probe but isn't one for this
        purpose: it catches its own errors and returns True ("might exist, verify with
        DB") by design, which is the right default for its actual caller (skip-the-DB
        optimization) but means a downed Redis silently reports as healthy to anything
        using it as a health check — exactly the bug this method exists to avoid."""
        await self._client.ping()

    async def reset_filter(self, tenant_id: str | None = None) -> None:
        """Delete the existing Bloom Filter (called before GC rebuild). `tenant_id=None`
        resets the GLOBAL filter only — GC's per-tenant rebuild (isolated mode) resets
        each tenant's own filter by name, never touching the others'."""
        name = _filter_name(tenant_id)
        try:
            await self._client.delete(name)
            logger.info("Bloom Filter '%s' deleted", name)
        except Exception as exc:
            logger.error("Failed to reset Bloom Filter: %s", exc)


# Module-level singleton shared across all requests.
cache = RedisCache()
