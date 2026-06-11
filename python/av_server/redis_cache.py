import logging
import os

import redis.asyncio as redis

logger = logging.getLogger("av_server.cache")

REDIS_URL: str = os.getenv("REDIS_URL", "redis://redis:6379/0")
FILTER_NAME: str = "av:hash_filter"


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

    async def init_filter(self) -> None:
        """Reserve a Bloom Filter with 1 M capacity and 0.1 % error rate."""
        try:
            exists = await self._client.execute_command("EXISTS", FILTER_NAME)
            if not exists:
                await self._client.execute_command(
                    "BF.RESERVE", FILTER_NAME, "0.001", "1000000"
                )
                logger.info("Initialized Bloom Filter '%s'", FILTER_NAME)
        except Exception as exc:
            logger.warning("RedisBloom unavailable, falling back to DB-only checks: %s", exc)
            self._bloom_available = False

    async def add_hash(self, sha256_hash: str) -> None:
        """Add a hash to the Bloom Filter after a successful upload."""
        if not self._bloom_available:
            return
        try:
            await self._client.execute_command("BF.ADD", FILTER_NAME, sha256_hash)
        except Exception as exc:
            logger.error("Failed to add hash to Bloom Filter: %s", exc)

    async def check_hash_exists(self, sha256_hash: str) -> bool:
        """Return True if the hash *might* exist (possible false positive)."""
        if not self._bloom_available:
            return True  # fallback: always verify with DB
        try:
            result = await self._client.execute_command("BF.EXISTS", FILTER_NAME, sha256_hash)
            return result == 1
        except Exception as exc:
            logger.error("Bloom Filter check failed, defaulting to True: %s", exc)
            return True

    async def reset_filter(self) -> None:
        """Delete the existing Bloom Filter (called before GC rebuild)."""
        try:
            await self._client.delete(FILTER_NAME)
            logger.info("Bloom Filter '%s' deleted", FILTER_NAME)
        except Exception as exc:
            logger.error("Failed to reset Bloom Filter: %s", exc)


# Module-level singleton shared across all requests.
cache = RedisCache()
