import asyncio
import contextlib
import hashlib
import itertools
import json
import logging
import os
import re
import secrets
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import Body, Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel
from sqlalchemy import and_, delete, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.middleware.base import BaseHTTPMiddleware

from .database import (
    TENANCY_ENFORCE,
    async_session_factory,
    get_session,
    get_system_session,
    init_db,
    system_session_factory,
)
from . import audit_signing
from . import identity as identity_module
from . import metrics
from . import rate_limit
from .models import (
    DBActionLog,
    DBApiToken,
    DBAuditLog,
    DBBlackboardEntry,
    DBBudget,
    DBCanaryResult,
    DBCausalLink,
    DBChangeSet,
    DBCommit,
    DBCritique,
    DBEvalAdapter,
    DBEvalResult,
    DBEvalSuite,
    DBEvent,
    DBGroup,
    DBGroupMember,
    DBImproverVersion,
    DBLessons,
    DBObject,
    DBPlan,
    DBPolicyPack,
    DBProject,
    DBProjectFreeze,
    DBRef,
    DBReview,
    DBRole,
    DBRoleBinding,
    DBRun,
    DBRunCommit,
    DBSandboxJob,
    DBSsoProvider,
    DBStrategyEntry,
    DBTask,
    DBTenant,
    DBToolManifest,
    DBTree,
    DBUser,
    DBWebhook,
    DBWebhookDelivery,
    DEFAULT_TENANT_ID,
    _new_uuid,
    utcnow_naive,
)
from .redis_cache import cache
from .storage import CASStorage

logger = logging.getLogger("av_server")

# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------

# Webhook delivery retry worker (v1.2.2): failed deliveries persist with a next_retry_at
# and are re-driven on an interval until AV_WEBHOOK_MAX_ATTEMPTS is exhausted → dead-letter.
WEBHOOK_MAX_ATTEMPTS = int(os.environ.get("AV_WEBHOOK_MAX_ATTEMPTS", "5"))
WEBHOOK_RETRY_INTERVAL_SECS = int(os.environ.get("AV_WEBHOOK_RETRY_INTERVAL_SECS", "30"))
# v1.2.5: exponential backoff cap and per-webhook auto-disable threshold.
WEBHOOK_RETRY_MAX_SECS = int(os.environ.get("AV_WEBHOOK_RETRY_MAX_SECS", "3600"))
# 0 = off (default): a webhook never auto-disables regardless of consecutive failures.
WEBHOOK_DISABLE_AFTER = int(os.environ.get("AV_WEBHOOK_DISABLE_AFTER", "0"))
# Terminal-status (delivered/dead) delivery rows are swept with the event retention window.
AUDIT_RETENTION_DAYS = int(os.environ.get("AV_AUDIT_RETENTION_DAYS", "90"))


async def _webhook_retry_worker(interval_secs: float = 30.0):
    """Background loop re-driving due webhook deliveries. Never raises outward — a tick
    that fails logs and retries on the next interval."""
    while True:
        await asyncio.sleep(interval_secs)
        try:
            await process_due_webhook_deliveries()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("webhook retry worker tick failed")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Replaces the deprecated @app.on_event("startup") hook.
    await init_db()
    await cache.init_filter()
    # v1.3.3 (WP-32): generates the server's audit-signing keypair on first boot if
    # AV_AUDIT_SIGNING_KEY_PATH is set and no key exists there yet -- never regenerates
    # over an existing one (see audit_signing.ensure_keypair's own docstring). A no-op,
    # not a startup failure, when the env var is unset or `cryptography` isn't installed.
    try:
        audit_signing.ensure_keypair()
    except Exception:
        logger.exception("audit signing keypair setup failed -- continuing without it")
    worker = asyncio.create_task(_webhook_retry_worker(WEBHOOK_RETRY_INTERVAL_SECS))
    try:
        yield
    finally:
        worker.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await worker


# app = FastAPI(...) itself is constructed further down (right before the middleware
# pipeline registration), not here — v1.3.2's `_enforce_project_tenant` global
# dependency (search for that name) must exist BEFORE the constructor call that
# references it (`dependencies=[Depends(_enforce_project_tenant)]`), and that function
# is defined after the auth/scope machinery it depends on (`_principal`, `require_scope`).
# Nothing between here and that constructor call references the module-level `app` name
# itself (verified: no `@app.` decorator or `app.` attribute access anywhere in between)
# so moving the construction site is safe.

# --- Authentication ("Protected" mode) ------------------------------------------
# Two credential sources, both optional, both read once at process start (matching
# DATA_DIR below: `av auth ...` writes .env and restarts the service, so a fresh process
# always picks up changes — no per-request re-read needed):
#
#   AV_API_TOKEN   the owner's shared secret (legacy single-key mode, still fully valid)
#   AV_AUTH_USERS  JSON map {"username": "token", ...} — per-user access tokens
#                  (managed by `av auth add-user/list-users/remove-user`)
#
# A request is authenticated when its Bearer token matches EITHER source; the resolved
# username ("owner" for the shared secret) is stored on request.state.username and used
# by push_commit to attribute commits whose client sent author="anonymous". Both empty
# = Anonymous mode: every route behaves exactly as it always has — no auth at all.
AV_API_TOKEN = os.environ.get("AV_API_TOKEN", "").strip()

# Always reachable even in Protected mode:
# - /api/health: Docker healthchecks and VaultClient.server_available() depend on this being
#   checkable with no credentials — docker_runtime.restart_service()'s own readiness wait calls
#   server_available(), so gating health would make a freshly-protected server look perpetually
#   unreachable to the very code restarting it.
# - /docs, /openapi.json, /redoc: FastAPI's bundled Swagger/ReDoc UI has no way to attach our
#   custom Bearer header, so gating them would just break the webui's "API Docs" link with no
#   real security benefit — they expose the API's shape, not any actual data.
# v1.2.5: /api/ready joins the exemption list for the same reason as /api/health — a
# readiness probe that itself requires auth to answer "am I ready" is useless to the
# orchestration checking it before the server is known-good.
_AUTH_EXEMPT_PATHS = {"/api/health", "/api/ready", "/docs", "/openapi.json", "/redoc"}


def _installed_version() -> str:
    """v1.2.5: the real installed package version, from ONE source (importlib.metadata)
    instead of a hardcoded literal — server.py:health_check() used to say "1.4.0" while
    av_server/__init__.py separately said "1.0.0" and the CLI's own setuptools-scm-derived
    version was a THIRD, different string; all three could silently drift from the
    actual release. Deliberately NOT importing av_cli here (the server package has never
    depended on it — see the run-summary endpoint's note on the same boundary) —
    importlib.metadata reads the installed DISTRIBUTION's version, which both av_cli and
    av_server ship as part of (one `aether-vault` package, one version, per pyproject.toml)."""
    try:
        from importlib.metadata import version as _pkg_version

        return _pkg_version("aether-vault")
    except Exception:
        return "unknown"


def _parse_auth_users(raw: str | None) -> dict[str, dict]:
    """Parses the AV_AUTH_USERS JSON map. Invalid payloads fail startup loudly — a
    silently ignored auth map would look exactly like Anonymous mode.

    v1.3.0: each value is either a bare token string (unchanged, never expires — the
    original and still-default shape) or an object {"token": "...", "expires_at":
    "<ISO-8601>"} for an optional expiry (`av auth add-user NAME TOKEN --expires-in-days
    N`). Returns {username: {"token": str, "expires_at": str|None}} either way, so every
    downstream reader has one shape to handle.

    v1.3.1: an object value MAY additionally carry "scopes": [str, ...] (`av auth add-user
    NAME TOKEN --scope <s>` repeatable), restricting that token to specific permissions
    (see `require_scope()` below). Deliberately NOT added to every entry unconditionally
    — the returned dict omits the "scopes" key entirely when the raw value didn't specify
    one, so `test_parse_accepts_a_valid_map`'s exact-shape assertion (and every other
    caller comparing this dict's shape) stays byte-for-byte unchanged for every payload
    that predates scopes. Absence is resolved to the unrestricted `["*"]` default by
    `_scopes_for_identity()`, not baked in here.
    """
    if not raw or not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except ValueError as exc:
        raise RuntimeError(f"AV_AUTH_USERS is not valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise RuntimeError("AV_AUTH_USERS must be a JSON object of {username: token}.")
    users: dict[str, dict] = {}
    for name, val in parsed.items():
        name = str(name).strip()
        if isinstance(val, dict):
            tok = str(val.get("token", "")).strip()
            expires_at = val.get("expires_at")
            expires_at = str(expires_at).strip() if expires_at else None
            scopes_raw = val.get("scopes")
        else:
            tok = str(val).strip()
            expires_at = None
            scopes_raw = None
        if not name or not tok:
            raise RuntimeError("AV_AUTH_USERS entries need non-empty username and token.")
        entry = {"token": tok, "expires_at": expires_at}
        if isinstance(scopes_raw, list) and scopes_raw:
            entry["scopes"] = sorted({str(s).strip() for s in scopes_raw if str(s).strip()})
        users[name] = entry
    return users


_AUTH_USERS = _parse_auth_users(os.environ.get("AV_AUTH_USERS"))


def _is_expired(expires_at: str | None) -> bool:
    if not expires_at:
        return False
    try:
        # _parse_iso_dt normalizes to naive UTC (this schema's storage convention) —
        # datetime.utcnow() matches that shape; comparing against an aware now() here
        # would raise (naive vs aware) and get silently swallowed below, making expiry
        # never actually take effect.
        return _parse_iso_dt(expires_at, "expires_at") < datetime.now(timezone.utc).replace(tzinfo=None)
    except Exception:
        return False  # an unparseable expiry fails open on parsing, not on auth


def _resolve_identity(supplied_token: str) -> str | None:
    """Bearer token → username ("owner" for the shared secret), or None when unknown OR
    expired. compare_digest on every candidate — timing-safe even though the map is small.

    Each `_AUTH_USERS` value is normally already normalized to {"token", "expires_at"} by
    `_parse_auth_users()` — but tolerates a bare token string too (both this module's own
    tests and any external code that pokes `_AUTH_USERS` directly, pre-v1.3.0 style, set
    it that way), so this doesn't assume the dict shape unconditionally.
    """
    if AV_API_TOKEN and secrets.compare_digest(supplied_token, AV_API_TOKEN):
        return "owner"  # the shared secret has no expiry concept
    for name, entry in _AUTH_USERS.items():
        token = entry["token"] if isinstance(entry, dict) else entry
        expires_at = entry.get("expires_at") if isinstance(entry, dict) else None
        if secrets.compare_digest(supplied_token, token):
            return None if _is_expired(expires_at) else name
    return None


def _scopes_for_identity(username: str | None) -> list[str]:
    """v1.3.1: scopes for an already-resolved identity — a second, independent step
    from `_resolve_identity()` so that function's return shape (and every existing test
    asserting it) never changes. "owner" (the shared secret, `AV_API_TOKEN`) is always
    unrestricted. A per-user entry's scopes default to `["*"]` when it declared none
    (every legacy/bare-string entry, and any entry created before this feature existed)
    — additive: no existing deployment loses access to anything it could already reach.
    Tolerates `_AUTH_USERS` values monkeypatched as bare strings (pre-v1.3.0/test style),
    not just the normalized dict shape `_parse_auth_users()` produces.
    """
    if username == "owner":
        return ["*"]
    entry = _AUTH_USERS.get(username) if username else None
    if isinstance(entry, dict):
        scopes = entry.get("scopes")
        if isinstance(scopes, list) and scopes:
            return [str(s) for s in scopes]
    return ["*"]


async def _deny_401(request: Request) -> JSONResponse:
    client_host = request.client.host if request.client else "unknown"
    if await _note_auth_failure(f"401:{client_host}"):
        asyncio.create_task(_emit_auth_spike_anomaly(client_host, "unauthenticated"))
    return JSONResponse(status_code=401, content={"detail": "Invalid or missing API token"})


async def require_token(request: Request, call_next):
    if request.url.path in _AUTH_EXEMPT_PATHS:
        return await call_next(request)

    scheme, _, supplied = request.headers.get("authorization", "").partition(" ")
    has_bearer = scheme.lower() == "bearer" and bool(supplied)
    protected_mode = bool(AV_API_TOKEN or _AUTH_USERS)

    if not has_bearer:
        # No credential presented at all: Anonymous mode's exact pre-v1.3.2 behavior —
        # zero extra work, zero DB round trips, request.state untouched. Protected mode
        # keeps rejecting outright, same as always. (No tenancy check either: with no
        # principal at all, `_enforce_project_tenant`'s own global-dependency no-op
        # covers this — this early return just skips the middleware call entirely.)
        return await call_next(request) if not protected_mode else await _deny_401(request)

    # A Bearer token WAS presented — resolve it through every credential source before
    # deciding, in both Anonymous and Protected mode. This is deliberate: v1.3.2's
    # DB-backed `api_tokens`/`sessions` (identity.py) are meant to work on a server that
    # has never touched AV_API_TOKEN/AV_AUTH_USERS at all — `av token create` should not
    # require also flipping the server into `.env`-based Protected mode first. A
    # deployment that has never created a DB token pays nothing extra beyond this one
    # branch: the two DB lookups below only ever run when a Bearer token is actually on
    # the request, and TTL-cache a "not found" result too, so a repeated bad/foreign
    # token doesn't re-query every request either (identity.py::AUTH_CACHE_TTL_SECS).
    identity = _resolve_identity(supplied)
    if identity is not None:
        scopes = _scopes_for_identity(identity)
        request.state.username = identity
        request.state.scopes = scopes
        request.state.principal = identity_module.env_principal(identity, DEFAULT_TENANT_ID, scopes)
        return await call_next(request)

    principal = None
    async with async_session_factory() as db:
        principal = await identity_module.resolve_db_token(db, supplied)
        if principal is None:
            principal = await identity_module.resolve_session(db, supplied)

    if principal is not None:
        request.state.username = principal.username
        request.state.scopes = principal.scopes
        request.state.principal = principal
        return await call_next(request)

    # No source recognized this token. Protected mode rejects, exactly as always.
    # Anonymous mode does NOT reject on an unrecognized token — an unknown/garbage Bearer
    # value must not turn an otherwise-open server into a 401 wall; it behaves exactly as
    # if no credential had been presented (today's Anonymous-mode contract).
    return await _deny_401(request) if protected_mode else await call_next(request)


def require_scope(scope: str):
    """FastAPI dependency factory (v1.3.1): denies a route unless the caller's token
    carries `scope` or the wildcard `"*"`.

    In Anonymous mode — or for any token that resolved with no explicit `scopes` list,
    which by design is every deployment predating this feature — `request.state.scopes`
    is `["*"]` (see `_scopes_for_identity()`), so this is purely additive: nothing that
    could already reach a route loses access by that route later declaring a required
    scope. `request.state.scopes` can only be genuinely absent when Anonymous mode's
    early return in `require_token()` skipped setting any request.state at all — treated
    identically to `["*"]` here, for the same reason.

    A denial here is a 403, never `require_token`'s 401: the caller authenticated fine,
    they just lack this one permission. Recorded as its own `scope.denied` audit action
    (with the required scope and the path) so "who couldn't even log in" stays
    distinguishable from "whose token doesn't cover this."
    """
    async def _dependency(request: Request, db: AsyncSession = Depends(get_session)) -> None:
        scopes = getattr(request.state, "scopes", None) or ["*"]
        if "*" in scopes or scope in scopes:
            return
        identity = getattr(request.state, "username", None)
        _audit(db, identity, "scope.denied", None,
               {"required_scope": scope, "path": request.url.path}, status_code=403)
        if await _note_auth_failure(f"403:{identity or 'unknown'}"):
            await _emit_event(db, None, "anomaly", {
                "type": "auth_spike", "identifier": identity or "unknown",
                "reason": "scope_denied", "threshold": AV_ANOMALY_AUTH_SPIKE_THRESHOLD,
                "window_secs": AV_ANOMALY_AUTH_SPIKE_WINDOW_SECS,
            })
        await db.commit()
        raise HTTPException(
            status_code=403,
            detail={"error": "scope_denied", "required_scope": scope},
        )
    return _dependency


def _principal(request: Request) -> identity_module.Principal:
    """v1.3.2: the resolved Principal for this request (`require_token`'s new third
    state attribute) — every identity source (env token, DB token, session) sets this
    alongside the pre-existing `.username`/`.scopes`. Anonymous mode with no matching
    credential leaves it unset, same as `.username`/`.scopes` — callers use the returned
    anonymous Principal (`tenant_id=None`) rather than crashing on a missing attribute.
    """
    return getattr(request.state, "principal", None) or identity_module.anonymous_principal()


async def _enforce_project_tenant(
    request: Request,
) -> Optional[str]:
    """v1.3.2 (hard multi-tenancy) — the application-layer guard, wired as a GLOBAL
    FastAPI dependency (`app = FastAPI(..., dependencies=[Depends(_enforce_project_tenant)])`
    immediately below) rather than added to ~80 individual route decorators.

    Two designs were tried, in order, before this one:
    1. Per-route `dependencies=[Depends(_enforce_project_tenant)]` on every project_id-
       taking route — rejected once the actual count of such routes (~80, found by
       running the anti-drift sweep test against an empty exempt list) made that many
       hand-edited decorators an error-prone surface a shared mechanism avoids entirely.
    2. Folded into `require_token`'s `BaseHTTPMiddleware` (which already runs for every
       request) — rejected after verifying LIVE (not assumed) that `request.path_params`
       is EMPTY inside `BaseHTTPMiddleware.dispatch()` before `call_next()`: Starlette
       only populates path params once routing actually matches a route, which for a
       `BaseHTTPMiddleware`-wrapped app happens INSIDE `call_next()`, not before it. A
       middleware-based check could only ever see query/body project_id, never a path
       param one (`/api/freeze/{project_id}` and friends) — silently incomplete.

    A global FastAPI dependency is the one shape that is both centralized (zero per-route
    wiring) AND runs at the right point in the stack: FastAPI resolves `dependencies=[]`
    passed to the `FastAPI()`/`APIRouter()` constructor as part of EVERY route's own
    dependency graph, which executes AFTER routing has matched the route and populated
    `request.path_params` — verified live the same way the middleware approach was ruled
    out, not assumed. `require_scope()` already proves the pattern works for
    `request.state` set by the earlier `require_token` middleware; this is the same
    shape, reading `request.state.principal` the same way.

    Postgres row-level security (migration 0013) is the BACKSTOP behind this, not the
    reverse: RLS catches a genuinely missed case (a future refactor that bypasses this
    global dependency somehow); this is what actually shapes the HTTP response (a clean
    403/404) for the normal case — an RLS mismatch alone would otherwise surface as an
    opaque empty result set or a bare integrity failure.

    Gated on `TENANCY_ENFORCE` and a real resolved tenant — genuinely a no-op (zero
    queries attempted) whenever either is absent, matching `database.py`'s own
    `_apply_tenant_guc` no-op contract for the exact same reasons (VERSIONING.md's
    MINOR-release, byte-identical-when-unconfigured guarantee).

    Resolves `project_id` from path → query → JSON body, in that order — all three
    shapes exist across this codebase's project_id-taking routes (a path param on
    `/api/freeze/{project_id}`, a query param on ~15 list endpoints, a JSON body field on
    `push_commit` and most POST/PUT/PATCH routes). `request.json()` is safe to call ahead
    of a route's own `Body(...)` parse — Starlette caches the raw body internally after
    the first read, so this never double-consumes the stream.

    An UNSEEN `project_id` is lazily claimed for the caller's tenant (first writer wins)
    — `project_id` has never been server-side pre-registered (`av init` mints it
    client-side with zero ceremony), so "unknown" cannot mean reject; it means "first
    time this tenant has used it." A project already owned by a DIFFERENT tenant is
    denied: a WRITE gets 403 `tenant_denied` (a write must never silently 404 — the
    caller has to learn its work did not land, or offline-queue semantics would quietly
    lose it, AGENTS.md non-negotiable #3); a READ gets a bare 404 (a 403 would confirm
    the project exists under some tenant, turning the route into a cross-tenant
    enumeration oracle — the same information-hiding tradeoff `/api/tokens/{id}/revoke`
    already applies to a foreign token id).

    Deliberately does NOT take `db: AsyncSession = Depends(get_session)` as a parameter,
    even though every other route dependency in this file does — because this one is
    now GLOBAL, that would make FastAPI eagerly open a real DB session for `/api/health`
    on every single call, silently breaking that route's own documented "DB-free,
    always green" liveness contract (`_AUTH_EXEMPT_PATHS`'s existing rationale; found
    live while wiring this up, not anticipated). A session is opened by hand, via
    `async_session_factory()`, and ONLY on the path that actually needs one — after
    every earlier no-op check (TENANCY_ENFORCE off, no tenant, no project_id) has
    already returned, which every exempt/irrelevant route hits before this point.
    """
    if not TENANCY_ENFORCE:
        return None
    principal = _principal(request)
    tenant_id = principal.tenant_id
    if tenant_id is None:
        return None

    project_id = request.path_params.get("project_id") or request.query_params.get("project_id")
    if project_id is None and request.method in ("POST", "PUT", "PATCH"):
        try:
            body = await request.json()
        except Exception:
            body = None
        if isinstance(body, dict):
            project_id = body.get("project_id")
    if not project_id or not isinstance(project_id, str):
        return None  # route doesn't target a single project (e.g. list_projects)

    async with async_session_factory() as db:
        project = (await db.execute(
            select(DBProject).where(DBProject.id == project_id)
        )).scalar_one_or_none()
        if project is None:
            db.add(DBProject(id=project_id, tenant_id=tenant_id, name=project_id,
                             created_at=utcnow_naive()))
            await db.commit()
            return project_id
        if project.tenant_id != tenant_id:
            is_write = request.method in ("POST", "PUT", "PATCH", "DELETE")
            _audit(db, _identity(request), "tenant.denied", project_id,
                   {"reason": "cross_tenant_project_id", "method": request.method},
                   status_code=403 if is_write else 404)
            await db.commit()
            if is_write:
                raise HTTPException(status_code=403,
                                    detail={"error": "tenant_denied", "project_id": project_id})
            raise HTTPException(status_code=404, detail="Project not found")
        return project_id


app = FastAPI(title="Aether-Vault Server", version="1.4.0", lifespan=lifespan,
             dependencies=[Depends(_enforce_project_tenant)])


# MIDDLEWARE PIPELINE — Starlette runs the LAST-added middleware OUTERMOST, so these
# three registrations ARE the architecture; reorder them and you change what browsers
# and floods experience (Probleme.md #75):
#
#   registration order:  auth  →  CORS  →  rate limit
#   runtime order:       rate  →  CORS  →  auth  →  routes
#
# * CORS must sit OUTSIDE auth: browser preflights are credentialless by spec, and —
#   the subtle part — auth's own 401 JSONResponses need ACAO headers too, or the
#   browser can't even READ the 401 and TokenGate's entry prompt never fires (the webui
#   rendered empty dashboards instead). The original v1.1.x order had auth outside
#   CORS: Anonymous dashboards worked, Protected ones silently broke.
app.add_middleware(BaseHTTPMiddleware, dispatch=require_token)

_CORS_ORIGINS = [
    origin.strip()
    for origin in os.environ.get("AV_CORS_ORIGINS", "http://localhost:3000").split(",")
    if origin.strip()
]
if not _CORS_ORIGINS:
    _CORS_ORIGINS = ["http://localhost:3000"]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_CORS_ORIGINS,
    allow_methods=["GET", "POST", "PUT", "HEAD"],
    allow_headers=["*"],
)


# --- Rate limiting (outermost middleware: floods are rejected before any other work) ---
# Defaults close the one destructive unauthenticated endpoint (GC) while leaving the data
# plane unlimited — legitimate clients burst (8-worker object uploads, thousand-file
# commits) and fixed global caps would false-positive on them. Operators opt the data
# plane in via AV_RATE_LIMIT_DEFAULT; see python/av_server/rate_limit.py.
_RATE_LIMITER = rate_limit.build_limiter_from_env()

# v1.3.2 (HA — E5): AV_RATE_LIMIT_BACKEND=redis switches to a Redis-backed counter
# (rate_limit.RedisWindowRateLimiter) shared across every replica, instead of each
# replica enforcing its own independent in-process window. Default ("memory", or unset)
# is byte-identical to pre-v1.3.2 — this whole block only ever constructs the in-process
# limiter above, which is exactly what every existing test/deployment already gets.
_RATE_LIMIT_BACKEND = os.environ.get("AV_RATE_LIMIT_BACKEND", "memory")
_REDIS_RATE_LIMITER = (
    rate_limit.build_redis_limiter_from_env(cache._client)
    if _RATE_LIMIT_BACKEND == "redis" else None
)


@app.middleware("http")
async def limit_request_rate(request: Request, call_next):
    bucket = rate_limit.bucket_class_for(request.url.path)
    if bucket is None:
        return await call_next(request)
    client = request.client.host if request.client else "unknown"
    retry_after = (
        await _REDIS_RATE_LIMITER.check(client, bucket) if _REDIS_RATE_LIMITER is not None
        else _RATE_LIMITER.check(client, bucket)
    )
    if retry_after is not None:
        return JSONResponse(
            status_code=429,
            content={"detail": f"Rate limit exceeded for '{bucket}' operations"},
            headers={"Retry-After": str(retry_after)},
        )
    return await call_next(request)


# v1.3.3 (WP-35): registered LAST among the four `http` middlewares, which Starlette's
# `add_middleware`/`@app.middleware("http")` both apply in REVERSE registration order —
# this is deliberately the OUTERMOST layer (runs first on the way in, last on the way
# out), so it observes EVERY request end to end: a 429 from the rate limiter above, a
# 401 from `require_token` below, and every real route response, all get timed and
# counted. Adds no response header and never touches the body, so it cannot interact
# with the auth/CORS header ordering this file's own comment already flags as fragile —
# purely read-only bookkeeping around `call_next`.
@app.middleware("http")
async def collect_metrics(request: Request, call_next):
    start = time.monotonic()
    response = await call_next(request)
    duration = time.monotonic() - start

    # Starlette's router sets `scope["route"]` DURING route resolution, which happens
    # inside the `call_next()` call above -- by the time it returns, this is populated
    # for anything that actually reached a route. An early rejection (429/401 before
    # routing) never sets it; falling back to the raw path there is an accepted, bounded
    # cardinality risk (401s cluster on the paths clients actually try, not arbitrary
    # user input) rather than silently mislabeling those requests as some other route.
    route = request.scope.get("route")
    path_template = route.path if route is not None else request.url.path

    principal = getattr(request.state, "principal", None)
    tenant_id = principal.tenant_id if principal is not None else None

    metrics.record_request(request.method, path_template, response.status_code, duration, tenant_id)
    return response


DATA_DIR = Path(os.environ.get("AV_DATA_DIR", "/data"))
storage = CASStorage(DATA_DIR)

# v1.3.3 (WP-21): physical per-tenant CAS object storage separation — OFF by default
# ("shared", byte-identical to every pre-v1.3.3 deployment: one global dedup domain, the
# exact behavior "shared" mode has always had). "isolated" physically separates every
# tenant's objects/trees on disk AND in the Bloom filter, at a real, stated cost:
# cross-tenant content-addressed deduplication is lost entirely (identical bytes held by
# k tenants are stored k times) — intra-tenant dedup, the product's actual headline
# claim, is completely unaffected either way. See development/architecture.md's Tenancy
# Isolation Contract for the full design and why shipping storage separation WITHOUT
# also fixing the existence-check/Bloom-filter/GC-sweep pieces together would have been
# a real data-loss bug (a global "already exists" check silently skipping a second
# tenant's upload) — this switch only ever ships as the complete WP-21 package.
CAS_ISOLATION: str = os.environ.get("AV_CAS_ISOLATION", "shared")
if CAS_ISOLATION not in ("shared", "isolated"):
    raise RuntimeError(f"AV_CAS_ISOLATION must be 'shared' or 'isolated', got {CAS_ISOLATION!r}")


def _cas_tenant_id(request: Request) -> str | None:
    """The tenant_id CAS storage/cache/DB-existence-checks should scope to for THIS
    request — `None` under the default `shared` isolation mode (every call site's
    existing behavior, completely unchanged) or the caller's real tenant (falling back
    to DEFAULT_TENANT_ID, matching every other tenant-resolution call site in this file)
    once an operator opts into `AV_CAS_ISOLATION=isolated`."""
    if CAS_ISOLATION != "isolated":
        return None
    return _principal(request).tenant_id or DEFAULT_TENANT_ID

# --- Request size guards for push_commit (reject hostile/oversized payloads early) ---
MAX_TREE_ENTRIES = 100_000
MAX_METRICS = 1_000
MAX_TAGS = 200
MAX_MESSAGE_LEN = 20_000
MAX_TAG_LEN = 200


class RefUpdate(BaseModel):
    commit_hash: str
    # v1.2.5, optional/additive: when set, update_ref only advances the ref if its
    # CURRENT commit_hash equals expected_hash — compare-and-swap instead of the old
    # unconditional last-write-wins. Omitted (None) preserves exact pre-1.2.5 behavior,
    # so existing clients are unaffected. See architecture.md's Remote Sync Contract.
    expected_hash: Optional[str] = None


# Ref names end up as filesystem paths in the legacy CASStorage fallback
# (refs_dir / ref_name). Because the route uses {ref_name:path}, a raw value like
# "../../etc/passwd" would otherwise escape the data directory (path traversal / LFI).
# Allow only safe, relative, slash-delimited names.
_REF_NAME_RE = re.compile(r"^[A-Za-z0-9._/-]+$")


def validate_ref_name(ref_name: str) -> str:
    if (
        not ref_name
        or not _REF_NAME_RE.match(ref_name)
        or ref_name.startswith("/")
        or "\\" in ref_name
        or ".." in ref_name.split("/")
    ):
        raise HTTPException(status_code=400, detail="Invalid ref name")
    return ref_name


# ---------------------------------------------------------------------------
# Merkle Tree builder
# ---------------------------------------------------------------------------

