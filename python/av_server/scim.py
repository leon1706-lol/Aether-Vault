"""SCIM 2.0 (RFC 7643/7644) provisioning — v1.3.3, under `/scim/v2`. Deliberately does
NOT import anything from `.server` (server.py imports THIS module at the bottom of its
own file, so a top-level import here would be a genuine circular import) -- auth, tenant
resolution, and audit logging are done locally with the same primitives `.identity`
exposes to server.py itself.

Authenticated by a dedicated `api_tokens` row carrying the `scim` scope, a provisioning
credential deliberately separate from any human user's session.

Error envelope is the SCIM standard shape, deliberately NOT this codebase's own `av`
JSON envelope -- SCIM is a foreign, versioned wire format an IdP parses by spec. A
documented exemption from the leakage sweep's envelope convention, not an oversight.
"""
from __future__ import annotations

import itertools
import os
import re
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .database import get_session
from .identity import anonymous_principal
from .models import DBAuditLog, DBGroup, DBGroupMember, DBSession, DBUser, DEFAULT_TENANT_ID, utcnow_naive

router = APIRouter()

SCHEMA_USER = "urn:ietf:params:scim:schemas:core:2.0:User"
SCHEMA_GROUP = "urn:ietf:params:scim:schemas:core:2.0:Group"
SCHEMA_LIST_RESPONSE = "urn:ietf:params:scim:api:messages:2.0:ListResponse"
SCHEMA_ERROR = "urn:ietf:params:scim:api:messages:2.0:Error"

# Mirrors server.py's own `AUDIT_ENABLED`/`_audit()` shape exactly -- duplicated rather
# than imported, to avoid a load-time circular import (see this module's docstring).
AUDIT_ENABLED = os.environ.get("AV_AUDIT_LOG", "1") not in ("", "0", "false")
_audit_seq_counter = itertools.count()


def _audit(db: AsyncSession, username: str | None, action: str,
           project_id: str | None, details: dict | None = None,
           status_code: int | None = None) -> None:
    if AUDIT_ENABLED:
        row = DBAuditLog(username=username, action=action, project_id=project_id,
                         details=details, status_code=status_code)
        row._chain_seq = next(_audit_seq_counter)
        db.add(row)


def _tenant_id(request: Request) -> str:
    principal = getattr(request.state, "principal", None) or anonymous_principal()
    return principal.tenant_id or DEFAULT_TENANT_ID


def _scim_actor(request: Request) -> str:
    return getattr(request.state, "username", None) or "scim"


async def _require_scim_scope(request: Request) -> None:
    """Mirrors `server.py::require_scope`'s exact posture: a token with no explicit
    `scopes` list resolves to `["*"]`, so this is additive -- nothing that could
    already reach a route loses access because SCIM routes now declare a required scope."""
    scopes = getattr(request.state, "scopes", None) or ["*"]
    if "*" in scopes or "scim" in scopes:
        return
    raise HTTPException(status_code=403, detail="Token lacks the 'scim' scope")


def _scim_error(status_code: int, detail: str, scim_type: str | None = None) -> JSONResponse:
    body: dict[str, Any] = {"schemas": [SCHEMA_ERROR], "detail": detail, "status": str(status_code)}
    if scim_type:
        body["scimType"] = scim_type
    return JSONResponse(status_code=status_code, content=body)


def _iso(dt) -> str | None:
    return dt.isoformat() + "Z" if dt is not None else None


def _first_email(body: dict) -> str | None:
    emails = body.get("emails")
    if isinstance(emails, list) and emails:
        primary = next((e for e in emails if isinstance(e, dict) and e.get("primary")), emails[0])
        if isinstance(primary, dict):
            return primary.get("value")
    return body.get("email")


