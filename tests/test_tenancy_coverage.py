"""Static proof that hard multi-tenancy's enforcement point is genuinely global.

v1.3.2: `_enforce_project_tenant` is wired as an app-level FastAPI dependency
(`FastAPI(..., dependencies=[Depends(_enforce_project_tenant)])`), not a per-route one —
two other designs (per-route `dependencies=[]` on every project_id-taking route, and
folding the check into the `require_token` middleware) were tried and rejected first;
see `_enforce_project_tenant`'s own docstring in server.py for why. Because enforcement
is global by construction, there is no per-route "did this one remember the guard?"
question the way `test_audit_coverage.py` has for `_audit(` calls — a route cannot opt
out of a dependency declared on the app/router itself. This file instead proves the
GLOBAL WIRING is real and hasn't silently regressed to a per-route or middleware shape.
"""

import inspect

import pytest

pytest.importorskip("fastapi", reason="server extras not installed")

from python.av_server import server as server_module  # noqa: E402


def test_enforce_project_tenant_is_a_global_app_dependency():
    """Guards against `_enforce_project_tenant` quietly being dropped from the app-level
    `dependencies=[]` list (e.g. during a refactor of the FastAPI(...) constructor call)
    without anything else failing loudly — every route would silently stop being
    tenancy-checked, with no test anywhere else positioned to catch that."""
    dependency_callables = {dep.dependency for dep in server_module.app.router.dependencies}
    assert server_module._enforce_project_tenant in dependency_callables, (
        "_enforce_project_tenant is no longer wired as a global app dependency — "
        "every project_id-taking route just silently lost tenancy enforcement"
    )


def test_enforce_project_tenant_takes_no_eager_db_dependency():
    """The other real bug this design already hit once, live: an earlier draft declared
    `db: AsyncSession = Depends(get_session)` as a parameter, which — because this is now
    a GLOBAL dependency — made FastAPI eagerly open a real DB session on every single
    call to /api/health, silently breaking that route's own documented "DB-free, always
    green" liveness contract. Guards the fix (a session opened by hand, only on the path
    that actually needs one) from regressing back to that shape."""
    sig = inspect.signature(server_module._enforce_project_tenant)
    for name, param in sig.parameters.items():
        assert name != "db", (
            "_enforce_project_tenant declared a `db` parameter again — this is a GLOBAL "
            "dependency now, so FastAPI would eagerly open a DB session for every route "
            "including /api/health, breaking its DB-free liveness contract"
        )


def test_enforce_project_tenant_is_off_by_default():
    """The MINOR-release, byte-identical-when-unconfigured guarantee (VERSIONING.md) —
    guards TENANCY_ENFORCE's own default from silently flipping to on."""
    from python.av_server.database import TENANCY_ENFORCE

    assert TENANCY_ENFORCE is False, (
        "AV_TENANCY_ENFORCE now defaults to on — this must stay off by default per "
        "VERSIONING.md's MINOR-release contract unless AV_TENANCY_ENFORCE=1 is set"
    )
