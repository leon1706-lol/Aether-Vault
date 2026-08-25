"""av_sdk — the Python-first agent SDK for Aether-Vault (v1.2.0).

Design contract:
* Drives the SAME single commit path the CLI uses (core.commit_staged), so every
  guarantee — deterministic hashing, atomic writes, offline queueing, run tagging,
  scoped plugin semantics — applies identically. There is no second writer.
* Structured by construction: every call returns plain dicts (the same shapes as
  `av --output json`), raising SDKError(code, message) with codes matching the CLI's
  documented exit-code registry.
"""

from .exceptions import SDKError, error_from_code
from .repo import Repo

__all__ = ["Repo", "SDKError", "error_from_code"]
