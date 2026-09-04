"""casobj.py — content-addressed CAS objects for non-file RSI artifacts (v1.3.1).

Generalizes the pattern env snapshots pioneered (`core.py::canonical_env_bytes` /
`env_snapshot_id` / `load_env_snapshot`): canonical sorted-keys JSON -> sha256 id -> a CAS
object under `.av/objects/<hh>/<rest>` -> uploaded through the existing object-upload path
(`core.py::upload_commit_objects`) -> referenced by id from a commit payload or another
object. Every new RSI artifact (improver manifest, change set, policy pack, eval suite,
plan, budget, lessons object, tool manifest, action log) is exactly this shape — no new
persistence mechanism, no new upload path.

Signing reuses `signing.py`'s ed25519 key material (`.av/keys/signing.pem`) but signs the
GENERIC canonical form defined here, not `signing.py`'s commit-specific timestamp-
normalized one — these documents don't have the clone/echo problem that normalization
exists for (they are never round-tripped through a server that reformats timestamps), so
signing the plain canonical bytes is correct and simpler. `signing.canonical_commit_bytes`
now delegates its own canonicalization step to `canonical_bytes()` below (see its
docstring) — this module is the shared core, commit-specific handling stays in
`signing.py`.
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
    """Writes `doc` to the CAS (idempotent — a matching object already on disk is left
    untouched) and returns its content-addressed id.

    The stored bytes are the sorted-keys JSON of the FULL doc (including any `signature`
    block, if present) so a reader gets everything back; the id itself is computed over the
    canonicalized (signature-excluded) form — mirrors
    `core.py::upload_commit_objects()`'s env-snapshot handling exactly, including writing
    exact canonical bytes rather than a pretty-printed rendering so a re-derived id always
    matches (see that function's comment about the sha256-mismatch bug this avoids).
    """
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
    """Signs `doc`'s canonical bytes with this repo's ed25519 key (same key material as
    signed commits, `signing.py`). Returns the signature blob, or None when no key is
    configured or the `[sign]` extra is missing — signing here is opt-in, never a gate by
    itself; a caller that needs to REQUIRE a valid signature checks for one via
    `verify_object()`."""
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

    Mirrors `signing.verify_signature()` exactly, but over the generic canonical form —
    same (ok, reason) contract, same "unsigned is not a failure" policy (callers decide)."""
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
