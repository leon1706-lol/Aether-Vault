import os
import redis.asyncio as redis
import logging

logger = logging.getLogger("av_server")
REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")
FILTER_NAME = "av:hash_filter"

class RedisCache:
    def __init__(self):
        self.client = redis.from_url(REDIS_URL, decode_responses=True)

    async def init_filter(self):
        """
        Initializes the Bloom Filter with a capacity of 1,000,000 and 0.1% error rate.
        Note: Requires RedisBloom module or a Redis server that supports BF.RESERVE.
        """
        try:
            # Check if filter exists
            exists = await self.client.execute_command("EXISTS", FILTER_NAME)
            if not exists:
                # BF.RESERVE {key} {error_rate} {capacity}
                await self.client.execute_command("BF.RESERVE", FILTER_NAME, "0.001", "1000000")
                logger.info(f"Initialized Bloom Filter: {FILTER_NAME}")
        except Exception as e:
            logger.warning(f"Could not initialize RedisBloom filter (maybe module missing?): {e}")
            # Fallback logic or error handling could go here

    async def add_hash(self, sha256_hash: str):
        """Adds a hash to the Bloom Filter."""
        try:
            await self.client.execute_command("BF.ADD", FILTER_NAME, sha256_hash)
        except Exception as e:
            logger.error(f"Failed to add hash to Bloom Filter: {e}")

    async def check_hash_exists(self, sha256_hash: str) -> bool:
        """
        Checks if a hash exists in the Bloom Filter.
        Returns True if it MIGHT exist (potential False Positive).
        Returns False if it definitely DOES NOT exist.
        """
        try:
            # BF.EXISTS returns 1 if exists, 0 if not
            result = await self.client.execute_command("BF.EXISTS", FILTER_NAME, sha256_hash)
            return result == 1
        except Exception as e:
            logger.error(f"Bloom Filter check failed: {e}")
            return True # Fallback to DB check on error

cache = RedisCache()
