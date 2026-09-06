"""The canonical audit-log hash-chain formula (v1.3.3) — ONE function, imported by both
migration `0016` and `database.py`'s runtime `before_flush` listener, so the two can
never silently drift. Audit rows chain by their own `id` column's natural order (no
`prev_id` column) — verification walks `ORDER BY id ASC` and recomputes cumulatively, so
deleting or editing any row breaks every chain_hash from that point forward.
"""
from __future__ import annotations

import hashlib
import json


def compute_chain_hash(
    prev_hash: str | None,
    ts,
    username: str | None,
    action: str,
    project_id: str | None,
    status_code: int | None,
    details,
) -> str:
    """`ts` accepts either a `datetime` or an already-ISO string, since the migration's
    backfill reads raw DB rows that may hand back either."""
    payload = json.dumps(
        {
            "prev": prev_hash,
            "ts": ts.isoformat() if hasattr(ts, "isoformat") else str(ts),
            "username": username,
            "action": action,
            "project_id": project_id,
            "status_code": status_code,
            "details": details,
        },
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(payload.encode()).hexdigest()
