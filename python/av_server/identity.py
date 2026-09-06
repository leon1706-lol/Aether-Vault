"""v1.3.2 — Principal resolution: the DB-backed identity/RBAC layer alongside the
existing `.env`-based `AV_API_TOKEN`/`AV_AUTH_USERS` credentials. Purely additive --
both existing credential sources keep working unchanged and resolve to a `Principal`
here too. `Principal` is the single resolved-identity object every enterprise surface
(RBAC checks, tenancy enforcement, audit attribution) reads instead of re-deriving
identity its own way.
"""
from __future__ import annotations

import hashlib
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import and_, or_, select

# See models.py::DEFAULT_TENANT_ID's own docstring: a fixed, well-known UUID (not
# _new_uuid()) so it can appear as a literal in the RLS policy fallback (a later phase)
# and be compared against directly here, in both directions, without a DB round trip.
from .models import DBApiToken, DBRoleBinding, DBSession, DBUser, DEFAULT_TENANT_ID


def _utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def hash_token(raw_token: str) -> str:
    """The ONLY form an `api_tokens`/`sessions` token is ever compared against or stored
    as -- plaintext never touches the database. A token is shown once, at creation, and
    never again."""
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Principal:
    """One resolved identity for the lifetime of a request. `tenant_id` is `None` only
    for a genuinely unauthenticated Anonymous-mode request -- every authenticated
    identity resolves to a real tenant. Tenancy enforcement gates on `tenant_id is not
    None`, so `None` must mean "truly no identity", never "tenant unknown"."""

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
    """Wraps an identity already resolved by `server.py`'s `.env`-based path
    (`AV_API_TOKEN`/`AV_AUTH_USERS`). Always resolves to `DEFAULT_TENANT_ID`, since
    every pre-v1.3.2 identity source predates multi-tenancy and was backfilled to it."""
    return Principal(username=username, tenant_id=tenant_id, auth_method="env_token",
                      scopes=scopes)


# ---------------------------------------------------------------------------
# DB-backed resolution (api_tokens, sessions). A small in-process TTL cache keyed on the
# token hash keeps this off the hot path for repeat requests; revocation additionally
# clears the specific entry so a revoked token stops working immediately.
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
    """Callable ONLY where the raw token is actually in hand -- `av login logout`'s own
    session token, or a token creation flow revoking itself. NOT called by admin-initiated
    revocation of someone else's token (the server stores only `token_hash`, so it can't
    look up that cache entry) -- that path accepts a bounded staleness window instead
    (AUTH_CACHE_TTL_SECS). Revocation is still permanent on the next uncached resolution;
    only the *cached* copy can outlive the revoke by up to one TTL window."""
    _principal_cache.pop(hash_token(raw_token), None)


async def _permissions_for_subject(
    db, tenant_id: str, subject_type: str, subject_id: str,
) -> tuple[list[str], list[str]]:
    """Every role bound to (subject_type, subject_id) within tenant_id, unioned into one
    scopes list plus the role names themselves. `role.tenant_id IS NULL` includes the
    six built-in roles, shared by every tenant rather than duplicated per-tenant. For
    `subject_type == "user"`, also includes every role bound to any GROUP that user
    belongs to, so SSO/SCIM group→role mappings actually grant permissions."""
    from .models import DBGroupMember, DBRole

    conditions = [
        DBRoleBinding.tenant_id == tenant_id,
        DBRoleBinding.subject_type == subject_type,
        DBRoleBinding.subject_id == subject_id,
    ]
    if subject_type == "user":
        group_ids_subq = (
            select(DBGroupMember.group_id).where(DBGroupMember.user_id == subject_id)
        )
        rows = (await db.execute(
            select(DBRole.name, DBRole.permissions)
            .join(DBRoleBinding, DBRoleBinding.role_id == DBRole.id)
            .where(
                DBRoleBinding.tenant_id == tenant_id,
                or_(
                    and_(DBRoleBinding.subject_type == "user", DBRoleBinding.subject_id == subject_id),
                    and_(DBRoleBinding.subject_type == "group", DBRoleBinding.subject_id.in_(group_ids_subq)),
                ),
            )
        )).all()
    else:
        rows = (await db.execute(
            select(DBRole.name, DBRole.permissions)
            .join(DBRoleBinding, DBRoleBinding.role_id == DBRole.id)
            .where(*conditions)
        )).all()
    scopes: set[str] = set()
    role_names: list[str] = []
    for name, permissions in rows:
        role_names.append(name)
        scopes.update(permissions or [])
    return sorted(scopes), role_names


async def resolve_db_token(db, raw_token: str) -> Principal | None:
    """`api_tokens` path (`av token create`) -- the remote-administrable alternative to
    `.env`-based `AV_AUTH_USERS`. Returns `None`, never raises, for any unknown/revoked/
    expired token -- the caller falls through to the next resolution path."""
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
    """`sessions` path -- an interactive SSO login. Same shape as `resolve_db_token`,
    distinct table: a session is shorter-lived and refreshable."""
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