def _user_resource(user: DBUser, base_url: str) -> dict:
    return {
        "schemas": [SCHEMA_USER],
        "id": user.id,
        "externalId": user.external_id,
        "userName": user.username,
        "displayName": user.display_name,
        "emails": [{"value": user.email, "primary": True}] if user.email else [],
        "active": user.status != "suspended",
        "meta": {
            "resourceType": "User",
            "created": _iso(user.created_at),
            "lastModified": _iso(user.updated_at or user.created_at),
            "location": f"{base_url}/scim/v2/Users/{user.id}",
            "version": f'W/"{int((user.updated_at or user.created_at or utcnow_naive()).timestamp())}"',
        },
    }


async def _group_members(db: AsyncSession, group_id: str) -> list[dict]:
    rows = (await db.execute(
        select(DBUser.id, DBUser.username)
        .join(DBGroupMember, DBGroupMember.user_id == DBUser.id)
        .where(DBGroupMember.group_id == group_id)
    )).all()
    return [{"value": uid, "display": uname, "type": "User"} for uid, uname in rows]


def _group_resource(group: DBGroup, members: list[dict], base_url: str) -> dict:
    return {
        "schemas": [SCHEMA_GROUP],
        "id": group.id,
        "externalId": group.external_id,
        "displayName": group.name,
        "members": members,
        "meta": {
            "resourceType": "Group",
            "created": _iso(group.created_at),
            "location": f"{base_url}/scim/v2/Groups/{group.id}",
        },
    }


# eq is the only operator real IdPs send for the two required-to-support attributes
# (userName, externalId); an unsupported filter is reported as 400 invalidFilter.
_FILTER_RE = re.compile(r'^\s*([\w.]+)\s+eq\s+"((?:[^"\\]|\\.)*)"\s*$', re.IGNORECASE)


def _parse_filter(filter_str: str | None) -> tuple[str, str] | None:
    if not filter_str:
        return None
    m = _FILTER_RE.match(filter_str)
    if not m:
        raise ValueError(f"Unsupported SCIM filter (only '<attr> eq \"value\"' is supported): {filter_str!r}")
    return m.group(1).lower(), m.group(2).replace('\\"', '"')


def _list_response(resources: list[dict], total: int, start_index: int, count: int) -> dict:
    return {
        "schemas": [SCHEMA_LIST_RESPONSE],
        "totalResults": total,
        "startIndex": start_index,
        "itemsPerPage": len(resources),
        "Resources": resources,
    }


async def _set_user_active(db: AsyncSession, user: DBUser, active: bool) -> None:
    """The deprovisioning path a real IdP drives (`PATCH {"active": false}`): suspend,
    never hard-delete, so audit history and authorship attribution survive. Revokes
    every live session immediately."""
    was_suspended = user.status == "suspended"
    user.status = "active" if active else "suspended"
    if not active and not was_suspended:
        await db.execute(
            DBSession.__table__.update()
            .where(DBSession.user_id == user.id, DBSession.revoked_at.is_(None))
            .values(revoked_at=utcnow_naive())
        )


# ---------------------------------------------------------------------------
# Discovery endpoints
# ---------------------------------------------------------------------------

@router.get("/scim/v2/ServiceProviderConfig")
async def scim_service_provider_config(_: None = Depends(_require_scim_scope)):
    return {
        "schemas": ["urn:ietf:params:scim:schemas:core:2.0:ServiceProviderConfig"],
        "patch": {"supported": True},
        "bulk": {"supported": False, "maxOperations": 0, "maxPayloadSize": 0},
        "filter": {"supported": True, "maxResults": 200},
        "changePassword": {"supported": False},
        "sort": {"supported": False},
        "etag": {"supported": True},
        "authenticationSchemes": [{
            "type": "oauthbearertoken", "name": "OAuth Bearer Token",
            "description": "An av api_tokens credential carrying the 'scim' scope "
                           "(minted via `av scim token create`).",
        }],
    }


