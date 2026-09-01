"""signing.py — ed25519 signed commits (v1.2.2).

Trust model (documented in SECURITY.md): **tamper evidence, not a trust network.**
A commit signed here proves the payload that reached a reader is byte-identical to
the one the signing key's owner produced. It does NOT prove who owns the key, and it
does NOT integrate with any PKI — key distribution is out of scope by design.

Canonical form: sorted-keys JSON of the commit payload minus its `signature` field,
UTF-8 encoded. The commit hash is computed BEFORE signing, so the signature binds to
a payload that already includes `hash` — tampering with anything (tree, message,
metrics, hash itself) breaks verification.

Storage: `.av/keys/signing.pem` (private, 0600 where the OS honors it) and
`.av/keys/signing.pub` (public). The `[sign]` extra (`cryptography`) is required only
when actually generating or using keys; every function degrades gracefully so a
missing extra can never break an ordinary commit.

Signatures ride commits as:
    {"algo": "ed25519", "public_key": "<hex>", "sig": "<base64>", "signed_at": iso}
The server persists the blob verbatim (commits.signature) so cloned copies verify too.
"""
from __future__ import annotations

import base64
import datetime
import json
import os
from pathlib import Path

SIGNING_KEY_PATH = "signing.pem"
PUBLIC_KEY_PATH = "signing.pub"
ALGO = "ed25519"


class SigningUnavailable(RuntimeError):
    """Raised when a caller explicitly needs crypto but the [sign] extra is missing."""


def _crypto():
    try:
        from cryptography.exceptions import InvalidSignature  # noqa: F401
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PrivateKey,
            Ed25519PublicKey,
        )

        return serialization, Ed25519PrivateKey, Ed25519PublicKey
    except ImportError as exc:  # pragma: no cover - import-guard proven by tests via stub
        raise SigningUnavailable(
            "cryptography is not installed — run `pip install aether-vault[sign]`."
        ) from exc


def keys_dir(repo_root: Path) -> Path:
    return repo_root / ".av" / "keys"


def private_key_path(repo_root: Path) -> Path:
    return keys_dir(repo_root) / SIGNING_KEY_PATH


def public_key_path(repo_root: Path) -> Path:
    return keys_dir(repo_root) / PUBLIC_KEY_PATH


def has_signing_key(repo_root: Path) -> bool:
    return private_key_path(repo_root).exists()


