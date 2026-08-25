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
}


class SDKError(AetherVaultException):
    """Raised for every SDK failure. `.code` matches docs/for-agents.md's registry."""

    def __init__(self, code: str, message: str):
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message
        self.exit_code = EXIT_CODES.get(code, 15)


def error_from_code(code: str, message: str) -> SDKError:
    return SDKError(code, message)
