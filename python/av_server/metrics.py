"""In-process Prometheus text-exposition metrics (v1.3.3, WP-35) — hand-rolled, no new
dependency, the same judgment call `rate_limit.py` already made for its own
fixed-window limiter rather than pulling in a library.

**Per-process only, honestly**, like the in-process rate limiter's own documented
N-replica caveat: a real multi-replica deployment scrapes each replica independently
(Prometheus's normal multi-target model) — this file makes no attempt to aggregate
across replicas, and `docs/slo.md` says so plainly rather than implying otherwise.

Plain module-level dicts, no lock: every mutation here happens inside a single
`await`-free code path (the metrics middleware's synchronous bookkeeping around
`call_next`), so asyncio's single-threaded interleaving makes each increment atomic —
the exact same reasoning `rate_limit.py`'s own module docstring already established for
this codebase.
"""
from __future__ import annotations

import time

_START_TIME = time.time()

# (method, path_template, status_class) -> count
_REQUEST_COUNTS: dict[tuple[str, str, str], int] = {}
# (method, path_template) -> running sum / count, for the histogram's _sum/_count lines
_REQUEST_DURATION_SUM: dict[tuple[str, str], float] = {}
_REQUEST_DURATION_COUNT: dict[tuple[str, str], int] = {}
# Fixed bucket boundaries (seconds) -- a small, standard-ish set; not configurable, this
# is diagnostic instrumentation, not a tuned SLO dashboard input.
DURATION_BUCKETS: tuple[float, ...] = (0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)
# (method, path_template, bucket_or_inf) -> count of observations <= that bucket --
# incremented for EVERY qualifying bucket at record time, so each entry already holds
# its final cumulative value; render time never re-accumulates (that would double count).
_REQUEST_DURATION_BUCKETS: dict[tuple[str, str, float], int] = {}
_TENANT_REQUEST_COUNTS: dict[str, int] = {}


def record_request(method: str, path_template: str, status_code: int,
                   duration_secs: float, tenant_id: str | None) -> None:
    status_class = f"{status_code // 100}xx"
    key = (method, path_template, status_class)
    _REQUEST_COUNTS[key] = _REQUEST_COUNTS.get(key, 0) + 1

    dkey = (method, path_template)
    _REQUEST_DURATION_SUM[dkey] = _REQUEST_DURATION_SUM.get(dkey, 0.0) + duration_secs
    _REQUEST_DURATION_COUNT[dkey] = _REQUEST_DURATION_COUNT.get(dkey, 0) + 1
    for bucket in DURATION_BUCKETS:
        if duration_secs <= bucket:
            bkey = (method, path_template, bucket)
            _REQUEST_DURATION_BUCKETS[bkey] = _REQUEST_DURATION_BUCKETS.get(bkey, 0) + 1
    inf_key = (method, path_template, float("inf"))
    _REQUEST_DURATION_BUCKETS[inf_key] = _REQUEST_DURATION_BUCKETS.get(inf_key, 0) + 1

    if tenant_id:
        _TENANT_REQUEST_COUNTS[tenant_id] = _TENANT_REQUEST_COUNTS.get(tenant_id, 0) + 1


def render_prometheus_text(webhook_queue_depth: int | None = None,
                          db_pool_stats: dict | None = None) -> str:
    lines: list[str] = []

    lines.append("# HELP av_uptime_seconds Seconds since this process started.")
    lines.append("# TYPE av_uptime_seconds gauge")
    lines.append(f"av_uptime_seconds {time.time() - _START_TIME:.3f}")

    lines.append("# HELP av_http_requests_total Total HTTP requests by method, path template, and status class.")
    lines.append("# TYPE av_http_requests_total counter")
    for (method, path, status_class), count in sorted(_REQUEST_COUNTS.items()):
        lines.append(
            f'av_http_requests_total{{method="{method}",path="{path}",status_class="{status_class}"}} {count}'
        )

    lines.append("# HELP av_http_request_duration_seconds Request duration in seconds.")
    lines.append("# TYPE av_http_request_duration_seconds histogram")
    for method, path in sorted(_REQUEST_DURATION_SUM.keys()):
        for bucket in DURATION_BUCKETS:
            count = _REQUEST_DURATION_BUCKETS.get((method, path, bucket), 0)
            lines.append(
                f'av_http_request_duration_seconds_bucket{{method="{method}",path="{path}",le="{bucket}"}} {count}'
            )
        inf_count = _REQUEST_DURATION_BUCKETS.get((method, path, float("inf")), 0)
        lines.append(
            f'av_http_request_duration_seconds_bucket{{method="{method}",path="{path}",le="+Inf"}} {inf_count}'
        )
        lines.append(
            f'av_http_request_duration_seconds_sum{{method="{method}",path="{path}"}} '
            f'{_REQUEST_DURATION_SUM[(method, path)]:.6f}'
        )
        lines.append(
            f'av_http_request_duration_seconds_count{{method="{method}",path="{path}"}} '
            f'{_REQUEST_DURATION_COUNT[(method, path)]}'
        )

    lines.append("# HELP av_requests_by_tenant_total Total requests seen per tenant "
                 "(only recorded once a request's tenant is resolved).")
    lines.append("# TYPE av_requests_by_tenant_total counter")
    for tenant_id, count in sorted(_TENANT_REQUEST_COUNTS.items()):
        lines.append(f'av_requests_by_tenant_total{{tenant_id="{tenant_id}"}} {count}')

    if webhook_queue_depth is not None:
        lines.append("# HELP av_webhook_queue_depth Pending or failed webhook deliveries awaiting retry.")
        lines.append("# TYPE av_webhook_queue_depth gauge")
        lines.append(f"av_webhook_queue_depth {webhook_queue_depth}")

    if db_pool_stats:
        lines.append("# HELP av_db_pool_checked_out Checked-out connections, by pool.")
        lines.append("# TYPE av_db_pool_checked_out gauge")
        for pool_name, stats in db_pool_stats.items():
            lines.append(f'av_db_pool_checked_out{{pool="{pool_name}"}} {stats.get("checked_out", 0)}')

    return "\n".join(lines) + "\n"


def reset() -> None:
    """Test-only: clears every in-process counter so a test can assert exact values
    instead of ever-growing ones from earlier tests in the same session."""
    _REQUEST_COUNTS.clear()
    _REQUEST_DURATION_SUM.clear()
    _REQUEST_DURATION_COUNT.clear()
    _REQUEST_DURATION_BUCKETS.clear()
    _TENANT_REQUEST_COUNTS.clear()
