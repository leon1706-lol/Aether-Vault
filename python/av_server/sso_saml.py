"""SAML 2.0 SP (metadata / ACS / SLS) — v1.3.3 (WP-13). Imports `pysaml2` (the `[saml]`
extra, needs the native `xmlsec1`/`libxml2` libraries — installed in the Dockerfile's
engine/server targets) at MODULE level deliberately: `server.py` wraps `import
sso_saml` in a `try/except ImportError` specifically so a deployment without the
extra installed simply never mounts these routes at all (a 404, not a crash) — see that
try/except's own comment.

Converges on the SAME `sso_common.py` functions OIDC uses (`upsert_user_from_claims`/
`issue_session`) so session issuance, JIT provisioning, and group→role mapping exist
ONCE across both protocols, not once each.
"""
from __future__ import annotations

import itertools
import os
import time

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import RedirectResponse, Response
from saml2 import BINDING_HTTP_POST, BINDING_HTTP_REDIRECT
from saml2.client import Saml2Client
from saml2.config import SPConfig
from saml2.metadata import entity_descriptor
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import DBAuditLog, DBSsoProvider
from .redis_cache import cache
from .sso_common import SsoProvisioningError, issue_session, upsert_user_from_claims
from .sso_crypto import decrypt_config

router = APIRouter()

_REPLAY_KEY_PREFIX = "av:saml:assertion:"
_REPLAY_TTL_SECS = 24 * 3600  # comfortably longer than any real assertion's validity window

# Same shape as server.py's own `AUDIT_ENABLED`/`_audit()` (and scim.py's copy) --
# duplicated, not imported, to avoid a load-time circular import (server.py imports THIS
# module at the bottom of its own file). A real login/logout is exactly the kind of
# event `tests/test_audit_coverage.py`'s sweep exists to guarantee never goes silently
# untracked -- this local helper is what makes `POST .../acs` and `POST .../sls` show up
# in that sweep as genuinely audited, not merely exempted.
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


def _base_url(request: Request) -> str:
    return os.environ.get("AV_PUBLIC_URL") or str(request.base_url).rstrip("/")


async def _get_provider(db: AsyncSession, provider_id: str) -> DBSsoProvider:
    provider = (await db.execute(
        select(DBSsoProvider).where(DBSsoProvider.id == provider_id, DBSsoProvider.kind == "saml")
    )).scalar_one_or_none()
    if provider is None or not provider.enabled:
        raise HTTPException(status_code=404, detail=f"No enabled SAML provider {provider_id!r}")
    return provider


def _sp_config(provider_id: str, config: dict, base_url: str) -> SPConfig:
    acs_url = f"{base_url}/api/auth/saml/{provider_id}/acs"
    sls_url = f"{base_url}/api/auth/saml/{provider_id}/sls"
    entity_id = config.get("sp_entity_id") or f"{base_url}/api/auth/saml/{provider_id}/metadata"

    settings: dict = {
        "entityid": entity_id,
        "service": {
            "sp": {
                "endpoints": {
                    "assertion_consumer_service": [(acs_url, BINDING_HTTP_POST)],
                    "single_logout_service": [
                        (sls_url, BINDING_HTTP_REDIRECT), (sls_url, BINDING_HTTP_POST),
                    ],
                },
                # IdP-initiated login is the common enterprise case (a user clicks a
                # tile in Okta/Entra's own dashboard, with no preceding SP AuthnRequest
                # for pysaml2 to correlate a response against) -- allow_unsolicited is
                # what makes that not fail signature/InResponseTo checks. Replay
                # protection for THIS case is handled explicitly below (Redis-backed
                # assertion-ID dedup), since pysaml2 has no InResponseTo to key off of
                # here either.
                "allow_unsolicited": True,
                "authn_requests_signed": False,
                "want_assertions_signed": True,
                "want_response_signed": False,
            },
        },
    }
    if config.get("idp_metadata_url"):
        settings["metadata"] = {"remote": [{"url": config["idp_metadata_url"]}]}
    elif config.get("idp_metadata_xml"):
        import tempfile

        # pysaml2's metadata loader takes a file path, not an inline string -- a
        # short-lived temp file is the simplest bridge; deleted immediately after load.
        with tempfile.NamedTemporaryFile(mode="w", suffix=".xml", delete=False) as f:
            f.write(config["idp_metadata_xml"])
            temp_path = f.name
        settings["metadata"] = {"local": [temp_path]}
    else:
        raise HTTPException(
            status_code=500,
            detail="SAML provider config has neither idp_metadata_url nor idp_metadata_xml",
        )

    sp_config = SPConfig()
    sp_config.load(settings)
    return sp_config


