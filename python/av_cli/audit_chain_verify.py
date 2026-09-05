"""Client-side, genuinely-independent audit-chain verification (v1.3.3, WP-32) — the
logic behind `av audit verify --export`. Reuses the EXACT SAME hash formula the server
uses (`av_server.audit_chain.compute_chain_hash`, imported directly — that module is
dependency-free stdlib, so importing it here never pulls in FastAPI/SQLAlchemy/etc.)
so a local recomputation is byte-for-byte comparable to what the server itself would
compute, without asking the server to grade its own homework.
"""
from __future__ import annotations


def verify_export(rows: list[dict], public_key_hex: str | None) -> dict:
    """`rows` is the parsed jsonl export (`av audit export --format jsonl`), already in
    ascending id order (the server's export route emits oldest-first specifically so
    this holds — see server.py's `export_audit_log`). Returns the same shape the live
    `/api/admin/audit/verify` route does, so both CLI paths render identically."""
    from av_server.audit_chain import compute_chain_hash

    prev_hash = None
    signature_checks = {"verified": 0, "failed": 0, "absent": 0}
    checked = 0
    for row in rows:
        expected = compute_chain_hash(
            prev_hash, row.get("ts"), row.get("username"), row.get("action"),
            row.get("project_id"), row.get("status_code"), row.get("details"),
        )
        if expected != row.get("chain_hash"):
            return {
                "ok": False, "broken_at_id": row.get("id"), "checked": checked,
                "signature_checks": signature_checks,
            }
        signature = row.get("signature")
        if signature:
            if public_key_hex and _verify_signature(row["chain_hash"], signature, public_key_hex):
                signature_checks["verified"] += 1
            else:
                signature_checks["failed"] += 1
        else:
            signature_checks["absent"] += 1
        prev_hash = row["chain_hash"]
        checked += 1

    return {
        "ok": True, "checked": checked,
        "last_id": rows[-1]["id"] if rows else None,
        "signature_checks": signature_checks,
    }


def _verify_signature(chain_hash: str, signature_hex: str, public_key_hex: str) -> bool:
    try:
        from av_server.audit_signing import verify

        return verify(chain_hash, signature_hex, public_key_hex)
    except ImportError:
        return False
