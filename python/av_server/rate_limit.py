"""Fixed-window rate limiting for the av_server API — zero dependencies.

Why hand-rolled instead of slowapi: the only endpoint that genuinely needs a hard
default limit is the destructive unauthenticated-by-default `POST /api/admin/gc`, and
the data plane must tolerate legitimate bursts (clients upload objects through an
8-worker pool; a 1000-file commit is hundreds of rapid POSTs). A ~80-line fixed-window
limiter with per-bucket configuration covers both without pulling in a library that
couples us to specific starlette versions.

Design:
- Buckets are keyed by `(client_host, bucket_class)`; the bucket class comes from the
  request path (`gc` for `/api/admin/gc`, `default` for everything else under `/api/`).
- Each bucket has its own `Limit` (parsed from env like `"10/minute"`); an absent or
  disabled limit means the class passes through WITHOUT recording state (so disabled
  limiter = zero memory growth).
- Fixed window (not sliding log): constant memory, trivially correct, and the classic
  boundary burst is acceptable here — this guards against accidental/abusive hammering,
  not determined attackers (that is what Protected mode is for).
- The check-and-increment is deliberately synchronous with no awaits inside, so it is
  atomic under asyncio's single-threaded interleaving — no locks required.

Pure logic + injectable clock (`now_fn`) makes every behavior unit-testable without
networks or real time.
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
            "default": parse_limit(env.get("AV_RATE_LIMIT_DEFAULT", "")),
        }
    )
