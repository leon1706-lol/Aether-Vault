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


class StorageError(AetherVaultException):
    """Raised when local storage operations fail."""
    pass