def generate_keypair(repo_root: Path) -> tuple[Path, Path]:
    """Generates an ed25519 keypair under .av/keys/ (private 0600, public 0644).

    Refuses to overwrite existing keys — losing a signer is worse than typing an
    explicit delete first."""
    serialization, Ed25519PrivateKey, _ = _crypto()

    priv_path = private_key_path(repo_root)
    pub_path = public_key_path(repo_root)
    if priv_path.exists() or pub_path.exists():
        raise FileExistsError(
            f"Signing keys already exist under {keys_dir(repo_root)} — delete them "
            "first if you really want to rotate (rotating invalidates nothing retroactively; "
            "old signatures just verify against their embedded public key)."
        )

    priv_path.parent.mkdir(parents=True, exist_ok=True)
    key = Ed25519PrivateKey.generate()
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    pub = key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )

    # 0600 on the private key: os.open-with-mode + chmod, best effort on Windows
    # (where POSIX modes are approximated by the CRT; read ACLs still apply).
    fd = os.open(priv_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(pem)
    try:
        os.chmod(priv_path, 0o600)
    except OSError:  # pragma: no cover - platform-dependent
        pass
    pub_path.write_bytes(pub)
    try:
        os.chmod(pub_path, 0o644)
    except OSError:  # pragma: no cover - platform-dependent
        pass
    return priv_path, pub_path


def load_public_key_hex(repo_root: Path) -> str | None:
    """Hex of THIS repo's public key (for comparing against a signature's embedded one)."""
    p = public_key_path(repo_root)
    if not p.exists():
        return None
    return p.read_bytes().hex()


def fingerprint(public_key_bytes: bytes) -> str:
    """v1.2.5: stable, short, human-comparable identifier for a public key.

    sha256(raw 32-byte public key) rendered as the first 16 hex chars in
    xxxx:xxxx:xxxx:xxxx groups — a golden-fixture-tested contract (tests/test_signing.py),
    NOT a trust claim: two different keys are (astronomically) unlikely to share a
    fingerprint, but a matching fingerprint says nothing about who controls the key.
    """
    import hashlib

    digest = hashlib.sha256(public_key_bytes).hexdigest()[:16]
    return ":".join(digest[i:i + 4] for i in range(0, 16, 4))


def archived_keys_dir(repo_root: Path) -> Path:
    return keys_dir(repo_root) / "archive"


def list_keys(repo_root: Path) -> list[dict]:
    """v1.2.5: every key this repo knows about — the active one (if any) plus every
    archived one from a previous `rotate`, newest first. Pure filesystem read, no crypto
    needed (fingerprinting only hashes bytes)."""
    entries: list[dict] = []
    pub_path = public_key_path(repo_root)
    if pub_path.exists():
        pub_bytes = pub_path.read_bytes()
        entries.append({
            "fingerprint": fingerprint(pub_bytes),
            "active": True,
            "path": str(pub_path),
            "created_at": datetime.datetime.fromtimestamp(
                pub_path.stat().st_mtime, tz=datetime.timezone.utc).isoformat(),
        })
    archive_dir = archived_keys_dir(repo_root)
    if archive_dir.is_dir():
        for sub in sorted(archive_dir.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
            archived_pub = sub / PUBLIC_KEY_PATH
            if not archived_pub.exists():
                continue
            entries.append({
                "fingerprint": fingerprint(archived_pub.read_bytes()),
                "active": False,
                "path": str(archived_pub),
                "created_at": datetime.datetime.fromtimestamp(
                    archived_pub.stat().st_mtime, tz=datetime.timezone.utc).isoformat(),
            })
    return entries


def rotate_keypair(repo_root: Path) -> tuple[Path, Path]:
    """v1.2.5: archives the current keypair under .av/keys/archive/<fingerprint>/ (if one
    exists), then generates a fresh one via generate_keypair(). Never deletes a private
    key — archived keys keep verifying commits signed before the rotation, since the
    signature blob carries its own public key. Raises FileNotFoundError if there is no
    current key to rotate (use `av registry keygen` for the first key instead)."""
    priv_path = private_key_path(repo_root)
    pub_path = public_key_path(repo_root)
    if not priv_path.exists():
        raise FileNotFoundError(
            f"No signing key to rotate at {priv_path} — run `av registry keygen` first."
        )

    pub_bytes = pub_path.read_bytes() if pub_path.exists() else b""
    fp = fingerprint(pub_bytes) if pub_bytes else "unknown"
    dest = archived_keys_dir(repo_root) / fp.replace(":", "")
    dest.mkdir(parents=True, exist_ok=True)
    import shutil

    shutil.move(str(priv_path), str(dest / SIGNING_KEY_PATH))
    if pub_path.exists():
        shutil.move(str(pub_path), str(dest / PUBLIC_KEY_PATH))

    return generate_keypair(repo_root)


def _canonical_timestamp(value) -> str | None:
    """Normalizes an ISO timestamp to one canonical UTC rendering.

    The registry persists naive UTC and echoes timestamps WITHOUT the '+00:00' the
    authoring client wrote — a cloned payload therefore differed from the signed one
    by that suffix alone and every clone verification failed (found by the manual wire
    pass, v1.2.2). Parsing both shapes yields the same instant, so the canonical form
    is computed from the instant, never the spelling."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value  # not a real timestamp: sign verbatim, don't hide mutations
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    return parsed.astimezone(datetime.timezone.utc).isoformat()


def canonical_commit_bytes(commit_data: dict) -> bytes:
    """Sorted-keys JSON of the commit payload minus `signature`, with the timestamp
    normalized — the exact bytes signed and verified everywhere."""
    canon = {k: v for k, v in commit_data.items() if k != "signature"}
    normalized_ts = _canonical_timestamp(canon.get("timestamp"))
    if normalized_ts is not None:
        canon["timestamp"] = normalized_ts
    return json.dumps(canon, sort_keys=True).encode("utf-8")


def sign_payload(commit_data: dict, repo_root: Path) -> dict | None:
    """Signs the canonical form with this repo's key. Returns the signature blob, or
    None when no key is configured or the [sign] extra is missing (never raises for
    those two cases — signing must stay best-effort for ordinary commits)."""
    priv_path = private_key_path(repo_root)
    if not priv_path.exists():
        return None
    try:
        serialization, _, _ = _crypto()
    except SigningUnavailable:
        return None

    key = serialization.load_pem_private_key(priv_path.read_bytes(), password=None)
    pub_hex = key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    ).hex()
    sig = key.sign(canonical_commit_bytes(commit_data))
    return {
        "algo": ALGO,
        "public_key": pub_hex,
        "sig": base64.b64encode(sig).decode("ascii"),
        "signed_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }


def export_signature_blob(commit_hash: str, commit_data: dict) -> dict:
    """v1.2.5: a standalone, portable record of one commit's signature — for handing to
    an external auditor who has the commit content but not this repo's config/registry
    access. `canonical_sha256` lets a verifier confirm the commit content matches what
    was actually signed even before checking the signature itself."""
    import hashlib

    sig = commit_data.get("signature")
    if not isinstance(sig, dict):
        raise ValueError(f"commit {commit_hash} has no signature to export")
    return {
        "hash": commit_hash,
        "algo": sig.get("algo"),
        "public_key": sig.get("public_key"),
        "sig": sig.get("sig"),
        "signed_at": sig.get("signed_at"),
        "fingerprint": fingerprint(bytes.fromhex(sig["public_key"])) if sig.get("public_key") else None,
        "canonical_sha256": hashlib.sha256(canonical_commit_bytes(commit_data)).hexdigest(),
        "exported_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }


def verify_detached(commit_data: dict, detached: dict) -> tuple[bool, str]:
    """v1.2.5: verifies a commit against a detached signature record from
    export_signature_blob(), independent of whatever `commit_data["signature"]` (if any)
    already says — this is the point of "detached": the verifier trusts the external
    record, not whatever the commit source claims about itself."""
    import hashlib

    if commit_data.get("hash") != detached.get("hash"):
        return False, (f"hash mismatch: commit is {commit_data.get('hash')!r}, "
                       f"detached record is for {detached.get('hash')!r}")
    merged = dict(commit_data)
    merged["signature"] = {
        "algo": detached.get("algo"), "public_key": detached.get("public_key"),
        "sig": detached.get("sig"), "signed_at": detached.get("signed_at"),
    }
    canonical = canonical_commit_bytes(merged)
    if detached.get("canonical_sha256") and hashlib.sha256(canonical).hexdigest() != detached["canonical_sha256"]:
        return False, "canonical bytes mismatch — commit content differs from what was signed"
    return verify_signature(merged)


def verify_signature(commit_data: dict) -> tuple[bool, str]:
    """Verifies a commit's embedded signature over its canonical form.

    Returns (ok, reason); reason is human-readable in every branch so both the CLI and
    agents get an honest verdict. Commits without a signature are NOT failures here —
    callers decide policy ("unsigned-ok" per the v1.2.2 plan)."""
    signature = commit_data.get("signature")
    if not isinstance(signature, dict):
        return False, "unsigned"
    if signature.get("algo") != ALGO:
        return False, f"unsupported algo: {signature.get('algo')!r}"
    try:
        serialization, _, Ed25519PublicKey = _crypto()
    except SigningUnavailable as exc:
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
        pub.verify(sig, canonical_commit_bytes(commit_data))
    except InvalidSignature:
        return False, "signature mismatch — payload was modified after signing"
    return True, "verified"
