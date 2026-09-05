"""OIDC login (authorization code + PKCE) and the CLI's device-code flow — v1.3.3
(WP-11/WP-12). Mounted into `server.py` via `include_router`; imports `authlib`/`pyjwt`
lazily (the `[sso]` extra) so a deployment that never configures an OIDC provider never
needs either installed.

**JIT provisioning, claim mapping, and group→role mapping** are all per-provider,
configurable via `sso_providers.config` (`av idp add`) — none of it is hardcoded here;
this module is the protocol mechanics, `sso_common.py` is the policy.
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import time
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import DBSsoProvider
from .sso_common import SsoProvisioningError, issue_session, upsert_user_from_claims
from .sso_crypto import decrypt_config

router = APIRouter()

# Signed short-TTL state, carried as a cookie through the redirect round-trip to the IdP
# and back — NOT server-side session storage (this server is otherwise stateless across
# requests), and not a database row either (this state is meaningless the instant the
# callback completes, and expires in minutes regardless). HMAC-signed with AV_SECRET_KEY
# so a client can't forge a callback claiming a `code_verifier`/`nonce` it didn't
# actually receive from `/login`.
STATE_TTL_SECS = 600
STATE_COOKIE_PREFIX = "av_oidc_state_"

DEVICE_CODE_TTL_SECS = int(os.environ.get("AV_DEVICE_CODE_TTL_SECS", "600"))
DEVICE_POLL_INTERVAL_SECS = 5


def _secret_key() -> bytes:
    raw = os.environ.get("AV_SECRET_KEY")
    if not raw:
        raise HTTPException(
            status_code=500,
            detail="AV_SECRET_KEY must be set on the server to use SSO login "
                   "(signs the short-lived OIDC state cookie).",
        )
    return raw.encode()


def _sign_state(payload: dict) -> str:
    import base64
    import hmac as hmac_mod

    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()
    sig = hmac_mod.new(_secret_key(), body.encode(), hashlib.sha256).hexdigest()
    return f"{body}.{sig}"


def _verify_state(token: str) -> dict:
    import base64
    import hmac as hmac_mod

    try:
        body, sig = token.rsplit(".", 1)
        expected = hmac_mod.new(_secret_key(), body.encode(), hashlib.sha256).hexdigest()
        if not hmac_mod.compare_digest(sig, expected):
            raise ValueError("bad signature")
        payload = json.loads(base64.urlsafe_b64decode(body.encode()))
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid or expired OIDC state") from None
    if time.time() > payload.get("exp", 0):
        raise HTTPException(status_code=400, detail="OIDC state has expired — please try logging in again")
    return payload


async def _get_provider(db: AsyncSession, provider_id: str, kind: str = "oidc") -> DBSsoProvider:
    provider = (await db.execute(
        select(DBSsoProvider).where(DBSsoProvider.id == provider_id, DBSsoProvider.kind == kind)
    )).scalar_one_or_none()
    if provider is None or not provider.enabled:
        raise HTTPException(status_code=404, detail=f"No enabled {kind} provider {provider_id!r}")
    return provider


_metadata_cache: dict[str, tuple[float, dict]] = {}
_METADATA_CACHE_TTL = 3600


async def _oidc_metadata(issuer: str) -> dict:
    """`.well-known/openid-configuration`, cached per-issuer for an hour — every login
    would otherwise cost an extra round trip to the IdP for data that changes, in
    practice, approximately never."""
    cached = _metadata_cache.get(issuer)
    if cached and time.time() < cached[0]:
        return cached[1]
    url = issuer.rstrip("/") + "/.well-known/openid-configuration"
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        metadata = resp.json()
    _metadata_cache[issuer] = (time.time() + _METADATA_CACHE_TTL, metadata)
    return metadata


_jwks_cache: dict[str, tuple[float, dict]] = {}


async def _jwks(jwks_uri: str) -> dict:
    cached = _jwks_cache.get(jwks_uri)
    if cached and time.time() < cached[0]:
        return cached[1]
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(jwks_uri)
        resp.raise_for_status()
        keys = resp.json()
    _jwks_cache[jwks_uri] = (time.time() + _METADATA_CACHE_TTL, keys)
    return keys


def _redirect_uri(request: Request, provider_id: str) -> str:
    base = os.environ.get("AV_PUBLIC_URL") or str(request.base_url).rstrip("/")
    return f"{base}/api/auth/oidc/{provider_id}/callback"


@router.get("/api/auth/oidc/{provider_id}/login")
async def oidc_login(provider_id: str, request: Request,
                     device_user_code: str | None = Query(None)):
    from .database import async_session_factory

    async with async_session_factory() as db:
        provider = await _get_provider(db, provider_id)
        config = decrypt_config(provider.config)

    code_verifier = secrets.token_urlsafe(64)[:64]
    code_challenge = _pkce_challenge(code_verifier)
    nonce = secrets.token_urlsafe(16)
    state_payload = {
        "provider_id": provider_id,
        "code_verifier": code_verifier,
        "nonce": nonce,
        "exp": time.time() + STATE_TTL_SECS,
        "device_user_code": device_user_code,
    }
    state = _sign_state(state_payload)

    metadata = await _oidc_metadata(config["issuer"])
    params = {
        "response_type": "code",
        "client_id": config["client_id"],
        "redirect_uri": _redirect_uri(request, provider_id),
        "scope": config.get("scope", "openid profile email"),
        "state": state,
        "nonce": nonce,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    auth_url = f"{metadata['authorization_endpoint']}?{urlencode(params)}"
    response = RedirectResponse(auth_url)
    response.set_cookie(
        f"{STATE_COOKIE_PREFIX}{provider_id}", state, max_age=STATE_TTL_SECS,
        httponly=True, samesite="lax",
    )
    return response


def _pkce_challenge(verifier: str) -> str:
    import base64

    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


@router.get("/api/auth/oidc/{provider_id}/callback")
async def oidc_callback(provider_id: str, request: Request,
                        code: str = Query(...), state: str = Query(...)):
    from .database import async_session_factory

    cookie_state = request.cookies.get(f"{STATE_COOKIE_PREFIX}{provider_id}")
    if not cookie_state or cookie_state != state:
        raise HTTPException(status_code=400, detail="OIDC state mismatch — possible CSRF, or an expired/reused login link")
    payload = _verify_state(state)

    async with async_session_factory() as db:
        provider = await _get_provider(db, provider_id)
        config = decrypt_config(provider.config)
        metadata = await _oidc_metadata(config["issuer"])

        async with httpx.AsyncClient(timeout=10) as client:
            token_resp = await client.post(metadata["token_endpoint"], data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": _redirect_uri(request, provider_id),
                "client_id": config["client_id"],
                "client_secret": config.get("client_secret", ""),
                "code_verifier": payload["code_verifier"],
            })
        if token_resp.status_code != 200:
            raise HTTPException(status_code=400, detail=f"IdP token exchange failed: {token_resp.text[:300]}")
        tokens = token_resp.json()
        id_token = tokens.get("id_token")
        if not id_token:
            raise HTTPException(status_code=400, detail="IdP response had no id_token")

        claims = await _verify_id_token(id_token, config, metadata, payload["nonce"])

        claim_map = config.get("claims") or {}
        email = claims.get(claim_map.get("email", "email"))
        name = claims.get(claim_map.get("name", "name"))
        groups = claims.get(claim_map.get("groups", "groups")) or []
        subject = claims["sub"]

        try:
            user = await upsert_user_from_claims(
                db, provider, issuer=config["issuer"], subject=subject,
                email=email, name=name, groups=groups,
            )
        except SsoProvisioningError as exc:
            await db.rollback()
            raise HTTPException(status_code=403, detail=exc.message) from exc

        session_token = await issue_session(
            db, user, ip=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
        )

        device_user_code = payload.get("device_user_code")
        if device_user_code:
            from . import device_flow

            await device_flow.approve(device_user_code, session_token)

        from .models import DBAuditLog

        # A real login is exactly the kind of event this system's own "immutable
        # audit trail" claim needs to actually cover -- this route is GET (an OAuth
        # callback, per spec) so `tests/test_audit_coverage.py`'s POST/PUT/PATCH/DELETE
        # sweep doesn't require it, but it's added anyway on the merits.
        db.add(DBAuditLog(username=user.username, action="auth.oidc_login", project_id=None,
                          details={"provider_id": provider.id, "user_id": user.id,
                                   "device_flow": bool(device_user_code)}, status_code=200))
        await db.commit()

    webui_url = os.environ.get("AV_WEBUI_URL", "http://localhost:3000")
    response = RedirectResponse(
        f"{webui_url}/?av_token={session_token}" if not device_user_code
        else f"{webui_url}/device-approved"
    )
    response.delete_cookie(f"{STATE_COOKIE_PREFIX}{provider_id}")
    return response


async def _verify_id_token(id_token: str, config: dict, metadata: dict, expected_nonce: str) -> dict:
    """Full validation, not a bare decode: signature (against the IdP's live JWKS),
    issuer, audience, expiry, and nonce (replay protection for this specific login
    attempt) — every one of these is a real, separately-exploitable gap if skipped."""
    import jwt as pyjwt
    from jwt import PyJWKClient

    jwks_uri = metadata["jwks_uri"]
    jwk_client = PyJWKClient(jwks_uri)
    signing_key = jwk_client.get_signing_key_from_jwt(id_token)
    claims = pyjwt.decode(
        id_token, signing_key.key, algorithms=["RS256", "ES256"],
        audience=config["client_id"], issuer=config["issuer"],
    )
    if claims.get("nonce") != expected_nonce:
        raise HTTPException(status_code=400, detail="ID token nonce mismatch — possible replay")
    return claims


# ---------------------------------------------------------------------------
# Device-code flow (WP-12) — what `av login` actually drives; a browser redirect is
# the wrong UX for a terminal. OIDC-provider-only (the concept doesn't generalize to
# SAML the same way; every real-world device-flow IdP is OAuth/OIDC-based).
# ---------------------------------------------------------------------------

@router.post("/api/auth/device/code")
async def device_code_start(body: dict):
    from . import device_flow
    from .database import async_session_factory

    provider_id = body.get("provider_id")
    if not provider_id:
        raise HTTPException(status_code=422, detail="provider_id is required")
    async with async_session_factory() as db:
        await _get_provider(db, provider_id)  # 404s early for an unknown/disabled provider

    base = os.environ.get("AV_PUBLIC_URL", "http://localhost:8000")
    result = await device_flow.create(provider_id, DEVICE_CODE_TTL_SECS)
    return {
        "device_code": result["device_code"],
        "user_code": result["user_code"],
        "verification_uri": f"{base}/api/auth/device/verify?user_code={result['user_code']}",
        "verification_uri_complete": f"{base}/api/auth/device/verify?user_code={result['user_code']}&auto=1",
        "expires_in": DEVICE_CODE_TTL_SECS,
        "interval": DEVICE_POLL_INTERVAL_SECS,
    }


@router.get("/api/auth/device/verify")
async def device_verify(user_code: str = Query(...)):
    """The page/redirect a human visits (from the CLI's printed instructions) to
    approve a pending device login — kicks off the NORMAL OIDC browser flow with the
    device's user_code threaded through the signed state cookie, so the callback knows
    to mark this device_code approved instead of (or alongside) setting a browser
    cookie session."""
    from . import device_flow

    pending = await device_flow.lookup_by_user_code(user_code)
    if pending is None:
        raise HTTPException(status_code=404, detail="Unknown or expired user_code")
    return RedirectResponse(f"/api/auth/oidc/{pending['provider_id']}/login?device_user_code={user_code}")


@router.post("/api/auth/device/token")
async def device_token(body: dict):
    from . import device_flow

    device_code = body.get("device_code")
    if not device_code:
        raise HTTPException(status_code=422, detail="device_code is required")
    status_, session_token = await device_flow.poll(device_code)
    if status_ == "pending":
        raise HTTPException(status_code=400, detail={"error": "authorization_pending"})
    if status_ == "expired":
        raise HTTPException(status_code=400, detail={"error": "expired_token"})
    return {"access_token": session_token, "token_type": "bearer"}
