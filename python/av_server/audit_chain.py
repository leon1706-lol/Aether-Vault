"""The canonical audit-log hash-chain formula (v1.3.3, WP-32) — ONE function, imported
by both migration `0016` (the historical backfill) and `database.py`'s runtime
`before_flush` listener, so the two can never silently drift into incompatible formulas.
Deliberately dependency-free (stdlib `json`/`hashlib` only) so a migration can import it
without pulling in any live-app machinery beyond this one module.

Unlike `policy_packs` (`prev_id` + `chain_hash`, a CLIENT-chosen previous pack), audit
rows chain by their own `id` column's natural order (a monotonic autoincrement primary
key) — there is no `prev_id` column here at all. Verification always walks
`ORDER BY id ASC` and recomputes cumulatively; an attacker who deletes or edits any row
breaks every chain_hash from that point forward, not just the one row.
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
    """`ts` accepts either a `datetime` or an already-ISO string (the migration's
    backfill reads raw DB rows, which may hand back either depending on driver/dialect;
    the runtime listener always has a real `datetime`)."""
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
