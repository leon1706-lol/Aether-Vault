"""Fixed-window rate limiting for the av_server API — zero dependencies, hand-rolled
instead of slowapi so a ~80-line limiter covers both the destructive `POST /api/admin/gc`
(needs a hard default limit) and the data plane (must tolerate legitimate upload bursts)
without coupling to specific starlette versions. Buckets are keyed by (client_host,
bucket_class); fixed window (not sliding log) for constant memory and trivial
correctness -- this guards against accidental/abusive hammering, not determined
attackers. Check-and-increment is synchronous with no awaits inside, so it's atomic
under asyncio's single-threaded interleaving.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass

_UNIT_SECONDS = {"s": 1, "sec": 1, "second": 1, "m": 60, "min": 60, "minute": 60,
                 "h": 3600, "hour": 3600}

_EXEMPT_PATHS = frozenset({"/api/health", "/docs", "/openapi.json", "/redoc"})
_GC_PREFIX = "/api/admin/gc"


@dataclass(frozen=True)
class Limit:
    max_requests: int
    window_seconds: int


def parse_limit(spec: str | None) -> Limit | None:
    """Parses `"10/minute"`, `"5/s"`, `"200/hour"` style specs.

    Empty/whitespace/"off"/"disabled" → None (limiter disabled for that bucket).
    Malformed non-empty specs raise ValueError — a silently unprotected deployment is
    worse than a loud startup failure.
    """
    if spec is None:
        return None
    cleaned = spec.strip().lower()
    if not cleaned or cleaned in {"off", "disabled", "none"}:
        return None
    try:
        count_s, _, unit = cleaned.partition("/")
        count = int(count_s.strip())
        unit = unit.strip()
        if unit not in _UNIT_SECONDS:
            raise ValueError
        if count <= 0:
            # "0/minute" as a config would mean permanently blocked — almost certainly
            # a mistake; treat like disabled rather than bricking the endpoint.
            return None
        return Limit(max_requests=count, window_seconds=_UNIT_SECONDS[unit])
    except ValueError:
        raise ValueError(
            f"Invalid rate-limit spec {spec!r} — expected '<count>/<unit>' with unit "
            f"one of {sorted(set(_UNIT_SECONDS))} (e.g. '10/minute'), 'off', or empty."
        ) from None


def bucket_class_for(path: str) -> str | None:
    """Bucket class for a request path; None when limiting does not apply at all."""
    if path in _EXEMPT_PATHS or not path.startswith("/api/"):
        return None
    if path == _GC_PREFIX or path.startswith(_GC_PREFIX + "/"):
        return "gc"
    if path.startswith("/api/events") or path.startswith("/api/webhooks"):
        # Agent polling/delivery surface — opt-in cap, never accidentally limited.
        return "events"
    return "default"


class WindowRateLimiter:
    """Fixed-window counter keyed by (client_key, bucket_class)."""

    def __init__(self, limits: dict[str, Limit | None], now_fn=time.monotonic):
        self._limits = limits
        self._now_fn = now_fn
        self._buckets: dict[tuple[str, str], tuple[int, float]] = {}

    def check(self, client_key: str, bucket_class: str) -> int | None:
        """Records one hit. Returns retry-after seconds when over the limit, else None."""
        limit = self._limits.get(bucket_class)
        if limit is None:
            return None  # disabled class — pass through, record nothing

        key = (client_key, bucket_class)
        now = self._now_fn()
        count, window_start = self._buckets.get(key, (0, now))
        if now - window_start >= limit.window_seconds:
            count, window_start = 0, now  # new window

        count += 1
        self._buckets[key] = (count, window_start)

        if count > limit.max_requests:
            import math

            remaining = limit.window_seconds - (now - window_start)
            return max(1, math.ceil(remaining))
        return None

    def reset(self) -> None:
        """Test hook: clears all recorded windows."""
        self._buckets.clear()


def build_limiter_from_env(env: dict[str, str] | None = None) -> WindowRateLimiter:
    """Builds the server's limiter from AV_RATE_LIMIT_GC / AV_RATE_LIMIT_DEFAULT.

    Defaults: GC protected at 10/minute even in Anonymous mode (it is destructive and
    historically unguarded); the data plane starts unlimited so bulk uploads never
    false-positive — operators opt in via AV_RATE_LIMIT_DEFAULT.
    """
    env = os.environ if env is None else env
    return WindowRateLimiter(
        limits={
            "gc": parse_limit(env.get("AV_RATE_LIMIT_GC", "10/minute")),
            # Agent event/webhook surface: unlimited by default (long-poll loops are
            # legitimate), opt-in cap via AV_RATE_LIMIT_EVENTS.
            "events": parse_limit(env.get("AV_RATE_LIMIT_EVENTS", "")),
            "default": parse_limit(env.get("AV_RATE_LIMIT_DEFAULT", "")),
        }
    )


# ---------------------------------------------------------------------------
# A Redis-backed counter, opt-in via AV_RATE_LIMIT_BACKEND=redis: WindowRateLimiter's
# in-process dict is correct at N=1 replica but silently WRONG at N>1 (each replica
# enforces its own independent window, so a round-robined client gets N times every
# limit). Default stays the in-process limiter above, byte-identical when unconfigured.
# ---------------------------------------------------------------------------

# INCR then EXPIRE on first increment, as ONE atomic Lua round trip -- avoids the window
# where a process dies between a successful INCR and an EXPIRE that never runs.
_INCR_AND_EXPIRE_LUA = """
local count = redis.call('INCR', KEYS[1])
if count == 1 then redis.call('EXPIRE', KEYS[1], ARGV[1]) end
return count
"""


class RedisWindowRateLimiter:
    """Same fixed-window semantics as `WindowRateLimiter`, same injectable-clock
    testability contract -- the clock decides the window BUCKET, the Redis key TTL is
    cleanup only, not part of the correctness path. `check()` is async, unlike its
    sync sibling."""

    def __init__(self, redis_client, limits: dict[str, Limit | None], now_fn=time.time):
        self._redis = redis_client
        self._limits = limits
        self._now_fn = now_fn

    async def check(self, client_key: str, bucket_class: str) -> int | None:
        limit = self._limits.get(bucket_class)
        if limit is None:
            return None
        window_id = int(self._now_fn() // limit.window_seconds)
        key = f"av:ratelimit:{bucket_class}:{client_key}:{window_id}"
        try:
            count = await self._redis.eval(_INCR_AND_EXPIRE_LUA, 1, key, limit.window_seconds)
        except Exception:
            # Fails OPEN -- a degraded/unreachable Redis must never become an outage
            # for every other request.
            return None
        if count > limit.max_requests:
            try:
                ttl = await self._redis.ttl(key)
            except Exception:
                ttl = limit.window_seconds
            return max(1, ttl if ttl and ttl > 0 else limit.window_seconds)
        return None

    def reset(self) -> None:
        raise NotImplementedError(
            "RedisWindowRateLimiter has no in-process state to clear — tests should use "
            "a real or fake Redis client scoped to the test instead (e.g. flushdb)."
        )


def build_redis_limiter_from_env(redis_client, env: dict[str, str] | None = None) -> RedisWindowRateLimiter:
    """Same env vars, same defaults as `build_limiter_from_env` -- only the backend
    differs."""
    env = os.environ if env is None else env
    return RedisWindowRateLimiter(
        redis_client,
        limits={
            "gc": parse_limit(env.get("AV_RATE_LIMIT_GC", "10/minute")),
            "events": parse_limit(env.get("AV_RATE_LIMIT_EVENTS", "")),
            "default": parse_limit(env.get("AV_RATE_LIMIT_DEFAULT", "")),
        },
    )
