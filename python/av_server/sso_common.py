"""Shared session-issuance/user-provisioning logic for BOTH OIDC (`sso_oidc.py`) and
SAML (`sso_saml.py`) — v1.3.3 (WP-11/WP-13). One function each so session issuance,
audit, and group→role mapping exist ONCE, not once per protocol.
"""
from __future__ import annotations

import os
import re
import secrets
from datetime import timedelta

from sqlalchemy import select

from .identity import hash_token
from .models import (
    DBGroup,
    DBGroupMember,
    DBRole,
    DBRoleBinding,
    DBSession,
    DBSsoProvider,
    DBUser,
    DBUserIdentity,
    utcnow_naive,
)

SESSION_TTL_SECS = int(os.environ.get("AV_SESSION_TTL_SECS", str(8 * 3600)))


class SsoProvisioningError(Exception):
    """Raised for any claims-processing failure the caller (an OIDC callback / SAML ACS
    handler) turns into a clean HTTP error — never a raw 500 for something as
    routine as "this IdP subject has no linked account and JIT is off."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _derive_username(email: str | None, subject: str) -> str:
    if email and "@" in email:
        local = email.split("@", 1)[0]
        candidate = re.sub(r"[^a-zA-Z0-9_.-]", "", local)
        if candidate:
            return candidate
    return f"sso-{subject[:12]}"


async def _resolve_role_by_name(db, tenant_id: str, role_name: str) -> DBRole | None:
    """A tenant's own custom role takes priority over a same-named built-in one (a
    tenant can locally shadow `reviewer` with its own stricter definition); falls back
    to the shared built-in (`tenant_id IS NULL`, migration 0011's seed)."""
    tenant_role = (await db.execute(
        select(DBRole).where(DBRole.tenant_id == tenant_id, DBRole.name == role_name)
    )).scalar_one_or_none()
    if tenant_role is not None:
        return tenant_role
    return (await db.execute(
        select(DBRole).where(DBRole.tenant_id.is_(None), DBRole.name == role_name)
    )).scalar_one_or_none()


async def sync_groups_and_role_bindings(
    db, provider: DBSsoProvider, user: DBUser, group_names: list[str],
) -> None:
    """Mirrors the IdP's current group list onto `groups`/`group_members` (source=`sso`)
    and, for any group present in `provider.config["group_role_map"]`, ensures a
    `role_bindings` row exists granting that role at tenant scope. Membership is
    replaced wholesale on every login (the IdP's assertion/claims are authoritative for
    "right now") — a group the user is no longer in gets its `group_members` row
    removed, which `identity.py::_permissions_for_subject`'s group-expansion (v1.3.3
    fix) means their EFFECTIVE permissions shrink immediately on next resolution, not
    just their group list."""
    group_role_map: dict = (provider.config or {}).get("group_role_map") or {}

    existing_group_ids = {
        row[0] for row in (await db.execute(
            select(DBGroupMember.group_id).where(DBGroupMember.user_id == user.id)
        )).all()
    }
    target_groups: dict[str, DBGroup] = {}
    for name in group_names:
        group = (await db.execute(
            select(DBGroup).where(DBGroup.tenant_id == user.tenant_id, DBGroup.name == name)
        )).scalar_one_or_none()
        if group is None:
            group = DBGroup(tenant_id=user.tenant_id, name=name, source="sso")
            db.add(group)
            await db.flush()
        target_groups[group.id] = group

    # Drop memberships for groups no longer asserted.
    for group_id in existing_group_ids - set(target_groups):
        await db.execute(
            DBGroupMember.__table__.delete().where(
                DBGroupMember.group_id == group_id, DBGroupMember.user_id == user.id
            )
        )

    for group_id, group in target_groups.items():
        if group_id not in existing_group_ids:
            db.add(DBGroupMember(group_id=group_id, user_id=user.id))

        mapped_role_name = group_role_map.get(group.name)
        if not mapped_role_name:
            continue
        role = await _resolve_role_by_name(db, user.tenant_id, mapped_role_name)
        if role is None:
            continue  # a misconfigured group_role_map entry -- not fatal to login
        existing_binding = (await db.execute(
            select(DBRoleBinding).where(
                DBRoleBinding.tenant_id == user.tenant_id,
                DBRoleBinding.subject_type == "group",
                DBRoleBinding.subject_id == group_id,
                DBRoleBinding.role_id == role.id,
                DBRoleBinding.scope_type == "tenant",
            )
        )).scalar_one_or_none()
        if existing_binding is None:
            db.add(DBRoleBinding(
                tenant_id=user.tenant_id, subject_type="group", subject_id=group_id,
                role_id=role.id, scope_type="tenant", created_by=f"sso:{provider.id}",
            ))


async def upsert_user_from_claims(
    db, provider: DBSsoProvider, issuer: str, subject: str,
    email: str | None, name: str | None, groups: list[str] | None,
) -> DBUser:
    """The ONE place an IdP's asserted identity becomes a local `DBUser` — shared by
    OIDC's ID-token claims and SAML's assertion attributes alike, so a subject already
    linked via one protocol and later logging in via the other (unusual, but the schema
    allows it: `user_identities` keys on (provider_id, subject), not the user) still
    resolves correctly.

    JIT provisioning is opt-in PER PROVIDER (`provider.config["jit_provisioning"]`) —
    off means an unknown (provider_id, subject) pair is rejected outright rather than
    silently creating a user, exactly as the design called for."""
    identity_row = (await db.execute(
        select(DBUserIdentity).where(
            DBUserIdentity.provider_id == provider.id, DBUserIdentity.subject == subject,
        )
    )).scalar_one_or_none()

    if identity_row is not None:
        user = (await db.execute(
            select(DBUser).where(DBUser.id == identity_row.user_id)
        )).scalar_one_or_none()
        if user is None:
            raise SsoProvisioningError(
                "identity_orphaned",
                "This IdP identity is linked to a user that no longer exists.",
            )
    else:
        if not (provider.config or {}).get("jit_provisioning"):
            raise SsoProvisioningError(
                "jit_disabled",
                "No local account is linked to this identity, and just-in-time "
                "provisioning is disabled for this provider. Ask an admin to "
                "provision your account first.",
            )
        user = None
        if email:
            user = (await db.execute(
                select(DBUser).where(DBUser.tenant_id == provider.tenant_id, DBUser.email == email)
            )).scalar_one_or_none()
        if user is None:
            user = DBUser(
                tenant_id=provider.tenant_id, username=_derive_username(email, subject),
                email=email, display_name=name, source="sso",
            )
            db.add(user)
            await db.flush()
        db.add(DBUserIdentity(
            user_id=user.id, provider_id=provider.id, issuer=issuer, subject=subject, email=email,
        ))

    if user.status == "suspended":
        raise SsoProvisioningError("user_suspended", "This user account is suspended.")

    if groups:
        await sync_groups_and_role_bindings(db, provider, user, groups)

    user.last_login_at = utcnow_naive()
    return user


async def issue_session(
    db, user: DBUser, ip: str | None = None, user_agent: str | None = None,
) -> str:
    """Returns the RAW session token (shown/redirected exactly once, never persisted in
    plaintext — matches `api_tokens`' own rule). `resolve_session()` (identity.py,
    already built and wired into `require_token`) is what checks it back in on every
    subsequent request."""
    raw = secrets.token_urlsafe(32)
    db.add(DBSession(
        user_id=user.id, tenant_id=user.tenant_id, token_hash=hash_token(raw),
        expires_at=utcnow_naive() + timedelta(seconds=SESSION_TTL_SECS),
        ip=ip, user_agent=user_agent,
    ))
    return raw
