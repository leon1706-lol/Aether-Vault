# Security Policy

Aether-Vault versions ML artifacts and runs a networked registry service — we take both
local-data-integrity and remote-attack-surface reports seriously.

## Reporting a vulnerability

**Please do not report security issues in public GitHub Issues.**

Use GitHub's private vulnerability reporting:
[Open a private security advisory](https://github.com/leon1706/aether-vault/security/advisories/new)
on this repository. This keeps the details confidential until a fix is ready.

Include what you can of: affected component (CLI / `av_server` / webui / packaging),
affected version (`av --version` or the tag), reproduction steps or PoC, and your assessment
of impact. You will get an acknowledgment within **72 hours**, a status update at least
every 7 days while a fix is in progress, and public credit in the fix's release notes
(unless you prefer to stay anonymous).

## Supported versions

Only the **latest tagged release line** receives security fixes:

| Version | Supported |
|---|---|
| latest `vX.Y.Z` on PyPI | ✅ |
| older tags | ❌ — upgrade via `pip install --upgrade aether-vault` |
| `master` HEAD | best effort (fixes land here first) |

## Scope guidance

**In scope:** anything that lets an unauthenticated/unauthorized party read, write, or
destroy repository data; path traversal in ref/hash handling; hash-verification bypasses;
unsafe deserialization; supply-chain issues in our published artifacts; credential leakage
in logs, config files, or the token handoff flow.

**Hardening status:** CORS is locked to the webui origin by default (`AV_CORS_ORIGINS` to
widen) and the destructive GC endpoint is rate-limited by default (`AV_RATE_LIMIT_GC`,
`10/minute`) — both shipped in the v1.1.x hardening cycle. Per-user access tokens
(`AV_AUTH_USERS` via `av auth add-user`, v1.1.8) now exist alongside the owner's shared
secret — each teammate revokes/rotates independently and commits attribute to their username.
Still on the enterprise tier: RBAC (per-route/per-branch permissions), SSO, and audit
logging. Reports about the shipped defaults are still welcome — reference them so we can
link.

**Out of scope:** social engineering, brute-force against deployments you don't own,
vulnerabilities in Docker/Postgres/Redis themselves (report upstream), and self-hosted
misconfigurations that expose ports publicly without Protected mode enabled.

## Hardening recommendations for operators

Run registries with `av auth set-token` ("Protected" mode) and issue per-user tokens via
`av auth add-user` so teammates never share the owner secret and can be revoked
individually; keep Postgres/Redis off the host network (the release compose file already
omits their port mappings), keep the default CORS lock and GC rate limit in place (widen
`AV_CORS_ORIGINS` only for deployments that actually need it), and put TLS termination
in front of uvicorn for any non-localhost use.

v1.2.0 trust surfaces: the audit trail (audit_log, on by default, disable with
AV_AUDIT_LOG=0) records who performed each mutation; webhook signing secrets are stored
registry-side because deliveries must be signed — treat registry compromise as secret
compromise for subscribers; commit attestations are HMAC-based integrity-v0 (tamper
evidence vs. key-holders only). Asymmetric (ed25519) commit signing is a regular,
free-tier feature since v1.2.2 (`av registry keygen`) — see the next section.

## Signed commits (v1.2.2) - the trust model, stated plainly

`av registry keygen` generates an ed25519 keypair under `.av/keys/` (private key 0600);
commits made afterward are signed automatically, and `av verify <hash>` validates the
signature over the canonical commit payload (sorted-keys JSON minus the signature, with
the timestamp normalized so registry round-trips verify identically). What this DOES and
DOES NOT mean:

- **It IS tamper evidence**: any modification of a signed commit's payload after signing -
  message, tree, metrics, tags, even the recorded hash - is detected by `av verify`, on the
  authoring machine AND on any clone (signatures persist server-side and ride clone/pull).
- **It is NOT a trust network**: there is no PKI, no web-of-trust, no identity binding.
  The public key travels WITH the signature, so it attests integrity of the payload, not
  WHO the signer is. Establishing authorship requires authenticating the public key out
  of band (e.g., comparing it against a copy fetched from the author directly).
- **Key loss/rotation**: signatures verify against each commit's EMBEDDED public key, so
  rotating keys never invalidates history; `av verify` additionally reports whether a
  signature was made with THIS repo's current key.
- **Unsigned commits are valid**: signing is opt-in per repository and best-effort by
  design; `av verify` reports UNSIGNED honestly rather than failing.
- **Threat-model boundary**: anyone who can write to `.av/keys/` can sign as you - protect
  it like an SSH private key. Registry compromise lets an attacker DELETE signatures but
  not forge new ones without the private key.

### Key management and rotation (v1.2.5)

`av registry keys list` shows every key this repo knows (active + archived) with its
fingerprint (`sha256` of the raw public key, first 16 hex chars, `xxxx:xxxx:xxxx:xxxx`) and
creation time; `av registry keys fingerprint` prints just the active one, for scripting.
`av registry keys rotate` archives the current keypair to `.av/keys/archive/<fingerprint>/`
(never deletes it) and generates a fresh one — old commits keep verifying against their
own embedded (archived) public key, new commits sign with the new one. Rotate on suspected
key compromise, or on a schedule your policy dictates; there is no server-side revocation,
because there is no PKI to revoke against — see "not a trust network" above.

For sharing a signature outside this repo's config, `av registry export-signature <hash>`
produces a standalone `{hash, algo, public_key, sig, canonical_sha256, exported_at}`
record; `av verify <hash> --signature FILE` verifies from that file alone, without needing
local repo config or registry access — useful for a third-party auditor.

### Signature requirement policy (v1.2.5)

`av policy set <branch> --require-signature` (optionally combined with a metric gate) denies
promotion/merge of a candidate with no valid signature embedded in its OWN commit (exit 16,
`policy_denied`) — a detached signature does not satisfy this, by design: the point is
enforcing that commits landing on a protected branch are themselves signed, not that a
signature exists somewhere. This is still tamper evidence, not an identity check: arming it
raises the bar to "every commit on this branch was signed by someone holding a private key
at commit time," not "signed by a specific, verified person."
