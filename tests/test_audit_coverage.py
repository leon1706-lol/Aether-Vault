"""Static proof that every mutating server route is either audited or explicitly exempt.

v1.2.5 (WP-2): before this file, four mutating routes wrote no audit_log row at all
(object upload, GC, the batch-objects existence check, and webhook test-delivery) — a
silent, undetectable gap since nothing failed, the trail was just incomplete. This walks
FastAPI's real route table (no Postgres needed — pure source inspection) and asserts every
POST/PUT/PATCH/DELETE endpoint either calls `_audit(` in its own source or is listed in
`AUDIT_EXEMPT_ROUTES` with a documented reason. New mutating routes fail this test by
default until someone makes that call deliberately — coverage can't silently regress again.
"""

import inspect
import re

import pytest

pytest.importorskip("fastapi", reason="server extras not installed")

from python.av_server import server as server_module  # noqa: E402

_MUTATING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
_CALL_RE = re.compile(r"\b([a-zA-Z_][a-zA-Z0-9_]*)\s*\(")


def _module_functions() -> dict[str, object]:
    return {
        name: obj for name, obj in vars(server_module).items()
        if inspect.isfunction(obj) and getattr(obj, "__module__", None) == server_module.__name__
    }


def _transitive_source(func, funcs_by_name: dict[str, object], depth: int = 2,
                        seen: set | None = None) -> str:
    """The endpoint's own source, plus (one level deep by default) the source of any
    other module-level function it calls by name — thin `return await _helper(...)`
    route wrappers (e.g. complete_run/fail_run -> _finish_run) are the established
    pattern in this file, and a pure single-function text scan would false-positive on
    every one of them."""
    seen = seen if seen is not None else set()
    name = getattr(func, "__name__", None)
    if name in seen:
        return ""
    seen.add(name)
    try:
        source = inspect.getsource(func)
    except (OSError, TypeError):
        return ""
    if depth <= 0:
        return source
    called_names = set(_CALL_RE.findall(source))
    extra = []
    for called in called_names & funcs_by_name.keys():
        extra.append(_transitive_source(funcs_by_name[called], funcs_by_name, depth - 1, seen))
    return source + "\n".join(extra)


def _mutating_routes():
    seen: set[tuple[str, str]] = set()
    for route in server_module.app.routes:
        methods = getattr(route, "methods", None)
        endpoint = getattr(route, "endpoint", None)
        path = getattr(route, "path", None)
        if not methods or endpoint is None or path is None:
            continue
        for method in methods & _MUTATING_METHODS:
            seen.add((method, path, endpoint))
    return seen


def test_every_mutating_route_is_audited_or_explicitly_exempt():
    exempt = server_module.AUDIT_EXEMPT_ROUTES
    funcs_by_name = _module_functions()
    missing = []
    for method, path, endpoint in _mutating_routes():
        if (method, path) in exempt:
            continue
        source = _transitive_source(endpoint, funcs_by_name)
        if not source:
            missing.append((method, path, "source unavailable"))
        elif "_audit(" not in source:
            missing.append((method, path, "no _audit( call (direct or one helper deep) "
                                          "and not in AUDIT_EXEMPT_ROUTES"))
    assert not missing, (
        "Mutating route(s) with no audit trail and no documented exemption:\n"
        + "\n".join(f"  {m} {p} — {why}" for m, p, why in missing)
    )


def test_exempt_routes_still_exist_and_are_actually_mutating():
    """Guards the exemption list itself from going stale (a route renamed/removed should
    fail loudly here, not silently stop being checked at all)."""
    live = {(method, path) for method, path, _ in _mutating_routes()}
    for entry in server_module.AUDIT_EXEMPT_ROUTES:
        assert entry in live, f"AUDIT_EXEMPT_ROUTES has a stale entry: {entry}"