@router.get("/api/auth/saml/{provider_id}/metadata")
async def saml_metadata(provider_id: str, request: Request):
    from .database import async_session_factory

    async with async_session_factory() as db:
        provider = await _get_provider(db, provider_id)
        config = decrypt_config(provider.config)
    sp_config = _sp_config(provider_id, config, _base_url(request))
    metadata_xml = str(entity_descriptor(sp_config))
    return Response(content=metadata_xml, media_type="application/samlmetadata+xml")


@router.post("/api/auth/saml/{provider_id}/acs")
async def saml_acs(provider_id: str, request: Request):
    """Assertion Consumer Service — where the IdP POSTs the SAML response after the
    user authenticates. pysaml2's `parse_authn_request_response` does the heavy
    lifting a hand-rolled XML parser never should: signature verification,
    `NotBefore`/`NotOnOrAfter` conditions, and audience restriction. Replay protection
    (a stored assertion-ID set) is this module's own addition on top."""
    from .database import async_session_factory

    form = await request.form()
    saml_response = form.get("SAMLResponse")
    if not saml_response:
        raise HTTPException(status_code=400, detail="Missing SAMLResponse")

    async with async_session_factory() as db:
        provider = await _get_provider(db, provider_id)
        config = decrypt_config(provider.config)
        sp_config = _sp_config(provider_id, config, _base_url(request))
        client = Saml2Client(config=sp_config)

        try:
            authn_response = client.parse_authn_request_response(
                saml_response, BINDING_HTTP_POST,
            )
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"Invalid SAML response: {exc}") from exc
        if authn_response is None:
            raise HTTPException(status_code=400, detail="SAML response failed validation")

        assertion_id = authn_response.assertion.id if authn_response.assertion else authn_response.in_response_to
        if assertion_id:
            replay_key = f"{_REPLAY_KEY_PREFIX}{assertion_id}"
            # NX: only succeeds if this key does NOT already exist -- the atomic
            # test-and-set that makes this a real replay guard, not a check-then-set
            # race. A second POST of the identical assertion (a captured/replayed
            # response) is rejected outright, not silently re-processed.
            first_use = await cache._client.set(replay_key, "1", nx=True, ex=_REPLAY_TTL_SECS)
            if not first_use:
                raise HTTPException(status_code=400, detail="SAML assertion already used (replay rejected)")

        subject = authn_response.get_subject().text
        ava = authn_response.ava or {}
        claim_map = config.get("claims") or {}

        def _claim(key: str, default_attr: str):
            attr = claim_map.get(key, default_attr)
            values = ava.get(attr)
            return values[0] if values else None

        email = _claim("email", "email")
        name = _claim("name", "displayName")
        groups_attr = claim_map.get("groups", "groups")
        groups = ava.get(groups_attr) or []

        try:
            user = await upsert_user_from_claims(
                db, provider, issuer=config.get("idp_metadata_url", provider_id),
                subject=subject, email=email, name=name, groups=groups,
            )
        except SsoProvisioningError as exc:
            await db.rollback()
            raise HTTPException(status_code=403, detail=exc.message) from exc

        session_token = await issue_session(
            db, user, ip=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
        )
        _audit(db, user.username, "auth.saml_login", None,
              {"provider_id": provider.id, "user_id": user.id}, status_code=200)
        await db.commit()

    webui_url = os.environ.get("AV_WEBUI_URL", "http://localhost:3000")
    relay_state = form.get("RelayState")
    target = relay_state if relay_state else f"{webui_url}/"
    separator = "&" if "?" in target else "?"
    return RedirectResponse(f"{target}{separator}av_token={session_token}", status_code=303)


@router.get("/api/auth/saml/{provider_id}/sls")
@router.post("/api/auth/saml/{provider_id}/sls")
async def saml_sls(provider_id: str, request: Request):
    """Single Logout — best-effort: revokes THIS server's own session (via the
    LogoutRequest's NameID, matched against `sessions`/`user_identities`) and
    acknowledges the IdP's request. Does not attempt to propagate logout to OTHER
    service providers the same IdP session may be federated with (SAML's IdP-driven
    multi-SP logout fan-out) — a real, stated scope limit, not silently unhandled."""
    from .database import async_session_factory
    from .models import DBSession, DBUserIdentity, utcnow_naive

    params = dict(request.query_params) if request.method == "GET" else dict(await request.form())
    name_id = params.get("NameID") or params.get("name_id")

    if name_id:
        async with async_session_factory() as db:
            identity_row = (await db.execute(
                select(DBUserIdentity).where(DBUserIdentity.subject == name_id)
            )).scalar_one_or_none()
            if identity_row is not None:
                await db.execute(
                    DBSession.__table__.update()
                    .where(DBSession.user_id == identity_row.user_id, DBSession.revoked_at.is_(None))
                    .values(revoked_at=utcnow_naive())
                )
                _audit(db, None, "auth.saml_logout", None,
                      {"provider_id": provider_id, "user_id": identity_row.user_id}, status_code=200)
                await db.commit()

    return Response(status_code=200, content="Logged out")