async def build_merkle_tree(db: AsyncSession, tree_data: Dict[str, Any],
                            cas_tenant_id: str | None = None) -> str:
    """
    Recursively converts the flat path→info dict (from a commit) into a
    content-addressed Merkle Tree stored in DBTree rows.
    Returns the root tree hash.

    `cas_tenant_id` (v1.3.3, WP-21): None under the default `shared` isolation mode —
    the "does this tree already exist" check below stays global, exactly as before.
    Under `AV_CAS_ISOLATION=isolated`, scoped to the caller's own tenant so tenant B
    never skips creating ITS OWN tree rows just because tenant A happens to have
    identical (content-addressed, so identically-hashed) tree content — the same class
    of cross-tenant existence-check bug WP-21's design review caught for objects.
    """
    nodes: Dict[str, Any] = {}
    for path, info in tree_data.items():
        parts = path.split("/", 1)
        name = parts[0]
        if len(parts) == 1:
            nodes[name] = {"is_dir": False, "info": info}
        else:
            if name not in nodes:
                nodes[name] = {"is_dir": True, "children": {}}
            nodes[name]["children"][parts[1]] = info

    entries = []
    for name, node in sorted(nodes.items()):
        if node["is_dir"]:
            child_hash = await build_merkle_tree(db, node["children"], cas_tenant_id)
            entries.append(
                {"name": name, "child_hash": child_hash, "obj_hash": None, "type": "tree", "size": 0}
            )
        else:
            info = node["info"]
            # Support both flat-tree format {"hash":..., "size":..., "type":...}
            # and legacy code/artifacts split format (just a plain hash string).
            if isinstance(info, str):
                info = {"hash": info, "size": 0, "type": "code"}
            entries.append(
                {
                    "name": name,
                    "child_hash": None,
                    "obj_hash": info.get("hash"),
                    "type": info.get("type", "file"),
                    "size": info.get("size", 0),
                    "layers": info.get("layers", []),
                    "chunks": info.get("chunks", []),
                }
            )

    tree_content = json.dumps(entries, sort_keys=True)
    tree_hash = hashlib.sha256(tree_content.encode()).hexdigest()

    tree_exists_stmt = select(DBTree).where(DBTree.tree_hash == tree_hash)
    if cas_tenant_id is not None:
        tree_exists_stmt = tree_exists_stmt.where(DBTree.tenant_id == cas_tenant_id)
    result = await db.execute(tree_exists_stmt.limit(1))
    if not result.first():
        for entry in entries:
            db.add(
                DBTree(
                    tree_hash=tree_hash,
                    path_name=entry["name"],
                    child_tree_hash=entry["child_hash"],
                    object_hash=entry["obj_hash"],
                    type=entry["type"],
                    size=entry["size"],
                    layers=entry.get("layers", []),
                    chunks=entry.get("chunks", []),
                )
            )
        await db.flush()

    return tree_hash


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

@app.get("/api/health")
def health_check() -> dict:
    """Liveness: DB-free, auth-exempt, always answers if the process is up at all. Do
    NOT add any dependency check here — VaultClient.server_available() and every CI
    probe key off this staying reachable even when the server is otherwise unhealthy
    (e.g. an unwritable AV_DATA_DIR — see /api/ready for that check)."""
    return {"status": "ok", "version": _installed_version()}


@app.get("/api/ready")
async def readiness_check(db: AsyncSession = Depends(get_session)) -> Response:
    """v1.2.5: readiness — DB connectivity, Redis reachability, and AV_DATA_DIR
    writability. Targets the failure mode documented as "the most misleading in the
    project" (development/infrastructure.md): /api/health stays green even when
    AV_DATA_DIR is unwritable and every object upload 500s. Auth-exempt for the same
    reason /api/health is (see _AUTH_EXEMPT_PATHS) — an orchestrator checking readiness
    before the server is known-good can't be expected to already hold a valid token.
    200 with `ready: true` when every check passes; 503 with per-check detail otherwise
    — never raises, so a broken check reports itself instead of crashing the probe."""
    checks: dict[str, bool] = {}

    try:
        await db.execute(select(1))
        checks["database"] = True
    except Exception:
        checks["database"] = False

    try:
        # v1.2.5 fix: check_hash_exists() deliberately fails OPEN (returns True) on a
        # Redis error -- correct for its actual caller (an optimistic skip-the-DB check)
        # but means a downed Redis silently read as healthy here. cache.ping() is a raw
        # connectivity probe that does not swallow the error (see RedisCache.ping()).
        await cache.ping()
        checks["redis"] = True
    except Exception:
        checks["redis"] = False

    try:
        probe = storage.base_path / f".ready-probe-{os.getpid()}"
        probe.write_text("ok")
        probe.unlink()
        checks["data_dir_writable"] = True
    except Exception:
        checks["data_dir_writable"] = False

    ready = all(checks.values())
    body = json.dumps({"ready": ready, "checks": checks})
    return Response(content=body, media_type="application/json",
                    status_code=200 if ready else 503)


@app.get("/api/metrics", dependencies=[Depends(require_scope("admin"))])
async def get_metrics(db: AsyncSession = Depends(get_session)) -> Response:
    """v1.3.3 (WP-35): Prometheus text-exposition metrics. `admin`-scoped like every
    other observability surface in this file — a real Prometheus scrape config points
    `bearer_token`/`bearer_token_file` at an admin-scoped token (`av token create ...
    --scope admin`), the same credential an operator already uses for `/api/admin/*`.

    Per-process counters ONLY (see metrics.py's own docstring) — the two numbers below
    (webhook queue depth, DB pool state) are live snapshots at scrape time, not
    counters; everything else is this process's own running totals since it started.
    """
    from .database import app_engine, engine

    try:
        queue_depth = (await db.execute(
            select(func.count()).select_from(DBWebhookDelivery)
            .where(DBWebhookDelivery.status.in_(["pending", "failed"]))
        )).scalar_one()
    except Exception:
        queue_depth = None

    pool_stats = {}
    for name, target_engine in (("primary", engine), ("app", app_engine)):
        if name == "app" and target_engine is engine:
            continue  # AV_APP_DATABASE_URL unset -- app_engine IS engine, don't double-report
        try:
            pool_stats[name] = {"checked_out": target_engine.pool.checkedout()}
        except Exception:
            pass

    body = metrics.render_prometheus_text(webhook_queue_depth=queue_depth, db_pool_stats=pool_stats)
    return Response(content=body, media_type="text/plain; version=0.0.4; charset=utf-8")


# ---------------------------------------------------------------------------
# Objects (CAS blobs)
# ---------------------------------------------------------------------------

@app.post("/api/objects/{hash}")
async def upload_object(
    hash: str, request: Request, db: AsyncSession = Depends(get_session)
) -> Response:
    if not re.match(r"^[a-f0-9]{64}$", hash):
        raise HTTPException(status_code=400, detail="Invalid hash format")

    cas_tenant_id = _cas_tenant_id(request)

    # Fast path: Bloom Filter check before hitting DB
    might_exist = await cache.check_hash_exists(hash, cas_tenant_id)
    if might_exist:
        stmt = select(DBObject).where(DBObject.hash == hash)
        # Isolated mode ONLY: scope the existence check to the caller's own tenant, so
        # tenant B never sees a 409 for content only tenant A has ever uploaded (the
        # exact data-loss shape WP-21's own design review caught -- see CAS_ISOLATION's
        # module comment). Shared mode (default) deliberately does NOT add this filter:
        # a global existence check across every tenant IS what "shared" means, and
        # every pre-v1.3.3 deployment already depends on that for cross-tenant dedup.
        if cas_tenant_id is not None:
            stmt = stmt.where(DBObject.tenant_id == cas_tenant_id)
        result = await db.execute(stmt)
        if result.scalar_one_or_none():
            return Response(status_code=409, content="Object already exists")

    try:
        path = await storage.store_object(hash, request.stream(), cas_tenant_id)
        size = path.stat().st_size
        db.add(DBObject(hash=hash, size=size))
        await db.commit()
        await cache.add_hash(hash, cas_tenant_id)
        return Response(status_code=201)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except IntegrityError:
        # A concurrent upload of the same hash (same tenant, same hash -- the composite
        # PK is (tenant_id, hash)) inserted the row first. CAS is idempotent (identical
        # content), so treat the duplicate as success rather than a 500.
        await db.rollback()
        await cache.add_hash(hash, cas_tenant_id)
        return Response(status_code=409, content="Object already exists")
    except Exception as exc:
        await db.rollback()
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/api/objects/{hash}")
def download_object(hash: str, request: Request) -> StreamingResponse:
    if not re.match(r"^[a-f0-9]{64}$", hash):
        raise HTTPException(status_code=400, detail="Invalid hash format")
    obj_path = storage.get_object_path(hash, _cas_tenant_id(request))
    if not obj_path:
        raise HTTPException(status_code=404, detail="Object not found")

    def iterfile():
        with open(obj_path, mode="rb") as f:
            while chunk := f.read(8 * 1024 * 1024):
                yield chunk

    return StreamingResponse(iterfile(), media_type="application/octet-stream")


@app.head("/api/objects/{hash}")
async def head_object(
    hash: str, request: Request, db: AsyncSession = Depends(get_session)
) -> Response:
    if not re.match(r"^[a-f0-9]{64}$", hash):
        raise HTTPException(status_code=400, detail="Invalid hash format")

    cas_tenant_id = _cas_tenant_id(request)
    might_exist = await cache.check_hash_exists(hash, cas_tenant_id)
    if might_exist:
        stmt = select(DBObject).where(DBObject.hash == hash)
        if cas_tenant_id is not None:
            stmt = stmt.where(DBObject.tenant_id == cas_tenant_id)
        result = await db.execute(stmt)
        obj = result.scalar_one_or_none()
        if obj:
            return Response(status_code=200, headers={"Content-Length": str(obj.size)})
    else:
        # Bloom says no → quick fallback to filesystem for safety
        size = storage.get_object_size(hash, cas_tenant_id)
        if size is not None:
            return Response(status_code=200, headers={"Content-Length": str(size)})

    return Response(status_code=404)


# ---------------------------------------------------------------------------
# Commits
# ---------------------------------------------------------------------------

@app.post("/api/commits")
async def push_commit(
    request: Request, commit_data: Dict[str, Any], db: AsyncSession = Depends(get_session)
) -> Response:
    commit_hash = commit_data.get("hash", "")
    if not re.match(r"^[a-f0-9]{64}$", commit_hash):
        raise HTTPException(status_code=400, detail="Invalid commit hash format")

    result = await db.execute(select(DBCommit).where(DBCommit.hash == commit_hash))
    if result.scalar_one_or_none():
        return Response(status_code=409, content="Commit already exists")

    # Reject oversized/abusive payloads before doing any DB work (the endpoint is otherwise
    # unauthenticated; without bounds a single request could store an unbounded tree/metrics
    # blob). These limits are generous for real ML repos but cap pathological input.
    raw_tree = commit_data.get("tree", {})
    if not isinstance(raw_tree, dict) or len(raw_tree) > MAX_TREE_ENTRIES:
        raise HTTPException(status_code=422, detail="Commit tree too large or malformed")
    metrics = commit_data.get("metrics", {})
    if not isinstance(metrics, dict) or len(metrics) > MAX_METRICS:
        raise HTTPException(status_code=422, detail="Too many metrics or malformed")
    tags = commit_data.get("tags", [])
    if not isinstance(tags, list) or len(tags) > MAX_TAGS or any(
        not isinstance(t, str) or len(t) > MAX_TAG_LEN for t in tags
    ):
        raise HTTPException(status_code=422, detail="Too many/oversized tags or malformed")
    if len(commit_data.get("message", "") or "") > MAX_MESSAGE_LEN:
        raise HTTPException(status_code=422, detail="Commit message too long")

    # Per-project separation: every repo gets a project_id at `av init` (backfilled for repos
    # initialized before this was added — see python/av_cli/main.py's load_config). Fall back
    # to a single "legacy" bucket rather than rejecting the push outright, so an older client
    # that hasn't picked up the backfill yet still syncs instead of erroring.
    project_id = commit_data.get("project_id") or "legacy"
    project_name = commit_data.get("project_name") or "Legacy / Unknown"
    if not isinstance(project_id, str) or len(project_id) > 128 or not isinstance(project_name, str) or len(project_name) > 200:
        raise HTTPException(status_code=422, detail="Invalid project_id/project_name")

    # Support both new flat-tree and legacy {code:{}, artifacts:{}} formats
    if "code" in raw_tree or "artifacts" in raw_tree:
        # Flatten legacy format into unified dict
        flat_tree: Dict[str, Any] = {}
        for path, h in raw_tree.get("code", {}).items():
            flat_tree[path] = {"hash": h, "size": 0, "type": "code"}
        for path, info in raw_tree.get("artifacts", {}).items():
            flat_tree[path] = info
        raw_tree = flat_tree

    # Persist the author-supplied commit time rather than the insert time, otherwise
    # commits flushed late from the client's pending-push queue would sort as "newest"
    # in the dashboard despite being authored earlier. Fall back to now() if absent/invalid.
    commit_ts = None
    raw_ts = commit_data.get("timestamp")
    if raw_ts:
        try:
            parsed = datetime.fromisoformat(raw_ts.replace("Z", "+00:00"))
            # Store naive UTC to stay consistent with the model's utcnow() default column.
            commit_ts = parsed.astimezone(timezone.utc).replace(tzinfo=None) if parsed.tzinfo else parsed
        except (ValueError, TypeError):
            commit_ts = None

    try:
        root_tree_hash = await build_merkle_tree(db, raw_tree, _cas_tenant_id(request))
        parents: List[str] = commit_data.get("parents", [])
        # Merge commits carry parents[1:] in extra_parents (JSON string); parent_hash stays
        # parents[0] for backward compatibility with every existing consumer (webui graph,
        # older clients) that only understands a single parent.
        extra_parents = json.dumps(parents[1:]) if len(parents) > 1 else None
        # Per-user attribution: an authenticated user pushing with the default "anonymous"
        # author gets their username stamped; explicit client-set authors (AV_AUTHOR) are
        # respected — scripts own their attribution. Anonymous mode has no identity.
        author = commit_data.get("author", "anonymous")
        username = getattr(request.state, "username", None)
        if author == "anonymous" and username:
            author = username
        new_commit = DBCommit(
            hash=commit_hash,
            message=commit_data.get("message", ""),
            author=author,
            parent_hash=parents[0] if parents else None,
            extra_parents=extra_parents,
            root_tree_hash=root_tree_hash,
            tags=tags,
            metrics=metrics,
            project_id=project_id,
            project_name=project_name,
        )
        # v1.2.2 signed commits: the client's signature blob rides along verbatim so
        # `av verify` keeps working on cloned/pulled copies, not just in the authoring repo.
        raw_signature = commit_data.get("signature")
        if isinstance(raw_signature, dict):
            new_commit.signature = json.dumps(raw_signature, sort_keys=True)
        # env_snapshot_id is part of the hashed/signed payload — persist it so cloned
        # payloads stay byte-equal to the authoring ones (signature validity + replay).
        env_id = commit_data.get("env_snapshot_id")
        if isinstance(env_id, str) and re.match(r"^[a-f0-9]{64}$", env_id):
            new_commit.env_snapshot_id = env_id
        if commit_ts is not None:
            new_commit.timestamp = commit_ts
        db.add(new_commit)
        await db.flush()

        # --- v1.2.0: run linkage + event + audit -------------------------------
        run_id = commit_data.get("run_id")
        if run_id:
            run_row = (await db.execute(select(DBRun).where(DBRun.id == run_id))).scalar_one_or_none()
            if not run_row:
                # Lazy-create: multi-agent pushes must never fail on ordering — a run the
                # server hasn't seen yet (client registered offline) is created in
                # 'created' state and linked, exactly as if it had been registered first.
                run_row = DBRun(
                    id=run_id, project_id=project_id,
                    created_by=username or author,
                    metrics_summary=dict(metrics) if isinstance(metrics, dict) else {},
                    created_at=utcnow_naive(),
                )
                db.add(run_row)
            else:
                if isinstance(metrics, dict) and metrics:
                    merged = dict(run_row.metrics_summary or {})
                    merged.update(metrics)
                    run_row.metrics_summary = merged
                run_row.updated_at = utcnow_naive()
            # v1.2.2 env snapshot/replay: a commit carrying env_snapshot_id back-fills the
            # linked run's pointer when the run doesn't have one yet (first-link wins).
            env_snapshot_id = commit_data.get("env_snapshot_id")
            if env_snapshot_id and not run_row.env_snapshot_id:
                run_row.env_snapshot_id = str(env_snapshot_id)
            db.add(DBRunCommit(run_id=run_id, commit_hash=commit_hash))

        await _emit_event(db, project_id, "commit", {
            "hash": commit_hash,
            "message": new_commit.message,
            "author": author,
            "run_id": run_id,
        })
        await _detect_commit_anomalies(db, project_id, commit_hash, metrics,
                                       parents[0] if parents else None, root_tree_hash)
        _audit(db, username, "commit.push", project_id,
               {"hash": commit_hash, "message": new_commit.message}, status_code=201)

        await db.commit()
        return Response(status_code=201)
    except IntegrityError as exc:
        await db.rollback()
        # An IntegrityError here is NOT necessarily "this commit hash already exists" — it can
        # equally be a FK violation on DBTree.object_hash (a tree entry references an object
        # that the client hasn't uploaded yet) or on DBRef in a later request. Blindly mapping
        # every IntegrityError to 409 previously caused commits referencing not-yet-uploaded
        # objects to be silently dropped: the client (which treats 409 as idempotent success,
        # by design, for genuine duplicate-hash races) believed the push succeeded while the
        # commit/tree never actually made it into the database. Re-check what actually
        # happened before deciding the response.
        recheck = await db.execute(select(DBCommit).where(DBCommit.hash == commit_hash))
        if recheck.scalar_one_or_none():
            return Response(status_code=409, content="Commit already exists")
        # Anything other than 201/409 is treated as a failed push by the client (it retries /
        # keeps the commit queued) — unlike the bug above, this must NOT be 409.
        raise HTTPException(
            status_code=500,
            detail=(
                "Commit references an object or tree that violates a database constraint "
                f"(commit not stored — are all objects uploaded first?): {exc}"
            ),
        )
    except Exception as exc:
        await db.rollback()
        raise HTTPException(status_code=500, detail=str(exc))


async def resolve_tree(db: AsyncSession, root_hash: str) -> dict:
    """Rebuilds a commit's full file tree from the DB Merkle Tree.

    Level-order traversal with one batched query per depth level (was one query per node, i.e.
    N+1). Because identical subtrees are deduplicated, the same tree_hash can appear under
    several paths, so we carry a list of path prefixes per hash for each level. Factored out of
    `get_commit` (module-level, not nested) so `list_commits`'s `include_layers` option can
    reuse the exact same logic instead of duplicating it.
    """
    tree_data: dict = {}
    frontier: list[tuple[str, str]] = [(root_hash, "")]  # (tree_hash, path_prefix)
    while frontier:
        prefixes_by_hash: Dict[str, List[str]] = {}
        for th, prefix in frontier:
            prefixes_by_hash.setdefault(th, []).append(prefix)

        rows = (
            await db.execute(
                select(DBTree).where(DBTree.tree_hash.in_(list(prefixes_by_hash.keys())))
            )
        ).scalars().all()
        rows_by_hash: Dict[str, list] = {}
        for r in rows:
            rows_by_hash.setdefault(r.tree_hash, []).append(r)

        next_frontier: list[tuple[str, str]] = []
        for th, prefixes in prefixes_by_hash.items():
            for prefix in prefixes:
                for entry in rows_by_hash.get(th, []):
                    full_path = f"{prefix}/{entry.path_name}" if prefix else entry.path_name
                    if entry.child_tree_hash:
                        next_frontier.append((entry.child_tree_hash, full_path))
                    else:
                        tree_data[full_path] = {
                            "hash": entry.object_hash,
                            "size": entry.size,
                            "type": entry.type,
                            "layers": entry.layers or [],
                            "chunks": getattr(entry, "chunks", None) or [],
                        }
        frontier = next_frontier
    return tree_data


def _full_parents(parent_hash: Optional[str], extra_parents_json: Optional[str]) -> List[str]:
    """Reconstructs a commit's complete parents list from the DB columns.

    parent_hash holds parents[0]; merge commits store the rest in extra_parents as a JSON
    array string. Tolerates corrupt/absent JSON (returns just the primary parent) so one
    bad row can't 500 the whole dashboard.
    """
    parents = [parent_hash] if parent_hash else []
    if extra_parents_json:
        try:
            extras = json.loads(extra_parents_json)
            if isinstance(extras, list):
                parents.extend(extras)
        except (ValueError, TypeError):
            pass
    return parents


def _signature_out(raw: Optional[str]) -> Optional[dict]:
    """Decodes a stored signature blob for API responses; corrupt JSON degrades to None
    so one bad row can't 500 commit reads."""
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else None
    except (ValueError, TypeError):
        return None


@app.get("/api/commits/{hash}")
async def get_commit(
    hash: str, db: AsyncSession = Depends(get_session)
) -> dict:
    if not re.match(r"^[a-f0-9]{64}$", hash):
        raise HTTPException(status_code=400, detail="Invalid hash format")
    result = await db.execute(select(DBCommit).where(DBCommit.hash == hash))
    commit = result.scalar_one_or_none()
    if not commit:
        # Fallback: try local CAS file storage (backward compat)
        local = storage.get_commit(hash)
        if local:
            return local
        raise HTTPException(status_code=404, detail="Commit not found")

    tree_data = await resolve_tree(db, commit.root_tree_hash) if commit.root_tree_hash else {}

    return {
        "hash": commit.hash,
        "message": commit.message,
        "author": commit.author,
        "timestamp": commit.timestamp.isoformat() if commit.timestamp else None,
        "parent_hash": commit.parent_hash,
        "parents": _full_parents(commit.parent_hash, commit.extra_parents),
        "root_tree_hash": commit.root_tree_hash,
        "tags": commit.tags or [],
        "metrics": commit.metrics or {},
        "tree": tree_data,
        "project_id": commit.project_id,
        "project_name": commit.project_name,
        "signature": _signature_out(commit.signature),
        "env_snapshot_id": commit.env_snapshot_id,
    }


@app.get("/api/commits/{base_hash}/diff/{target_hash}")
async def get_commit_diff(
    base_hash: str, target_hash: str, db: AsyncSession = Depends(get_session)
) -> dict:
    """v1.3.0 (todo.md item 3): the semantic diff between any two commits, server-side —
    previously the only server-side semantic diff lived inside GET /api/runs/{id}/summary
    (bounded to a run's own linked commits). Feeds the WebUI's arbitrary two-commit
    weight-diff compare. Full semdiff-1.0 schema shape via the same
    _summarize_tree_diff() the run-summary endpoint uses — one implementation, two
    call sites."""
    for h in (base_hash, target_hash):
        if not re.match(r"^[a-f0-9]{64}$", h):
            raise HTTPException(status_code=400, detail="Invalid hash format")

    async def _tree_of(h: str) -> dict:
        result = await db.execute(select(DBCommit).where(DBCommit.hash == h))
        commit = result.scalar_one_or_none()
        if not commit:
            raise HTTPException(status_code=404, detail=f"Commit not found: {h}")
        return await resolve_tree(db, commit.root_tree_hash) if commit.root_tree_hash else {}

    old_tree = await _tree_of(base_hash)
    new_tree = await _tree_of(target_hash)
    summary = _summarize_tree_diff(old_tree, new_tree)
    summary["base"] = base_hash
    summary["target"] = target_hash
    return summary


# ---------------------------------------------------------------------------
# Refs
# ---------------------------------------------------------------------------

@app.put("/api/refs/{ref_name:path}")
async def update_ref(
    ref_name: str, payload: RefUpdate, request: Request,
    db: AsyncSession = Depends(get_session),
) -> dict:
    ref_name = validate_ref_name(ref_name)
    project_id = ref_name.split("/", 1)[0] if "/" in ref_name else None
    # SELECT ... FOR UPDATE serializes concurrent writers on this ref row; the
    # expected_hash check below (v1.2.5) is what makes that serialization meaningful —
    # previously the second writer of a race just silently won (last-write-wins).
    stmt = select(DBRef).where(DBRef.name == ref_name).with_for_update()
    result = await db.execute(stmt)
    ref = result.scalar_one_or_none()
    current_hash = ref.commit_hash if ref else None
    if payload.expected_hash is not None and current_hash != payload.expected_hash:
        _audit(db, _identity(request), "ref.update", project_id,
               {"ref": ref_name, "commit_hash": payload.commit_hash,
                "expected_hash": payload.expected_hash, "current_hash": current_hash},
               status_code=409)
        await db.commit()
        raise HTTPException(
            status_code=409,
            detail={"error": "ref_race", "ref": ref_name, "current": current_hash,
                    "expected": payload.expected_hash},
        )
    if ref:
        ref.commit_hash = payload.commit_hash
    else:
        db.add(DBRef(name=ref_name, commit_hash=payload.commit_hash))
    await _emit_event(db, project_id, "ref", {"ref": ref_name, "commit_hash": payload.commit_hash})
    _audit(db, _identity(request), "ref.update", project_id,
           {"ref": ref_name, "commit_hash": payload.commit_hash}, status_code=200)
    await db.commit()
    return {"status": "updated"}


@app.get("/api/refs/{ref_name:path}")
async def get_ref(
    ref_name: str, db: AsyncSession = Depends(get_session)
) -> dict:
    ref_name = validate_ref_name(ref_name)
    result = await db.execute(select(DBRef).where(DBRef.name == ref_name))
    ref = result.scalar_one_or_none()
    if not ref:
        # Fallback to legacy file-based storage
        commit_hash = storage.get_ref(ref_name)
        if commit_hash:
            return {"ref": ref_name, "commit_hash": commit_hash}
        raise HTTPException(status_code=404, detail="Ref not found")
    return {"ref": ref.name, "commit_hash": ref.commit_hash}


@app.get("/api/refs")
async def list_refs(project_id: Optional[str] = None, db: AsyncSession = Depends(get_session)) -> dict:
    # Refs are namespaced "<project_id>/<branch>" by the client (see av_cli/main.py's
    # `commit` command) rather than via a DB column, since the ref-name path parameter
    # already supports slashes and is already validated — no schema change needed here.
    query = select(DBRef)
    if project_id:
        query = query.where(DBRef.name.like(f"{project_id}/%"))
    result = await db.execute(query)
    refs = result.scalars().all()
    if refs:
        return {r.name: r.commit_hash for r in refs}
    if project_id:
        return {}
    # Fallback to legacy storage
    return storage.list_refs()


# ---------------------------------------------------------------------------
# Stats (legacy endpoint preserved)
# ---------------------------------------------------------------------------

@app.get("/api/stats")
async def get_stats(db: AsyncSession = Depends(get_session)) -> dict:
    # Previously this walked the entire CAS objects directory and stat()ed every shard on
    # every call — and the Web UI polls it every ~15s. Use indexed DB aggregates instead;
    # fall back to the filesystem only when the DB has no objects yet (legacy/empty state).
    total_objects = (await db.execute(select(func.count(DBObject.hash)))).scalar_one()
    if total_objects == 0:
        return storage.get_storage_stats()

    total_size = (await db.execute(select(func.sum(DBObject.size)))).scalar_one() or 0
    total_commits = (await db.execute(select(func.count(DBCommit.hash)))).scalar_one()
    total_refs = (await db.execute(select(func.count(DBRef.name)))).scalar_one()
    return {
        "total_objects": total_objects,
        "total_commits": total_commits,
        "total_refs": total_refs,
        "total_size_bytes": total_size,
    }


# ---------------------------------------------------------------------------
# Garbage Collection
# ---------------------------------------------------------------------------

# Objects newer than this many seconds are never collected, even if no commit references
# them yet. A client uploads object shards first and pushes the commit afterwards, so a GC
# running inside that window would otherwise delete a live object whose commit is still
# in-flight. This grace period closes that race without needing a global GC/upload lock.
# Env-overridable (AV_GC_GRACE_SECONDS, integer) so ops — and the e2e suite — can shrink
# it for drills; defaults to the production hour.
GC_GRACE_SECONDS = int(os.environ.get("AV_GC_GRACE_SECONDS", "3600"))

# Delete in batches to stay well under driver bind-parameter limits (asyncpg ~32k).
_GC_DELETE_BATCH = 500


def _collect_alive_in_memory(
    root_hash: Optional[str], tree_map: Dict[str, list], visited: set, alive: set
) -> None:
    """Iteratively mark every object/layer/chunk hash reachable from a root tree as alive.

    Operates over a pre-loaded {tree_hash: [entries]} map, so the whole GC mark phase costs
    a single DBTree query instead of one query per tree node (was N+1 and recursive).
    """
    stack = [root_hash]
    while stack:
        th = stack.pop()
        if not th or th in visited:
            continue
        visited.add(th)
        for entry in tree_map.get(th, []):
            if entry.child_tree_hash:
                stack.append(entry.child_tree_hash)
            if entry.object_hash:
                alive.add(entry.object_hash)
            if entry.layers:
                for layer in entry.layers:
                    if isinstance(layer, dict) and "hash" in layer:
                        alive.add(layer["hash"])
            # CDC chunk shards (opaque .pt/.pth/.ckpt checkpoints) live as their own objects,
            # exactly like safetensors layer shards — unmarked here, GC would reap the pieces
            # a chunked checkpoint needs to reassemble.
            chunks = getattr(entry, "chunks", None) or []
            for chunk in chunks:
                if isinstance(chunk, dict) and "hash" in chunk:
                    alive.add(chunk["hash"])



