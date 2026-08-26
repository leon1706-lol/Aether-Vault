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
