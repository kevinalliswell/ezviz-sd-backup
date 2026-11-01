"""Public error categories and their command-line exit codes."""

from __future__ import annotations


class AppError(Exception):
    """An expected failure whose message is safe to show to the user."""

    exit_code = 5
    error_code = "PROBE_FAILED"

    def __init__(self, message: str, *, error_code: str | None = None) -> None:
        super().__init__(message)
        if error_code is not None:
            self.error_code = error_code


class ConfigError(AppError):
    exit_code = 2
    error_code = "CONFIG_INVALID"


class CredentialError(AppError):
    exit_code = 3
    error_code = "CREDENTIAL_UNAVAILABLE"


class AuthorizationError(AppError):
    exit_code = 3
    error_code = "AUTHORIZATION_FAILED"


class CapabilityError(AppError):
    exit_code = 4
    error_code = "CAPABILITY_UNSUPPORTED"


class ProbeError(AppError):
    exit_code = 5
    error_code = "PROBE_FAILED"


class PaginationError(ProbeError):
    error_code = "PAGINATION_NO_PROGRESS"


class MediaError(ProbeError):
    error_code = "MEDIA_VALIDATION_FAILED"


class BudgetError(AppError):
    exit_code = 6
    error_code = "BUDGET_EXHAUSTED"