@app.post("/api/admin/gc", dependencies=[Depends(require_scope("admin"))])
async def run_garbage_collection(request: Request, db: AsyncSession = Depends(get_system_session)) -> dict:
    """
    Mark-and-sweep GC:
    1. Walk every commit's Merkle Tree to collect live hashes.
    2. Delete orphaned DBObject rows and physical shard files (respecting a grace period
       so concurrently-uploaded-but-not-yet-committed objects are not reaped).
    3. Delete DBTree rows for trees no longer referenced.
    4. Rebuild the Redis Bloom Filter from surviving hashes.
    """
    import asyncio
    from datetime import timedelta

    try:
        gc_cutoff = utcnow_naive() - timedelta(seconds=GC_GRACE_SECONDS)

        # --- Mark phase: PER-TENANT trees/marks always (v1.3.3, WP-21) — a single query
        # shape that serves both isolation modes without two divergent implementations.
        # `alive_hashes`/`visited_trees` (the union across every tenant) are what SHARED
        # mode's dead-computation and the LEGACY flat-directory sweep use below —
        # mathematically identical to this route's pre-v1.3.3 flat computation, because
        # each tenant's own commits only ever reference trees THAT SAME tenant fully
        # wrote (the single-materialization-path invariant guarantees a referenced tree
        # is never partially written) — merging per-tenant sets back into one flat set
        # loses nothing a truly-flat walk would have found.
        all_trees = (await db.execute(select(DBTree))).scalars().all()
        trees_by_tenant: Dict[str, Dict[str, list]] = {}
        for entry in all_trees:
            trees_by_tenant.setdefault(entry.tenant_id, {}).setdefault(entry.tree_hash, []).append(entry)

        alive_by_tenant: Dict[str, set] = {}
        visited_by_tenant: Dict[str, set] = {}
        for commit in (await db.execute(select(DBCommit))).scalars().all():
            t_alive = alive_by_tenant.setdefault(commit.tenant_id, set())
            t_visited = visited_by_tenant.setdefault(commit.tenant_id, set())
            _collect_alive_in_memory(commit.root_tree_hash, trees_by_tenant.get(commit.tenant_id, {}),
                                     t_visited, t_alive)

        alive_hashes: set = set().union(*alive_by_tenant.values()) if alive_by_tenant else set()
        visited_trees: set = set().union(*visited_by_tenant.values()) if visited_by_tenant else set()
        all_tree_hashes = {th for tenant_map in trees_by_tenant.values() for th in tenant_map}

        # --- Sweep DB objects (protect recently-created rows via grace period) ---
        obj_rows = (await db.execute(select(DBObject.tenant_id, DBObject.hash, DBObject.created_at))).all()
        if CAS_ISOLATION == "isolated":
            # Tenant-scoped: a row is dead only if ITS OWN tenant's commit history no
            # longer references it. Using the flat union here would be WRONG under
            # isolated mode specifically -- see this route's own module-level design
            # note (CAS_ISOLATION) for the cross-tenant scenario this would otherwise
            # mishandle. Safe under isolated mode precisely because uploads/dedup never
            # cross the tenant boundary there, so each row's tenant_id is authoritative.
            dead_pairs = {
                (t, h) for (t, h, created_at) in obj_rows
                if h not in alive_by_tenant.get(t, set()) and (created_at is None or created_at < gc_cutoff)
            }
            dead_pairs_list = list(dead_pairs)
            for i in range(0, len(dead_pairs_list), _GC_DELETE_BATCH):
                batch = dead_pairs_list[i : i + _GC_DELETE_BATCH]
                for t, h in batch:
                    await db.execute(delete(DBObject).where(DBObject.tenant_id == t, DBObject.hash == h))
        else:
            # Shared mode (default): a hash is dead only if NO tenant's commit history
            # references it anywhere -- the flat union, exactly this route's pre-v1.3.3
            # behavior. A per-tenant check here would be wrong: in shared mode, tenant B
            # can reference an object whose ONE DBObject row happens to carry tenant A's
            # id (A uploaded it first; B's identical-content upload was correctly
            # rejected as a duplicate) -- checking only against A's own alive set could
            # delete a row B still needs.
            dead_hashes = {
                h for (_t, h, created_at) in obj_rows
                if h not in alive_hashes and (created_at is None or created_at < gc_cutoff)
            }
            dead_list = list(dead_hashes)
            for i in range(0, len(dead_list), _GC_DELETE_BATCH):
                batch = dead_list[i : i + _GC_DELETE_BATCH]
                await db.execute(delete(DBObject).where(DBObject.hash.in_(batch)))

        if visited_trees:
            dead_trees = [th for th in all_tree_hashes if th not in visited_trees]
            for i in range(0, len(dead_trees), _GC_DELETE_BATCH):
                batch = dead_trees[i : i + _GC_DELETE_BATCH]
                await db.execute(delete(DBTree).where(DBTree.tree_hash.in_(batch)))

        # --- Sweep physical shard files (skip alive + recently-written, off the event loop) ---
        loop = asyncio.get_running_loop()
        # gc_cutoff is a *naive* datetime that represents UTC (see utcnow_naive()'s docstring).
        # Calling .timestamp() directly on a naive datetime makes Python treat it as *local*
        # time, silently shifting the resulting epoch by the host's UTC offset — on a host
        # ahead of UTC this makes grace_ts artificially too early, so st_mtime (a real,
        # correctly-UTC-based epoch) almost never looks "old enough" and physical shards are
        # never actually swept; on a host behind UTC it would do the opposite and delete
        # objects *before* their real grace window expires. Attaching tzinfo=utc first makes
        # .timestamp() compute the correct epoch regardless of the host's local timezone.
        grace_ts = gc_cutoff.replace(tzinfo=timezone.utc).timestamp()

        def purge_orphans():
            count = 0
            # Legacy flat layout (objects_dir/xx/yyyy...) -- exists in BOTH modes (an
            # isolated deployment can still be serving objects uploaded before it
            # switched, per storage.py's own fallback-read design) and is always swept
            # against the FLAT union, matching how content there was originally written.
            for obj_path in storage.objects_dir.glob("*/*"):
                if obj_path.is_file():
                    h = obj_path.parent.name + obj_path.name
                    if h in alive_hashes:
                        continue
                    if obj_path.stat().st_mtime >= grace_ts:
                        continue
                    obj_path.unlink()
                    count += 1
            # Tenant-scoped layout (objects_dir/<tenant_id>/xx/yyyy...) -- only ever
            # populated under isolated mode, but the walk is harmless (finds nothing) if
            # a deployment has never used it. `*/*/*` requires exactly 3 path segments
            # under objects_dir, which the flat layout's own `*/*` glob above can never
            # match (2 segments) -- the two globs are naturally disjoint, no double-sweep.
            for obj_path in storage.objects_dir.glob("*/*/*"):
                if not obj_path.is_file():
                    continue
                tenant_id = obj_path.parents[1].name
                h = obj_path.parent.name + obj_path.name
                if h in alive_by_tenant.get(tenant_id, set()):
                    continue
                if obj_path.stat().st_mtime >= grace_ts:
                    continue
                obj_path.unlink()
                count += 1
            return count

        deleted_count = await loop.run_in_executor(None, purge_orphans)

        # Rebuild the Bloom Filter(s) from the surviving set(s). Shared mode: just the
        # global filter, from the flat union (unchanged pre-v1.3.3 behavior). Isolated
        # mode: the global filter too (still needed for the legacy flat directory) PLUS
        # each tenant's own filter, from that tenant's own alive set.
        await cache.reset_filter()
        await cache.init_filter()
        if CAS_ISOLATION == "isolated":
            for tenant_id, tenant_alive in alive_by_tenant.items():
                await cache.reset_filter(tenant_id)
                await cache.init_filter(tenant_id)
                for h in tenant_alive:
                    await cache.add_hash(h, tenant_id)
        for h in alive_hashes:
            await cache.add_hash(h)

        # Retention sweeps for the autonomous-loop surfaces:
        # - events (default 30 days, AV_EVENT_RETENTION_DAYS)
        # - audit_log (default 90 days, AV_AUDIT_RETENTION_DAYS)
        # - terminal-status webhook deliveries (delivered/dead) ride the event window;
        #   stuck pending/failed rows are never swept here — the retry worker owns them.
        from datetime import timedelta as _td

        event_cutoff = utcnow_naive() - _td(days=EVENT_RETENTION_DAYS)
        await db.execute(delete(DBEvent).where(DBEvent.ts < event_cutoff))

        audit_cutoff = utcnow_naive() - _td(days=AUDIT_RETENTION_DAYS)
        await db.execute(delete(DBAuditLog).where(DBAuditLog.ts < audit_cutoff))

        await db.execute(
            delete(DBWebhookDelivery).where(
                DBWebhookDelivery.status.in_(["delivered", "dead"]),
                DBWebhookDelivery.updated_at < event_cutoff,
            )
        )

        await db.commit()
        await _emit_event(db, None, "gc", {
            "deleted_objects": deleted_count,
            "alive_objects": len(alive_hashes),
            "reused_trees": len(visited_trees),
        })
        # v1.2.5: GC is destructive (permanently deletes objects/trees) and was the most
        # notable audit-coverage gap — closing it per the WP-2 coverage matrix.
        _audit(db, _identity(request), "admin.gc", None, {
            "deleted_objects": deleted_count,
            "alive_objects": len(alive_hashes),
            "reused_trees": len(visited_trees),
        }, status_code=200)
        await db.commit()
        return {
            "status": "success",
            "alive_objects": len(alive_hashes),
            "deleted_objects": deleted_count,
            "reused_trees": len(visited_trees),
        }
    except Exception as exc:
        await db.rollback()
        raise HTTPException(status_code=500, detail=str(exc))

@app.get("/api/sync/refs")
async def sync_refs(limit: int = 1000, offset: int = 0, db: AsyncSession = Depends(get_session)):
    """Endpoint for remote teams to pull branch references with pagination."""
    result = await db.execute(select(DBRef).limit(limit).offset(offset))
    refs = result.scalars().all()
    return {
        "timestamp": utcnow_naive().isoformat(),
        "refs": {r.name: r.commit_hash for r in refs},
        "next_offset": offset + limit if len(refs) == limit else None
    }

@app.post("/api/sync/batch-objects")
async def check_objects_batch(hashes: List[str], request: Request, db: AsyncSession = Depends(get_session)):
    """Check existence of multiple objects at once for faster synchronization."""
    found = []
    definitely_missing = []
    might_exist = []
    cas_tenant_id = _cas_tenant_id(request)

    for h in hashes:
        if await cache.check_hash_exists(h, cas_tenant_id):
            might_exist.append(h)
        else:
            definitely_missing.append(h)

    if might_exist:
        stmt = select(DBObject.hash).where(DBObject.hash.in_(might_exist))
        # Same shared-vs-isolated distinction as upload_object/head_object above --
        # shared mode's global batch existence check is deliberately unfiltered.
        if cas_tenant_id is not None:
            stmt = stmt.where(DBObject.tenant_id == cas_tenant_id)
        result = await db.execute(stmt)
        db_found = list(result.scalars().all())
    else:
        db_found = []
        
    for h in db_found:
        found.append(h)
        
    db_found_set = set(db_found)
    actually_missing = definitely_missing + [h for h in might_exist if h not in db_found_set]
    
    return {
        "found": found,
        "missing": actually_missing
    }


# ---------------------------------------------------------------------------
# Web UI Dashboard Endpoints
# ---------------------------------------------------------------------------

@app.get("/api/commits")
async def list_commits(
    request: Request,
    limit: int = 50,
    offset: int = 0,
    project_id: Optional[str] = None,
    include_layers: bool = False,
    db: AsyncSession = Depends(get_session)
) -> dict:
    """Paginated commit list for the Web UI dashboard, newest first.

    Optionally scoped to a single project via ?project_id= — without it, commits from every
    project on this shared registry are returned (matches the dashboard's pre-existing
    behavior so it doesn't break for callers that don't know about projects yet).

    v1.3.2: when TENANCY_ENFORCE is on, "every project" above narrows to "every project
    THE CALLER'S TENANT owns". Filtered explicitly here — NOT left to RLS (migration
    0013) alone — because this repo's own default docker-compose.yml connects as
    `av_user`, which Postgres auto-creates as a SUPERUSER (the official postgres image's
    POSTGRES_USER behavior); superusers unconditionally bypass row-level security, and
    no `FORCE ROW LEVEL SECURITY` can override that (found live: a real two-tenant test
    against this exact deployment topology, not a doc reference — RLS's own policy was
    confirmed correctly defined and forced, and still did not filter). RLS remains real
    defense-in-depth for any deployment that connects as a genuinely non-superuser role,
    and still fully backstops every route with an explicit single `project_id` target
    (`_enforce_project_tenant`'s own write/read checks, unaffected by this since those
    denials happen in application code before RLS is ever relevant) — but an UNFILTERED
    list route is exactly the shape that had no other protection under this topology,
    so it gets one here directly, matching `list_projects`'s own fix.

    ?include_layers=true additionally resolves each returned commit's full tree (same shape
    GET /api/commits/{hash} already returns) in this single response — added specifically to
    replace WeightDiffPanel.tsx's old N-parallel-requests pattern (one GET /api/commits/{hash}
    per candidate checkpoint) with one round trip. Trees are resolved sequentially here (NOT
    via asyncio.gather) — get_session() hands out one AsyncSession per request, backed by a
    single underlying connection, and concurrent queries on the same connection aren't safe
    (asyncpg raises "another operation is in progress"). The win this endpoint provides is
    collapsing N HTTP round trips into one; resolve_tree() itself already eliminated the
    expensive per-node N+1 *within* a single tree, which is the part that actually scales with
    tree size — sequential-but-one-request is still a large improvement over N full requests.
    """
    query = select(DBCommit)
    count_query = select(func.count(DBCommit.hash))
    if project_id:
        query = query.where(DBCommit.project_id == project_id)
        count_query = count_query.where(DBCommit.project_id == project_id)
    if TENANCY_ENFORCE:
        caller_tenant = _principal(request).tenant_id
        if caller_tenant is not None:
            query = query.where(DBCommit.tenant_id == caller_tenant)
            count_query = count_query.where(DBCommit.tenant_id == caller_tenant)

    result = await db.execute(query.order_by(DBCommit.timestamp.desc()).limit(limit).offset(offset))
    commits = result.scalars().all()
    total_result = await db.execute(count_query)
    total = total_result.scalar_one()

    commit_dicts = []
    for c in commits:
        d = {
            "hash": c.hash,
            "message": c.message,
            "author": c.author,
            "timestamp": c.timestamp.isoformat() if c.timestamp else None,
            "parent_hash": c.parent_hash,
            "parents": _full_parents(c.parent_hash, c.extra_parents),
            "root_tree_hash": c.root_tree_hash,
            "tags": c.tags or [],
            "metrics": c.metrics or {},
            "project_id": c.project_id,
            "project_name": c.project_name,
            "signature": _signature_out(c.signature),
            "env_snapshot_id": c.env_snapshot_id,
        }
        if include_layers:
            d["tree"] = await resolve_tree(db, c.root_tree_hash) if c.root_tree_hash else {}
        commit_dicts.append(d)

    return {
        "commits": commit_dicts,
        "total": total,
        "limit": limit,
        "offset": offset,
        "next_offset": offset + limit if offset + limit < total else None,
    }


@app.get("/api/projects")
async def list_projects(request: Request, db: AsyncSession = Depends(get_session)) -> dict:
    """Every project that has ever pushed a commit to this registry, for the Web UI's
    Projects tab (lets a user discover and switch between local repos sharing this server).

    v1.3.2: a full-table enumeration with no single `project_id` to check ownership of —
    exactly the shape `_enforce_project_tenant`'s docstring calls out as a legitimate
    no-op (`return None  # route doesn't target a single project`), so it needs its own
    explicit filter, applied directly below.

    **This is NOT covered by RLS alone in this repo's own default deployment — verified
    live, not assumed, and the assumption it WOULD be is exactly what a first draft of
    this comment claimed before that live test caught it being wrong.** RLS (migration
    0013) is correctly enabled, forced, and its policy correctly defined — confirmed
    directly via `pg_class.relrowsecurity`/`relforcerowsecurity` and the rendered policy
    expression — but Postgres unconditionally exempts SUPERUSERS from row-level security,
    and no `FORCE ROW LEVEL SECURITY` can override that exemption. `docker-compose.yml`'s
    `av_user` IS a superuser (the official `postgres` image auto-grants superuser to
    whatever `POSTGRES_USER` names), so RLS is currently INERT for every query this app
    issues under this repo's own shipped default topology. RLS still has real value as
    defense-in-depth for any deployment that connects as a genuinely non-superuser role
    (a real, common production pattern — e.g. a managed Postgres whose app user is
    deliberately unprivileged) — but every list route in THIS deployment needs its own
    explicit tenant filter to be correct, the same way this one and `list_commits` now
    have. Flagged in this phase's own docs as a residual item: the remaining unfiltered
    list routes this phase did not individually touch (`GET /api/runs`, `GET /api/events`,
    and others) are NOT currently tenant-filtered under this topology, and a genuine fix
    — connecting as a dedicated non-superuser role — is an infrastructure change, not a
    migration, and is called out explicitly rather than silently left unfixed."""
    stmt = (
        select(
            DBCommit.project_id,
            DBCommit.project_name,
            func.count(DBCommit.hash).label("commit_count"),
            func.max(DBCommit.timestamp).label("last_push"),
        ).group_by(DBCommit.project_id, DBCommit.project_name)
        .order_by(func.max(DBCommit.timestamp).desc())
    )
    if TENANCY_ENFORCE:
        principal = _principal(request)
        if principal.tenant_id is not None:
            stmt = stmt.where(DBCommit.tenant_id == principal.tenant_id)
    result = await db.execute(stmt)
    return {
        "projects": [
            {
                "project_id": row.project_id,
                "project_name": row.project_name,
                "commit_count": row.commit_count,
                "last_push": row.last_push.isoformat() if row.last_push else None,
            }
            for row in result.all()
        ]
    }


@app.get("/api/dashboard/summary")
async def dashboard_summary(db: AsyncSession = Depends(get_session)) -> dict:
    """Unified summary endpoint for the Web UI dashboard home page."""
    # Commits
    commit_result = await db.execute(
        select(DBCommit).order_by(DBCommit.timestamp.desc()).limit(50)
    )
    commits = commit_result.scalars().all()

    total_commits_result = await db.execute(select(func.count(DBCommit.hash)))
    total_commits = total_commits_result.scalar_one()

    # Refs
    ref_result = await db.execute(select(DBRef))
    refs = ref_result.scalars().all()

    # Objects count and size
    total_objects_result = await db.execute(select(func.count(DBObject.hash)))
    total_objects = total_objects_result.scalar_one()
    
    total_size_result = await db.execute(select(func.sum(DBObject.size)))
    total_size = total_size_result.scalar_one() or 0

    return {
        "server_version": "1.4.0",
        "total_commits": total_commits,
        "total_branches": len(refs),
        "total_objects": total_objects,
        "total_size_bytes": total_size,
        "refs": {r.name: r.commit_hash for r in refs},
        "recent_commits": [
            {
                "hash": c.hash,
                "message": c.message,
                "author": c.author,
                "timestamp": c.timestamp.isoformat() if c.timestamp else None,
                "parent_hash": c.parent_hash,
                "root_tree_hash": c.root_tree_hash,
                "tags": c.tags or [],
                "metrics": c.metrics or {},
            }
            for c in commits[:20]
        ],
    }


# ---------------------------------------------------------------------------
# Autonomous-loop surface (v1.2.0): runs, event stream, webhooks, audit
# ---------------------------------------------------------------------------
# Design notes:
# * Events are an append-only feed whose autoincrement id IS the resumable cursor
#   (?since=<id> returns strictly newer rows, ascending). Orchestrators long-poll with
#   wait=<secs> instead of hot-looping.
# * Webhooks are signed HMAC-SHA256 over the raw JSON body; the signing secret lives in
#   this database by necessity (deliveries must be signed) and is never returned.
# * Runs are first-class (see models.DBRun); commits link to runs at push time via the
#   payload's optional run_id, lazily creating unknown runs so multi-agent pushes never
#   fail on ordering.

from fastapi import Body  # noqa: E402

import hashlib  # noqa: E402
import hmac as hmac_mod  # noqa: E402

EVENT_RETENTION_DAYS = int(os.environ.get("AV_EVENT_RETENTION_DAYS", "30"))
_WEBHOOK_TIMEOUT_SECS = 10

# --- v1.3.1 RSI R6 (todo.md I.38, WP-36): server-side anomaly detectors ------------
#
# Each detector emits its own `kind="anomaly"` event (payload always carries a `"type"`
# discriminator) ALONGSIDE whatever event the mutation already emits — a dedicated,
# low-noise feed a monitoring webhook can subscribe to by kind alone, without filtering
# the full event stream itself. No new delivery path: `_emit_event()` already fans every
# event out to active webhooks (see its own docstring above); anomalies ride the exact
# same mechanism as `commit`/`policy`/`run` events.
AV_ANOMALY_METRIC_JUMP_RATIO = float(os.environ.get("AV_ANOMALY_METRIC_JUMP_RATIO", "3.0"))
AV_ANOMALY_MASS_REWRITE_FILES = int(os.environ.get("AV_ANOMALY_MASS_REWRITE_FILES", "200"))
AV_ANOMALY_AUTH_SPIKE_THRESHOLD = int(os.environ.get("AV_ANOMALY_AUTH_SPIKE_THRESHOLD", "5"))
AV_ANOMALY_AUTH_SPIKE_WINDOW_SECS = float(os.environ.get("AV_ANOMALY_AUTH_SPIKE_WINDOW_SECS", "60"))

# In-process sliding window of recent auth failures, keyed by client identifier (resolved
# username for a scope denial; client host for an unauthenticated 401, where no identity
# exists yet). Intentionally NOT Redis-backed: this is a single-process best-effort
# signal ("this process just saw a burst"), not a durable security record — the audit
# log (`_audit()`) already IS the durable record of every individual denial; this only
# decides when a BURST of them is itself worth a dedicated anomaly event. Resets after
# tripping so one burst raises exactly one anomaly, not one per subsequent failure.
_AUTH_FAILURE_WINDOW: dict[str, list[float]] = {}


# v1.3.2 (HA — E5): AV_AUTH_SPIKE_BACKEND=redis makes the burst count itself accurate
# across N replicas, for operators who want that specifically — the in-process default
# above remains the deliberate choice for everyone else (its own comment's reasoning —
# "a single-process best-effort signal, not a durable security record" — still holds;
# this is the lowest-severity of the three cross-replica gaps this phase found, since an
# under-counted burst degrades detection sensitivity, it does not cause wrong behavior
# the way the webhook-duplicate-delivery bug did). Same Lua INCR+EXPIRE primitive and
# fail-open posture as the rate limiter (rate_limit.py), reused rather than reinvented.
_AUTH_SPIKE_BACKEND = os.environ.get("AV_AUTH_SPIKE_BACKEND", "memory")


async def _note_auth_failure(key: str) -> bool:
    """Records one auth failure for `key`; returns True the moment this failure pushes
    the recent count (within the window) over the threshold — the caller emits an
    anomaly exactly then, and only then.

    Now async (was sync) — both call sites already `await` it unconditionally, so the
    default (in-process) path pays one coroutine-scheduling hop it didn't before; the
    in-process branch itself does zero actual I/O either way, so this is not a
    meaningfully different cost, and keeping ONE call shape for both backends (rather
    than a sync/async split by backend) is simpler and less error-prone than the
    alternative."""
    if _AUTH_SPIKE_BACKEND == "redis":
        try:
            redis_key = f"av:authfail:{key}"
            count = await cache._client.eval(
                rate_limit._INCR_AND_EXPIRE_LUA, 1, redis_key,
                int(AV_ANOMALY_AUTH_SPIKE_WINDOW_SECS),
            )
        except Exception:
            return False  # fail open — a missed anomaly beats a 500 on every login
        if count >= AV_ANOMALY_AUTH_SPIKE_THRESHOLD:
            try:
                await cache._client.delete(redis_key)  # mirrors window.clear() below
            except Exception:
                pass
            return True
        return False

    import time as _time

    now = _time.monotonic()
    window = _AUTH_FAILURE_WINDOW.setdefault(key, [])
    window[:] = [t for t in window if now - t < AV_ANOMALY_AUTH_SPIKE_WINDOW_SECS]
    window.append(now)
    if len(window) >= AV_ANOMALY_AUTH_SPIKE_THRESHOLD:
        window.clear()
        return True
    return False


async def _emit_auth_spike_anomaly(identifier: str, reason: str) -> None:
    """Called from `require_token`'s 401 branch (raw ASGI middleware, no `db` dependency
    injected) — opens its own short-lived session, same fire-and-forget shape as
    `_emit_event()`'s own webhook-delivery task, so a burst of bad tokens never adds
    latency to the 401 response itself."""
    try:
        async with async_session_factory() as session:
            await _emit_event(session, None, "anomaly", {
                "type": "auth_spike", "identifier": identifier, "reason": reason,
                "threshold": AV_ANOMALY_AUTH_SPIKE_THRESHOLD,
                "window_secs": AV_ANOMALY_AUTH_SPIKE_WINDOW_SECS,
            })
            await session.commit()
    except Exception:  # pragma: no cover — must never surface to the caller
        logger.exception("auth-spike anomaly emission failed")


def _detect_metric_jump(old_metrics: dict | None, new_metrics: dict | None) -> list[dict]:
    """Flags any metric present in both commits whose magnitude changed by more than
    `AV_ANOMALY_METRIC_JUMP_RATIO`x — a coarse, dependency-free proxy for "something
    unusual just happened to training," not a statistical outlier model. `old` == 0 is
    treated as "any nonzero new value is a jump" rather than dividing by zero."""
    old_metrics, new_metrics = old_metrics or {}, new_metrics or {}
    jumps = []
    for key, new_val in new_metrics.items():
        old_val = old_metrics.get(key)
        if not isinstance(new_val, (int, float)) or not isinstance(old_val, (int, float)):
            continue
        if old_val == 0:
            if new_val != 0:
                jumps.append({"metric": key, "old": old_val, "new": new_val, "ratio": None})
            continue
        ratio = abs(new_val - old_val) / abs(old_val)
        if ratio >= AV_ANOMALY_METRIC_JUMP_RATIO:
            jumps.append({"metric": key, "old": old_val, "new": new_val, "ratio": ratio})
    return jumps


async def _detect_commit_anomalies(db: AsyncSession, project_id: str, commit_hash: str,
                                   metrics: dict, parent_hash: str | None,
                                   new_tree_hash: str | None) -> None:
    """Best-effort: metric-jump and mass-rewrite detection against the parent commit.
    Never raises — a detector failing must never fail the commit it's inspecting."""
    if not parent_hash:
        return
    try:
        parent = (await db.execute(
            select(DBCommit).where(DBCommit.hash == parent_hash)
        )).scalar_one_or_none()
        if parent is None:
            return
        for jump in _detect_metric_jump(parent.metrics, metrics):
            await _emit_event(db, project_id, "anomaly", {
                "type": "metric_jump", "commit_hash": commit_hash,
                "parent_hash": parent_hash, **jump,
            })
        if parent.root_tree_hash and new_tree_hash:
            old_tree = await resolve_tree(db, parent.root_tree_hash)
            new_tree = await resolve_tree(db, new_tree_hash)
            diff = _summarize_tree_diff(old_tree, new_tree)
            # v1.3.1 WP-44 fix (found live): _summarize_tree_diff() nests these three
            # lists under "files" (`{"files": {"added": [...], ...}}`) — reading them as
            # top-level keys always returned [], so changed_count was always 0 and this
            # detector never fired, on any input, ever (metric_jump's own detector sits
            # right above this and was unaffected, which is why unit tests never caught
            # this: no stack-free test exercises the live tree-diff path this reuses).
            diff_files = diff.get("files") or {}
            changed_count = (len(diff_files.get("added") or []) + len(diff_files.get("removed") or [])
                            + len(diff_files.get("changed") or []))
            if changed_count >= AV_ANOMALY_MASS_REWRITE_FILES:
                await _emit_event(db, project_id, "anomaly", {
                    "type": "mass_rewrite", "commit_hash": commit_hash,
                    "parent_hash": parent_hash, "changed_files": changed_count,
                })
    except Exception:  # pragma: no cover — a detector bug must never break a push
        logger.exception("anomaly detection failed for commit %s", commit_hash)


async def _emit_event(db: AsyncSession, project_id: str | None, kind: str, payload: dict | None):
    """Appends one event row (flushed so the cursor id exists) and schedules signed
    webhook deliveries as a background task — never blocking or failing the mutation.
    With zero active webhooks no task is created at all: fire-and-forget tasks that
    open their own sessions must never become routine overhead (they leak connections
    across TestClient requests otherwise)."""
    row = DBEvent(project_id=project_id, kind=kind, payload=payload)
    db.add(row)
    await db.flush()
    hook_count = (
        await db.execute(
            select(func.count()).select_from(DBWebhook).where(DBWebhook.active.is_(True))
        )
    ).scalar_one()
    if not hook_count:
        return row.id

    async def _deliver_later():
        try:
            async with async_session_factory() as session:
                hooks = (
                    await session.execute(select(DBWebhook).where(DBWebhook.active.is_(True)))
                ).scalars().all()
                await _deliver_webhooks(session, hooks,
                    {"id": row.id, "kind": kind, "project_id": project_id, "payload": payload})
                await session.commit()
        except Exception:  # pragma: no cover — delivery must never break the mutation
            logger.exception("webhook delivery scheduling failed")

    asyncio.create_task(_deliver_later())
    return row.id


def _sign(secret: str, body: bytes) -> str:
    return hmac_mod.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _event_body(event: dict) -> bytes:
    """The canonical signed body for an event dict {id, kind, project_id, payload}.

    Shared by first-attempt delivery AND retries so a retry reconstructs the
    byte-identical (and therefore signature-identical) payload."""
    import json as _json

    return _json.dumps(
        {"id": event["id"], "kind": event["kind"], "project_id": event.get("project_id"), "payload": event.get("payload")},
        sort_keys=True,
    ).encode()


