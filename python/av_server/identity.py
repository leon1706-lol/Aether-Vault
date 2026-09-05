"""v1.3.2 — Principal resolution: the DB-backed identity/RBAC layer alongside the
existing `.env`-based `AV_API_TOKEN`/`AV_AUTH_USERS` credentials (`server.py`).

Nothing here replaces `server.py::require_token`'s existing two credential sources — both
keep working completely unchanged and resolve to a `Principal` here too (paths 1/2 below),
so this module is purely additive, matching the same shape v1.3.1's `_scopes_for_identity()`
used to make scopes additive: a token with no matching row in any new table resolves
exactly as it always has.

`Principal` is the single resolved-identity object every new enterprise surface (RBAC
permission checks, tenancy enforcement in a later phase, audit attribution) reads instead
of re-deriving identity its own way. `server.py::require_token` builds one per request and
stores it on `request.state.principal`; existing code paths keep reading
`request.state.username`/`request.state.scopes` exactly as before (see `_wire` below) —
nothing that reads those two attributes today needs to change.
"""
from __future__ import annotations

import hashlib
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import select

# See models.py::DEFAULT_TENANT_ID's own docstring: a fixed, well-known UUID (not
# _new_uuid()) so it can appear as a literal in the RLS policy fallback (a later phase)
# and be compared against directly here, in both directions, without a DB round trip.
from .models import DBApiToken, DBRoleBinding, DBSession, DBUser, DEFAULT_TENANT_ID


def _utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def hash_token(raw_token: str) -> str:
    """The ONLY form an `api_tokens`/`sessions` token is ever compared against or stored
    as — plaintext never touches the database (models.py::DBApiToken/DBSession docstrings).
    Matches `av registry keygen`'s "the private key never round-trips back out" posture:
    a token is shown once, at creation, and never again."""
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Principal:
    """One resolved identity for the lifetime of a request. `tenant_id` is `None` only
    for a genuinely unauthenticated Anonymous-mode request (`auth_method="anonymous"`) —
    every authenticated identity, `.env`-based or DB-based, resolves to a real tenant
    (the seeded `DEFAULT_TENANT_ID` for every identity source that predates tenancy).
    This is deliberate: a later phase's tenancy enforcement gates on `tenant_id is not
    None`, and `None` must mean "truly no identity", never "identity exists but its
    tenant is unknown" — those are different failure modes with different correct
    responses (fail open vs. fail closed)."""

    username: str
    tenant_id: str | None
    auth_method: str  # anonymous | env_token | db_token | session
    scopes: list[str] = field(default_factory=lambda: ["*"])
    role_names: list[str] = field(default_factory=list)
    user_id: str | None = None

    @property
    def is_owner(self) -> bool:
        return self.username == "owner"


def anonymous_principal() -> Principal:
    return Principal(username="anonymous", tenant_id=None, auth_method="anonymous",
                      scopes=["*"])


def env_principal(username: str, tenant_id: str, scopes: list[str]) -> Principal:
    """Wraps an identity already resolved by `server.py`'s existing `_resolve_identity()`/
    `_scopes_for_identity()` — the `.env`-based path (`AV_API_TOKEN`/`AV_AUTH_USERS`).
    Always resolves to `DEFAULT_TENANT_ID`: every pre-v1.3.2 identity source, by
    definition, predates multi-tenancy, and every pre-existing row across every table was
    backfilled to that same tenant (migration 0011's seed / a later migration's backfill)
    — so this is the one tenant such an identity could ever meaningfully mean."""
    return Principal(username=username, tenant_id=tenant_id, auth_method="env_token",
                      scopes=scopes)


# ---------------------------------------------------------------------------
# DB-backed resolution (api_tokens, sessions) — new in v1.3.2. A small in-process TTL
# cache keyed on the token hash keeps this off the hot path for repeat requests from the
# same client; entries self-expire, and revocation (WP-8, `av token revoke`) additionally
# clears the specific entry so a revoked token stops working immediately rather than
# waiting out the TTL.
# ---------------------------------------------------------------------------

AUTH_CACHE_TTL_SECS = float(os.environ.get("AV_AUTH_CACHE_TTL_SECS", "30"))
_principal_cache: dict[str, tuple[float, Principal | None]] = {}


def _cache_get(token_hash: str) -> tuple[bool, Principal | None]:
    entry = _principal_cache.get(token_hash)
    if entry is None:
        return False, None
    expires_at, principal = entry
    if time.monotonic() >= expires_at:
        _principal_cache.pop(token_hash, None)
        return False, None
    return True, principal


def _cache_put(token_hash: str, principal: Principal | None) -> None:
    _principal_cache[token_hash] = (time.monotonic() + AUTH_CACHE_TTL_SECS, principal)