@router.get("/scim/v2/ResourceTypes")
async def scim_resource_types(_: None = Depends(_require_scim_scope)):
    resources = [
        {"schemas": ["urn:ietf:params:scim:schemas:core:2.0:ResourceType"], "id": "User",
         "name": "User", "endpoint": "/scim/v2/Users", "schema": SCHEMA_USER},
        {"schemas": ["urn:ietf:params:scim:schemas:core:2.0:ResourceType"], "id": "Group",
         "name": "Group", "endpoint": "/scim/v2/Groups", "schema": SCHEMA_GROUP},
    ]
    return _list_response(resources, len(resources), 1, len(resources))


@router.get("/scim/v2/Schemas")
async def scim_schemas(_: None = Depends(_require_scim_scope)):
    resources = [
        {"id": SCHEMA_USER, "name": "User", "attributes": []},
        {"id": SCHEMA_GROUP, "name": "Group", "attributes": []},
    ]
    return _list_response(resources, len(resources), 1, len(resources))


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------

@router.get("/scim/v2/Users")
async def scim_list_users(
    request: Request, db: AsyncSession = Depends(get_session),
    filter: str | None = Query(None), startIndex: int = Query(1, ge=1),
    count: int = Query(100, ge=0, le=200), _: None = Depends(_require_scim_scope),
):
    tenant_id = _tenant_id(request)
    stmt = select(DBUser).where(DBUser.tenant_id == tenant_id)
    try:
        parsed = _parse_filter(filter)
    except ValueError as exc:
        return _scim_error(400, str(exc), "invalidFilter")
    if parsed:
        attr, value = parsed
        if attr == "username":
            stmt = stmt.where(DBUser.username == value)
        elif attr == "externalid":
            stmt = stmt.where(DBUser.external_id == value)
        elif attr in ("emails.value", "email"):
            stmt = stmt.where(DBUser.email == value)
        else:
            return _scim_error(400, f"Unsupported filter attribute: {attr}", "invalidFilter")

    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    rows = (await db.execute(
        stmt.order_by(DBUser.created_at).offset(startIndex - 1).limit(count)
    )).scalars().all()
    base_url = str(request.base_url).rstrip("/")
    return _list_response([_user_resource(u, base_url) for u in rows], total, startIndex, count)


@router.get("/scim/v2/Users/{user_id}")
async def scim_get_user(user_id: str, request: Request, db: AsyncSession = Depends(get_session),
                        _: None = Depends(_require_scim_scope)):
    tenant_id = _tenant_id(request)
    user = (await db.execute(
        select(DBUser).where(DBUser.id == user_id, DBUser.tenant_id == tenant_id)
    )).scalar_one_or_none()
    if user is None:
        return _scim_error(404, "User not found")
    return _user_resource(user, str(request.base_url).rstrip("/"))


@router.post("/scim/v2/Users")
async def scim_create_user(request: Request, body: dict = Body(...), db: AsyncSession = Depends(get_session),
                           _: None = Depends(_require_scim_scope)):
    """A 409 uniqueness conflict on a repeat POST is what makes IdP retries safe: the
    standard SCIM client behavior on 409 is to fall back to GET+PATCH, so a retried
    provisioning sync converges on one row, never a duplicate."""
    tenant_id = _tenant_id(request)
    username = body.get("userName")
    if not username:
        return _scim_error(400, "userName is required", "invalidValue")
    existing = (await db.execute(
        select(DBUser).where(DBUser.tenant_id == tenant_id, DBUser.username == username)
    )).scalar_one_or_none()
    if existing is not None:
        return _scim_error(409, f"User {username!r} already exists", "uniqueness")

    user = DBUser(
        tenant_id=tenant_id, username=username, email=_first_email(body),
        display_name=body.get("displayName"),
        status="active" if body.get("active", True) else "suspended",
        source="scim", external_id=body.get("externalId"),
    )
    db.add(user)
    await db.flush()
    _audit(db, _scim_actor(request), "scim.user.create", None,
                      {"user_id": user.id, "username": username}, status_code=201)
    await db.commit()
    return JSONResponse(status_code=201, content=_user_resource(user, str(request.base_url).rstrip("/")))


