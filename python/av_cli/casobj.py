"""casobj.py — content-addressed CAS objects for non-file RSI artifacts (v1.3.1).
Canonical sorted-keys JSON -> sha256 id -> a CAS object under `.av/objects/<hh>/<rest>`,
referenced by id from a commit payload or another object. Every RSI artifact (improver
manifest, change set, policy pack, eval suite, plan, budget, lessons object, tool
manifest, action log) uses this same shape.
"""
from __future__ import annotations

import base64
import datetime
import hashlib
import json
from pathlib import Path

DEFAULT_EXCLUDE = ("signature",)


def canonical_bytes(doc: dict, exclude: tuple = DEFAULT_EXCLUDE) -> bytes:
    """Sorted-keys JSON of `doc` minus `exclude` — the one canonicalization rule every
    CAS object (and, via `signing.canonical_commit_bytes`, every commit) shares."""
    canon = {k: v for k, v in doc.items() if k not in exclude}
    return json.dumps(canon, sort_keys=True).encode("utf-8")


def object_id(doc: dict, exclude: tuple = DEFAULT_EXCLUDE) -> str:
    """Content-addressed id: sha256 of the canonical bytes."""
    return hashlib.sha256(canonical_bytes(doc, exclude=exclude)).hexdigest()


def object_path(repo_root: Path, oid: str) -> Path:
    return repo_root / ".av" / "objects" / oid[:2] / oid[2:]


def write_object(repo_root: Path, doc: dict, exclude: tuple = DEFAULT_EXCLUDE) -> str:
    """Writes `doc` to the CAS (idempotent) and returns its content-addressed id. Stored
    bytes are the sorted-keys JSON of the full doc (including any `signature`), but the id
    itself is computed over the canonicalized, signature-excluded form."""
    oid = object_id(doc, exclude=exclude)
    path = object_path(repo_root, oid)
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(json.dumps(doc, sort_keys=True).encode("utf-8"))
    return oid


def read_object(repo_root: Path, oid: str) -> dict | None:
    """Reads a CAS object back as a dict, or None if absent/corrupt."""
    path = object_path(repo_root, oid)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def sign_object(doc: dict, repo_root: Path) -> dict | None:
    """Signs `doc`'s canonical bytes with this repo's ed25519 key (`signing.py`). Returns
    the signature blob, or None when no key is configured — signing here is opt-in."""
    from . import signing

    priv_path = signing.private_key_path(repo_root)
    if not priv_path.exists():
        return None
    try:
        serialization, _, _ = signing._crypto()
    except signing.SigningUnavailable:
        return None

    key = serialization.load_pem_private_key(priv_path.read_bytes(), password=None)
    pub_hex = key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    ).hex()
    sig = key.sign(canonical_bytes(doc))
    return {
        "algo": signing.ALGO,
        "public_key": pub_hex,
        "sig": base64.b64encode(sig).decode("ascii"),
        "signed_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }


def verify_object(doc: dict) -> tuple[bool, str]:
    """Verifies `doc`'s embedded `signature` over its canonical (signature-excluded) bytes.
    Mirrors `signing.verify_signature()`'s (ok, reason) contract; an unsigned doc isn't a
    failure by itself — callers decide."""
    from . import signing

    signature = doc.get("signature")
    if not isinstance(signature, dict):
        return False, "unsigned"
    if signature.get("algo") != signing.ALGO:
        return False, f"unsupported algo: {signature.get('algo')!r}"
    try:
        serialization, _, Ed25519PublicKey = signing._crypto()
    except signing.SigningUnavailable as exc:
        return False, str(exc)

    try:
        pub = Ed25519PublicKey.from_public_bytes(bytes.fromhex(signature["public_key"]))
    except (KeyError, ValueError, TypeError) as exc:
        return False, f"malformed public key: {exc}"
    try:
        sig = base64.b64decode(signature["sig"], validate=True)
    except Exception as exc:
        return False, f"malformed signature bytes: {exc}"

    from cryptography.exceptions import InvalidSignature

    try:
        pub.verify(sig, canonical_bytes(doc))
    except InvalidSignature:
        return False, "signature mismatch — payload was modified after signing"
    return True, "verified"
