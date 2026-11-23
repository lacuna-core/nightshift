"""Errors that Nightshift expects and reports, rather than crashes on."""

from datetime import datetime


class NightshiftError(Exception):
    """A problem with a message meant for the user."""


class ConfigError(NightshiftError):
    """``nightshift.yaml`` is missing or invalid."""


class ToolError(NightshiftError):
    """A command-line tool is missing, failed or took too long."""


class HerdrError(ToolError):
    """Herdr cannot be reached, or refused a command."""

    def __init__(self, message: str, kind: str = "") -> None:
        super().__init__(message)
        self.kind = kind


class GitError(ToolError):
    """A git command failed."""


class TrackerError(NightshiftError):
    """The issue tracker refused a request, or something in it cannot be read."""


class TrackerAuthError(TrackerError):
    """The login is missing, expired or lacks a permission."""


class TrackerNotFoundError(TrackerError):
    """The tracker says the thing does not exist, or hides it."""


class TrackerUnavailableError(TrackerError):
    """The tracker is down, unreachable or rate limiting. Waiting helps."""

    def __init__(self, message: str, retry_at: datetime | None = None) -> None:
        super().__init__(message)
        self.retry_at = retry_at