async def _deliver_one(hook, delivery: DBWebhookDelivery, event: dict,
                       db: AsyncSession | None = None) -> None:
    """One POST attempt for one hook, persisting the outcome onto its delivery row.

    Failure handling: attempt++ with next_retry_at scheduled at the retry interval;
    exhausting AV_WEBHOOK_MAX_ATTEMPTS dead-letters the row (status='dead'). The row is
    created 'pending' BEFORE the POST so a crash mid-delivery still leaves a retryable
    record rather than a silently dropped fan-out."""
    body = _event_body(event)
    headers = {
        "Content-Type": "application/json",
        "X-AV-Event-Id": str(event["id"]),
        "X-AV-Event-Kind": event["kind"],
        "X-AV-Signature": _sign(hook.secret, body),
    }
    loop = asyncio.get_running_loop()

    def _post():
        import requests as _requests

        try:
            resp = _requests.post(hook.url, data=body, headers=headers,
                                  timeout=_WEBHOOK_TIMEOUT_SECS)
            return resp.status_code, None
        except Exception as exc:
            return None, str(exc)

    status_code, error = await loop.run_in_executor(None, _post)
    now = utcnow_naive()
    if status_code is not None and 200 <= status_code < 300:
        delivery.status = "delivered"
        delivery.response_code = status_code
        delivery.last_error = None
        delivery.next_retry_at = None
        # v1.2.5 per-webhook health: a success clears the failure streak — a webhook
        # that fails 4 times then succeeds is healthy again, not "3 away from disabled".
        hook.last_success_at = now
        hook.consecutive_failures = 0
    else:
        delivery.attempt += 1
        delivery.response_code = status_code
        delivery.last_error = error or f"http_{status_code}"
        hook.last_failure_at = now
        hook.consecutive_failures = (hook.consecutive_failures or 0) + 1
        if delivery.attempt >= WEBHOOK_MAX_ATTEMPTS:
            delivery.status = "dead"
            logger.warning("webhook %s dead-lettered after %s attempts", hook.url,
                           delivery.attempt)
        else:
            delivery.status = "failed"
            # v1.2.5 exponential backoff (was a fixed WEBHOOK_RETRY_INTERVAL_SECS every
            # time): attempt 1->interval, 2->2x, 3->4x, ... capped at WEBHOOK_RETRY_MAX_SECS
            # so a chronically-broken endpoint doesn't hammer itself OR its subscriber.
            backoff = min(WEBHOOK_RETRY_INTERVAL_SECS * (2 ** (delivery.attempt - 1)),
                          WEBHOOK_RETRY_MAX_SECS)
            delivery.next_retry_at = now + timedelta(seconds=backoff)
        # v1.2.5 disable-after-N: 0 (default) = never auto-disable. A webhook that's
        # already inactive stays as the caller left it — this only ever transitions
        # active -> disabled, never touches a webhook a human already turned off.
        if (WEBHOOK_DISABLE_AFTER > 0 and hook.active
                and hook.consecutive_failures >= WEBHOOK_DISABLE_AFTER):
            hook.active = False
            hook.disabled_reason = (
                f"auto-disabled after {hook.consecutive_failures} consecutive failed "
                f"deliveries (last: {delivery.last_error})"
            )
            logger.warning("webhook %s auto-disabled after %s consecutive failures",
                           hook.url, hook.consecutive_failures)
            if db is not None:
                await _emit_event(db, hook.project_id, "webhook_disabled", {
                    "webhook_id": hook.id, "url": hook.url,
                    "consecutive_failures": hook.consecutive_failures,
                })
                # System-triggered (no HTTP request in the retry-worker path) — username
                # None reads correctly in the trail as "not a human action", same as any
                # other Anonymous-mode entry.
                _audit(db, None, "webhook.auto_disable", hook.project_id, {
                    "webhook_id": hook.id, "consecutive_failures": hook.consecutive_failures,
                }, status_code=200)


async def process_due_webhook_deliveries() -> int:
    """Re-drives every due pending/failed delivery (called by the interval worker and
    exposed to tests). Returns how many rows were re-attempted.

    v1.3.2: uses `system_session_factory`, not `async_session_factory` / `get_session` —
    this worker is legitimately cross-tenant by design (every tenant's due deliveries
    must be re-driven, not just one), so it needs the bypass-RLS session (see
    database.py's own docstring on why bypass is GUC-based, not a second Postgres role).

    v1.3.2 (HA — the E5 webhook-retry-worker fix): `.with_for_update(skip_locked=True)`
    is the difference between this being safe under N replicas and not. Before this fix,
    the plain SELECT here had no row-claiming at all — every replica's own interval
    timer would independently select and re-deliver the SAME due rows, N-fold duplicate
    webhook POSTs per tick. `SKIP LOCKED` makes this a claim-a-batch queue-consumer
    pattern instead: N replicas processing DIFFERENT due rows in parallel, each row
    delivered by exactly one replica. Chosen over leader election deliberately — this is
    a claim-a-batch workload (independent rows, no single global decision to serialize),
    not a single-decision one, so N replicas doing useful parallel work beats one elected
    leader doing all of it serially, with no leader-crash/lock-timeout failure mode to
    reason about. Degrades to exactly today's single-replica behavior at N=1. The same
    `with_for_update()` pattern the ref-update path (`update_ref`) and budget spend
    (`consume_budget`) already use elsewhere in this file — not a new idiom.

    Deliberately the MINIMAL fix, not the full claim/deliver-split hardening a later
    pass could add (a short claim transaction releasing its lock before the actual
    outbound HTTP calls, versus holding the row lock across all 100 deliveries in this
    batch as it does today) — `SKIP LOCKED` alone already closes the DUPLICATION bug,
    which is the correctness-critical half; the lock-hold-duration concern is a
    throughput/contention refinement, not a correctness one, and is explicitly flagged
    here as unbuilt rather than silently implied.
    """
    now = utcnow_naive()
    delivered = 0
    async with system_session_factory() as db:
        db.info["bypass_rls"] = True
        rows = (await db.execute(
            select(DBWebhookDelivery)
            .where(DBWebhookDelivery.status.in_(["pending", "failed"]))
            .where((DBWebhookDelivery.next_retry_at.is_(None))
                   | (DBWebhookDelivery.next_retry_at <= now))
            .limit(100)
            .with_for_update(skip_locked=True)
        )).scalars().all()
        for delivery in rows:
            hook = (await db.execute(
                select(DBWebhook).where(DBWebhook.id == delivery.webhook_id)
            )).scalar_one_or_none()
            # Hook deleted since scheduling → dead-letter the orphan instead of looping.
            if hook is None:
                delivery.status = "dead"
                delivery.last_error = "webhook deleted"
                continue
            if not hook.active:
                delivery.status = "dead"
                delivery.last_error = "webhook deactivated"
                continue
            event = {"id": delivery.event_id or -1, "kind": delivery.event_kind,
                     "project_id": delivery.project_id, "payload": delivery.payload}
            await _deliver_one(hook, delivery, event, db)
            delivered += 1
        await db.commit()
    return delivered


async def _deliver_webhooks(db: AsyncSession, hooks: list, event: dict) -> None:
    """POSTs the event to every matching active webhook, signed, in worker threads.

    v1.2.2: every attempt is persisted in webhook_deliveries BEFORE the request goes
    out and updated after — failed deliveries are retried by the background worker
    (startup + interval) until AV_WEBHOOK_MAX_ATTEMPTS exhausts into a dead-letter.
    Delivery rows ride the MUTATION's own session/transaction, so a rolled-back
    mutation never leaves phantom delivery records. Per-URL try/except stays inside
    _deliver_one: a dead subscriber must never fail the original mutation."""
    matching = [
        h for h in hooks
        if (h.project_id is None or h.project_id == event.get("project_id"))
        and (h.kinds is None or event["kind"] in h.kinds)
    ]
    if not matching:
        return

    for hook in matching:
        delivery = DBWebhookDelivery(
            webhook_id=hook.id, event_id=event["id"] if event["id"] >= 0 else None,
            event_kind=event["kind"], project_id=event.get("project_id"),
            payload=event.get("payload"), attempt=1, status="pending",
            next_retry_at=None,
        )
        db.add(delivery)
        await db.flush()
        await _deliver_one(hook, delivery, event, db)


# v1.3.3 (WP-32): a monotonic counter stamped onto each new DBAuditLog row at CREATION
# time, read (not written) by database.py's `_chain_audit_log` before_flush listener to
# chain multiple rows added within the SAME flush in the order `_audit()` was actually
# called — `session.new` itself has no ordering guarantee. Module-level, not per-request,
# since ordering only needs to be locally consistent within one flush, and a single
# ever-increasing counter trivially guarantees that regardless of which request created
# which row.
_audit_seq_counter = itertools.count()


def _audit(db: AsyncSession, username: str | None, action: str,
           project_id: str | None, details: dict | None = None,
           status_code: int | None = None):
    """Records one mutation. v1.2.2: `status_code` captures the HTTP outcome the caller
    is about to return, so the trail answers "did it land?" — not just "was it tried".
    v1.3.3: the row's `chain_hash`/`signature` are populated later, by database.py's
    `before_flush` listener — never here — see that listener's own docstring."""
    if AUDIT_ENABLED:
        row = DBAuditLog(username=username, action=action,
                         project_id=project_id, details=details,
                         status_code=status_code)
        row._chain_seq = next(_audit_seq_counter)
        db.add(row)


# v1.2.5: (method, path) pairs for mutating routes DELIBERATELY not audited, each with
# a reason — kept alongside a coverage test (tests/test_audit_coverage.py) that walks
# every POST/PUT/PATCH/DELETE route in `app.routes` and asserts it's either audited (an
# `_audit(` call in its endpoint source) or listed here. This is how the WP-2 "guaranteed
# coverage matrix" from the V1.2.5 plan stays true after the fact, not just at review time.
AUDIT_EXEMPT_ROUTES: frozenset[tuple[str, str]] = frozenset({
    # High-frequency, content-addressed, idempotent (identical bytes -> identical hash;
    # a 409 "already exists" is a normal, harmless outcome, not a notable event). The
    # meaningful "who changed what" signal for an upload is captured by the commit.push
    # audit row that references these object hashes — auditing every individual object/
    # chunk PUT would dominate the audit_log table without adding attribution value.
    ("POST", "/api/objects/{hash}"),
    # Existence-check only (client asks "which of these hashes do you already have?"
    # before uploading) — never creates, deletes, or mutates anything itself. Same
    # high-frequency rationale as object upload.
    ("POST", "/api/sync/batch-objects"),
    # v1.3.3 (WP-12): both device-code endpoints (sso_oidc.py) mutate only an ephemeral
    # Redis record (a pending/polled device code, minutes-lived) -- never a `DBAuditLog`-
    # backed row. The security-relevant event is the login itself, which IS audited
    # (`auth.oidc_login`, added in `oidc_callback`'s own body) once the device-code flow
    # actually succeeds; auditing "someone started a login attempt" and "a CLI polled"
    # separately would add noise with no attribution value the eventual login row
    # doesn't already carry.
    ("POST", "/api/auth/device/code"),
    ("POST", "/api/auth/device/token"),
})


AUDIT_ENABLED = os.environ.get("AV_AUDIT_LOG", "1") not in ("", "0", "false")


def _identity(request: Request) -> str | None:
    return getattr(request.state, "username", None)


@app.get("/api/events")
async def list_events(
    since: int = 0,
    project_id: Optional[str] = None,
    kinds: Optional[str] = None,
    run_id: Optional[str] = None,
    limit: int = Query(100, ge=1, le=1000),
    wait: int = 0,
    db: AsyncSession = Depends(get_session),
):
    """Resumable ordered event feed. wait=<secs> long-polls for at least one new row.

    v1.3.0: `run_id` joins `project_id`/`kinds` as one stable query model (todo.md item
    9) — matches events whose payload carries that run id (currently `commit` and `run`
    kind events; a kind with no run_id in its payload simply never matches). Response
    also gains `gap` (todo.md item 9's backlog-honesty half): true when `since` predates
    this project's oldest retained event id (AV_EVENT_RETENTION_DAYS already swept it) —
    a resuming consumer can tell "I missed events" apart from "there are simply no new
    ones yet", which a stale cursor used to make silently indistinguishable.
    """
    kind_list = [k.strip() for k in kinds.split(",")] if kinds else None
    waited = 0.0

    async def _fetch():
        stmt = select(DBEvent).where(DBEvent.id > since)
        if project_id:
            stmt = stmt.where((DBEvent.project_id == project_id) | (DBEvent.project_id.is_(None)))
        if kind_list:
            stmt = stmt.where(DBEvent.kind.in_(kind_list))
        if run_id:
            stmt = stmt.where(DBEvent.payload["run_id"].as_string() == run_id)
        rows = (await db.execute(stmt.order_by(DBEvent.id.asc()).limit(limit))).scalars().all()
        return [
            {"id": e.id, "ts": e.ts.isoformat() if e.ts else None,
             "kind": e.kind, "project_id": e.project_id, "payload": e.payload}
            for e in rows
        ]

    events = await _fetch()
    while not events and wait > waited:
        await asyncio.sleep(0.5)
        waited += 0.5
        events = await _fetch()

    next_cursor = events[-1]["id"] if events else since

    gap = False
    if since > 0:
        oldest_stmt = select(func.min(DBEvent.id))
        if project_id:
            oldest_stmt = oldest_stmt.where(
                (DBEvent.project_id == project_id) | (DBEvent.project_id.is_(None)))
        oldest_id = (await db.execute(oldest_stmt)).scalar_one_or_none()
        gap = oldest_id is not None and since < oldest_id - 1
        return {"events": events, "next_cursor": next_cursor, "gap": gap,
                "oldest_id": oldest_id}
    return {"events": events, "next_cursor": next_cursor, "gap": False, "oldest_id": None}


QUERY_BEFORE_DAYS_DEFAULT = EVENT_RETENTION_DAYS


@app.delete("/api/events")
async def prune_events(request: Request, before_days: int = QUERY_BEFORE_DAYS_DEFAULT,
                       db: AsyncSession = Depends(get_session)):
    """Manual retention pruning (default also applied automatically during GC)."""
    from datetime import timedelta

    cutoff = utcnow_naive() - timedelta(days=max(before_days, 0))
    result = await db.execute(delete(DBEvent).where(DBEvent.ts < cutoff))
    await db.commit()
    _audit(db, _identity(request), "events.prune", None, {"deleted": result.rowcount, "before_days": before_days}, status_code=200)
    await db.commit()
    return {"deleted": result.rowcount}


@app.post("/api/runs")
async def create_run(request: Request, run: Dict[str, Any] = Body(...),
                     db: AsyncSession = Depends(get_session)):
    run_id = run.get("id") or _new_uuid()
    project_id = run.get("project_id")
    if not project_id:
        raise HTTPException(status_code=422, detail="project_id is required")
    exists = (await db.execute(select(DBRun).where(DBRun.id == run_id))).scalar_one_or_none()
    if exists:
        return {"status": "exists", "id": exists.id}  # idempotent create (multi-agent safe)
    now = utcnow_naive()
    # v1.3.1 (RSI R1, todo.md A.1): kind ∈ {train, meta, scoring, eval} — a "meta" run
    # improves the improver itself, not the target model. Server-side validation (not just
    # a CLI click.Choice) because SDK/plugin callers post here directly too. Unknown kinds
    # fall back to "train" rather than 422ing — additive default, never a hard break for
    # an older/other-language client that starts sending its own custom kind string.
    kind = run.get("kind") or "train"
    if kind not in ("train", "meta", "scoring", "eval"):
        kind = "train"
    db_run = DBRun(
        id=run_id, project_id=project_id, name=run.get("name"),
        status="running", kind=kind, improver_id=run.get("improver_id"),
        parent_run_id=run.get("parent_run_id"),
        created_by=_identity(request), config_hash=run.get("config_hash"),
        code_pointer=run.get("code_pointer"), env_snapshot_id=run.get("env_snapshot_id"),
        created_at=now, updated_at=now,
    )
    db.add(db_run)
    await _emit_event(db, project_id, "run",
                      {"action": "started", "run_id": run_id, "name": run.get("name"),
                       "kind": kind})
    _audit(db, _identity(request), "run.create", project_id,
           {"run_id": run_id, "kind": kind}, status_code=201)
    await db.commit()
    return {"status": "created", "id": run_id}


@app.get("/api/runs")
async def list_runs(project_id: Optional[str] = None, status: Optional[str] = None,
                    parent_run_id: Optional[str] = None, kind: Optional[str] = None,
                    limit: int = 50, offset: int = 0,
                    db: AsyncSession = Depends(get_session)):
    stmt = select(DBRun).order_by(DBRun.created_at.desc())
    if project_id:
        stmt = stmt.where(DBRun.project_id == project_id)
    if status:
        stmt = stmt.where(DBRun.status == status)
    if parent_run_id:
        stmt = stmt.where(DBRun.parent_run_id == parent_run_id)
    if kind:  # v1.3.1
        stmt = stmt.where(DBRun.kind == kind)
    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar()
    rows = (await db.execute(stmt.limit(limit).offset(offset))).scalars().all()
    return {"runs": [_run_to_dict(r) for r in rows], "total": total,
            "limit": limit, "offset": offset}


def _run_to_dict(r: DBRun) -> dict:
    return {
        "id": r.id, "project_id": r.project_id, "name": r.name, "status": r.status,
        "kind": r.kind, "improver_id": r.improver_id,
        "parent_run_id": r.parent_run_id, "created_by": r.created_by,
        "code_pointer": r.code_pointer, "env_snapshot_id": r.env_snapshot_id,
        "avh_object_id": r.avh_object_id,
        "policy_outcome": r.policy_outcome,
        "integrity_signals": r.integrity_signals,
        "plan_id": r.plan_id, "budget_id": r.budget_id, "stop_reason": r.stop_reason,
        "metrics_summary": r.metrics_summary or {},
        "created_at": r.created_at.isoformat() if r.created_at else None,
        "completed_at": r.completed_at.isoformat() if r.completed_at else None,
    }


@app.get("/api/runs/{run_id}")
async def get_run(run_id: str, db: AsyncSession = Depends(get_session)):
    r = (await db.execute(select(DBRun).where(DBRun.id == run_id))).scalar_one_or_none()
    if not r:
        raise HTTPException(status_code=404, detail="Run not found")
    commit_rows = (await db.execute(
        select(DBRunCommit.commit_hash).where(DBRunCommit.run_id == run_id)
    )).scalars().all()
    d = _run_to_dict(r)
    d["commit_hashes"] = commit_rows
    return d


_DATASET_EXTS = {".parquet", ".csv", ".h5", ".hdf5", ".npz", ".npy", ".arrow",
                 ".jsonl", ".tfrecord", ".wav", ".flac"}


def _summarize_tree_diff(old_tree: dict, new_tree: dict) -> dict:
    """v1.2.5, full-schema parity v1.3.0: a server-OWNED semantic summary for the
    run-detail endpoint — deliberately NOT importing python/av_cli/semdiff.py (the
    server package has never depended on av_cli; it ships and deploys standalone, see
    docker/engine-entrypoint.sh and the Plugin/Release contracts).

    v1.3.0 (todo.md item 3): this used to return only files/totals — a strict SUBSET of
    `av_cli.semdiff.diff_trees()`'s schema, silently missing models/chunks/datasets for
    any WebUI consumer that wanted them. Now produces the FULL semdiff-1.0 schema shape,
    independently re-implemented (same "no av_cli dependency" rule as before) but
    algorithmically identical — proven identical on identical input by
    tests/test_server.py::test_server_side_summary_matches_client_side_semdiff_on_the_same_trees
    (a shared golden fixture both implementations are run against), so the two can never
    silently drift apart again the way the files-only version already had.
    """
    old_tree = old_tree or {}
    new_tree = new_tree or {}
    old_keys, new_keys = set(old_tree), set(new_tree)
    added = sorted(new_keys - old_keys)
    removed = sorted(old_keys - new_keys)
    changed = sorted(
        p for p in (old_keys & new_keys)
        if (old_tree.get(p) or {}).get("hash") != (new_tree.get(p) or {}).get("hash")
    )

    def _layer_map(entry: dict) -> dict:
        return {l["name"]: l["hash"] for l in (entry.get("layers") or [])}

    def _chunk_hashes(entry: dict) -> set:
        return {c["hash"] for c in (entry.get("chunks") or [])}

    models = []
    for path in sorted(old_keys | new_keys):
        entry = new_tree.get(path) or {}
        if not entry.get("layers"):
            continue
        parent_entry = old_tree.get(path) or {}
        pmap, nmap = _layer_map(parent_entry), _layer_map(entry)
        moved = [name for name, h in nmap.items() if pmap.get(name) != h]
        total = len(nmap) or 1
        size_by_name = {l["name"]: l.get("size", 0) for l in (entry.get("layers") or [])}
        largest = sorted(
            ({"name": m, "size": size_by_name.get(m, 0)} for m in moved),
            key=lambda d: d["size"], reverse=True,
        )[:5]
        total_bytes = sum(size_by_name.values()) or 0
        moved_bytes = sum(size_by_name.get(m, 0) for m in moved)
        models.append({
            "path": path, "layers_changed": len(moved), "layers_total": len(nmap),
            "pct": round(len(moved) / total, 4), "moved": moved[:20],
            "largest_moved": largest, "bytes_changed": moved_bytes,
            "bytes_total": total_bytes,
            "pct_bytes": round(moved_bytes / total_bytes, 4) if total_bytes else 0.0,
        })

    chunks_reused = chunks_new = chunks_reused_bytes = chunks_new_bytes = 0
    for path in new_keys:
        entry = new_tree[path]
        chs = _chunk_hashes(entry)
        if not chs:
            continue
        parent_chs = _chunk_hashes(old_tree.get(path) or {})
        new_hashes = chs - parent_chs
        reused_hashes = chs & parent_chs
        chunks_new += len(new_hashes)
        chunks_reused += len(reused_hashes)
        size_by_hash = {c["hash"]: c.get("size", 0) for c in (entry.get("chunks") or [])}
        chunks_new_bytes += sum(size_by_hash.get(h, 0) for h in new_hashes)
        chunks_reused_bytes += sum(size_by_hash.get(h, 0) for h in reused_hashes)
    chunk_total = chunks_reused + chunks_new
    dedup_efficiency = round(chunks_reused / chunk_total, 4) if chunk_total else None
    chunk_total_bytes = chunks_reused_bytes + chunks_new_bytes
    dedup_efficiency_bytes = (
        round(chunks_reused_bytes / chunk_total_bytes, 4) if chunk_total_bytes else None
    )

    def _is_dataset(path: str) -> bool:
        low = path.lower()
        return any(low.endswith(e) for e in _DATASET_EXTS) or "dataset" in low

    datasets = sorted(
        p for p in (old_keys | new_keys)
        if _is_dataset(p)
        and (old_tree.get(p) or {}).get("hash") != (new_tree.get(p) or {}).get("hash")
        and p not in added
    )

    bytes_before = sum((e or {}).get("size") or 0 for e in old_tree.values())
    bytes_after = sum((e or {}).get("size") or 0 for e in new_tree.values())

    return {
        "files": {
            "added": [{"path": p, "kind": (new_tree.get(p) or {}).get("type")} for p in added],
            "removed": [{"path": p, "kind": (old_tree.get(p) or {}).get("type")} for p in removed],
            "changed": [{"path": p, "kind": (new_tree.get(p) or {}).get("type")
                        or (old_tree.get(p) or {}).get("type")} for p in changed],
        },
        "models": models,
        "chunks": {"reused": chunks_reused, "new": chunks_new,
                   "dedup_efficiency": dedup_efficiency,
                   "status": "measured" if chunk_total else "no_chunks",
                   "reused_bytes": chunks_reused_bytes, "new_bytes": chunks_new_bytes,
                   "dedup_efficiency_bytes": dedup_efficiency_bytes},
        "datasets": datasets,
        "totals": {"bytes_before": bytes_before, "bytes_after": bytes_after},
        "summary": f"+{len(added)} -{len(removed)} ~{len(changed)} file(s)",
    }


# v1.2.5: caps how many linked commits a run-summary resolves trees/metrics for — same
# rationale and same number as the WebUI's client-side MAX_DETAIL_COMMITS precedent
# (webui/src/components/RunsPanel.tsx): bound the response size, never silently drop
# data without saying so (the endpoint reports total_commits vs commits returned).
_RUN_SUMMARY_MAX_COMMITS = 20
# Same precedent (RunsPanel.tsx) for how far up the parent_run_id chain to walk.
_RUN_SUMMARY_MAX_LINEAGE_DEPTH = 10


@app.get("/api/runs/{run_id}/summary")
async def get_run_summary(run_id: str, db: AsyncSession = Depends(get_session)):
    """v1.2.5: one aggregate request for the WebUI run-detail view — lineage chain,
    linked commits (message + metrics, newest first), a SERVER-COMPUTED semantic summary
    over the two most-recently-linked commits' trees, the env_snapshot_id pointer, and
    (when the repo owner has opted in via `av handoff --publish`) the avh_object_id
    pointer for context-memory notes. Replaces the WebUI's previous N individual
    GET /api/commits/{hash} calls (client-side re-composition in
    webui/src/lib/runDetail.ts, kept as the pure-function fallback/test surface)."""
    r = (await db.execute(select(DBRun).where(DBRun.id == run_id))).scalar_one_or_none()
    if not r:
        raise HTTPException(status_code=404, detail="Run not found")

    # Lineage chain: walk parent_run_id upward, same depth bound the client used.
    lineage = []
    cursor = r
    seen_ids = set()
    for _ in range(_RUN_SUMMARY_MAX_LINEAGE_DEPTH):
        if cursor.id in seen_ids:
            break  # defensive: a corrupted parent_run_id cycle must never infinite-loop
        seen_ids.add(cursor.id)
        lineage.append({"id": cursor.id, "name": cursor.name, "status": cursor.status})
        if not cursor.parent_run_id:
            break
        cursor = (await db.execute(
            select(DBRun).where(DBRun.id == cursor.parent_run_id)
        )).scalar_one_or_none()
        if cursor is None:
            break

    commit_hashes = (await db.execute(
        select(DBRunCommit.commit_hash).where(DBRunCommit.run_id == run_id)
    )).scalars().all()
    total_commits = len(commit_hashes)

    commit_rows = []
    if commit_hashes:
        commit_rows = (await db.execute(
            select(DBCommit)
            .where(DBCommit.hash.in_(commit_hashes))
            .order_by(DBCommit.timestamp.desc())
            .limit(_RUN_SUMMARY_MAX_COMMITS)
        )).scalars().all()

    commits_out = [
        {"hash": c.hash, "message": c.message, "metrics": c.metrics or {},
         "timestamp": c.timestamp.isoformat() if c.timestamp else None}
        for c in commit_rows
    ]

    semantic_summary = None
    if len(commit_rows) >= 2:
        newest, previous = commit_rows[0], commit_rows[1]
        old_tree = await resolve_tree(db, previous.root_tree_hash) if previous.root_tree_hash else {}
        new_tree = await resolve_tree(db, newest.root_tree_hash) if newest.root_tree_hash else {}
        semantic_summary = _summarize_tree_diff(old_tree, new_tree)

    return {
        "run": _run_to_dict(r),
        "lineage": lineage,
        "commits": commits_out,
        "total_commits": total_commits,
        "semantic_summary": semantic_summary,
        "env_snapshot_id": r.env_snapshot_id,
        "avh_object_id": r.avh_object_id,
    }


async def _fetch_run(db: AsyncSession, run_id: str) -> Optional[DBRun]:
    return (await db.execute(select(DBRun).where(DBRun.id == run_id))).scalar_one_or_none()


# v1.3.0 (todo.md item 7): full per-commit metric series and full lineage chain, both
# cursor-paginated — `/summary` keeps its capped inline copy (bounded response size for
# the common case), these two exist for a WebUI/agent that wants to page past the cap.
_RUN_METRICS_DEFAULT_LIMIT = 50
_RUN_METRICS_MAX_LIMIT = 500
_RUN_LINEAGE_DEFAULT_DEPTH = 50
_RUN_LINEAGE_MAX_DEPTH = 500


def _encode_run_commit_cursor(created_at: datetime, commit_hash: str) -> str:
    import base64

    raw = f"{created_at.isoformat()}|{commit_hash}"
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def _decode_run_commit_cursor(cursor: str) -> tuple[datetime, str]:
    import base64

    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        raw = base64.urlsafe_b64decode(padded.encode()).decode()
        ts_str, chash = raw.split("|", 1)
        return datetime.fromisoformat(ts_str), chash
    except Exception:
        raise HTTPException(status_code=422, detail=f"Invalid cursor: {cursor!r}")


@app.get("/api/runs/{run_id}/metrics")
async def get_run_metrics(run_id: str, limit: int = _RUN_METRICS_DEFAULT_LIMIT,
                          cursor: Optional[str] = None,
                          db: AsyncSession = Depends(get_session)):
    """v1.3.0: the full per-commit metric series for a run, oldest-linked-first (chart
    order), cursor-paginated on (run_commits.created_at, commit_hash) — `/summary`'s
    inline `commits` copy is capped at `_RUN_SUMMARY_MAX_COMMITS` and newest-first; this
    is the uncapped complement for a WebUI chart or an agent that wants every point."""
    r = await _fetch_run(db, run_id)
    if not r:
        raise HTTPException(status_code=404, detail="Run not found")
    limit = max(1, min(limit, _RUN_METRICS_MAX_LIMIT))

    stmt = (
        select(DBRunCommit.created_at, DBCommit)
        .join(DBCommit, DBCommit.hash == DBRunCommit.commit_hash)
        .where(DBRunCommit.run_id == run_id)
        .order_by(DBRunCommit.created_at.asc(), DBRunCommit.commit_hash.asc())
    )
    if cursor:
        cur_ts, cur_hash = _decode_run_commit_cursor(cursor)
        stmt = stmt.where(
            or_(DBRunCommit.created_at > cur_ts,
                and_(DBRunCommit.created_at == cur_ts, DBRunCommit.commit_hash > cur_hash))
        )
    rows = (await db.execute(stmt.limit(limit))).all()

    points = [
        {"hash": c.hash, "message": c.message, "metrics": c.metrics or {},
         "timestamp": c.timestamp.isoformat() if c.timestamp else None,
         "linked_at": linked_at.isoformat() if linked_at else None}
        for linked_at, c in rows
    ]
    next_cursor = (
        _encode_run_commit_cursor(rows[-1][0], rows[-1][1].hash) if len(rows) == limit else None
    )
    return {"run_id": run_id, "points": points, "limit": limit, "next_cursor": next_cursor}