@router.put("/scim/v2/Users/{user_id}")
async def scim_replace_user(user_id: str, request: Request, body: dict = Body(...),
                            db: AsyncSession = Depends(get_session), _: None = Depends(_require_scim_scope)):
    tenant_id = _tenant_id(request)
    user = (await db.execute(
        select(DBUser).where(DBUser.id == user_id, DBUser.tenant_id == tenant_id)
    )).scalar_one_or_none()
    if user is None:
        return _scim_error(404, "User not found")

    if body.get("userName"):
        user.username = body["userName"]
    if "displayName" in body:
        user.display_name = body["displayName"]
    email = _first_email(body)
    if email is not None:
        user.email = email
    if "externalId" in body:
        user.external_id = body["externalId"]
    await _set_user_active(db, user, bool(body.get("active", True)))
    user.updated_at = utcnow_naive()

    _audit(db, _scim_actor(request), "scim.user.replace", None,
                      {"user_id": user.id}, status_code=200)
    await db.commit()
    return _user_resource(user, str(request.base_url).rstrip("/"))


@router.patch("/scim/v2/Users/{user_id}")
async def scim_patch_user(user_id: str, request: Request, body: dict = Body(...),
                          db: AsyncSession = Depends(get_session), _: None = Depends(_require_scim_scope)):
    """Handles the shapes real IdPs actually send: a `path`-qualified single-attribute
    op (Okta's/Entra's deprovisioning PATCH) and a path-less whole-object `replace`.
    Full SCIM PATCH path-filter grammar is intentionally not implemented."""
    tenant_id = _tenant_id(request)
    user = (await db.execute(
        select(DBUser).where(DBUser.id == user_id, DBUser.tenant_id == tenant_id)
    )).scalar_one_or_none()
    if user is None:
        return _scim_error(404, "User not found")

    operations = body.get("Operations") or []
    for op in operations:
        path = (op.get("path") or "").strip().lower()
        value = op.get("value")
        if path == "active":
            await _set_user_active(db, user, bool(value))
        elif path == "displayname":
            user.display_name = value
        elif path == "username":
            user.username = value
        elif path == "externalid":
            user.external_id = value
        elif path == "" and isinstance(value, dict):
            if "active" in value:
                await _set_user_active(db, user, bool(value["active"]))
            if "displayName" in value:
                user.display_name = value["displayName"]
            if "externalId" in value:
                user.external_id = value["externalId"]
            email = _first_email(value)
            if email:
                user.email = email
    user.updated_at = utcnow_naive()

    _audit(db, _scim_actor(request), "scim.user.patch", None,
                      {"user_id": user.id, "operations": len(operations)}, status_code=200)
    await db.commit()
    return _user_resource(user, str(request.base_url).rstrip("/"))


@router.delete("/scim/v2/Users/{user_id}", status_code=204)
async def scim_delete_user(user_id: str, request: Request, db: AsyncSession = Depends(get_session),
                           _: None = Depends(_require_scim_scope)):
    """A SCIM DELETE suspends and revokes sessions rather than physically removing the
    row, so audit history and authorship attribution survive -- a documented deviation
    from a literal reading of RFC 7644 §3.6."""
    tenant_id = _tenant_id(request)
    user = (await db.execute(
        select(DBUser).where(DBUser.id == user_id, DBUser.tenant_id == tenant_id)
    )).scalar_one_or_none()
    if user is None:
        return _scim_error(404, "User not found")
    await _set_user_active(db, user, False)
    user.updated_at = utcnow_naive()
    _audit(db, _scim_actor(request), "scim.user.delete", None,
                      {"user_id": user.id}, status_code=204)
    await db.commit()
    return JSONResponse(status_code=204, content=None)


