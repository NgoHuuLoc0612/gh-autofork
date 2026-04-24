"""
gh-autofork exception hierarchy.
All library-specific errors derive from GHAutoForkError.
"""

from __future__ import annotations
from typing import Optional


class GHAutoForkError(Exception):
    """Base exception for all gh-autofork errors."""

    def __init__(self, message: str, cause: Optional[Exception] = None):
        super().__init__(message)
        self.cause = cause

    def __str__(self) -> str:
        base = super().__str__()
        if self.cause:
            return f"{base} (caused by: {type(self.cause).__name__}: {self.cause})"
        return base


class ConfigError(GHAutoForkError):
    """Raised for configuration file or value errors."""


class DatabaseError(GHAutoForkError):
    """Raised for SQLite / persistence errors."""


class AuthenticationError(GHAutoForkError):
    """Raised when GitHub token is missing, invalid, or lacks required scopes."""


class RateLimitError(GHAutoForkError):
    """Raised when the GitHub API rate limit is exhausted."""

    def __init__(self, message: str, reset_at: Optional[int] = None, cause: Optional[Exception] = None):
        super().__init__(message, cause)
        self.reset_at = reset_at  # Unix timestamp when the limit resets


class ForkError(GHAutoForkError):
    """Raised when a fork operation fails."""

    def __init__(self, message: str, repo: str = "", cause: Optional[Exception] = None):
        super().__init__(message, cause)
        self.repo = repo


class SyncError(GHAutoForkError):
    """Raised when syncing a fork with its upstream fails."""

    def __init__(self, message: str, repo: str = "", cause: Optional[Exception] = None):
        super().__init__(message, cause)
        self.repo = repo


class FilterError(GHAutoForkError):
    """Raised when a filter expression is invalid."""


class ServiceError(GHAutoForkError):
    """Raised when installing/removing the OS startup service fails."""


class SchedulerError(GHAutoForkError):
    """Raised for background scheduler failures."""


class RepoNotFoundError(GHAutoForkError):
    """Raised when the requested repository does not exist on GitHub."""

    def __init__(self, repo: str, cause: Optional[Exception] = None):
        super().__init__(f"Repository '{repo}' not found on GitHub.", cause)
        self.repo = repo


class AlreadyForkedError(GHAutoForkError):
    """Raised when the repository has already been forked (and skip_existing is True)."""

    def __init__(self, repo: str, fork_name: str = "", cause: Optional[Exception] = None):
        super().__init__(f"Repository '{repo}' is already forked as '{fork_name}'.", cause)
        self.repo = repo
        self.fork_name = fork_name
