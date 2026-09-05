"""SDK exception types mirroring the CLI exit-code registry (docs/for-agents.md)."""

from av_cli.exceptions import AetherVaultException

# code string -> CLI exit code (single source of truth duplicated intentionally:
# the SDK must remain importable without dragging the whole click app surface).
EXIT_CODES = {
    "not_a_repo": 10,
    "nothing_to_commit": 11,
    "auth_failed": 12,
    "unreachable_queued": 13,
    "merge_conflict": 14,
    "validation": 15,
    "policy_denied": 16,
    "budget_exhausted": 17,
    "frozen": 18,
    "review_required": 19,
    "scope_denied": 20,
    "tenant_denied": 22,  # 21 (login_required) is deliberately unregistered -- see core.py
}


class SDKError(AetherVaultException):
    """Raised for every SDK failure. `.code` matches docs/for-agents.md's registry.

    Every raise in this package goes through `error_from_code()` below, which returns
    the matching typed subclass (v1.3.0) — `except SDKError` still catches all of them
    (nothing about existing code breaks), but callers who want to branch on a specific
    failure can now do `except NotARepoError` instead of `except SDKError as e: if
    e.code == "not_a_repo"`. `.exit_code` mirrors the CLI's exit-code registry exactly,
    so an agent that shells out sometimes and uses the SDK other times sees one number
    space either way.
    """

    code: str = "validation"

    def __init__(self, code: str | None = None, message: str = ""):
        # Backward-compatible: `SDKError("not_a_repo", "...")` (the pre-v1.3.0 call
        # shape, still used directly in a few call sites and any external code written
        # against the old signature) keeps working — `code` overrides the subclass's own
        # class-level default when given explicitly.
        resolved_code = code if code is not None else self.code
        super().__init__(f"[{resolved_code}] {message}")
        self.code = resolved_code
        self.message = message
        self.exit_code = EXIT_CODES.get(resolved_code, 15)


class NotARepoError(SDKError):
    code = "not_a_repo"


class NothingToCommitError(SDKError):
    code = "nothing_to_commit"


class AuthFailedError(SDKError):
    code = "auth_failed"


class UnreachableQueuedError(SDKError):
    code = "unreachable_queued"


class MergeConflictError(SDKError):
    code = "merge_conflict"


class ValidationError(SDKError):
    code = "validation"


class PolicyDeniedError(SDKError):
    code = "policy_denied"


class BudgetExhaustedError(SDKError):
    code = "budget_exhausted"


class FrozenError(SDKError):
    code = "frozen"


class ReviewRequiredError(SDKError):
    code = "review_required"


class ScopeDeniedError(SDKError):
    code = "scope_denied"


class TenantDeniedError(SDKError):
    code = "tenant_denied"


_CODE_TO_CLASS: dict[str, type[SDKError]] = {
    "not_a_repo": NotARepoError,
    "nothing_to_commit": NothingToCommitError,
    "auth_failed": AuthFailedError,
    "unreachable_queued": UnreachableQueuedError,
    "merge_conflict": MergeConflictError,
    "validation": ValidationError,
    "policy_denied": PolicyDeniedError,
    "budget_exhausted": BudgetExhaustedError,
    "frozen": FrozenError,
    "review_required": ReviewRequiredError,
    "scope_denied": ScopeDeniedError,
    "tenant_denied": TenantDeniedError,
}


def error_from_code(code: str, message: str) -> SDKError:
    """Factory used by every raise site in this package — returns the typed subclass for
    `code` (falling back to the base SDKError for an unrecognized code, e.g. a future
    server-side error string this SDK version doesn't know about yet)."""
    cls = _CODE_TO_CLASS.get(code, SDKError)
    return cls(code, message)