@app.get("/api/runs/{run_id}/lineage")
async def get_run_lineage(run_id: str, depth: int = _RUN_LINEAGE_DEFAULT_DEPTH,
                          cursor: Optional[str] = None,
                          db: AsyncSession = Depends(get_session)):
    """v1.3.0: the full parent_run_id chain, depth- and cursor-bounded per page —
    `/summary`'s inline `lineage` copy is capped at `_RUN_SUMMARY_MAX_LINEAGE_DEPTH`; this
    is the uncapped complement. `cursor` (opaque: a run id) resumes the walk from that run
    inclusive, so a caller pages by re-issuing with `next_cursor` until it comes back null."""
    r = await _fetch_run(db, run_id)
    if not r:
        raise HTTPException(status_code=404, detail="Run not found")
    if depth < 1 or depth > _RUN_LINEAGE_MAX_DEPTH:
        raise HTTPException(status_code=422,
                            detail=f"depth must be between 1 and {_RUN_LINEAGE_MAX_DEPTH}")

    node = r if not cursor else await _fetch_run(db, cursor)
    if cursor and node is None:
        raise HTTPException(status_code=422, detail=f"Invalid cursor: unknown run {cursor!r}")

    chain: list = []
    seen: set = set()
    while len(chain) < depth:
        if node is None or node.id in seen:
            node = None
            break
        seen.add(node.id)
        chain.append({"id": node.id, "name": node.name, "status": node.status,
                      "project_id": node.project_id, "parent_run_id": node.parent_run_id})
        if not node.parent_run_id:
            node = None
            break
        node = await _fetch_run(db, node.parent_run_id)

    # `node` still points at the next unconsumed run only when the loop stopped because
    # the depth cap was hit — root/missing-parent/cycle all leave it None above, meaning
    # this page is the end of the chain.
    next_cursor = node.id if (len(chain) == depth and node is not None) else None
    return {"run_id": run_id, "lineage": chain, "next_cursor": next_cursor}


@app.post("/api/runs/{run_id}/policy-outcome")
async def set_run_policy_outcome(run_id: str, request: Request,
                                 body: Dict[str, Any] = Body(...),
                                 db: AsyncSession = Depends(get_session)):
    """v1.3.0 (todo.md item 7): records the most recent `av promote`/`enforce_policy`
    decision for this run's active commit — best-effort telemetry the CLI calls right
    after deciding, never a gate itself (see cmd_policy.py::_report_policy_outcome, which
    swallows every failure from this call rather than let a reporting failure block a
    promotion)."""
    decision = body.get("decision")
    if decision not in ("allow", "deny"):
        raise HTTPException(status_code=422, detail="decision must be 'allow' or 'deny'")
    r = await _fetch_run(db, run_id)
    if not r:
        raise HTTPException(status_code=404, detail="Run not found")
    outcome = {"decision": decision, "rule": body.get("rule"), "at": utcnow_naive().isoformat()}
    r.policy_outcome = outcome
    r.updated_at = utcnow_naive()
    _audit(db, _identity(request), "run.policy_outcome", r.project_id,
          {"run_id": run_id, **outcome}, status_code=200)
    await db.commit()
    return {"status": "recorded", "run_id": run_id, "policy_outcome": outcome}


@app.post("/api/runs/{run_id}/integrity-signals")
async def set_run_integrity_signals(run_id: str, request: Request,
                                    body: Dict[str, Any] = Body(...),
                                    db: AsyncSession = Depends(get_session)):
    """v1.3.1 (RSI R2, todo.md B.10): records metric-gaming detection signals for a run —
    `av run integrity-check` computes these client-side (train/eval metric gap, eval-only
    improvement, data overlap) and reports them here, same best-effort telemetry contract
    as `policy-outcome` above: never a gate, a reporting failure never blocks anything."""
    signals = body.get("signals")
    if not isinstance(signals, dict):
        raise HTTPException(status_code=422, detail="signals must be a JSON object")
    r = await _fetch_run(db, run_id)
    if not r:
        raise HTTPException(status_code=404, detail="Run not found")
    r.integrity_signals = signals
    r.updated_at = utcnow_naive()
    _audit(db, _identity(request), "run.integrity_signals", r.project_id,
          {"run_id": run_id}, status_code=200)
    await db.commit()
    return {"status": "recorded", "run_id": run_id, "integrity_signals": signals}


@app.post("/api/runs/{run_id}/plan")
async def link_run_plan(run_id: str, request: Request, body: Dict[str, Any] = Body(...),
                        db: AsyncSession = Depends(get_session)):
    """v1.3.1 (RSI R3, todo.md D.16): attaches an experiment plan to a run — can happen
    at `av run start --plan ID` or any time after via `av plan attach`, since planning
    legitimately happens both before and mid-run."""
    plan_id = body.get("plan_id")
    r = (await db.execute(select(DBRun).where(DBRun.id == run_id))).scalar_one_or_none()
    if not r:
        raise HTTPException(status_code=404, detail="Run not found")
    plan = (await db.execute(select(DBPlan).where(DBPlan.id == plan_id))).scalar_one_or_none()
    if not plan:
        raise HTTPException(status_code=422, detail=f"Unknown plan: {plan_id}")
    r.plan_id = plan_id
    r.updated_at = utcnow_naive()
    _audit(db, _identity(request), "run.plan_attach", r.project_id,
          {"run_id": run_id, "plan_id": plan_id}, status_code=200)
    await db.commit()
    return {"status": "linked", "run_id": run_id, "plan_id": plan_id}


@app.post("/api/runs/{run_id}/budget")
async def link_run_budget(run_id: str, request: Request, body: Dict[str, Any] = Body(...),
                          db: AsyncSession = Depends(get_session)):
    """Attaches a budget account to a run, same optional/anytime pattern as plan linking."""
    budget_id = body.get("budget_id")
    r = (await db.execute(select(DBRun).where(DBRun.id == run_id))).scalar_one_or_none()
    if not r:
        raise HTTPException(status_code=404, detail="Run not found")
    budget = (await db.execute(select(DBBudget).where(DBBudget.id == budget_id))).scalar_one_or_none()
    if not budget:
        raise HTTPException(status_code=422, detail=f"Unknown budget: {budget_id}")
    r.budget_id = budget_id
    r.updated_at = utcnow_naive()
    _audit(db, _identity(request), "run.budget_attach", r.project_id,
          {"run_id": run_id, "budget_id": budget_id}, status_code=200)
    await db.commit()
    return {"status": "linked", "run_id": run_id, "budget_id": budget_id}


@app.post("/api/runs/{run_id}/avh")
async def link_run_avh(run_id: str, request: Request,
                       body: Dict[str, Any] = Body(...),
                       db: AsyncSession = Depends(get_session)):
    """v1.2.5: explicit, OPT-IN pointer from a run to a published `.avh` context-memory
    object — set only by `av handoff --publish`, never implicitly by a normal commit or
    push. Context notes can hold private reasoning, so nothing about this route is
    automatic; the object itself already had to be uploaded through the normal object
    flow (POST /api/objects/{hash}) before this call links it to the run."""
    avh_object_id = body.get("avh_object_id")
    if not avh_object_id or not re.match(r"^[a-f0-9]{64}$", avh_object_id):
        raise HTTPException(status_code=422, detail="avh_object_id must be a sha256 hex hash")
    r = (await db.execute(select(DBRun).where(DBRun.id == run_id))).scalar_one_or_none()
    if not r:
        raise HTTPException(status_code=404, detail="Run not found")
    avh_exists_stmt = select(DBObject.hash).where(DBObject.hash == avh_object_id)
    cas_tenant_id = _cas_tenant_id(request)
    if cas_tenant_id is not None:
        avh_exists_stmt = avh_exists_stmt.where(DBObject.tenant_id == cas_tenant_id)
    exists = (await db.execute(avh_exists_stmt)).scalar_one_or_none()
    if not exists:
        raise HTTPException(status_code=422,
                            detail="avh_object_id must reference an already-uploaded object "
                                   "(POST /api/objects/{hash} first)")
    r.avh_object_id = avh_object_id
    _audit(db, _identity(request), "run.avh_publish", r.project_id,
           {"run_id": run_id, "avh_object_id": avh_object_id}, status_code=200)
    await db.commit()
    return {"status": "linked", "run_id": run_id, "avh_object_id": avh_object_id}


@app.post("/api/runs/{run_id}/complete")
async def complete_run(run_id: str, request: Request,
                       body: Dict[str, Any] = Body(default={}),
                       db: AsyncSession = Depends(get_session)):
    return await _finish_run(run_id, request, "completed", body, db)


@app.post("/api/runs/{run_id}/fail")
async def fail_run(run_id: str, request: Request,
                   body: Dict[str, Any] = Body(default={}),
                   db: AsyncSession = Depends(get_session)):
    return await _finish_run(run_id, request, "failed", body, db)


async def _finish_run(run_id: str, request: Request, status: str, body: dict, db: AsyncSession):
    r = (await db.execute(select(DBRun).where(DBRun.id == run_id))).scalar_one_or_none()
    if not r:
        raise HTTPException(status_code=404, detail="Run not found")
    r.status = status
    r.completed_at = utcnow_naive()
    r.updated_at = r.completed_at
    if isinstance(body, dict) and body.get("metrics_summary"):
        r.metrics_summary = {**(r.metrics_summary or {}), **body["metrics_summary"]}
    await _emit_event(db, r.project_id, "run", {"action": status, "run_id": run_id})
    _audit(db, _identity(request), f"run.{status}", r.project_id, {"run_id": run_id}, status_code=200)
    await db.commit()
    return {"status": status, "id": run_id}


# ---------------------------------------------------------------------------
# RSI R1 (v1.3.1, migration 0006): improver versioning, self-edit change sets, signed
# policy packs, capability canaries, project freeze. See development/architecture.md's
# Improver Artifact / Dual-Gate Promotion / Project Freeze contract sections.
#
# Every artifact row here indexes a CAS object (`python/av_cli/casobj.py`) uploaded
# through the normal `POST /api/objects/{hash}` flow BEFORE this call — mirrors
# `link_run_avh()`'s existing "must already exist" check above, not a new pattern.
# ---------------------------------------------------------------------------

async def _object_exists(db: AsyncSession, object_id: str, cas_tenant_id: str | None = None) -> bool:
    """`cas_tenant_id` (v1.3.3, WP-21): None under `shared` isolation (default) — every
    call site's original, unscoped behavior. Under `isolated` mode, scoped to the
    caller's own tenant so a caller can't "prove" they hold an object merely because
    SOME OTHER tenant uploaded identical content — every one of this helper's ~11 call
    sites passes `_cas_tenant_id(request)` for exactly this reason."""
    stmt = select(DBObject.hash).where(DBObject.hash == object_id)
    if cas_tenant_id is not None:
        stmt = stmt.where(DBObject.tenant_id == cas_tenant_id)
    return bool((await db.execute(stmt)).scalar_one_or_none())


def _require_uploaded_object_id(field: str, object_id: Optional[str]) -> str:
    if not object_id or not re.match(r"^[a-f0-9]{64}$", object_id):
        raise HTTPException(status_code=422, detail=f"{field} must be a sha256 hex hash")
    return object_id


# --- Improver versions -------------------------------------------------------

@app.post("/api/improvers", dependencies=[Depends(require_scope("improver:write"))])
async def create_improver_version(request: Request, body: Dict[str, Any] = Body(...),
                                  db: AsyncSession = Depends(get_session)):
    """Registers one improver version — idempotent by client-generated id, same
    lazy/ordering-safe contract as `POST /api/runs`."""
    improver_id = body.get("id") or _new_uuid()
    project_id = body.get("project_id")
    if not project_id:
        raise HTTPException(status_code=422, detail="project_id is required")
    manifest_object_id = _require_uploaded_object_id("manifest_object_id", body.get("manifest_object_id"))
    exists = (await db.execute(
        select(DBImproverVersion).where(DBImproverVersion.id == improver_id)
    )).scalar_one_or_none()
    if exists:
        return {"status": "exists", "id": exists.id}
    if not await _object_exists(db, manifest_object_id, _cas_tenant_id(request)):
        raise HTTPException(status_code=422,
                            detail="manifest_object_id must reference an already-uploaded "
                                   "object (POST /api/objects/{hash} first)")
    row = DBImproverVersion(
        id=improver_id, project_id=project_id, manifest_object_id=manifest_object_id,
        parent_id=body.get("parent_id"), created_by=_identity(request),
        created_at=utcnow_naive(),
    )
    db.add(row)
    await _emit_event(db, project_id, "improver",
                      {"action": "registered", "improver_id": improver_id,
                       "parent_id": row.parent_id})
    _audit(db, _identity(request), "improver.register", project_id,
           {"improver_id": improver_id, "parent_id": row.parent_id}, status_code=201)
    await db.commit()
    return {"status": "created", "id": improver_id}


def _improver_to_dict(r: DBImproverVersion) -> dict:
    return {"id": r.id, "project_id": r.project_id,
            "manifest_object_id": r.manifest_object_id, "parent_id": r.parent_id,
            "created_by": r.created_by,
            "created_at": r.created_at.isoformat() if r.created_at else None}


@app.get("/api/improvers")
async def list_improver_versions(project_id: Optional[str] = None, limit: int = 50,
                                 offset: int = 0, db: AsyncSession = Depends(get_session)):
    stmt = select(DBImproverVersion).order_by(DBImproverVersion.created_at.desc())
    if project_id:
        stmt = stmt.where(DBImproverVersion.project_id == project_id)
    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar()
    rows = (await db.execute(stmt.limit(limit).offset(offset))).scalars().all()
    return {"improvers": [_improver_to_dict(r) for r in rows], "total": total,
            "limit": limit, "offset": offset}


async def _fetch_improver(db: AsyncSession, improver_id: str) -> Optional[DBImproverVersion]:
    return (await db.execute(
        select(DBImproverVersion).where(DBImproverVersion.id == improver_id)
    )).scalar_one_or_none()


@app.get("/api/improvers/{improver_id}")
async def get_improver_version(improver_id: str, db: AsyncSession = Depends(get_session)):
    row = await _fetch_improver(db, improver_id)
    if not row:
        raise HTTPException(status_code=404, detail="Improver version not found")
    return _improver_to_dict(row)


_IMPROVER_LINEAGE_MAX_DEPTH = 500


@app.get("/api/improvers/{improver_id}/lineage")
async def get_improver_lineage(improver_id: str, depth: int = 50, cursor: Optional[str] = None,
                               db: AsyncSession = Depends(get_session)):
    """Parent-chain walk, depth/cursor-bounded with a cycle guard — same shape as
    `GET /api/runs/{id}/lineage` (see that endpoint's docstring for the paging contract)."""
    r = await _fetch_improver(db, improver_id)
    if not r:
        raise HTTPException(status_code=404, detail="Improver version not found")
    if depth < 1 or depth > _IMPROVER_LINEAGE_MAX_DEPTH:
        raise HTTPException(status_code=422,
                            detail=f"depth must be between 1 and {_IMPROVER_LINEAGE_MAX_DEPTH}")

    node = r if not cursor else await _fetch_improver(db, cursor)
    if cursor and node is None:
        raise HTTPException(status_code=422, detail=f"Invalid cursor: unknown improver {cursor!r}")

    chain: list = []
    seen: set = set()
    while len(chain) < depth:
        if node is None or node.id in seen:
            node = None
            break
        seen.add(node.id)
        chain.append(_improver_to_dict(node))
        if not node.parent_id:
            node = None
            break
        node = await _fetch_improver(db, node.parent_id)

    next_cursor = node.id if (len(chain) == depth and node is not None) else None
    return {"improver_id": improver_id, "lineage": chain, "next_cursor": next_cursor}


# --- Change sets (self-edit proposals) ---------------------------------------

@app.post("/api/change-sets", dependencies=[Depends(require_scope("improver:write"))])
async def create_change_set(request: Request, body: Dict[str, Any] = Body(...),
                            db: AsyncSession = Depends(get_session)):
    cs_id = body.get("id") or _new_uuid()
    project_id = body.get("project_id")
    if not project_id:
        raise HTTPException(status_code=422, detail="project_id is required")
    object_id = _require_uploaded_object_id("object_id", body.get("object_id"))
    exists = (await db.execute(select(DBChangeSet).where(DBChangeSet.id == cs_id))).scalar_one_or_none()
    if exists:
        return {"status": "exists", "id": exists.id}
    if not await _object_exists(db, object_id, _cas_tenant_id(request)):
        raise HTTPException(status_code=422,
                            detail="object_id must reference an already-uploaded object "
                                   "(POST /api/objects/{hash} first)")
    risk = body.get("risk")
    if risk is not None and risk not in ("low", "medium", "high"):
        raise HTTPException(status_code=422, detail="risk must be one of low/medium/high")
    now = utcnow_naive()
    row = DBChangeSet(
        id=cs_id, project_id=project_id, improver_id=body.get("improver_id"),
        object_id=object_id, status="proposed", risk=risk,
        created_by=_identity(request), created_at=now, updated_at=now,
    )
    db.add(row)
    await _emit_event(db, project_id, "change_set",
                      {"action": "proposed", "change_set_id": cs_id, "risk": risk})
    _audit(db, _identity(request), "improver.propose", project_id,
          {"change_set_id": cs_id, "risk": risk}, status_code=201)
    await db.commit()
    return {"status": "created", "id": cs_id}


def _change_set_to_dict(r: DBChangeSet) -> dict:
    return {"id": r.id, "project_id": r.project_id, "improver_id": r.improver_id,
            "object_id": r.object_id, "status": r.status, "risk": r.risk,
            "created_by": r.created_by,
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "updated_at": r.updated_at.isoformat() if r.updated_at else None}


@app.get("/api/change-sets")
async def list_change_sets(project_id: Optional[str] = None, status: Optional[str] = None,
                           improver_id: Optional[str] = None, limit: int = 50, offset: int = 0,
                           db: AsyncSession = Depends(get_session)):
    stmt = select(DBChangeSet).order_by(DBChangeSet.created_at.desc())
    if project_id:
        stmt = stmt.where(DBChangeSet.project_id == project_id)
    if status:
        stmt = stmt.where(DBChangeSet.status == status)
    if improver_id:
        stmt = stmt.where(DBChangeSet.improver_id == improver_id)
    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar()
    rows = (await db.execute(stmt.limit(limit).offset(offset))).scalars().all()
    return {"change_sets": [_change_set_to_dict(r) for r in rows], "total": total,
            "limit": limit, "offset": offset}


@app.get("/api/change-sets/{cs_id}")
async def get_change_set(cs_id: str, db: AsyncSession = Depends(get_session)):
    row = (await db.execute(select(DBChangeSet).where(DBChangeSet.id == cs_id))).scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Change set not found")
    return _change_set_to_dict(row)


_CHANGE_SET_TRANSITIONS = {
    "proposed": {"approved", "rejected"},
    "approved": {"applied", "rejected"},
    "applied": {"rolled_back"},
    "rejected": set(),
    "rolled_back": set(),
}


@app.post("/api/change-sets/{cs_id}/status",
         dependencies=[Depends(require_scope("improver:write"))])
async def update_change_set_status(cs_id: str, request: Request,
                                   body: Dict[str, Any] = Body(...),
                                   db: AsyncSession = Depends(get_session)):
    """Transitions a change set's lifecycle state. Only the transitions declared in
    `_CHANGE_SET_TRANSITIONS` are legal (proposed -> approved|rejected -> applied ->
    rolled_back) — an illegal jump (e.g. straight to "applied") is a 422, not a silent
    overwrite, so `av improver apply` can never apply something nobody approved."""
    new_status = body.get("status")
    row = (await db.execute(select(DBChangeSet).where(DBChangeSet.id == cs_id))).scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Change set not found")
    allowed = _CHANGE_SET_TRANSITIONS.get(row.status, set())
    if new_status not in allowed:
        raise HTTPException(
            status_code=422,
            detail=f"Cannot transition change set from '{row.status}' to '{new_status!r}' "
                   f"(allowed: {sorted(allowed) or 'none — terminal state'})",
        )
    row.status = new_status
    row.updated_at = utcnow_naive()
    await _emit_event(db, row.project_id, "change_set",
                      {"action": new_status, "change_set_id": cs_id})
    _audit(db, _identity(request), f"improver.{new_status}", row.project_id,
          {"change_set_id": cs_id}, status_code=200)
    await db.commit()
    return {"status": "updated", "id": cs_id, "new_status": new_status}


# --- Policy packs (signed, hash-chained, append-only) ------------------------

@app.post("/api/policy-packs", dependencies=[Depends(require_scope("policy:write"))])
async def create_policy_pack(request: Request, body: Dict[str, Any] = Body(...),
                             db: AsyncSession = Depends(get_session)):
    """Publishes one signed policy pack onto the project's append-only chain.

    `chain_hash = sha256(f"{prev_id or ''}:{object_id}")` — each pack cryptographically
    commits to its predecessor, so the SEQUENCE of promotion-rule changes is tamper-
    evident (not just each pack's own signature), the same "detect silent history
    rewrites" property an append-only audit log gives you. There is deliberately no
    PUT/DELETE route for this table — publishing a new pack is the only mutation."""
    pack_id = body.get("id") or _new_uuid()
    project_id = body.get("project_id")
    if not project_id:
        raise HTTPException(status_code=422, detail="project_id is required")
    object_id = _require_uploaded_object_id("object_id", body.get("object_id"))
    exists = (await db.execute(select(DBPolicyPack).where(DBPolicyPack.id == pack_id))).scalar_one_or_none()
    if exists:
        return {"status": "exists", "id": exists.id}
    if not await _object_exists(db, object_id, _cas_tenant_id(request)):
        raise HTTPException(status_code=422,
                            detail="object_id must reference an already-uploaded object "
                                   "(POST /api/objects/{hash} first)")
    prev_id = body.get("prev_id")
    if prev_id:
        prev_exists = (await db.execute(
            select(DBPolicyPack.id).where(DBPolicyPack.id == prev_id)
        )).scalar_one_or_none()
        if not prev_exists:
            raise HTTPException(status_code=422,
                                detail=f"prev_id {prev_id!r} is not a known policy pack")
    chain_hash = hashlib.sha256(f"{prev_id or ''}:{object_id}".encode()).hexdigest()
    row = DBPolicyPack(
        id=pack_id, project_id=project_id, object_id=object_id, prev_id=prev_id,
        chain_hash=chain_hash, published_by=_identity(request), created_at=utcnow_naive(),
    )
    db.add(row)
    await _emit_event(db, project_id, "policy",
                      {"action": "published", "policy_pack_id": pack_id, "prev_id": prev_id})
    # A policy change is itself a security-relevant signal worth a dedicated anomaly
    # feed, regardless of direction (tightened or loosened) — a monitoring webhook
    # watching `kind=anomaly` alone should never miss "the promotion rules just changed."
    await _emit_event(db, project_id, "anomaly", {
        "type": "policy_change", "policy_pack_id": pack_id, "prev_id": prev_id,
    })
    _audit(db, _identity(request), "policy.pack_publish", project_id,
          {"policy_pack_id": pack_id, "prev_id": prev_id, "chain_hash": chain_hash},
          status_code=201)
    await db.commit()
    return {"status": "created", "id": pack_id, "chain_hash": chain_hash}


def _policy_pack_to_dict(r: DBPolicyPack) -> dict:
    return {"id": r.id, "project_id": r.project_id, "object_id": r.object_id,
            "prev_id": r.prev_id, "chain_hash": r.chain_hash,
            "published_by": r.published_by,
            "created_at": r.created_at.isoformat() if r.created_at else None}


@app.get("/api/policy-packs")
async def list_policy_packs(project_id: Optional[str] = None, limit: int = 50, offset: int = 0,
                            db: AsyncSession = Depends(get_session)):
    stmt = select(DBPolicyPack).order_by(DBPolicyPack.created_at.desc())
    if project_id:
        stmt = stmt.where(DBPolicyPack.project_id == project_id)
    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar()
    rows = (await db.execute(stmt.limit(limit).offset(offset))).scalars().all()
    return {"policy_packs": [_policy_pack_to_dict(r) for r in rows], "total": total,
            "limit": limit, "offset": offset}


@app.get("/api/policy-packs/latest")
async def get_latest_policy_pack(project_id: str, db: AsyncSession = Depends(get_session)):
    row = (await db.execute(
        select(DBPolicyPack).where(DBPolicyPack.project_id == project_id)
        .order_by(DBPolicyPack.created_at.desc()).limit(1)
    )).scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="No policy pack published for this project")
    return _policy_pack_to_dict(row)


@app.get("/api/policy-packs/{pack_id}")
async def get_policy_pack(pack_id: str, db: AsyncSession = Depends(get_session)):
    row = (await db.execute(select(DBPolicyPack).where(DBPolicyPack.id == pack_id))).scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Policy pack not found")
    return _policy_pack_to_dict(row)


# --- Capability canaries ------------------------------------------------------

@app.post("/api/canary-results")
async def report_canary_result(request: Request, body: Dict[str, Any] = Body(...),
                               db: AsyncSession = Depends(get_session)):
    project_id = body.get("project_id")
    improver_id = body.get("improver_id")
    suite_object_id = _require_uploaded_object_id("suite_object_id", body.get("suite_object_id"))
    if not project_id or not improver_id:
        raise HTTPException(status_code=422, detail="project_id and improver_id are required")
    if not await _object_exists(db, suite_object_id, _cas_tenant_id(request)):
        raise HTTPException(status_code=422,
                            detail="suite_object_id must reference an already-uploaded "
                                   "object (POST /api/objects/{hash} first)")
    passed = bool(body.get("passed"))
    row = DBCanaryResult(
        project_id=project_id, improver_id=improver_id, suite_object_id=suite_object_id,
        passed=passed, details=body.get("details"), run_id=body.get("run_id"),
        created_at=utcnow_naive(),
    )
    db.add(row)
    await db.flush()
    await _emit_event(db, project_id, "canary",
                      {"action": "recorded", "improver_id": improver_id, "passed": passed,
                       "canary_result_id": row.id})
    _audit(db, _identity(request), "canary.run", project_id,
          {"improver_id": improver_id, "passed": passed}, status_code=201)
    await db.commit()
    return {"status": "recorded", "id": row.id, "passed": passed}


def _canary_result_to_dict(r: DBCanaryResult) -> dict:
    return {"id": r.id, "project_id": r.project_id, "improver_id": r.improver_id,
            "suite_object_id": r.suite_object_id, "passed": r.passed,
            "details": r.details, "run_id": r.run_id,
            "created_at": r.created_at.isoformat() if r.created_at else None}


@app.get("/api/canary-results")
async def list_canary_results(project_id: Optional[str] = None, improver_id: Optional[str] = None,
                              limit: int = 50, offset: int = 0,
                              db: AsyncSession = Depends(get_session)):
    stmt = select(DBCanaryResult).order_by(DBCanaryResult.created_at.desc())
    if project_id:
        stmt = stmt.where(DBCanaryResult.project_id == project_id)
    if improver_id:
        stmt = stmt.where(DBCanaryResult.improver_id == improver_id)
    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar()
    rows = (await db.execute(stmt.limit(limit).offset(offset))).scalars().all()
    return {"canary_results": [_canary_result_to_dict(r) for r in rows], "total": total,
            "limit": limit, "offset": offset}


# --- Project freeze (kill-switch) ---------------------------------------------

def _freeze_to_dict(project_id: str, row: Optional[DBProjectFreeze]) -> dict:
    if row is None:
        return {"project_id": project_id, "frozen": False, "reason": None,
                "frozen_by": None, "frozen_at": None}
    return {"project_id": row.project_id, "frozen": row.frozen, "reason": row.reason,
            "frozen_by": row.frozen_by,
            "frozen_at": row.frozen_at.isoformat() if row.frozen_at else None}


@app.get("/api/freeze/{project_id}")
async def get_freeze_state(project_id: str, db: AsyncSession = Depends(get_session)):
    row = (await db.execute(
        select(DBProjectFreeze).where(DBProjectFreeze.project_id == project_id)
    )).scalar_one_or_none()
    return _freeze_to_dict(project_id, row)


@app.post("/api/freeze/{project_id}", dependencies=[Depends(require_scope("admin"))])
async def set_freeze_state(project_id: str, request: Request, body: Dict[str, Any] = Body(...),
                           db: AsyncSession = Depends(get_session)):
    """Global per-project kill-switch (todo.md C.15/I.40): while frozen, `_AuthRetryGroup`
    (client-side) AND this scope-gated route (server-side) both refuse every write except
    reads and rollback — a compromised or rogue local client can't just skip the client-
    side check. Requires the `admin` scope so an improver-level identity can never
    freeze/unfreeze its own promotion gate."""
    frozen = bool(body.get("frozen"))
    row = (await db.execute(
        select(DBProjectFreeze).where(DBProjectFreeze.project_id == project_id)
    )).scalar_one_or_none()
    now = utcnow_naive()
    if row is None:
        row = DBProjectFreeze(project_id=project_id, frozen=frozen, reason=body.get("reason"),
                              frozen_by=_identity(request) if frozen else None,
                              frozen_at=now if frozen else None, updated_at=now)
        db.add(row)
    else:
        row.frozen = frozen
        row.reason = body.get("reason") if frozen else None
        row.frozen_by = _identity(request) if frozen else None
        row.frozen_at = now if frozen else None
        row.updated_at = now
    await _emit_event(db, project_id, "freeze",
                      {"action": "frozen" if frozen else "unfrozen", "reason": row.reason})
    _audit(db, _identity(request), "freeze.set", project_id,
          {"frozen": frozen, "reason": row.reason}, status_code=200)
    await db.commit()
    return _freeze_to_dict(project_id, row)


# ---------------------------------------------------------------------------
# RSI R2 (v1.3.1, migration 0007): task/eval registry, eval integrity, held-out eval
# vault, blind scoring, external adapters. See development/architecture.md's
# Eval Registry & Integrity contract section.
# ---------------------------------------------------------------------------