# ---------------------------------------------------------------------------
# Groups
# ---------------------------------------------------------------------------

@router.get("/scim/v2/Groups")
async def scim_list_groups(
    request: Request, db: AsyncSession = Depends(get_session),
    filter: str | None = Query(None), startIndex: int = Query(1, ge=1),
    count: int = Query(100, ge=0, le=200), _: None = Depends(_require_scim_scope),
):
    tenant_id = _tenant_id(request)
    stmt = select(DBGroup).where(DBGroup.tenant_id == tenant_id)
    try:
        parsed = _parse_filter(filter)
    except ValueError as exc:
        return _scim_error(400, str(exc), "invalidFilter")
    if parsed:
        attr, value = parsed
        if attr == "displayname":
            stmt = stmt.where(DBGroup.name == value)
        elif attr == "externalid":
            stmt = stmt.where(DBGroup.external_id == value)
        else:
            return _scim_error(400, f"Unsupported filter attribute: {attr}", "invalidFilter")

    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    rows = (await db.execute(
        stmt.order_by(DBGroup.created_at).offset(startIndex - 1).limit(count)
    )).scalars().all()
    base_url = str(request.base_url).rstrip("/")
    resources = [_group_resource(g, await _group_members(db, g.id), base_url) for g in rows]
    return _list_response(resources, total, startIndex, count)


@router.get("/scim/v2/Groups/{group_id}")
async def scim_get_group(group_id: str, request: Request, db: AsyncSession = Depends(get_session),
                         _: None = Depends(_require_scim_scope)):
    tenant_id = _tenant_id(request)
    group = (await db.execute(
        select(DBGroup).where(DBGroup.id == group_id, DBGroup.tenant_id == tenant_id)
    )).scalar_one_or_none()
    if group is None:
        return _scim_error(404, "Group not found")
    members = await _group_members(db, group.id)
    return _group_resource(group, members, str(request.base_url).rstrip("/"))


async def _replace_membership(db: AsyncSession, group: DBGroup, member_ids: list[str]) -> None:
    existing = {
        row[0] for row in (await db.execute(
            select(DBGroupMember.user_id).where(DBGroupMember.group_id == group.id)
        )).all()
    }
    target = set(member_ids)
    for user_id in existing - target:
        await db.execute(
            DBGroupMember.__table__.delete().where(
                DBGroupMember.group_id == group.id, DBGroupMember.user_id == user_id
            )
        )
    for user_id in target - existing:
        db.add(DBGroupMember(group_id=group.id, user_id=user_id))


@router.post("/scim/v2/Groups")
async def scim_create_group(request: Request, body: dict = Body(...), db: AsyncSession = Depends(get_session),
                            _: None = Depends(_require_scim_scope)):
    tenant_id = _tenant_id(request)
    display_name = body.get("displayName")
    if not display_name:
        return _scim_error(400, "displayName is required", "invalidValue")
    existing = (await db.execute(
        select(DBGroup).where(DBGroup.tenant_id == tenant_id, DBGroup.name == display_name)
    )).scalar_one_or_none()
    if existing is not None:
        return _scim_error(409, f"Group {display_name!r} already exists", "uniqueness")

    group = DBGroup(tenant_id=tenant_id, name=display_name, external_id=body.get("externalId"), source="scim")
    db.add(group)
    await db.flush()
    member_ids = [m["value"] for m in (body.get("members") or []) if isinstance(m, dict) and m.get("value")]
    await _replace_membership(db, group, member_ids)
    _audit(db, _scim_actor(request), "scim.group.create", None,
                      {"group_id": group.id, "name": display_name}, status_code=201)
    await db.commit()
    members = await _group_members(db, group.id)
    return JSONResponse(status_code=201, content=_group_resource(group, members, str(request.base_url).rstrip("/")))


