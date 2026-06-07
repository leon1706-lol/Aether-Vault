import os
import redis.asyncio as redis

REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")

class RedisCache:
    def __init__(self):
        self.client = redis.from_url(REDIS_URL, decode_responses=True)

    async def set_object_exists(self, sha256_hash: str, exists: bool = True):
        await self.client.set(f"obj:{sha256_hash}", "1" if exists else "0", ex=3600)

    async def get_object_exists(self, sha256_hash: str) -> bool | None:
        val = await self.client.get(f"obj:{sha256_hash}")
        if val is None:
            return None
        return val == "1"

cache = RedisCache()
