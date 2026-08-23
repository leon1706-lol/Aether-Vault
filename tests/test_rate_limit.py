"""Unit tests for the fixed-window rate limiter (python/av_server/rate_limit.py).

Pure-logic tests — no server, no database, no real time (injectable clock). The
TestClient-level integration (429 on GC, data-plane pass-through) lives in
tests/test_server.py behind the standard reachability skip.
"""

import pytest

from python.av_server.rate_limit import (
    Limit,
    WindowRateLimiter,
    bucket_class_for,
    build_limiter_from_env,
    parse_limit,
)


# ---------------------------------------------------------------------------
# parse_limit
# ---------------------------------------------------------------------------

def test_parse_limit_valid_specs():
    assert parse_limit("10/minute") == Limit(10, 60)
    assert parse_limit("5/S") == Limit(5, 1)
    assert parse_limit(" 200/HOUR ") == Limit(200, 3600)
    assert parse_limit("3/min") == Limit(3, 60)


def test_parse_limit_disabled_forms_return_none():
    for spec in ("", "   ", "off", "OFF", "disabled", "none", None):
        assert parse_limit(spec) is None, spec


def test_parse_limit_zero_is_treated_as_disabled_not_permanent_deny():
    assert parse_limit("0/minute") is None


def test_parse_limit_malformed_raises_loudly():
    for bad in ("abc", "10/", "/minute", "10/fortnight", "ten/minute"):
        with pytest.raises(ValueError, match="Invalid rate-limit spec"):
            parse_limit(bad)


# ---------------------------------------------------------------------------
# bucket_class_for
# ---------------------------------------------------------------------------

def test_bucket_class_for_routing():
    assert bucket_class_for("/api/admin/gc") == "gc"
    assert bucket_class_for("/api/admin/gc/") == "gc"
    # prefix lookalikes must NOT ride the gc class
    assert bucket_class_for("/api/admin/gcfoo") == "default"
    assert bucket_class_for("/api/objects/abcd") == "default"
    assert bucket_class_for("/api/commits") == "default"


def test_bucket_class_for_exemptions_and_non_api():
    for path in ("/api/health", "/docs", "/openapi.json", "/redoc", "/", "/index.html"):
        assert bucket_class_for(path) is None, path


# ---------------------------------------------------------------------------
# WindowRateLimiter behaviour (injectable clock — no sleeping)
# ---------------------------------------------------------------------------

class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _limiter(max_requests=2, window=60, clock=None):
    return WindowRateLimiter(
        limits={"gc": Limit(max_requests, window), "default": None},
        now_fn=(clock or FakeClock()),
    )


def test_allows_up_to_limit_then_denies_with_retry_after_in_window():
    clock = FakeClock()
    limiter = _limiter(max_requests=2, window=60, clock=clock)

    assert limiter.check("ip", "gc") is None      # 1
    assert limiter.check("ip", "gc") is None      # 2 = at limit
    retry = limiter.check("ip", "gc")             # 3 = over
    assert retry is not None
    assert 1 <= retry <= 60


def test_window_rollover_restores_capacity():
    clock = FakeClock()
    limiter = _limiter(max_requests=2, window=60, clock=clock)

    assert limiter.check("ip", "gc") is None
    assert limiter.check("ip", "gc") is None
    assert limiter.check("ip", "gc") is not None

    clock.now += 61                               # past the window boundary
    assert limiter.check("ip", "gc") is None
    assert limiter.check("ip", "gc") is None
    assert limiter.check("ip", "gc") is not None


def test_clients_are_isolated_per_key():
    limiter = _limiter(max_requests=1, window=60)
    assert limiter.check("alice", "gc") is None
    assert limiter.check("bob", "gc") is None     # alice's full window doesn't block bob
    assert limiter.check("alice", "gc") is not None


def test_classes_have_separate_budgets():
    limits = {"gc": Limit(1, 60), "default": Limit(1, 60)}
    limiter = WindowRateLimiter(limits=limits)
    assert limiter.check("ip", "gc") is None
    assert limiter.check("ip", "default") is None  # separate budget per class
    assert limiter.check("ip", "gc") is not None


def test_disabled_class_records_nothing_and_passes_through():
    clock = FakeClock()
    limiter = _limiter(clock=clock)
    for _ in range(50):                            # far beyond any conceivable cap
        assert limiter.check("ip", "default") is None
    assert limiter._buckets == {}                  # disabled classes store no state


def test_reset_clears_recorded_windows():
    limiter = _limiter(max_requests=1, window=60)
    assert limiter.check("ip", "gc") is None
    assert limiter.check("ip", "gc") is not None
    limiter.reset()
    assert limiter.check("ip", "gc") is None


# ---------------------------------------------------------------------------
# env wiring
# ---------------------------------------------------------------------------

def test_build_limiter_from_env_defaults():
    limiter = build_limiter_from_env(env={})
    assert limiter._limits["gc"] == Limit(10, 60)   # destructive endpoint protected by default
    assert limiter._limits["default"] is None       # data plane opt-in


def test_build_limiter_from_env_overrides():
    limiter = build_limiter_from_env(
        env={"AV_RATE_LIMIT_GC": "off", "AV_RATE_LIMIT_DEFAULT": "500/minute"}
    )
    assert limiter._limits["gc"] is None
    assert limiter._limits["default"] == Limit(500, 60)


def test_build_limiter_from_env_malformed_fails_loudly():
    with pytest.raises(ValueError):
        build_limiter_from_env(env={"AV_RATE_LIMIT_GC": "banana"})