@app.post("/api/eval/suites", dependencies=[Depends(require_scope("eval:write"))])
async def create_eval_suite(request: Request, body: Dict[str, Any] = Body(...),
                            db: AsyncSession = Depends(get_session)):
    suite_id = body.get("id") or _new_uuid()
    project_id = body.get("project_id")
    if not project_id:
        raise HTTPException(status_code=422, detail="project_id is required")
    object_id = _require_uploaded_object_id("object_id", body.get("object_id"))
    exists = (await db.execute(select(DBEvalSuite).where(DBEvalSuite.id == suite_id))).scalar_one_or_none()
    if exists:
        return {"status": "exists", "id": exists.id}
    if not await _object_exists(db, object_id, _cas_tenant_id(request)):
        raise HTTPException(status_code=422,
                            detail="object_id must reference an already-uploaded object "
                                   "(POST /api/objects/{hash} first)")
    now = utcnow_naive()
    row = DBEvalSuite(id=suite_id, project_id=project_id, object_id=object_id,
                      name=body.get("name"), blind=bool(body.get("blind")),
                      created_by=_identity(request), created_at=now, updated_at=now)
    db.add(row)
    await _emit_event(db, project_id, "eval",
                      {"action": "suite_registered", "suite_id": suite_id})
    _audit(db, _identity(request), "eval.register", project_id, {"suite_id": suite_id},
          status_code=201)
    await db.commit()
    return {"status": "created", "id": suite_id}


def _eval_suite_to_dict(r: DBEvalSuite) -> dict:
    return {"id": r.id, "project_id": r.project_id, "object_id": r.object_id,
            "name": r.name, "frozen": r.frozen, "blind": r.blind,
            "created_by": r.created_by,
            "created_at": r.created_at.isoformat() if r.created_at else None}


@app.get("/api/eval/suites")
async def list_eval_suites(project_id: Optional[str] = None, limit: int = 50, offset: int = 0,
                           db: AsyncSession = Depends(get_session)):
    stmt = select(DBEvalSuite).order_by(DBEvalSuite.created_at.desc())
    if project_id:
        stmt = stmt.where(DBEvalSuite.project_id == project_id)
    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar()
    rows = (await db.execute(stmt.limit(limit).offset(offset))).scalars().all()
    return {"suites": [_eval_suite_to_dict(r) for r in rows], "total": total,
            "limit": limit, "offset": offset}


async def _fetch_eval_suite(db: AsyncSession, suite_id: str) -> Optional[DBEvalSuite]:
    return (await db.execute(select(DBEvalSuite).where(DBEvalSuite.id == suite_id))).scalar_one_or_none()


@app.get("/api/eval/suites/{suite_id}")
async def get_eval_suite(suite_id: str, db: AsyncSession = Depends(get_session)):
    row = await _fetch_eval_suite(db, suite_id)
    if not row:
        raise HTTPException(status_code=404, detail="Eval suite not found")
    return _eval_suite_to_dict(row)


@app.put("/api/eval/suites/{suite_id}", dependencies=[Depends(require_scope("eval:write"))])
async def update_eval_suite(suite_id: str, request: Request, body: Dict[str, Any] = Body(...),
                            db: AsyncSession = Depends(get_session)):
    """todo.md B.7 (eval immutability locks): rejects ANY mutation of a frozen suite with
    409 — a training run may not modify the eval it's scored against, enforced here
    server-side rather than by convention."""
    row = await _fetch_eval_suite(db, suite_id)
    if not row:
        raise HTTPException(status_code=404, detail="Eval suite not found")
    if row.frozen:
        raise HTTPException(status_code=409, detail="Eval suite is frozen and cannot be modified")
    if "object_id" in body:
        object_id = _require_uploaded_object_id("object_id", body.get("object_id"))
        if not await _object_exists(db, object_id, _cas_tenant_id(request)):
            raise HTTPException(status_code=422,
                                detail="object_id must reference an already-uploaded object")
        row.object_id = object_id
    if "name" in body:
        row.name = body["name"]
    if "blind" in body:
        row.blind = bool(body["blind"])
    row.updated_at = utcnow_naive()
    _audit(db, _identity(request), "eval.update", row.project_id, {"suite_id": suite_id},
          status_code=200)
    await db.commit()
    return _eval_suite_to_dict(row)


@app.post("/api/eval/suites/{suite_id}/freeze", dependencies=[Depends(require_scope("eval:write"))])
async def freeze_eval_suite(suite_id: str, request: Request,
                            db: AsyncSession = Depends(get_session)):
    row = await _fetch_eval_suite(db, suite_id)
    if not row:
        raise HTTPException(status_code=404, detail="Eval suite not found")
    row.frozen = True
    row.updated_at = utcnow_naive()
    await _emit_event(db, row.project_id, "eval", {"action": "suite_frozen", "suite_id": suite_id})
    _audit(db, _identity(request), "eval.freeze", row.project_id, {"suite_id": suite_id},
          status_code=200)
    await db.commit()
    return _eval_suite_to_dict(row)


# --- Eval results (the held-out vault + blind scoring) ------------------------

@app.post("/api/eval/results", dependencies=[Depends(require_scope("scorer"))])
async def create_eval_result(request: Request, body: Dict[str, Any] = Body(...),
                             db: AsyncSession = Depends(get_session)):
    """todo.md F.25 (held-out eval vault): requiring the `scorer` scope IS the
    enforcement — a trainer's token (no `scorer` scope) is rejected here with 403,
    regardless of which project it targets. No separate mechanism is needed: point a
    training agent's token at one project and a scorer's token at another (or the same
    project with different tokens) and this route is the actual vault wall."""
    project_id = body.get("project_id")
    suite_id = body.get("suite_id")
    if not project_id or not suite_id:
        raise HTTPException(status_code=422, detail="project_id and suite_id are required")
    suite = await _fetch_eval_suite(db, suite_id)
    if not suite:
        raise HTTPException(status_code=422, detail=f"Unknown eval suite: {suite_id}")
    row = DBEvalResult(
        project_id=project_id, suite_id=suite_id, run_id=body.get("run_id"),
        score=body.get("score"), details=body.get("details"),
        revealed=not suite.blind, scored_by=_identity(request), created_at=utcnow_naive(),
    )
    db.add(row)
    await db.flush()
    await _emit_event(db, project_id, "eval",
                      {"action": "scored", "suite_id": suite_id, "eval_result_id": row.id,
                       "run_id": row.run_id})
    _audit(db, _identity(request), "eval.score", project_id,
          {"suite_id": suite_id, "eval_result_id": row.id}, status_code=201)
    await db.commit()
    return {"status": "recorded", "id": row.id, "revealed": row.revealed}


def _eval_result_to_dict(r: DBEvalResult, redact: bool) -> dict:
    """`redact=True` (a non-scorer reader against an unrevealed blind result) hides the
    score/details entirely — the reader learns a result EXISTS, not its VALUE. This is
    todo.md F.26 (blind/delayed scoring): the agent sees training metrics live, the final
    held-out score only after reveal."""
    if redact and not r.revealed:
        return {"id": r.id, "project_id": r.project_id, "suite_id": r.suite_id,
                "run_id": r.run_id, "revealed": False, "score": None, "details": None}
    return {"id": r.id, "project_id": r.project_id, "suite_id": r.suite_id,
            "run_id": r.run_id, "revealed": r.revealed, "score": r.score,
            "details": r.details, "scored_by": r.scored_by,
            "created_at": r.created_at.isoformat() if r.created_at else None}


@app.get("/api/eval/results")
async def list_eval_results(request: Request, project_id: Optional[str] = None,
                            suite_id: Optional[str] = None, run_id: Optional[str] = None,
                            limit: int = 50, offset: int = 0,
                            db: AsyncSession = Depends(get_session)):
    stmt = select(DBEvalResult).order_by(DBEvalResult.created_at.desc())
    if project_id:
        stmt = stmt.where(DBEvalResult.project_id == project_id)
    if suite_id:
        stmt = stmt.where(DBEvalResult.suite_id == suite_id)
    if run_id:
        stmt = stmt.where(DBEvalResult.run_id == run_id)
    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar()
    rows = (await db.execute(stmt.limit(limit).offset(offset))).scalars().all()
    scopes = getattr(request.state, "scopes", None) or ["*"]
    redact = not ("*" in scopes or "scorer" in scopes)
    return {"eval_results": [_eval_result_to_dict(r, redact) for r in rows], "total": total,
            "limit": limit, "offset": offset}


@app.post("/api/eval/results/{result_id}/reveal", dependencies=[Depends(require_scope("scorer"))])
async def reveal_eval_result(result_id: int, request: Request,
                             db: AsyncSession = Depends(get_session)):
    row = (await db.execute(select(DBEvalResult).where(DBEvalResult.id == result_id))).scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Eval result not found")
    row.revealed = True
    await _emit_event(db, row.project_id, "eval",
                      {"action": "revealed", "eval_result_id": result_id})
    _audit(db, _identity(request), "eval.reveal", row.project_id,
          {"eval_result_id": result_id}, status_code=200)
    await db.commit()
    return _eval_result_to_dict(row, redact=False)


# --- External eval adapters (todo.md F.27) ------------------------------------

@app.post("/api/eval/adapters", dependencies=[Depends(require_scope("eval:write"))])
async def create_eval_adapter(request: Request, body: Dict[str, Any] = Body(...),
                              db: AsyncSession = Depends(get_session)):
    adapter_id = body.get("id") or _new_uuid()
    project_id = body.get("project_id")
    name = body.get("name")
    command = body.get("command")
    if not project_id or not name:
        raise HTTPException(status_code=422, detail="project_id and name are required")
    if not isinstance(command, list) or not command or not all(isinstance(c, str) for c in command):
        raise HTTPException(status_code=422, detail="command must be a non-empty list of strings")
    exists = (await db.execute(select(DBEvalAdapter).where(DBEvalAdapter.id == adapter_id))).scalar_one_or_none()
    if exists:
        return {"status": "exists", "id": exists.id}
    row = DBEvalAdapter(id=adapter_id, project_id=project_id, name=name, command=command,
                        created_by=_identity(request), created_at=utcnow_naive())
    db.add(row)
    _audit(db, _identity(request), "eval.adapter_register", project_id,
          {"adapter_id": adapter_id, "name": name}, status_code=201)
    await db.commit()
    return {"status": "created", "id": adapter_id}


@app.get("/api/eval/adapters")
async def list_eval_adapters(project_id: Optional[str] = None, db: AsyncSession = Depends(get_session)):
    stmt = select(DBEvalAdapter)
    if project_id:
        stmt = stmt.where(DBEvalAdapter.project_id == project_id)
    rows = (await db.execute(stmt)).scalars().all()
    return {"adapters": [{"id": r.id, "project_id": r.project_id, "name": r.name,
                          "command": r.command, "created_by": r.created_by,
                          "created_at": r.created_at.isoformat() if r.created_at else None}
                         for r in rows]}


# --- Curriculum tasks (todo.md B.8) -------------------------------------------

@app.post("/api/tasks")
async def create_task(request: Request, body: Dict[str, Any] = Body(...),
                      db: AsyncSession = Depends(get_session)):
    task_id = body.get("id") or _new_uuid()
    project_id = body.get("project_id")
    title = body.get("title")
    if not project_id or not title:
        raise HTTPException(status_code=422, detail="project_id and title are required")
    exists = (await db.execute(select(DBTask).where(DBTask.id == task_id))).scalar_one_or_none()
    if exists:
        return {"status": "exists", "id": exists.id}
    now = utcnow_naive()
    row = DBTask(id=task_id, project_id=project_id, title=title,
                description=body.get("description"), difficulty=body.get("difficulty"),
                status="proposed", created_by=_identity(request), created_at=now, updated_at=now)
    db.add(row)
    _audit(db, _identity(request), "task.propose", project_id, {"task_id": task_id}, status_code=201)
    await db.commit()
    return {"status": "created", "id": task_id}


def _task_to_dict(r: DBTask) -> dict:
    return {"id": r.id, "project_id": r.project_id, "title": r.title,
            "description": r.description, "difficulty": r.difficulty, "status": r.status,
            "created_by": r.created_by,
            "created_at": r.created_at.isoformat() if r.created_at else None}


@app.get("/api/tasks")
async def list_tasks(project_id: Optional[str] = None, status: Optional[str] = None,
                     db: AsyncSession = Depends(get_session)):
    stmt = select(DBTask).order_by(DBTask.created_at.desc())
    if project_id:
        stmt = stmt.where(DBTask.project_id == project_id)
    if status:
        stmt = stmt.where(DBTask.status == status)
    rows = (await db.execute(stmt)).scalars().all()
    return {"tasks": [_task_to_dict(r) for r in rows]}


@app.post("/api/tasks/{task_id}/status")
async def update_task_status(task_id: str, request: Request, body: Dict[str, Any] = Body(...),
                             db: AsyncSession = Depends(get_session)):
    new_status = body.get("status")
    if new_status not in ("accepted", "rejected"):
        raise HTTPException(status_code=422, detail="status must be 'accepted' or 'rejected'")
    row = (await db.execute(select(DBTask).where(DBTask.id == task_id))).scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Task not found")
    row.status = new_status
    row.updated_at = utcnow_naive()
    _audit(db, _identity(request), f"task.{new_status}", row.project_id, {"task_id": task_id},
          status_code=200)
    await db.commit()
    return _task_to_dict(row)


# ---------------------------------------------------------------------------
# RSI R3 (v1.3.1, migration 0008): experiment plans, budget accounts, scheduler hooks.
# See development/architecture.md's Research Control Contract section.
# ---------------------------------------------------------------------------

@app.post("/api/plans")
async def create_plan(request: Request, body: Dict[str, Any] = Body(...),
                      db: AsyncSession = Depends(get_session)):
    plan_id = body.get("id") or _new_uuid()
    project_id = body.get("project_id")
    if not project_id:
        raise HTTPException(status_code=422, detail="project_id is required")
    object_id = _require_uploaded_object_id("object_id", body.get("object_id"))
    exists = (await db.execute(select(DBPlan).where(DBPlan.id == plan_id))).scalar_one_or_none()
    if exists:
        return {"status": "exists", "id": exists.id}
    if not await _object_exists(db, object_id, _cas_tenant_id(request)):
        raise HTTPException(status_code=422,
                            detail="object_id must reference an already-uploaded object")
    row = DBPlan(id=plan_id, project_id=project_id, object_id=object_id,
                created_by=_identity(request), created_at=utcnow_naive())
    db.add(row)
    _audit(db, _identity(request), "plan.create", project_id, {"plan_id": plan_id}, status_code=201)
    await db.commit()
    return {"status": "created", "id": plan_id}


def _plan_to_dict(r: DBPlan) -> dict:
    return {"id": r.id, "project_id": r.project_id, "object_id": r.object_id,
            "created_by": r.created_by,
            "created_at": r.created_at.isoformat() if r.created_at else None}


@app.get("/api/plans")
async def list_plans(project_id: Optional[str] = None, db: AsyncSession = Depends(get_session)):
    stmt = select(DBPlan).order_by(DBPlan.created_at.desc())
    if project_id:
        stmt = stmt.where(DBPlan.project_id == project_id)
    rows = (await db.execute(stmt)).scalars().all()
    return {"plans": [_plan_to_dict(r) for r in rows]}


@app.get("/api/plans/{plan_id}")
async def get_plan(plan_id: str, db: AsyncSession = Depends(get_session)):
    row = (await db.execute(select(DBPlan).where(DBPlan.id == plan_id))).scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Plan not found")
    return _plan_to_dict(row)


# --- Budget accounts -----------------------------------------------------------

@app.post("/api/budgets")
async def create_budget(request: Request, body: Dict[str, Any] = Body(...),
                        db: AsyncSession = Depends(get_session)):
    budget_id = body.get("id") or _new_uuid()
    project_id = body.get("project_id")
    scope = body.get("scope")
    scope_ref = body.get("scope_ref")
    if not project_id or scope not in ("run", "lineage") or not scope_ref:
        raise HTTPException(status_code=422,
                            detail="project_id, scope ('run'|'lineage'), and scope_ref are required")
    exists = (await db.execute(select(DBBudget).where(DBBudget.id == budget_id))).scalar_one_or_none()
    if exists:
        return {"status": "exists", "id": exists.id}
    row = DBBudget(
        id=budget_id, project_id=project_id, scope=scope, scope_ref=scope_ref,
        compute_seconds_limit=body.get("compute_seconds_limit"),
        storage_bytes_limit=body.get("storage_bytes_limit"),
        step_limit=body.get("step_limit"),
        created_by=_identity(request), created_at=utcnow_naive(), updated_at=utcnow_naive(),
    )
    db.add(row)
    _audit(db, _identity(request), "budget.set", project_id, {"budget_id": budget_id}, status_code=201)
    await db.commit()
    return {"status": "created", "id": budget_id}


def _budget_to_dict(r: DBBudget) -> dict:
    return {"id": r.id, "project_id": r.project_id, "scope": r.scope, "scope_ref": r.scope_ref,
            "compute_seconds_limit": r.compute_seconds_limit,
            "storage_bytes_limit": r.storage_bytes_limit, "step_limit": r.step_limit,
            "compute_seconds_used": r.compute_seconds_used,
            "storage_bytes_used": r.storage_bytes_used, "steps_used": r.steps_used,
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "updated_at": r.updated_at.isoformat() if r.updated_at else None}


@app.get("/api/budgets/{budget_id}")
async def get_budget(budget_id: str, db: AsyncSession = Depends(get_session)):
    row = (await db.execute(select(DBBudget).where(DBBudget.id == budget_id))).scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Budget not found")
    return _budget_to_dict(row)


@app.get("/api/budgets")
async def list_budgets(project_id: Optional[str] = None, scope_ref: Optional[str] = None,
                       db: AsyncSession = Depends(get_session)):
    stmt = select(DBBudget)
    if project_id:
        stmt = stmt.where(DBBudget.project_id == project_id)
    if scope_ref:
        stmt = stmt.where(DBBudget.scope_ref == scope_ref)
    rows = (await db.execute(stmt)).scalars().all()
    return {"budgets": [_budget_to_dict(r) for r in rows]}


def _budget_exceeded_dims(row: DBBudget) -> List[str]:
    dims = []
    if row.compute_seconds_limit is not None and row.compute_seconds_used > row.compute_seconds_limit:
        dims.append("compute_seconds")
    if row.storage_bytes_limit is not None and row.storage_bytes_used > row.storage_bytes_limit:
        dims.append("storage_bytes")
    if row.step_limit is not None and row.steps_used > row.step_limit:
        dims.append("steps")
    return dims


@app.post("/api/budgets/{budget_id}/consume")
async def consume_budget(budget_id: str, request: Request, body: Dict[str, Any] = Body(...),
                         db: AsyncSession = Depends(get_session)):
    """Increments usage counters (never decrements — a budget is spent, not refunded) and
    reports whether any limit is now exceeded, so `av budget consume`/an autonomous loop's
    own auto-stop check can react in the SAME round trip that recorded the spend, rather
    than a separate read-after-write that could race another consumer."""
    row = (await db.execute(
        select(DBBudget).where(DBBudget.id == budget_id).with_for_update()
    )).scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Budget not found")
    row.compute_seconds_used += float(body.get("compute_seconds", 0) or 0)
    row.storage_bytes_used += int(body.get("storage_bytes", 0) or 0)
    row.steps_used += int(body.get("steps", 0) or 0)
    row.updated_at = utcnow_naive()
    exceeded = _budget_exceeded_dims(row)
    _audit(db, _identity(request), "budget.consume", row.project_id,
          {"budget_id": budget_id, "exceeded": exceeded}, status_code=200)
    await db.commit()
    return {**_budget_to_dict(row), "exhausted": bool(exceeded), "exceeded_dims": exceeded}


# --- Scheduler hooks (todo.md D.20) --------------------------------------------

@app.post("/api/runs/{run_id}/stop")
async def stop_run(run_id: str, request: Request, body: Dict[str, Any] = Body(default={}),
                   db: AsyncSession = Depends(get_session)):
    """External stop (a scheduler, an auto-stop check) — distinct from `/complete`/`/fail`:
    `status` becomes `"stopped"` (not `"failed"`) and `stop_reason` records why, so a
    dashboard/lineage query can tell "the training genuinely failed" apart from "something
    outside the run decided to end it" (plateau, divergence, NaN, canary failure, budget)."""
    r = (await db.execute(select(DBRun).where(DBRun.id == run_id))).scalar_one_or_none()
    if not r:
        raise HTTPException(status_code=404, detail="Run not found")
    reason = (body or {}).get("reason")
    r.status = "stopped"
    r.stop_reason = reason
    r.completed_at = utcnow_naive()
    r.updated_at = r.completed_at
    await _emit_event(db, r.project_id, "run", {"action": "stopped", "run_id": run_id, "reason": reason})
    _audit(db, _identity(request), "run.stopped", r.project_id, {"run_id": run_id, "reason": reason},
          status_code=200)
    await db.commit()
    return {"status": "stopped", "id": run_id, "stop_reason": reason}


@app.get("/api/scheduler/queue")
async def scheduler_queue(project_id: Optional[str] = None, limit: int = 100,
                          db: AsyncSession = Depends(get_session)):
    """The live set of running runs a scheduler can act on — same fields as
    `GET /api/runs` but purpose-named so a scheduler doesn't have to guess which generic
    listing endpoint models "what's currently in flight"."""
    stmt = select(DBRun).where(DBRun.status == "running").order_by(DBRun.created_at.asc())
    if project_id:
        stmt = stmt.where(DBRun.project_id == project_id)
    rows = (await db.execute(stmt.limit(limit))).scalars().all()
    return {"queue": [_run_to_dict(r) for r in rows]}


# ---------------------------------------------------------------------------
# RSI R4 (v1.3.1, migration 0009): causal graphs, strategy memory, distilled lessons,
# cross-run search, reviewer gate, critiques, shared blackboard. See
# development/architecture.md's Multi-Agent & Strategy Memory Contract section.
# ---------------------------------------------------------------------------

@app.post("/api/causal-links")
async def create_causal_link(request: Request, body: Dict[str, Any] = Body(...),
                             db: AsyncSession = Depends(get_session)):
    project_id = body.get("project_id")
    cause_type = body.get("cause_type")
    cause_ref = body.get("cause_ref")
    effect_metric = body.get("effect_metric")
    if not project_id or cause_type not in ("change_set", "commit") or not cause_ref or not effect_metric:
        raise HTTPException(status_code=422,
                            detail="project_id, cause_type ('change_set'|'commit'), "
                                   "cause_ref, and effect_metric are required")
    row = DBCausalLink(project_id=project_id, cause_type=cause_type, cause_ref=cause_ref,
                       effect_metric=effect_metric, effect_delta=body.get("effect_delta"),
                       verified=bool(body.get("verified")), created_by=_identity(request),
                       created_at=utcnow_naive())
    db.add(row)
    await db.flush()
    _audit(db, _identity(request), "causal_link.create", project_id, {"causal_link_id": row.id},
          status_code=201)
    await db.commit()
    return {"status": "created", "id": row.id}


@app.get("/api/causal-links")
async def list_causal_links(project_id: Optional[str] = None, cause_ref: Optional[str] = None,
                            db: AsyncSession = Depends(get_session)):
    stmt = select(DBCausalLink).order_by(DBCausalLink.created_at.desc())
    if project_id:
        stmt = stmt.where(DBCausalLink.project_id == project_id)
    if cause_ref:
        stmt = stmt.where(DBCausalLink.cause_ref == cause_ref)
    rows = (await db.execute(stmt)).scalars().all()
    return {"causal_links": [
        {"id": r.id, "project_id": r.project_id, "cause_type": r.cause_type,
         "cause_ref": r.cause_ref, "effect_metric": r.effect_metric,
         "effect_delta": r.effect_delta, "verified": r.verified, "created_by": r.created_by,
         "created_at": r.created_at.isoformat() if r.created_at else None}
        for r in rows]}


# --- Strategy memory -----------------------------------------------------------

@app.post("/api/strategy")
async def create_strategy_entry(request: Request, body: Dict[str, Any] = Body(...),
                                db: AsyncSession = Depends(get_session)):
    project_id = body.get("project_id")
    technique = body.get("technique")
    outcome = body.get("outcome")
    if not project_id or not technique or outcome not in ("worked", "failed", "inconclusive"):
        raise HTTPException(status_code=422,
                            detail="project_id, technique, and outcome "
                                   "('worked'|'failed'|'inconclusive') are required")
    entry_id = body.get("id") or _new_uuid()
    row = DBStrategyEntry(id=entry_id, project_id=project_id, technique=technique,
                          hyperparameters=body.get("hyperparameters"), data_mix=body.get("data_mix"),
                          outcome=outcome, run_ids=body.get("run_ids") or [],
                          created_by=_identity(request), created_at=utcnow_naive())
    db.add(row)
    _audit(db, _identity(request), "strategy.add", project_id, {"strategy_id": entry_id},
          status_code=201)
    await db.commit()
    return {"status": "created", "id": entry_id}


def _strategy_to_dict(r: DBStrategyEntry) -> dict:
    return {"id": r.id, "project_id": r.project_id, "technique": r.technique,
            "hyperparameters": r.hyperparameters, "data_mix": r.data_mix,
            "outcome": r.outcome, "run_ids": r.run_ids or [], "created_by": r.created_by,
            "created_at": r.created_at.isoformat() if r.created_at else None}


@app.get("/api/strategy")
async def search_strategy(project_id: Optional[str] = None, technique: Optional[str] = None,
                          outcome: Optional[str] = None, q: Optional[str] = None,
                          limit: int = 50, db: AsyncSession = Depends(get_session)):
    """`q` does a simple case-insensitive substring match over `technique` — the
    "searchable store" todo.md E.22 asks for, without pulling in a full-text engine for
    what is, in practice, a small table an agent skims rather than fuzzy-searches."""
    stmt = select(DBStrategyEntry).order_by(DBStrategyEntry.created_at.desc())
    if project_id:
        stmt = stmt.where(DBStrategyEntry.project_id == project_id)
    if technique:
        stmt = stmt.where(DBStrategyEntry.technique == technique)
    if outcome:
        stmt = stmt.where(DBStrategyEntry.outcome == outcome)
    if q:
        stmt = stmt.where(DBStrategyEntry.technique.ilike(f"%{q}%"))
    rows = (await db.execute(stmt.limit(limit))).scalars().all()
    return {"entries": [_strategy_to_dict(r) for r in rows]}


# --- Distilled lessons -----------------------------------------------------------

@app.post("/api/lessons")
async def create_lessons(request: Request, body: Dict[str, Any] = Body(...),
                         db: AsyncSession = Depends(get_session)):
    project_id = body.get("project_id")
    if not project_id:
        raise HTTPException(status_code=422, detail="project_id is required")
    object_id = _require_uploaded_object_id("object_id", body.get("object_id"))
    if not await _object_exists(db, object_id, _cas_tenant_id(request)):
        raise HTTPException(status_code=422,
                            detail="object_id must reference an already-uploaded object")
    lessons_id = body.get("id") or _new_uuid()
    row = DBLessons(id=lessons_id, project_id=project_id, object_id=object_id,
                    created_by=_identity(request), created_at=utcnow_naive())
    db.add(row)
    _audit(db, _identity(request), "lessons.update", project_id, {"lessons_id": lessons_id},
          status_code=201)
    await db.commit()
    return {"status": "created", "id": lessons_id}


def _lessons_to_dict(r: DBLessons) -> dict:
    return {"id": r.id, "project_id": r.project_id, "object_id": r.object_id,
            "created_by": r.created_by,
            "created_at": r.created_at.isoformat() if r.created_at else None}


@app.get("/api/lessons/latest")
async def get_latest_lessons(project_id: str, db: AsyncSession = Depends(get_session)):
    row = (await db.execute(
        select(DBLessons).where(DBLessons.project_id == project_id)
        .order_by(DBLessons.created_at.desc()).limit(1)
    )).scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="No lessons object published for this project")
    return _lessons_to_dict(row)


@app.get("/api/lessons/{lessons_id}")
async def get_lessons(lessons_id: str, db: AsyncSession = Depends(get_session)):
    row = (await db.execute(select(DBLessons).where(DBLessons.id == lessons_id))).scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Lessons object not found")
    return _lessons_to_dict(row)


# --- Reviewer gate + critiques (todo.md H.34/H.35) --------------------------------

async def _reviewable_target(target_type: str, target_id: str, db: AsyncSession):
    """Returns (project_id, proposer_identity) for a change_set or improver target, or
    raises 404/422. `proposer_identity` is who self-review is checked against."""
    if target_type == "change_set":
        row = (await db.execute(select(DBChangeSet).where(DBChangeSet.id == target_id))).scalar_one_or_none()
        if not row:
            raise HTTPException(status_code=404, detail="Change set not found")
        return row.project_id, row.created_by
    if target_type == "improver":
        row = await _fetch_improver(db, target_id)
        if not row:
            raise HTTPException(status_code=404, detail="Improver version not found")
        return row.project_id, row.created_by
    raise HTTPException(status_code=422, detail="target_type must be 'change_set' or 'improver'")


@app.post("/api/reviews", dependencies=[Depends(require_scope("review"))])
async def create_review(request: Request, body: Dict[str, Any] = Body(...),
                        db: AsyncSession = Depends(get_session)):
    """Requires the `review` scope. The reviewer must NOT be the target's own proposer —
    a self-review is rejected with 422, not silently accepted, so "another agent (or
    human) must approve" (todo.md H.34) is an enforced fact, not a convention.
    `target_type` ∈ {"change_set","improver"}: `av improver promote`'s dual gate checks
    reviews against the CANDIDATE IMPROVER id directly (target_type="improver"), since one
    improver version can be the eventual promotion target regardless of which change set
    (if any) produced it; change-set-targeted reviews exist for earlier-stage sign-off."""
    decision = body.get("decision")
    target_type = body.get("target_type")
    target_id = body.get("target_id")
    if decision not in ("approve", "reject"):
        raise HTTPException(status_code=422, detail="decision must be 'approve' or 'reject'")
    if not target_id:
        raise HTTPException(status_code=422, detail="target_id is required")
    project_id, proposer = await _reviewable_target(target_type, target_id, db)
    reviewer = _identity(request)
    if reviewer is not None and reviewer == proposer:
        raise HTTPException(status_code=422, detail="A target's own proposer cannot review it")
    review_id = _new_uuid()
    row = DBReview(id=review_id, project_id=project_id, target_type=target_type,
                   target_id=target_id, reviewer=reviewer, decision=decision,
                   comment=body.get("comment"), created_at=utcnow_naive())
    db.add(row)
    await _emit_event(db, project_id, "review",
                      {"action": decision, "target_type": target_type, "target_id": target_id,
                       "review_id": review_id})
    _audit(db, reviewer, f"review.{decision}", project_id,
          {"target_type": target_type, "target_id": target_id, "review_id": review_id},
          status_code=201)
    await db.commit()
    return {"status": "created", "id": review_id, "decision": decision}


