"""Redis-backed device-code flow state (v1.3.3, WP-12) — deliberately NOT a new DB
table: a device code is short-lived (minutes) and inherently ephemeral, the same shape
Redis already holds for this codebase's Bloom filter and rate-limit counters. Also means
this naturally supports N replicas with no extra work — any replica can approve a code
a different replica issued, since both read/write the same Redis instance.
"""
from __future__ import annotations

import json
import secrets
import string

from .redis_cache import cache

_KEY_PREFIX = "av:device:"
_USER_CODE_ALPHABET = string.ascii_uppercase.replace("O", "").replace("I", "")  # no ambiguous chars


def _user_code() -> str:
    part = lambda: "".join(secrets.choice(_USER_CODE_ALPHABET) for _ in range(4))
    return f"{part()}-{part()}"


async def create(provider_id: str, ttl_secs: int) -> dict:
    device_code = secrets.token_urlsafe(32)
    user_code = _user_code()
    record = {"provider_id": provider_id, "status": "pending", "session_token": None}
    client = cache._client
    await client.set(f"{_KEY_PREFIX}code:{device_code}", json.dumps(record), ex=ttl_secs)
    await client.set(f"{_KEY_PREFIX}usercode:{user_code}", device_code, ex=ttl_secs)
    return {"device_code": device_code, "user_code": user_code}


async def lookup_by_user_code(user_code: str) -> dict | None:
    client = cache._client
    device_code = await client.get(f"{_KEY_PREFIX}usercode:{user_code}")
    if device_code is None:
        return None
    raw = await client.get(f"{_KEY_PREFIX}code:{device_code}")
    if raw is None:
        return None
    record = json.loads(raw)
    record["device_code"] = device_code
    return record


async def approve(user_code: str, session_token: str) -> bool:
    """Called from the OIDC callback once the human has actually logged in — flips the
    pending device code to approved and stashes the (now real) session token for the
    polling CLI to collect exactly once."""
    client = cache._client
    device_code = await client.get(f"{_KEY_PREFIX}usercode:{user_code}")
    if device_code is None:
        return False
    key = f"{_KEY_PREFIX}code:{device_code}"
    raw = await client.get(key)
    if raw is None:
        return False
    ttl = await client.ttl(key)
    record = json.loads(raw)
    record["status"] = "approved"
    record["session_token"] = session_token
    await client.set(key, json.dumps(record), ex=ttl if ttl and ttl > 0 else 60)
    return True


async def poll(device_code: str) -> tuple[str, str | None]:
    """Returns (status, session_token). status is "pending"/"approved"/"expired".
    Approval is single-use: a successful poll immediately deletes the record, so a
    session token can never be collected twice even if a client retries the same
    request (e.g. after a dropped response)."""
    client = cache._client
    key = f"{_KEY_PREFIX}code:{device_code}"
    raw = await client.get(key)
    if raw is None:
        return "expired", None
    record = json.loads(raw)
    if record["status"] != "approved":
        return "pending", None
    await client.delete(key)
    return "approved", record["session_token"]