def invalidate_cached_token(raw_token: str) -> None:
    """Callable ONLY where the raw token is actually in hand — `av login logout`'s own
    session token, or a token creation flow revoking itself. Deliberately NOT called by
    `POST /api/tokens/{id}/revoke` (admin-initiated revocation of SOMEONE ELSE's token):
    the server stores only `token_hash` (models.py::DBApiToken's own docstring — the
    plaintext is shown once, at creation, and never persisted), so an admin revoking a
    token they don't hold cannot look up its cache entry to clear it. That path accepts
    a bounded staleness window instead — AUTH_CACHE_TTL_SECS (30s default), the same
    number already documented as this cache's performance bound. Revocation is still
    permanent and takes effect on the very next uncached resolution; it is only the
    *cached* copy that can outlive the revoke by up to one TTL window. Real remote
    revocation within that window (a compromised token) should pair `av token revoke`
    with lowering `AV_AUTH_CACHE_TTL_SECS` or restarting the engine, documented in
    docs/rsi-operator-guide.md's enterprise-identity companion."""
    _principal_cache.pop(hash_token(raw_token), None)


async def _permissions_for_subject(
    db, tenant_id: str, subject_type: str, subject_id: str,
) -> tuple[list[str], list[str]]:
    """Every role bound to (subject_type, subject_id) within tenant_id, unioned into one
    scopes list plus the role names themselves (surfaced for `av whoami`/audit detail).
    `role.tenant_id IS NULL` includes the six built-in roles (migration 0011), which are
    shared by every tenant rather than duplicated per-tenant."""
    from .models import DBRole

    rows = (await db.execute(
        select(DBRole.name, DBRole.permissions)
        .join(DBRoleBinding, DBRoleBinding.role_id == DBRole.id)
        .where(
            DBRoleBinding.tenant_id == tenant_id,
            DBRoleBinding.subject_type == subject_type,
            DBRoleBinding.subject_id == subject_id,
        )
    )).all()
    scopes: set[str] = set()
    role_names: list[str] = []
    for name, permissions in rows:
        role_names.append(name)
        scopes.update(permissions or [])
    return sorted(scopes), role_names


async def resolve_db_token(db, raw_token: str) -> Principal | None:
    """`api_tokens` path (WP-8's `av token create`) — the remote-administrable
    alternative to `.env`-based `AV_AUTH_USERS`, which requires `docker compose` shell
    access on the host running the stack to create/rotate/revoke (`cmd_auth.py`).
    Returns `None` (never raises) for any unknown/revoked/expired token — the caller
    falls through to the next resolution path, exactly like a `.env` miss does today."""
    token_hash = hash_token(raw_token)
    cached, principal = _cache_get(token_hash)
    if cached:
        return principal

    row = (await db.execute(
        select(DBApiToken).where(DBApiToken.token_hash == token_hash)
    )).scalar_one_or_none()

    resolved: Principal | None = None
    if row is not None and row.revoked_at is None and (
        row.expires_at is None or row.expires_at > _utcnow_naive()
    ):
        subject_id = row.user_id or row.id  # a service token IS its own subject
        role_scopes, role_names = await _permissions_for_subject(
            db, row.tenant_id, "user" if row.user_id else "token", subject_id,
        )
        # An explicit token-level `scopes` list further RESTRICTS what the token can do
        # (an intersection, not a union) — a token minted for a narrow purpose stays
        # narrow even if its holder's roles would otherwise grant more. Absent, the
        # token carries exactly its role-derived scopes.
        scopes = sorted(set(row.scopes) & set(role_scopes)) if row.scopes else role_scopes
        username = row.user_id and (
            (await db.execute(select(DBUser.username).where(DBUser.id == row.user_id)))
            .scalar_one_or_none()
        ) or f"token:{row.name}"
        resolved = Principal(
            username=username, tenant_id=row.tenant_id, auth_method="db_token",
            scopes=scopes or ["read"], role_names=role_names, user_id=row.user_id,
        )

    _cache_put(token_hash, resolved)
    return resolved


async def resolve_session(db, raw_token: str) -> Principal | None:
    """`sessions` path — an interactive SSO login (`av login`, or the webui's browser
    flow, a later phase). Same shape as `resolve_db_token`, distinct table: a session is
    shorter-lived and refreshable, a token is a long-lived credential minted directly."""
    token_hash = hash_token(raw_token)
    cache_key = f"session:{token_hash}"
    cached, principal = _cache_get(cache_key)
    if cached:
        return principal

    row = (await db.execute(
        select(DBSession).where(DBSession.token_hash == token_hash)
    )).scalar_one_or_none()

    resolved: Principal | None = None
    if row is not None and row.revoked_at is None and row.expires_at > _utcnow_naive():
        username = (await db.execute(
            select(DBUser.username).where(DBUser.id == row.user_id)
        )).scalar_one_or_none()
        if username is not None:
            scopes, role_names = await _permissions_for_subject(
                db, row.tenant_id, "user", row.user_id,
            )
            resolved = Principal(
                username=username, tenant_id=row.tenant_id, auth_method="session",
                scopes=scopes or ["read"], role_names=role_names, user_id=row.user_id,
            )

    _cache_put(cache_key, resolved)
    return resolved