@app.get("/api/reviews")
async def list_reviews(target_type: Optional[str] = None, target_id: Optional[str] = None,
                       db: AsyncSession = Depends(get_session)):
    stmt = select(DBReview).order_by(DBReview.created_at.desc())
    if target_type:
        stmt = stmt.where(DBReview.target_type == target_type)
    if target_id:
        stmt = stmt.where(DBReview.target_id == target_id)
    rows = (await db.execute(stmt)).scalars().all()
    return {"reviews": [
        {"id": r.id, "target_type": r.target_type, "target_id": r.target_id,
         "reviewer": r.reviewer, "decision": r.decision, "comment": r.comment,
         "created_at": r.created_at.isoformat() if r.created_at else None}
        for r in rows]}


@app.post("/api/critiques")
async def create_critique(request: Request, body: Dict[str, Any] = Body(...),
                          db: AsyncSession = Depends(get_session)):
    objection = body.get("objection")
    target_type = body.get("target_type")
    target_id = body.get("target_id")
    if not objection:
        raise HTTPException(status_code=422, detail="objection is required")
    project_id, _proposer = await _reviewable_target(target_type, target_id, db)
    critique_id = _new_uuid()
    now = utcnow_naive()
    row = DBCritique(id=critique_id, project_id=project_id, target_type=target_type,
                     target_id=target_id, author=_identity(request), objection=objection,
                     status="open", created_at=now, updated_at=now)
    db.add(row)
    await _emit_event(db, project_id, "review",
                      {"action": "critiqued", "target_type": target_type, "target_id": target_id,
                       "critique_id": critique_id})
    _audit(db, _identity(request), "critique.raise", project_id,
          {"target_type": target_type, "target_id": target_id, "critique_id": critique_id},
          status_code=201)
    await db.commit()
    return {"status": "created", "id": critique_id}


def _critique_to_dict(r: DBCritique) -> dict:
    return {"id": r.id, "target_type": r.target_type, "target_id": r.target_id,
            "author": r.author, "objection": r.objection, "status": r.status,
            "resolution": r.resolution,
            "created_at": r.created_at.isoformat() if r.created_at else None}


@app.get("/api/critiques")
async def list_critiques(target_type: Optional[str] = None, target_id: Optional[str] = None,
                         status: Optional[str] = None, db: AsyncSession = Depends(get_session)):
    stmt = select(DBCritique).order_by(DBCritique.created_at.desc())
    if target_type:
        stmt = stmt.where(DBCritique.target_type == target_type)
    if target_id:
        stmt = stmt.where(DBCritique.target_id == target_id)
    if status:
        stmt = stmt.where(DBCritique.status == status)
    rows = (await db.execute(stmt)).scalars().all()
    return {"critiques": [_critique_to_dict(r) for r in rows]}


async def _set_critique_status(critique_id: str, new_status: str, request: Request,
                               body: dict, db: AsyncSession):
    row = (await db.execute(select(DBCritique).where(DBCritique.id == critique_id))).scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Critique not found")
    if row.status != "open":
        raise HTTPException(status_code=409, detail=f"Critique is already '{row.status}'")
    row.status = new_status
    row.resolution = body.get("resolution")
    row.updated_at = utcnow_naive()
    _audit(db, _identity(request), f"critique.{new_status}", row.project_id,
          {"critique_id": critique_id}, status_code=200)
    await db.commit()
    return _critique_to_dict(row)


@app.post("/api/critiques/{critique_id}/resolve")
async def resolve_critique(critique_id: str, request: Request, body: Dict[str, Any] = Body(default={}),
                           db: AsyncSession = Depends(get_session)):
    return await _set_critique_status(critique_id, "resolved", request, body, db)


@app.post("/api/critiques/{critique_id}/waive", dependencies=[Depends(require_scope("review"))])
async def waive_critique(critique_id: str, request: Request, body: Dict[str, Any] = Body(default={}),
                         db: AsyncSession = Depends(get_session)):
    """Waiving (as opposed to resolving) means the objection stands but is deliberately
    overridden — requires the `review` scope, and (like every other mutation) is audited,
    so a waiver is always a visible, attributable decision, never a silent bypass."""
    return await _set_critique_status(critique_id, "waived", request, body, db)


# --- Shared blackboard (todo.md H.36) ----------------------------------------------

@app.post("/api/blackboard")
async def post_blackboard_entry(request: Request, body: Dict[str, Any] = Body(...),
                                db: AsyncSession = Depends(get_session)):
    project_id = body.get("project_id")
    claim = body.get("claim")
    if not project_id or not claim:
        raise HTTPException(status_code=422, detail="project_id and claim are required")
    entry_id = _new_uuid()
    now = utcnow_naive()
    row = DBBlackboardEntry(id=entry_id, project_id=project_id, claim=claim,
                            author=_identity(request), evidence=body.get("evidence") or [],
                            status="open", created_at=now, updated_at=now)
    db.add(row)
    await _emit_event(db, project_id, "blackboard", {"action": "posted", "entry_id": entry_id})
    _audit(db, _identity(request), "blackboard.post", project_id, {"entry_id": entry_id},
          status_code=201)
    await db.commit()
    return {"status": "created", "id": entry_id}


def _blackboard_to_dict(r: DBBlackboardEntry) -> dict:
    return {"id": r.id, "project_id": r.project_id, "claim": r.claim, "author": r.author,
            "evidence": r.evidence or [], "status": r.status,
            "created_at": r.created_at.isoformat() if r.created_at else None}


@app.get("/api/blackboard")
async def list_blackboard(project_id: Optional[str] = None, status: Optional[str] = None,
                          db: AsyncSession = Depends(get_session)):
    stmt = select(DBBlackboardEntry).order_by(DBBlackboardEntry.created_at.desc())
    if project_id:
        stmt = stmt.where(DBBlackboardEntry.project_id == project_id)
    if status:
        stmt = stmt.where(DBBlackboardEntry.status == status)
    rows = (await db.execute(stmt)).scalars().all()
    return {"entries": [_blackboard_to_dict(r) for r in rows]}


@app.post("/api/blackboard/{entry_id}/resolve")
async def resolve_blackboard_entry(entry_id: str, request: Request,
                                   db: AsyncSession = Depends(get_session)):
    row = (await db.execute(
        select(DBBlackboardEntry).where(DBBlackboardEntry.id == entry_id)
    )).scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Blackboard entry not found")
    row.status = "resolved"
    row.updated_at = utcnow_naive()
    _audit(db, _identity(request), "blackboard.resolve", row.project_id, {"entry_id": entry_id},
          status_code=200)
    await db.commit()
    return _blackboard_to_dict(row)


# --- Cross-run search (todo.md E.24) ------------------------------------------------

@app.get("/api/search/runs")
async def search_runs(project_id: Optional[str] = None, metric: str = "",
                      direction: str = "up", min_delta: float = 0.0, limit: int = 50,
                      db: AsyncSession = Depends(get_session)):
    """A structured (not free-text) predicate: runs whose `metric` moved `direction`
    ("up"|"down") by at least `min_delta` relative to their PARENT run's latest value for
    that same metric — e.g. "all runs where eval_acc rose after the change that produced
    them." Deterministic, no LLM, no external index: a bounded scan over one project's
    runs plus one parent lookup each, which is exactly the shape `av search runs` needs
    for a project sized like the ones this tool targets."""
    if not metric:
        raise HTTPException(status_code=422, detail="metric is required")
    if direction not in ("up", "down"):
        raise HTTPException(status_code=422, detail="direction must be 'up' or 'down'")
    stmt = select(DBRun).order_by(DBRun.created_at.desc())
    if project_id:
        stmt = stmt.where(DBRun.project_id == project_id)
    rows = (await db.execute(stmt.limit(500))).scalars().all()  # bounded scan window

    matches = []
    for r in rows:
        val = (r.metrics_summary or {}).get(metric)
        if not isinstance(val, (int, float)) or not r.parent_run_id:
            continue
        parent = await _fetch_run(db, r.parent_run_id)
        if not parent:
            continue
        parent_val = (parent.metrics_summary or {}).get(metric)
        if not isinstance(parent_val, (int, float)):
            continue
        delta = val - parent_val
        if (direction == "up" and delta >= min_delta) or (direction == "down" and -delta >= min_delta):
            matches.append({"run_id": r.id, "parent_run_id": r.parent_run_id, "metric": metric,
                            "value": val, "parent_value": parent_val, "delta": delta})
        if len(matches) >= limit:
            break
    return {"matches": matches}


# ---------------------------------------------------------------------------
# RSI R5 (v1.3.1, migration 0010): sandbox jobs, tool manifests, action logs. See
# development/architecture.md's Sandbox Execution Contract section.
# ---------------------------------------------------------------------------

@app.post("/api/sandbox/jobs", dependencies=[Depends(require_scope("improver:write"))])
async def create_sandbox_job(request: Request, body: Dict[str, Any] = Body(...),
                             db: AsyncSession = Depends(get_session)):
    """Records a job submission — the driver itself (see `python/av_cli/sandbox/`)
    already started (or ran) the real job by the time this is called; this is the
    server-side index/audit row, not the execution itself."""
    job_id = body.get("id")
    project_id = body.get("project_id")
    driver = body.get("driver")
    if not job_id or not project_id or driver not in ("local", "docker", "kubernetes", "slurm"):
        raise HTTPException(status_code=422,
                            detail="id, project_id, and a valid driver are required")
    exists = (await db.execute(select(DBSandboxJob).where(DBSandboxJob.id == job_id))).scalar_one_or_none()
    if exists:
        return {"status": "exists", "id": job_id}
    now = utcnow_naive()
    row = DBSandboxJob(id=job_id, project_id=project_id, improver_id=body.get("improver_id"),
                       driver=driver, state=body.get("state", "pending"),
                       command=body.get("command"), created_by=_identity(request),
                       created_at=now, updated_at=now)
    db.add(row)
    await _emit_event(db, project_id, "sandbox",
                      {"action": "submitted", "job_id": job_id, "driver": driver})
    _audit(db, _identity(request), "sandbox.submit", project_id, {"job_id": job_id, "driver": driver},
          status_code=201)
    await db.commit()
    return {"status": "created", "id": job_id}


def _sandbox_job_to_dict(r: DBSandboxJob) -> dict:
    return {"id": r.id, "project_id": r.project_id, "improver_id": r.improver_id,
            "driver": r.driver, "state": r.state, "exit_code": r.exit_code,
            "command": r.command, "created_by": r.created_by,
            "created_at": r.created_at.isoformat() if r.created_at else None}


@app.post("/api/sandbox/jobs/{job_id}/status", dependencies=[Depends(require_scope("improver:write"))])
async def update_sandbox_job_status(job_id: str, request: Request, body: Dict[str, Any] = Body(...),
                                    db: AsyncSession = Depends(get_session)):
    row = (await db.execute(select(DBSandboxJob).where(DBSandboxJob.id == job_id))).scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Sandbox job not found")
    new_state = body.get("state")
    if new_state not in ("pending", "running", "succeeded", "failed", "cancelled"):
        raise HTTPException(status_code=422, detail="invalid state")
    row.state = new_state
    row.exit_code = body.get("exit_code")
    row.updated_at = utcnow_naive()
    if new_state in ("succeeded", "failed", "cancelled"):
        await _emit_event(db, row.project_id, "sandbox",
                          {"action": new_state, "job_id": job_id})
    _audit(db, _identity(request), "sandbox.status", row.project_id,
          {"job_id": job_id, "state": new_state}, status_code=200)
    await db.commit()
    return _sandbox_job_to_dict(row)


@app.get("/api/sandbox/jobs/{job_id}")
async def get_sandbox_job(job_id: str, db: AsyncSession = Depends(get_session)):
    row = (await db.execute(select(DBSandboxJob).where(DBSandboxJob.id == job_id))).scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Sandbox job not found")
    return _sandbox_job_to_dict(row)


@app.get("/api/sandbox/jobs")
async def list_sandbox_jobs(project_id: Optional[str] = None, state: Optional[str] = None,
                            limit: int = 50, db: AsyncSession = Depends(get_session)):
    stmt = select(DBSandboxJob).order_by(DBSandboxJob.created_at.desc())
    if project_id:
        stmt = stmt.where(DBSandboxJob.project_id == project_id)
    if state:
        stmt = stmt.where(DBSandboxJob.state == state)
    rows = (await db.execute(stmt.limit(limit))).scalars().all()
    return {"jobs": [_sandbox_job_to_dict(r) for r in rows]}


@app.post("/api/sandbox/jobs/{job_id}/cancel", dependencies=[Depends(require_scope("improver:write"))])
async def cancel_sandbox_job_record(job_id: str, request: Request,
                                    db: AsyncSession = Depends(get_session)):
    """Records that a cancellation was requested/performed — the actual cancel() call
    against the driver happens client-side (`av sandbox cancel`) before this is called;
    same "driver executes, server indexes" split as job creation above."""
    row = (await db.execute(select(DBSandboxJob).where(DBSandboxJob.id == job_id))).scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Sandbox job not found")
    row.state = "cancelled"
    row.updated_at = utcnow_naive()
    await _emit_event(db, row.project_id, "sandbox", {"action": "cancelled", "job_id": job_id})
    _audit(db, _identity(request), "sandbox.cancel", row.project_id, {"job_id": job_id}, status_code=200)
    await db.commit()
    return _sandbox_job_to_dict(row)


# --- Tool permission manifests -------------------------------------------------

@app.post("/api/tool-manifests", dependencies=[Depends(require_scope("improver:write"))])
async def create_tool_manifest(request: Request, body: Dict[str, Any] = Body(...),
                               db: AsyncSession = Depends(get_session)):
    project_id = body.get("project_id")
    improver_id = body.get("improver_id")
    if not project_id or not improver_id:
        raise HTTPException(status_code=422, detail="project_id and improver_id are required")
    object_id = _require_uploaded_object_id("object_id", body.get("object_id"))
    if not await _object_exists(db, object_id, _cas_tenant_id(request)):
        raise HTTPException(status_code=422,
                            detail="object_id must reference an already-uploaded object")
    manifest_id = body.get("id") or _new_uuid()
    row = DBToolManifest(id=manifest_id, project_id=project_id, improver_id=improver_id,
                         object_id=object_id, created_by=_identity(request), created_at=utcnow_naive())
    db.add(row)
    _audit(db, _identity(request), "tools.manifest_set", project_id,
          {"manifest_id": manifest_id, "improver_id": improver_id}, status_code=201)
    await db.commit()
    return {"status": "created", "id": manifest_id}


def _tool_manifest_to_dict(r: DBToolManifest) -> dict:
    return {"id": r.id, "project_id": r.project_id, "improver_id": r.improver_id,
            "object_id": r.object_id, "created_by": r.created_by,
            "created_at": r.created_at.isoformat() if r.created_at else None}


@app.get("/api/tool-manifests/latest")
async def get_latest_tool_manifest(project_id: str, improver_id: str,
                                   db: AsyncSession = Depends(get_session)):
    row = (await db.execute(
        select(DBToolManifest)
        .where(DBToolManifest.project_id == project_id, DBToolManifest.improver_id == improver_id)
        .order_by(DBToolManifest.created_at.desc()).limit(1)
    )).scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="No tool manifest published for this improver version")
    return _tool_manifest_to_dict(row)


@app.get("/api/tool-manifests/{manifest_id}")
async def get_tool_manifest(manifest_id: str, db: AsyncSession = Depends(get_session)):
    row = (await db.execute(select(DBToolManifest).where(DBToolManifest.id == manifest_id))).scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Tool manifest not found")
    return _tool_manifest_to_dict(row)


# --- Action logs (deterministic replay, todo.md G.31) --------------------------

@app.post("/api/action-logs")
async def create_action_log(request: Request, body: Dict[str, Any] = Body(...),
                            db: AsyncSession = Depends(get_session)):
    project_id = body.get("project_id")
    if not project_id:
        raise HTTPException(status_code=422, detail="project_id is required")
    object_id = _require_uploaded_object_id("object_id", body.get("object_id"))
    if not await _object_exists(db, object_id, _cas_tenant_id(request)):
        raise HTTPException(status_code=422,
                            detail="object_id must reference an already-uploaded object")
    log_id = body.get("id") or _new_uuid()
    row = DBActionLog(id=log_id, project_id=project_id, run_id=body.get("run_id"),
                      object_id=object_id, created_by=_identity(request), created_at=utcnow_naive())
    db.add(row)
    _audit(db, _identity(request), "action_log.publish", project_id,
          {"action_log_id": log_id, "run_id": row.run_id}, status_code=201)
    await db.commit()
    return {"status": "created", "id": log_id}


def _action_log_to_dict(r: DBActionLog) -> dict:
    return {"id": r.id, "project_id": r.project_id, "run_id": r.run_id,
            "object_id": r.object_id, "created_by": r.created_by,
            "created_at": r.created_at.isoformat() if r.created_at else None}


@app.get("/api/action-logs/{log_id}")
async def get_action_log(log_id: str, db: AsyncSession = Depends(get_session)):
    row = (await db.execute(select(DBActionLog).where(DBActionLog.id == log_id))).scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Action log not found")
    return _action_log_to_dict(row)


@app.get("/api/action-logs")
async def list_action_logs(project_id: Optional[str] = None, run_id: Optional[str] = None,
                           db: AsyncSession = Depends(get_session)):
    stmt = select(DBActionLog).order_by(DBActionLog.created_at.desc())
    if project_id:
        stmt = stmt.where(DBActionLog.project_id == project_id)
    if run_id:
        stmt = stmt.where(DBActionLog.run_id == run_id)
    rows = (await db.execute(stmt)).scalars().all()
    return {"action_logs": [_action_log_to_dict(r) for r in rows]}


def _parse_iso_dt(value: str, field: str) -> datetime:
    """ISO-8601 audit filter parsing; invalid input is a 422, never a silent match-all."""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        raise HTTPException(status_code=422, detail=f"Invalid {field} timestamp: {value!r}")
    # Naive UTC storage throughout the schema (see utcnow_naive) — normalize aware inputs.
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def _encode_id_cursor(row_id: int) -> str:
    import base64

    return base64.urlsafe_b64encode(f"id:{row_id}".encode()).decode().rstrip("=")


def _decode_id_cursor(cursor: str) -> int:
    import base64

    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        raw = base64.urlsafe_b64decode(padded.encode()).decode()
        if not raw.startswith("id:"):
            raise ValueError
        return int(raw[3:])
    except Exception:
        raise HTTPException(status_code=422, detail=f"Invalid cursor: {cursor!r}")


def _apply_audit_filters(stmt, *, project_id, action, action_prefix, username,
                          status_code, outcome, since, until):
    """Shared WHERE-clause builder for the list and export endpoints — kept as one
    function so the two routes can never drift on what a given filter set matches."""
    if project_id:
        stmt = stmt.where(DBAuditLog.project_id == project_id)
    if action:
        stmt = stmt.where(DBAuditLog.action == action)
    if action_prefix:
        # Escape SQL LIKE wildcards in the user-supplied prefix itself, then append ours.
        escaped = action_prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        stmt = stmt.where(DBAuditLog.action.like(f"{escaped}%", escape="\\"))
    if username:
        stmt = stmt.where(DBAuditLog.username == username)
    if status_code is not None:
        stmt = stmt.where(DBAuditLog.status_code == status_code)
    if outcome:
        if outcome not in ("ok", "error"):
            raise HTTPException(status_code=422, detail=f"Invalid outcome: {outcome!r} (want 'ok' or 'error')")
        if outcome == "ok":
            stmt = stmt.where(DBAuditLog.status_code.is_not(None), DBAuditLog.status_code < 400)
        else:
            stmt = stmt.where(DBAuditLog.status_code.is_not(None), DBAuditLog.status_code >= 400)
    if since:
        stmt = stmt.where(DBAuditLog.ts >= _parse_iso_dt(since, "since"))
    if until:
        stmt = stmt.where(DBAuditLog.ts <= _parse_iso_dt(until, "until"))
    return stmt


def _audit_row_dict(a: "DBAuditLog") -> dict:
    return {"id": a.id, "ts": a.ts.isoformat() if a.ts else None, "username": a.username,
            "action": a.action, "project_id": a.project_id, "details": a.details,
            "status_code": a.status_code,
            # v1.3.3 (WP-32): exported/listed alongside everything else so an offline
            # `av audit verify --export` has what it needs without a second round trip.
            "chain_hash": a.chain_hash, "signature": a.signature}


@app.get("/api/admin/audit", dependencies=[Depends(require_scope("admin"))])
async def get_audit_log(
    request: Request,
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    cursor: Optional[str] = None,
    project_id: Optional[str] = None,
    action: Optional[str] = None,
    action_prefix: Optional[str] = None,
    username: Optional[str] = None,
    status_code: Optional[int] = None,
    outcome: Optional[str] = None,
    since: Optional[str] = None,
    until: Optional[str] = None,
    db: AsyncSession = Depends(get_session),
):
    """Trust surface: recent mutating-call trail with outcome capture. Auth-gated like
    every other route; in Anonymous mode usernames are simply None entries.

    Filters (v1.2.2): action (exact match), project_id, since/until (ISO-8601 ts bounds).
    Filters (v1.2.5): action_prefix (route family, e.g. "commit." matches "commit.push"),
    username (actor), status_code (exact), outcome ("ok" = 2xx/3xx, "error" = 4xx/5xx).

    Pagination: `offset` (legacy, kept working — a page N stays valid even as new rows are
    inserted ahead of it, since `id DESC` ordering is stable) OR `cursor` (v1.2.5, stable
    under concurrent inserts: opaque, encodes the last row's id, and `id < cursor` is exact
    regardless of how many new rows landed since the previous page was fetched — offset-N
    can skip or repeat rows under concurrent inserts, cursor cannot). Passing both is a 422;
    `cursor` is the recommended path for agents polling this endpoint repeatedly.
    """
    if cursor and offset:
        raise HTTPException(status_code=422, detail="Pass either `cursor` or `offset`, not both.")
    stmt = _apply_audit_filters(
        select(DBAuditLog), project_id=project_id, action=action, action_prefix=action_prefix,
        username=username, status_code=status_code, outcome=outcome, since=since, until=until,
    )
    count_stmt = _apply_audit_filters(
        select(func.count()).select_from(DBAuditLog), project_id=project_id, action=action,
        action_prefix=action_prefix, username=username, status_code=status_code,
        outcome=outcome, since=since, until=until,
    )
    total = (await db.execute(count_stmt)).scalar_one()
    if cursor:
        stmt = stmt.where(DBAuditLog.id < _decode_id_cursor(cursor))
        rows = (await db.execute(stmt.order_by(DBAuditLog.id.desc()).limit(limit))).scalars().all()
    else:
        rows = (await db.execute(
            stmt.order_by(DBAuditLog.id.desc()).limit(limit).offset(offset)
        )).scalars().all()
    next_cursor = _encode_id_cursor(rows[-1].id) if len(rows) == limit else None
    return {"entries": [_audit_row_dict(a) for a in rows], "total": total,
            "limit": limit, "offset": offset, "next_cursor": next_cursor}


@app.get("/api/admin/audit/export", dependencies=[Depends(require_scope("admin"))])
async def export_audit_log(
    request: Request,
    format: str = Query("jsonl", pattern="^(jsonl|csv)$"),
    project_id: Optional[str] = None,
    action: Optional[str] = None,
    action_prefix: Optional[str] = None,
    username: Optional[str] = None,
    status_code: Optional[int] = None,
    outcome: Optional[str] = None,
    since: Optional[str] = None,
    until: Optional[str] = None,
    db: AsyncSession = Depends(get_session),
):
    """v1.2.5: streams the FILTERED set (same filters as the list endpoint, no
    pagination) as jsonl or csv for compliance export — `av audit export` is the CLI
    surface. Ordered oldest-first (unlike the list endpoint's newest-first) so a csv/jsonl
    file reads as a natural audit timeline top to bottom."""
    import csv
    import io
    import json as _json

    stmt = _apply_audit_filters(
        select(DBAuditLog), project_id=project_id, action=action, action_prefix=action_prefix,
        username=username, status_code=status_code, outcome=outcome, since=since, until=until,
    ).order_by(DBAuditLog.id.asc())
    rows = (await db.execute(stmt)).scalars().all()

    if format == "jsonl":
        body = "\n".join(_json.dumps(_audit_row_dict(a)) for a in rows)
        if body:
            body += "\n"
        media_type, filename = "application/x-ndjson", "audit-export.jsonl"
    else:
        buf = io.StringIO()
        writer = csv.DictWriter(
            buf, fieldnames=["id", "ts", "username", "action", "project_id", "details",
                            "status_code", "chain_hash", "signature"]
        )
        writer.writeheader()
        for a in rows:
            d = _audit_row_dict(a)
            d["details"] = _json.dumps(d["details"]) if d["details"] is not None else ""
            writer.writerow(d)
        body = buf.getvalue()
        media_type, filename = "text/csv", "audit-export.csv"

    return Response(
        content=body, media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.delete("/api/admin/audit", dependencies=[Depends(require_scope("admin"))])
async def prune_audit_log(request: Request, before_days: int = Query(AUDIT_RETENTION_DAYS, ge=0),
                          dry_run: bool = Query(False),
                          db: AsyncSession = Depends(get_session)):
    """Manual audit-trail pruning; the same window is swept automatically during GC.

    v1.3.0: dry_run=true reports the count that WOULD be deleted (a plain SELECT count)
    without touching anything — no audit row is written for a dry run either, since
    nothing actually happened.
    """
    cutoff = utcnow_naive() - timedelta(days=max(before_days, 0))
    if dry_run:
        count = (await db.execute(
            select(func.count()).select_from(DBAuditLog).where(DBAuditLog.ts < cutoff)
        )).scalar_one()
        return {"deleted": 0, "would_delete": count, "dry_run": True}
    result = await db.execute(delete(DBAuditLog).where(DBAuditLog.ts < cutoff))
    await db.commit()
    _audit(db, _identity(request), "audit.prune", None,
           {"deleted": result.rowcount, "before_days": before_days}, status_code=200)
    await db.commit()
    return {"deleted": result.rowcount, "dry_run": False}


@app.get("/api/admin/audit/verify", dependencies=[Depends(require_scope("admin"))])
async def verify_audit_chain(
    since_id: int = Query(0, ge=0),
    limit: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_session),
):
    """v1.3.3 (WP-32): walks the hash chain from `since_id` (exclusive) forward,
    recomputing each row's chain_hash and comparing against the stored value — the
    FIRST mismatch is reported as a break (everything after it is definitionally
    suspect too, but the first break is where an investigation starts). `since_id=0`
    (default) verifies the WHOLE table from the beginning; passing a previous check's
    `last_id` lets a repeat caller only re-verify what's new since then. `limit=0`
    (default) means no cap — verify everything from `since_id` to the end.

    Also reports how many rows in the checked range carry a signature and whether it
    verifies against this server's OWN public key (`GET .../public-key`) — a
    self-consistency check, not independent third-party verification (a real external
    verifier needs their own recomputation using an exported copy of the log plus the
    public key, which is exactly what `av audit verify` is for)."""
    from .audit_chain import compute_chain_hash

    prev_hash = None
    if since_id:
        prev_row = (await db.execute(
            select(DBAuditLog.chain_hash).where(DBAuditLog.id == since_id)
        )).first()
        if prev_row is None:
            raise HTTPException(status_code=404, detail=f"no audit row with id {since_id}")
        prev_hash = prev_row[0]

    stmt = select(DBAuditLog).where(DBAuditLog.id > since_id).order_by(DBAuditLog.id.asc())
    if limit:
        stmt = stmt.limit(limit)
    rows = (await db.execute(stmt)).scalars().all()

    signature_checks = {"verified": 0, "failed": 0, "absent": 0}
    pub_key = audit_signing.public_key_hex()
    checked = 0
    for row in rows:
        expected = compute_chain_hash(
            prev_hash, row.ts, row.username, row.action, row.project_id,
            row.status_code, row.details,
        )
        if expected != row.chain_hash:
            return {
                "ok": False, "broken_at_id": row.id, "checked": checked,
                "signature_checks": signature_checks,
            }
        if row.signature:
            if pub_key and audit_signing.verify(row.chain_hash, row.signature, pub_key):
                signature_checks["verified"] += 1
            else:
                signature_checks["failed"] += 1
        else:
            signature_checks["absent"] += 1
        prev_hash = row.chain_hash
        checked += 1

    return {
        "ok": True, "checked": checked,
        "last_id": rows[-1].id if rows else (since_id or None),
        "signature_checks": signature_checks,
    }


@app.get("/api/admin/audit/public-key", dependencies=[Depends(require_scope("admin"))])
async def audit_signing_public_key():
    """The server's audit-signing public key, hex-encoded — what an external verifier
    (or `av audit verify --export`) checks signatures against. 404 when
    AV_AUDIT_SIGNING_KEY_PATH isn't configured; chain-hash verification above works
    regardless of whether signing is configured at all."""
    key = audit_signing.public_key_hex()
    if key is None:
        raise HTTPException(status_code=404, detail="audit signing is not configured on this server")
    return {"public_key": key}


# --- webhook management ------------------------------------------------------

class WebhookCreate(BaseModel):
    url: str
    secret: str
    project_id: Optional[str] = None
    kinds: Optional[List[str]] = None


