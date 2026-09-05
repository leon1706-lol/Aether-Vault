"""Fernet-based encryption at rest for `sso_providers.config`'s client-secret fields
(v1.3.3, WP-10/WP-16). Reuses the `[sign]` extra's `cryptography` dependency (already
optional elsewhere in this codebase, e.g. `av_cli/signing.py`) rather than adding a new
one just for this.

**Secrets rule, enforced here, not just documented:** a provider config containing a
plaintext secret is refused at creation time with a clear error when `AV_SECRET_KEY`
isn't set — never silently stored in plaintext. This mirrors `api_tokens.token_hash`/
`sessions.token_hash`'s own "never the raw value" rule one level up (there it's a hash,
here it's a reversible encryption because the OIDC/SAML client actually needs the
plaintext secret back to authenticate with the IdP).
"""
from __future__ import annotations

import base64
import hashlib
import os

# Config keys treated as secrets — encrypted on write, decrypted on read, masked on
# list/export. Provider-shape-specific (OIDC's `client_secret`, SAML's
# `private_key_pem`) rather than a blanket "encrypt everything", so non-secret config
# (issuer URLs, entity IDs) stays human-readable in the database for operators
# debugging a provider config directly.
SECRET_CONFIG_KEYS = ("client_secret", "private_key_pem")


class SecretsUnavailable(RuntimeError):
    """AV_SECRET_KEY is unset but a provider config with a secret field was given."""


def _fernet():
    try:
        from cryptography.fernet import Fernet

        return Fernet
    except ImportError:
        return None


def _derive_key() -> bytes | None:
    """AV_SECRET_KEY can be any string (an operator-chosen passphrase, not necessarily a
    raw Fernet key already) — SHA-256 + base64 derives a valid 32-byte Fernet key from
    it deterministically, the same "derive, don't require a specific format" approach
    this codebase already takes for other operator-supplied secrets."""
    raw = os.environ.get("AV_SECRET_KEY")
    if not raw:
        return None
    return base64.urlsafe_b64encode(hashlib.sha256(raw.encode()).digest())


def encrypt_config(config: dict) -> dict:
    """Returns a COPY of `config` with every key in SECRET_CONFIG_KEYS Fernet-encrypted.
    Raises SecretsUnavailable if the config has a secret field but no key is configured
    — refused loudly rather than ever persisting plaintext."""
    secret_fields = [k for k in SECRET_CONFIG_KEYS if config.get(k)]
    if not secret_fields:
        return dict(config)

    Fernet = _fernet()
    key = _derive_key()
    if Fernet is None:
        raise SecretsUnavailable(
            "cryptography is not installed -- install the '[sign]' extra to store a "
            "provider client secret (or omit the secret field entirely for a public client)."
        )
    if key is None:
        raise SecretsUnavailable(
            "AV_SECRET_KEY is not set -- refusing to store a provider client secret in "
            "plaintext. Set AV_SECRET_KEY, or omit the secret field for a public client."
        )

    fernet = Fernet(key)
    out = dict(config)
    for field in secret_fields:
        out[field] = f"enc:{fernet.encrypt(out[field].encode()).decode()}"
    return out


def decrypt_config(config: dict) -> dict:
    """The inverse of encrypt_config — returns a COPY with secret fields decrypted back
    to plaintext, for the ONE legitimate internal use (actually authenticating with the
    IdP). Never call this on a response body headed to an HTTP client — use
    `mask_config` for that."""
    Fernet = _fernet()
    key = _derive_key()
    if Fernet is None or key is None:
        return dict(config)  # no secret fields could have been encrypted without both
    fernet = Fernet(key)
    out = dict(config)
    for field in SECRET_CONFIG_KEYS:
        value = out.get(field)
        if isinstance(value, str) and value.startswith("enc:"):
            out[field] = fernet.decrypt(value[len("enc:"):].encode()).decode()
    return out


def mask_config(config: dict) -> dict:
    """What `GET /api/sso-providers*` actually returns — every secret field replaced
    with a fixed marker, never even a prefix (unlike token masking elsewhere in this
    codebase, which shows a few characters — a client secret has no safe-to-show
    prefix convention an IdP guarantees, so this masks it completely)."""
    out = dict(config)
    for field in SECRET_CONFIG_KEYS:
        if out.get(field):
            out[field] = "***REDACTED***"
    return out