@router.put("/scim/v2/Groups/{group_id}")
async def scim_replace_group(group_id: str, request: Request, body: dict = Body(...),
                             db: AsyncSession = Depends(get_session), _: None = Depends(_require_scim_scope)):
    tenant_id = _tenant_id(request)
    group = (await db.execute(
        select(DBGroup).where(DBGroup.id == group_id, DBGroup.tenant_id == tenant_id)
    )).scalar_one_or_none()
    if group is None:
        return _scim_error(404, "Group not found")
    if body.get("displayName"):
        group.name = body["displayName"]
    if "externalId" in body:
        group.external_id = body["externalId"]
    member_ids = [m["value"] for m in (body.get("members") or []) if isinstance(m, dict) and m.get("value")]
    await _replace_membership(db, group, member_ids)
    _audit(db, _scim_actor(request), "scim.group.replace", None,
                      {"group_id": group.id}, status_code=200)
    await db.commit()
    members = await _group_members(db, group.id)
    return _group_resource(group, members, str(request.base_url).rstrip("/"))


@router.patch("/scim/v2/Groups/{group_id}")
async def scim_patch_group(group_id: str, request: Request, body: dict = Body(...),
                           db: AsyncSession = Depends(get_session), _: None = Depends(_require_scim_scope)):
    """Handles the member-management PATCH shape real IdPs send for group sync:
    `{"op": "add"/"remove", "path": "members", "value": [{"value": "<user_id>"}]}`."""
    tenant_id = _tenant_id(request)
    group = (await db.execute(
        select(DBGroup).where(DBGroup.id == group_id, DBGroup.tenant_id == tenant_id)
    )).scalar_one_or_none()
    if group is None:
        return _scim_error(404, "Group not found")

    operations = body.get("Operations") or []
    for op in operations:
        op_name = (op.get("op") or "").lower()
        path = (op.get("path") or "").strip().lower()
        value = op.get("value")
        if path != "members":
            if path == "displayname":
                group.name = value
            continue
        member_ids = [m["value"] for m in (value or []) if isinstance(m, dict) and m.get("value")]
        if op_name == "remove":
            for user_id in member_ids:
                await db.execute(
                    DBGroupMember.__table__.delete().where(
                        DBGroupMember.group_id == group.id, DBGroupMember.user_id == user_id
                    )
                )
        else:  # add / replace
            if op_name == "replace":
                await _replace_membership(db, group, member_ids)
            else:
                existing = {
                    row[0] for row in (await db.execute(
                        select(DBGroupMember.user_id).where(DBGroupMember.group_id == group.id)
                    )).all()
                }
                for user_id in member_ids:
                    if user_id not in existing:
                        db.add(DBGroupMember(group_id=group.id, user_id=user_id))

    _audit(db, _scim_actor(request), "scim.group.patch", None,
                      {"group_id": group.id, "operations": len(operations)}, status_code=200)
    await db.commit()
    members = await _group_members(db, group.id)
    return _group_resource(group, members, str(request.base_url).rstrip("/"))


@router.delete("/scim/v2/Groups/{group_id}", status_code=204)
async def scim_delete_group(group_id: str, request: Request, db: AsyncSession = Depends(get_session),
                            _: None = Depends(_require_scim_scope)):
    tenant_id = _tenant_id(request)
    group = (await db.execute(
        select(DBGroup).where(DBGroup.id == group_id, DBGroup.tenant_id == tenant_id)
    )).scalar_one_or_none()
    if group is None:
        return _scim_error(404, "Group not found")
    await db.execute(DBGroupMember.__table__.delete().where(DBGroupMember.group_id == group.id))
    await db.execute(DBGroup.__table__.delete().where(DBGroup.id == group.id))
    _audit(db, _scim_actor(request), "scim.group.delete", None,
                      {"group_id": group_id}, status_code=204)
    await db.commit()
    return JSONResponse(status_code=204, content=None)
