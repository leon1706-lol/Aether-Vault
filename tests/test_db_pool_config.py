"""V1.6.0 (WS5.8): `AV_DB_POOL_SIZE`/`AV_DB_MAX_OVERFLOW` -- SQLAlchemy's own defaults
(pool_size=5, max_overflow=10) were never overridden, tight for a single-worker uvicorn
process serving many concurrent requests each holding a session for its request's duration.
These check the WIRING (the configured engine's actual pool reflects the constants) rather
than re-testing `int()`/`os.environ.get()` in isolation -- the module reads its env vars at
import time, and by the time any test runs, `python.av_server.database` is already imported
with whatever the process environment held at FIRST import (this suite's conftest.py doesn't
set either var, so the defaults are what's actually exercised here)."""
from python.av_server import database


def test_engine_pool_size_matches_configured_default():
    assert database.DB_POOL_SIZE == 10
    assert database.engine.pool.size() == database.DB_POOL_SIZE


def test_engine_pool_max_overflow_matches_configured_default():
    assert database.DB_MAX_OVERFLOW == 20
    assert database.engine.pool._max_overflow == database.DB_MAX_OVERFLOW


def test_app_engine_shares_same_pool_config_when_using_shared_engine():
    """AV_APP_DATABASE_URL is unset in this test environment, so app_engine IS engine --
    same object, same pool config, by construction."""
    if database.APP_DATABASE_URL is None:
        assert database.app_engine is database.engine
    else:
        assert database.app_engine.pool.size() == database.DB_POOL_SIZE