@app.post("/api/webhooks")
async def create_webhook(request: Request, wh: WebhookCreate,
                         db: AsyncSession = Depends(get_session)):
    if not wh.url.startswith(("http://", "https://")):
        raise HTTPException(status_code=422, detail="url must be http(s)")
    row = DBWebhook(url=wh.url, secret=wh.secret, project_id=wh.project_id, kinds=wh.kinds)
    db.add(row)
    _audit(db, _identity(request), "webhook.create", wh.project_id, {"webhook_id": row.id, "url": wh.url}, status_code=201)
    await db.commit()
    return {"id": row.id, "url": wh.url, "active": True}


@app.get("/api/webhooks")
async def list_webhooks(db: AsyncSession = Depends(get_session)):
    rows = (await db.execute(select(DBWebhook))).scalars().all()
    return {"webhooks": [
        {"id": w.id, "url": w.url, "project_id": w.project_id,
         "kinds": w.kinds, "active": w.active,
         "secret": (w.secret[:3] + "…") if w.secret else None,
         # v1.2.5 per-webhook health — "is this currently healthy?" without joining
         # webhook_deliveries.
         "last_success_at": w.last_success_at.isoformat() if w.last_success_at else None,
         "last_failure_at": w.last_failure_at.isoformat() if w.last_failure_at else None,
         "consecutive_failures": w.consecutive_failures or 0,
         "disabled_reason": w.disabled_reason}
        for w in rows
    ]}


@app.post("/api/webhooks/{webhook_id}/enable")
async def enable_webhook(webhook_id: str, request: Request, db: AsyncSession = Depends(get_session)):
    """v1.2.5: re-enables a webhook — the explicit counterpart to auto-disable. Clears the
    failure streak so it starts from a clean slate rather than being one failure from
    disabling itself again immediately."""
    row = (await db.execute(select(DBWebhook).where(DBWebhook.id == webhook_id))).scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Webhook not found")
    row.active = True
    row.disabled_reason = None
    row.consecutive_failures = 0
    _audit(db, _identity(request), "webhook.enable", row.project_id, {"webhook_id": webhook_id},
           status_code=200)
    await db.commit()
    return {"status": "enabled"}


@app.delete("/api/webhooks/{webhook_id}")
async def delete_webhook(webhook_id: str, request: Request,
                         db: AsyncSession = Depends(get_session)):
    row = (await db.execute(select(DBWebhook).where(DBWebhook.id == webhook_id))).scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Webhook not found")
    await db.execute(delete(DBWebhook).where(DBWebhook.id == webhook_id))
    _audit(db, _identity(request), "webhook.delete", row.project_id, {"webhook_id": webhook_id}, status_code=200)
    await db.commit()
    return {"status": "deleted"}


@app.post("/api/webhooks/{webhook_id}/test")
async def test_webhook(webhook_id: str, request: Request, db: AsyncSession = Depends(get_session)):
    row = (await db.execute(select(DBWebhook).where(DBWebhook.id == webhook_id))).scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Webhook not found")
    await _deliver_webhooks(db, [row], {"id": -1, "kind": "webhook_test",
                                    "project_id": row.project_id, "payload": {"ping": True}})
    _audit(db, _identity(request), "webhook.test", row.project_id, {"webhook_id": webhook_id},
           status_code=200)
    await db.commit()
    return {"status": "delivered"}


# --- webhook delivery observability (v1.2.2) ----------------------------------

def _delivery_row_dict(d: "DBWebhookDelivery") -> dict:
    return {"id": d.id, "webhook_id": d.webhook_id, "event_id": d.event_id,
            "event_kind": d.event_kind, "project_id": d.project_id,
            "attempt": d.attempt, "status": d.status,
            "response_code": d.response_code, "last_error": d.last_error,
            "next_retry_at": d.next_retry_at.isoformat() if d.next_retry_at else None,
            "created_at": d.created_at.isoformat() if d.created_at else None,
            "updated_at": d.updated_at.isoformat() if d.updated_at else None}


@app.get("/api/admin/webhook-deliveries", dependencies=[Depends(require_scope("admin"))])
async def list_webhook_deliveries(
    status: Optional[str] = None,
    webhook_id: Optional[str] = None,
    event_kind: Optional[str] = None,
    since: Optional[str] = None,
    until: Optional[str] = None,
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    cursor: Optional[str] = None,
    db: AsyncSession = Depends(get_session),
):
    """Delivery-ledger observability: attempts, outcomes, retry schedule, dead-letters.

    v1.2.5 additions: `event_kind`, `since`/`until` filters, and `cursor` pagination
    (same opaque-id scheme as /api/admin/audit — see its docstring for the rationale)."""
    if cursor and offset:
        raise HTTPException(status_code=422, detail="Pass either `cursor` or `offset`, not both.")
    stmt = select(DBWebhookDelivery)
    count_stmt = select(func.count()).select_from(DBWebhookDelivery)
    for col, val in (
        (DBWebhookDelivery.status, status),
        (DBWebhookDelivery.webhook_id, webhook_id),
        (DBWebhookDelivery.event_kind, event_kind),
    ):
        if val:
            stmt = stmt.where(col == val)
            count_stmt = count_stmt.where(col == val)
    if since:
        cutoff = _parse_iso_dt(since, "since")
        stmt = stmt.where(DBWebhookDelivery.created_at >= cutoff)
        count_stmt = count_stmt.where(DBWebhookDelivery.created_at >= cutoff)
    if until:
        cutoff = _parse_iso_dt(until, "until")
        stmt = stmt.where(DBWebhookDelivery.created_at <= cutoff)
        count_stmt = count_stmt.where(DBWebhookDelivery.created_at <= cutoff)
    total = (await db.execute(count_stmt)).scalar_one()
    if cursor:
        stmt = stmt.where(DBWebhookDelivery.id < _decode_id_cursor(cursor))
        rows = (await db.execute(stmt.order_by(DBWebhookDelivery.id.desc()).limit(limit))).scalars().all()
    else:
        rows = (await db.execute(
            stmt.order_by(DBWebhookDelivery.id.desc()).limit(limit).offset(offset)
        )).scalars().all()
    next_cursor = _encode_id_cursor(rows[-1].id) if len(rows) == limit else None
    return {"deliveries": [_delivery_row_dict(d) for d in rows], "total": total,
            "limit": limit, "offset": offset, "next_cursor": next_cursor}


@app.post("/api/admin/webhook-deliveries/{delivery_id}/replay",
          dependencies=[Depends(require_scope("admin"))])
async def replay_webhook_delivery(delivery_id: int, request: Request,
                                  db: AsyncSession = Depends(get_session)):
    """v1.2.5: re-queues one failed/dead delivery for immediate retry — the CLI/admin
    counterpart to waiting for the interval worker (or for a dead row, which the worker
    never touches again on its own). Resets the attempt counter so a manually-replayed
    delivery gets the full AV_WEBHOOK_MAX_ATTEMPTS budget again, not just what was left."""
    row = (await db.execute(
        select(DBWebhookDelivery).where(DBWebhookDelivery.id == delivery_id)
    )).scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Delivery not found")
    if row.status in ("delivered", "pending"):
        raise HTTPException(status_code=409,
                            detail=f"Delivery {delivery_id} is '{row.status}' — only "
                                   "'failed'/'dead' deliveries can be replayed.")
    row.status = "pending"
    row.attempt = 0
    row.last_error = None
    row.next_retry_at = utcnow_naive()
    _audit(db, _identity(request), "webhook.delivery_replay", row.project_id,
           {"delivery_id": delivery_id, "webhook_id": row.webhook_id}, status_code=200)
    await db.commit()
    return {"status": "queued", "delivery": _delivery_row_dict(row)}


# ---------------------------------------------------------------------------
# v1.3.2 — Enterprise identity: DB-backed API tokens (`av token`). The remote-
# administrable alternative to `AV_AUTH_USERS`, which requires `docker compose` shell
# access on the host running the stack to create/rotate/revoke (`cmd_auth.py`). A token
# minted here works from any machine that can reach this registry over HTTP, resolved by
# `identity.py::resolve_db_token` inside `require_token` — see that module's docstring
# for the full resolution order relative to `.env`-based credentials.
#
# `token:write` gates every mutation here (granted by the built-in `admin` role,
# migration 0011) — creating/revoking a credential is itself an admin action, distinct
# from whatever scopes the MINTED token ends up carrying.
# ---------------------------------------------------------------------------

def _token_row_dict(row: DBApiToken) -> dict:
    return {
        "id": row.id, "name": row.name, "prefix": row.prefix,
        "user_id": row.user_id, "scopes": row.scopes,
        "expires_at": row.expires_at.isoformat() if row.expires_at else None,
        "last_used_at": row.last_used_at.isoformat() if row.last_used_at else None,
        "revoked_at": row.revoked_at.isoformat() if row.revoked_at else None,
        "created_by": row.created_by,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


@app.post("/api/tokens", dependencies=[Depends(require_scope("token:write"))])
async def create_api_token(request: Request, body: Dict[str, Any] = Body(...),
                           db: AsyncSession = Depends(get_session)):
    """Mints a new DB-backed bearer token, scoped to the CALLER's own tenant — never a
    tenant the caller merely names in the body, so a caller can only ever grow their own
    tenant's credential surface, not someone else's (the tenant comes from the resolved
    Principal, `_principal(request).tenant_id`, not from `body`).

    The plaintext token is returned exactly once, here, and never again — only its
    sha256 (`identity.py::hash_token`) and an 8-char display prefix are persisted
    (models.py::DBApiToken's own docstring), matching `av registry keygen`'s "the
    private key never round-trips back out" posture.
    """
    import secrets as secrets_module

    principal = _principal(request)
    # v1.3.2 fix (found by the mandatory manual real-CLI repro, AGENTS.md non-negotiable
    # #5 — no live test caught this because every existing test exercised Protected mode
    # via scoped_users): a genuinely Anonymous server (no AV_API_TOKEN/AV_AUTH_USERS at
    # all — the overwhelmingly common single-operator OSS case) resolves EVERY caller to
    # `anonymous_principal()`, `tenant_id=None`. The original code rejected that outright
    # with a 422, which made `av token create` completely unusable on the exact
    # deployment shape most likely to use it first — nobody could ever mint a first
    # token to bootstrap into Protected-by-DB-tokens mode. Anonymous mode implicitly
    # means "there is exactly one tenant" (the same reasoning `env_principal()` already
    # applies to every `.env`-based identity, which always resolves to DEFAULT_TENANT_ID,
    # never None) — so a token minted here with no resolved tenant falls back to the same
    # default tenant, not a hard failure.
    tenant_id = principal.tenant_id or DEFAULT_TENANT_ID

    name = body.get("name")
    if not name or not isinstance(name, str):
        raise HTTPException(status_code=422, detail="name is required")
    scopes = body.get("scopes")
    if scopes is not None and not (isinstance(scopes, list) and all(isinstance(s, str) for s in scopes)):
        raise HTTPException(status_code=422, detail="scopes must be a list of strings")
    expires_at = None
    if body.get("expires_in_days") is not None:
        try:
            expires_at = utcnow_naive() + timedelta(days=int(body["expires_in_days"]))
        except (TypeError, ValueError):
            raise HTTPException(status_code=422, detail="expires_in_days must be an integer")

    raw_token = secrets_module.token_urlsafe(32)
    row = DBApiToken(
        id=_new_uuid(), tenant_id=tenant_id, user_id=principal.user_id,
        name=name, token_hash=identity_module.hash_token(raw_token),
        prefix=raw_token[:8], scopes=scopes, expires_at=expires_at,
        created_by=_identity(request), created_at=utcnow_naive(),
    )
    db.add(row)
    _audit(db, _identity(request), "token.create", None,
           {"token_id": row.id, "name": name, "scopes": scopes}, status_code=201)
    await db.commit()
    return {"status": "created", "id": row.id, "token": raw_token,
            "prefix": row.prefix, "expires_at": expires_at.isoformat() if expires_at else None}


@app.get("/api/tokens", dependencies=[Depends(require_scope("token:write"))])
async def list_api_tokens(request: Request, db: AsyncSession = Depends(get_session)):
    """Lists tokens for the CALLER's own tenant only — never another tenant's, and never
    the token hash itself (only the display `prefix`, matching `av auth list-users`'
    existing masked-token convention). Anonymous-mode callers (no resolved tenant) see
    the default tenant's tokens — the same fallback `create_api_token` uses to mint
    them in the first place; see that route's docstring for the full reasoning."""
    principal = _principal(request)
    tenant_id = principal.tenant_id or DEFAULT_TENANT_ID
    rows = (await db.execute(
        select(DBApiToken).where(DBApiToken.tenant_id == tenant_id)
        .order_by(DBApiToken.created_at.desc())
    )).scalars().all()
    return {"tokens": [_token_row_dict(r) for r in rows]}


@app.post("/api/tokens/{token_id}/revoke", dependencies=[Depends(require_scope("token:write"))])
async def revoke_api_token(token_id: str, request: Request,
                           db: AsyncSession = Depends(get_session)):
    """Revokes immediately for any FUTURE resolution; a copy already cached by
    `identity.py`'s TTL cache can outlive this by up to `AV_AUTH_CACHE_TTL_SECS` (default
    30s) — see `identity_module.invalidate_cached_token`'s docstring for why an
    admin-initiated revoke cannot clear that cache entry directly (the server never
    stores the plaintext token the cache is keyed on)."""
    principal = _principal(request)
    tenant_id = principal.tenant_id or DEFAULT_TENANT_ID
    row = (await db.execute(
        select(DBApiToken).where(DBApiToken.id == token_id)
    )).scalar_one_or_none()
    if not row or row.tenant_id != tenant_id:
        # 404, not 403: a token in another tenant is treated as not existing, not as a
        # permission you lack — the same information-hiding tradeoff a later tenancy
        # phase applies uniformly elsewhere (see development/architecture.md's Tenancy
        # Isolation contract section).
        raise HTTPException(status_code=404, detail="Token not found")
    if row.revoked_at is None:
        row.revoked_at = utcnow_naive()
        _audit(db, _identity(request), "token.revoke", None,
               {"token_id": token_id}, status_code=200)
        await db.commit()
    return {"status": "revoked", "id": token_id}


# ---------------------------------------------------------------------------
# v1.3.2 — Enterprise identity: tenants, users, roles, role bindings (`av tenant`/
# `av user`/`av role`). Every route here that names a specific tenant to act on resolves
# it from the CALLER's own Principal, exactly like `/api/tokens*` above — never from a
# client-supplied tenant_id in the body — so a caller can only ever administer their own
# tenant. There is deliberately no "platform superadmin can manage every tenant" surface
# yet: that needs a real platform-operator identity concept this phase does not build,
# and is called out honestly rather than faked with an implicit trust assumption.
# ---------------------------------------------------------------------------

def _effective_tenant_id(request: Request) -> str:
    return _principal(request).tenant_id or DEFAULT_TENANT_ID


@app.post("/api/tenants", dependencies=[Depends(require_scope("admin"))])
async def create_tenant(request: Request, body: Dict[str, Any] = Body(...),
                        db: AsyncSession = Depends(get_session)):
    """Provisions a NEW tenant — a genuinely platform-level bootstrap operation (standing
    up a fresh customer), deliberately left behind the same `admin` scope every other
    admin route uses rather than inventing a separate "platform superadmin" identity
    tier this phase doesn't otherwise build (see this section's own module-level note).
    An operator's existing unrestricted owner/admin credential is what creates the
    tenant a new customer's own admin then takes over."""
    slug = body.get("slug")
    name = body.get("name")
    if not slug or not isinstance(slug, str) or not name or not isinstance(name, str):
        raise HTTPException(status_code=422, detail="slug and name are required")
    existing = (await db.execute(select(DBTenant).where(DBTenant.slug == slug))).scalar_one_or_none()
    if existing:
        return {"status": "exists", "id": existing.id, "slug": existing.slug}
    row = DBTenant(id=_new_uuid(), slug=slug, name=name, status="active", created_at=utcnow_naive())
    db.add(row)
    _audit(db, _identity(request), "tenant.create", None, {"tenant_id": row.id, "slug": slug},
           status_code=201)
    await db.commit()
    return {"status": "created", "id": row.id, "slug": slug}


@app.get("/api/tenants/me")
async def get_my_tenant(request: Request, db: AsyncSession = Depends(get_session)):
    """The caller's own tenant — needs no scope beyond being authenticated at all
    (or Anonymous mode's implicit default tenant), matching `/api/freeze/{id}`'s GET
    precedent that reads need no scope even in Protected mode."""
    tenant_id = _effective_tenant_id(request)
    row = (await db.execute(select(DBTenant).where(DBTenant.id == tenant_id))).scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Tenant not found")
    return {"id": row.id, "slug": row.slug, "name": row.name, "status": row.status}


def _user_row_dict(row: DBUser) -> dict:
    return {"id": row.id, "username": row.username, "email": row.email,
            "display_name": row.display_name, "status": row.status, "source": row.source,
            "external_id": row.external_id,
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "last_login_at": row.last_login_at.isoformat() if row.last_login_at else None}


@app.post("/api/users", dependencies=[Depends(require_scope("user:write"))])
async def create_user(request: Request, body: Dict[str, Any] = Body(...),
                      db: AsyncSession = Depends(get_session)):
    """Provisions a LOCAL user (source='local') directly — the manual counterpart to
    JIT provisioning via SSO login or SCIM `POST /scim/v2/Users` (a later phase), for
    operators who want to create accounts by hand."""
    tenant_id = _effective_tenant_id(request)
    username = body.get("username")
    if not username or not isinstance(username, str):
        raise HTTPException(status_code=422, detail="username is required")
    existing = (await db.execute(
        select(DBUser).where(DBUser.tenant_id == tenant_id, DBUser.username == username)
    )).scalar_one_or_none()
    if existing:
        return {"status": "exists", "id": existing.id}
    row = DBUser(
        id=_new_uuid(), tenant_id=tenant_id, username=username,
        email=body.get("email"), display_name=body.get("display_name"),
        source="local", created_at=utcnow_naive(), updated_at=utcnow_naive(),
    )
    db.add(row)
    _audit(db, _identity(request), "user.create", None, {"user_id": row.id, "username": username},
           status_code=201)
    await db.commit()
    return {"status": "created", "id": row.id}


@app.get("/api/users", dependencies=[Depends(require_scope("user:write"))])
async def list_users(request: Request, db: AsyncSession = Depends(get_session)):
    tenant_id = _effective_tenant_id(request)
    rows = (await db.execute(
        select(DBUser).where(DBUser.tenant_id == tenant_id).order_by(DBUser.created_at.desc())
    )).scalars().all()
    return {"users": [_user_row_dict(r) for r in rows]}


@app.post("/api/users/{user_id}/suspend", dependencies=[Depends(require_scope("user:write"))])
async def suspend_user(user_id: str, request: Request, db: AsyncSession = Depends(get_session)):
    """Sets status='suspended' (never a hard delete — audit history and existing
    commit/run authorship attribution must survive) and revokes every live session AND
    api_token issued to this user, so a suspension takes effect immediately rather than
    only on next token expiry."""
    tenant_id = _effective_tenant_id(request)
    row = (await db.execute(
        select(DBUser).where(DBUser.id == user_id, DBUser.tenant_id == tenant_id)
    )).scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="User not found")
    row.status = "suspended"
    row.updated_at = utcnow_naive()
    from .models import DBSession as _DBSession

    now = utcnow_naive()
    await db.execute(
        DBApiToken.__table__.update()
        .where(DBApiToken.user_id == user_id, DBApiToken.revoked_at.is_(None))
        .values(revoked_at=now)
    )
    await db.execute(
        _DBSession.__table__.update()
        .where(_DBSession.user_id == user_id, _DBSession.revoked_at.is_(None))
        .values(revoked_at=now)
    )
    _audit(db, _identity(request), "user.suspend", None, {"user_id": user_id}, status_code=200)
    await db.commit()
    return {"status": "suspended", "id": user_id}


@app.get("/api/roles")
async def list_roles(request: Request, db: AsyncSession = Depends(get_session)):
    """Every role visible to the caller's tenant: the six built-in roles (`tenant_id`
    NULL, shared by all tenants) plus any custom roles the tenant defined itself. Needs
    no scope — seeing what roles EXIST is not itself a sensitive operation; granting one
    (`POST /api/role-bindings`) is."""
    tenant_id = _effective_tenant_id(request)
    rows = (await db.execute(
        select(DBRole).where(or_(DBRole.tenant_id.is_(None), DBRole.tenant_id == tenant_id))
        .order_by(DBRole.builtin.desc(), DBRole.name)
    )).scalars().all()
    return {"roles": [{"id": r.id, "name": r.name, "description": r.description,
                       "permissions": r.permissions, "builtin": r.builtin} for r in rows]}


@app.post("/api/role-bindings", dependencies=[Depends(require_scope("user:write"))])
async def create_role_binding(request: Request, body: Dict[str, Any] = Body(...),
                              db: AsyncSession = Depends(get_session)):
    """Grants ROLE to a user/group/token, at tenant or project scope — see
    `identity.py::_permissions_for_subject` for how this is read back at auth time."""
    tenant_id = _effective_tenant_id(request)
    subject_type = body.get("subject_type")
    subject_id = body.get("subject_id")
    role_id = body.get("role_id")
    if subject_type not in ("user", "group", "token") or not subject_id or not role_id:
        raise HTTPException(status_code=422,
                            detail="subject_type (user|group|token), subject_id, and "
                                   "role_id are required")
    role = (await db.execute(
        select(DBRole).where(DBRole.id == role_id,
                             or_(DBRole.tenant_id.is_(None), DBRole.tenant_id == tenant_id))
    )).scalar_one_or_none()
    if not role:
        raise HTTPException(status_code=422, detail=f"Unknown role '{role_id}' for this tenant")
    scope_type = body.get("scope_type", "tenant")
    if scope_type not in ("tenant", "project"):
        raise HTTPException(status_code=422, detail="scope_type must be 'tenant' or 'project'")
    row = DBRoleBinding(
        id=_new_uuid(), tenant_id=tenant_id, subject_type=subject_type, subject_id=subject_id,
        role_id=role_id, scope_type=scope_type, scope_id=body.get("scope_id"),
        created_by=_identity(request), created_at=utcnow_naive(),
    )
    db.add(row)
    _audit(db, _identity(request), "role_binding.create", None,
           {"subject_type": subject_type, "subject_id": subject_id, "role_id": role_id},
           status_code=201)
    await db.commit()
    return {"status": "created", "id": row.id}


@app.get("/api/role-bindings", dependencies=[Depends(require_scope("user:write"))])
async def list_role_bindings(request: Request, subject_id: Optional[str] = None,
                             db: AsyncSession = Depends(get_session)):
    tenant_id = _effective_tenant_id(request)
    stmt = select(DBRoleBinding).where(DBRoleBinding.tenant_id == tenant_id)
    if subject_id:
        stmt = stmt.where(DBRoleBinding.subject_id == subject_id)
    rows = (await db.execute(stmt.order_by(DBRoleBinding.created_at.desc()))).scalars().all()
    return {"bindings": [{"id": r.id, "subject_type": r.subject_type, "subject_id": r.subject_id,
                          "role_id": r.role_id, "scope_type": r.scope_type,
                          "scope_id": r.scope_id, "created_by": r.created_by} for r in rows]}


@app.post("/api/role-bindings/{binding_id}/revoke", dependencies=[Depends(require_scope("user:write"))])
async def revoke_role_binding(binding_id: str, request: Request,
                              db: AsyncSession = Depends(get_session)):
    tenant_id = _effective_tenant_id(request)
    row = (await db.execute(
        select(DBRoleBinding).where(DBRoleBinding.id == binding_id, DBRoleBinding.tenant_id == tenant_id)
    )).scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Role binding not found")
    await db.execute(DBRoleBinding.__table__.delete().where(DBRoleBinding.id == binding_id))
    _audit(db, _identity(request), "role_binding.revoke", None,
           {"binding_id": binding_id}, status_code=200)
    await db.commit()
    return {"status": "revoked", "id": binding_id}


@app.get("/api/auth/whoami")
async def auth_whoami(request: Request) -> Dict[str, Any]:
    """`av whoami`'s data source, and what `av login` calls right after a device-code
    flow completes to report who it just logged in as. No scope required -- the whole
    point is telling ANY caller, including a genuinely anonymous one, what identity (if
    any) the server resolved them as."""
    principal = _principal(request)
    return {
        "username": principal.username,
        "tenant_id": principal.tenant_id,
        "auth_method": principal.auth_method,
        "scopes": principal.scopes,
        "role_names": principal.role_names,
        "user_id": principal.user_id,
    }


# ---------------------------------------------------------------------------
# v1.3.3 — SSO providers (`av idp add/list/show/test/remove`). CRUD only here; the
# actual OIDC/SAML protocol handlers (login/callback/ACS/device-code) live in
# `sso_oidc.py`/`sso_saml.py`, mounted below via `include_router` — kept in their own
# modules since they need `authlib`/`pyjwt`/`pysaml2` (the optional `[sso]`/`[saml]`
# extras), which this file itself must never require just to define these CRUD routes.
# ---------------------------------------------------------------------------

@app.post("/api/sso-providers", dependencies=[Depends(require_scope("admin"))])
async def create_sso_provider(request: Request, body: Dict[str, Any] = Body(...),
                              db: AsyncSession = Depends(get_session)):
    from .sso_crypto import SecretsUnavailable, encrypt_config

    kind = body.get("kind")
    if kind not in ("oidc", "saml"):
        raise HTTPException(status_code=422, detail="kind must be 'oidc' or 'saml'")
    name = body.get("name")
    config = body.get("config") or {}
    if not name or not isinstance(config, dict):
        raise HTTPException(status_code=422, detail="name and config are required")

    try:
        stored_config = encrypt_config(config)
    except SecretsUnavailable as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    tenant_id = _effective_tenant_id(request)
    row = DBSsoProvider(tenant_id=tenant_id, kind=kind, name=name, config=stored_config,
                       enabled=body.get("enabled", True))
    db.add(row)
    _audit(db, _identity(request), "sso_provider.create", None,
          {"provider_id": row.id, "kind": kind, "name": name}, status_code=201)
    await db.commit()
    return {"id": row.id, "kind": kind, "name": name, "enabled": row.enabled}


@app.get("/api/sso-providers", dependencies=[Depends(require_scope("admin"))])
async def list_sso_providers(request: Request, db: AsyncSession = Depends(get_session)):
    from .sso_crypto import mask_config

    tenant_id = _effective_tenant_id(request)
    rows = (await db.execute(
        select(DBSsoProvider).where(DBSsoProvider.tenant_id == tenant_id)
    )).scalars().all()
    return {"providers": [
        {"id": r.id, "kind": r.kind, "name": r.name, "enabled": r.enabled,
         "config": mask_config(r.config)} for r in rows
    ]}


@app.get("/api/sso-providers/{provider_id}", dependencies=[Depends(require_scope("admin"))])
async def get_sso_provider(provider_id: str, request: Request, db: AsyncSession = Depends(get_session)):
    from .sso_crypto import mask_config

    tenant_id = _effective_tenant_id(request)
    row = (await db.execute(
        select(DBSsoProvider).where(DBSsoProvider.id == provider_id, DBSsoProvider.tenant_id == tenant_id)
    )).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="SSO provider not found")
    return {"id": row.id, "kind": row.kind, "name": row.name, "enabled": row.enabled,
            "config": mask_config(row.config)}


@app.delete("/api/sso-providers/{provider_id}", dependencies=[Depends(require_scope("admin"))])
async def delete_sso_provider(provider_id: str, request: Request, db: AsyncSession = Depends(get_session)):
    tenant_id = _effective_tenant_id(request)
    row = (await db.execute(
        select(DBSsoProvider).where(DBSsoProvider.id == provider_id, DBSsoProvider.tenant_id == tenant_id)
    )).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="SSO provider not found")
    await db.execute(DBSsoProvider.__table__.delete().where(DBSsoProvider.id == provider_id))
    _audit(db, _identity(request), "sso_provider.delete", None,
          {"provider_id": provider_id}, status_code=200)
    await db.commit()
    return {"status": "deleted", "id": provider_id}


@app.get("/api/sso-providers/{provider_id}/test", dependencies=[Depends(require_scope("admin"))])
async def test_sso_provider(provider_id: str, request: Request, db: AsyncSession = Depends(get_session)):
    """`av idp test` — reachability only (fetches the IdP's own metadata document), never
    a full login round trip (that needs a real human/browser). Reports what it can
    verify without one: the issuer/metadata endpoint is reachable and well-formed."""
    tenant_id = _effective_tenant_id(request)
    row = (await db.execute(
        select(DBSsoProvider).where(DBSsoProvider.id == provider_id, DBSsoProvider.tenant_id == tenant_id)
    )).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="SSO provider not found")

    from .sso_crypto import decrypt_config

    config = decrypt_config(row.config)
    if row.kind == "oidc":
        try:
            from .sso_oidc import _oidc_metadata

            metadata = await _oidc_metadata(config["issuer"])
            return {"ok": True, "issuer": config["issuer"],
                    "authorization_endpoint": metadata.get("authorization_endpoint"),
                    "token_endpoint": metadata.get("token_endpoint")}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
    else:
        return {"ok": bool(config.get("idp_metadata_url") or config.get("idp_metadata_xml")),
                "note": "SAML providers are verified via a real ACS round trip "
                        "(av idp test only confirms config is present for SAML)."}


try:
    from . import sso_oidc

    app.include_router(sso_oidc.router)
except ImportError:  # pragma: no cover -- httpx/pyjwt are core deps, this should never fire
    pass

try:
    from . import sso_saml

    app.include_router(sso_saml.router)
except ImportError:
    # pysaml2 (the [saml] extra) is not installed -- SAML routes simply don't exist on
    # this server, matching every other optional-dependency pattern in this codebase
    # (av watch/av doctor --compose/etc.): a 404 for an unmounted route, not a crash.
    logger.info("pysaml2 not installed -- SAML SSO routes are not mounted")

try:
    from . import scim as scim_module

    app.include_router(scim_module.router)
except ImportError:  # pragma: no cover
    pass
