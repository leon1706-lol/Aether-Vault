"""Server-side ed25519 signing for the audit-log hash chain (v1.3.3, WP-32).

Deliberately SEPARATE from `python/av_cli/signing.py` — that module is repo-scoped
(keys live under a checkout's `.av/keys/`, one keypair per repo, used to sign COMMITS a
client controls). Audit-log rows are server-generated and server-wide, not per-repo, so
this module manages ONE keypair per registry deployment instead, at a path the operator
controls (`AV_AUDIT_SIGNING_KEY_PATH`, unset by default — chain-hashing alone, this
module's other half, works with zero signing at all).

Fails soft, not hard: `cryptography` is an optional extra (`[sign]`) elsewhere in this
codebase, and this module keeps that contract — every function here returns `None`
(never raises) when the dependency is missing or no key is configured, so a deployment
that never opts into signing never even imports `cryptography`.
"""
from __future__ import annotations

import os
from pathlib import Path


def _crypto():
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PrivateKey,
            Ed25519PublicKey,
        )
        from cryptography.hazmat.primitives import serialization

        return Ed25519PrivateKey, Ed25519PublicKey, serialization
    except ImportError:
        return None


def signing_key_path() -> Path | None:
    raw = os.environ.get("AV_AUDIT_SIGNING_KEY_PATH")
    return Path(raw) if raw else None


def ensure_keypair() -> tuple[Path, Path] | None:
    """Generates a keypair at `AV_AUDIT_SIGNING_KEY_PATH` (+ `.pub`) if one doesn't
    already exist there. Returns (private_path, public_path), or None if signing isn't
    configured/available. Called once at server startup when the env var is set — never
    silently regenerates over an existing key (that would invalidate every previously
    issued signature)."""
    crypto = _crypto()
    key_path = signing_key_path()
    if crypto is None or key_path is None:
        return None
    Ed25519PrivateKey, _, serialization = crypto
    pub_path = key_path.with_suffix(key_path.suffix + ".pub")
    if key_path.exists():
        return key_path, pub_path
    key_path.parent.mkdir(parents=True, exist_ok=True)
    private_key = Ed25519PrivateKey.generate()
    key_path.write_bytes(
        private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    key_path.chmod(0o600)
    pub_path.write_bytes(
        private_key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw
        ).hex().encode()
    )
    return key_path, pub_path


def _load_private_key():
    crypto = _crypto()
    key_path = signing_key_path()
    if crypto is None or key_path is None or not key_path.exists():
        return None
    Ed25519PrivateKey, _, serialization = crypto
    return serialization.load_pem_private_key(key_path.read_bytes(), password=None)


def sign(chain_hash: str) -> str | None:
    """Signs a chain_hash hex string, returning a hex-encoded ed25519 signature, or
    None when signing isn't configured/available (the caller stores None -- a row with
    no signature is still chain-hashed, just not additionally signed)."""
    private_key = _load_private_key()
    if private_key is None:
        return None
    return private_key.sign(chain_hash.encode()).hex()


def public_key_hex() -> str | None:
    """The raw public key, hex-encoded -- what `GET /api/admin/audit/public-key`
    exposes so an external verifier can check signatures without any server access
    beyond that one read-only endpoint."""
    crypto = _crypto()
    key_path = signing_key_path()
    if crypto is None or key_path is None:
        return None
    pub_path = key_path.with_suffix(key_path.suffix + ".pub")
    if pub_path.exists():
        return pub_path.read_text(encoding="utf-8").strip()
    private_key = _load_private_key()
    if private_key is None:
        return None
    _, _, serialization = crypto
    return private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw
    ).hex()


def verify(chain_hash: str, signature_hex: str, public_key_hex_str: str) -> bool:
    """Verifies one (chain_hash, signature) pair against a given public key -- the
    shape an EXTERNAL verifier uses (they hold only the public key, from `av audit
    verify` or the public-key endpoint, never the private key)."""
    crypto = _crypto()
    if crypto is None:
        return False
    _, Ed25519PublicKey, _ = crypto
    try:
        public_key = Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_key_hex_str))
        public_key.verify(bytes.fromhex(signature_hex), chain_hash.encode())
        return True
    except Exception:
        return False
