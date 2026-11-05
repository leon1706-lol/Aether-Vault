import click


class AetherVaultException(click.ClickException):
    """Base exception for all Aether-Vault CLI errors."""

    def __init__(self, message: str):
        super().__init__(message)

    def format_message(self) -> str:
        return click.style(f"Error: {self.message}", fg="red", bold=True)


class AuthenticationError(AetherVaultException):
    """Raised when server authentication fails."""
    pass


class NetworkError(AetherVaultException):
    """Raised when server communication fails."""
    pass


class ValidationError(AetherVaultException):
    """Raised when local validation (e.g., file not found) fails."""
    pass


class AmbiguousCommitHash(ValidationError):
    """Raised when a short commit-hash prefix matches more than one commit."""
    pass


class ReportedValidationError(ValidationError):
    """A validation failure the root group turns into exit 15 plus `error.data.reason`."""

    reason = "validation"


class CorruptStateError(ReportedValidationError):
    """`.av/index` or `.av/pending_push` can't be read. Never treated as empty: the next
    write would silently drop every tracked file or queued commit."""

    reason = "corrupt_state"


class RepoLockedError(ReportedValidationError):
    """Another writer held the repository lock past the timeout."""

    reason = "repo_locked"


class BadMetricError(ReportedValidationError):
    """A `--metric` entry with no `=`, an empty key, or a non-finite number."""

    reason = "bad_metric"


class MissingObjectError(ReportedValidationError):
    """A restore needs an object that is absent or whose bytes don't match its hash."""

    reason = "missing_object"


class StorageError(AetherVaultException):
    """Raised when local storage operations fail."""
    pass
