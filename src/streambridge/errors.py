"""Typed error hierarchy and process exit codes.

Every error carries an exit code so the CLI can translate any failure into a
stable process status without showing a traceback for ordinary user mistakes.
"""

from __future__ import annotations

from enum import IntEnum


class ExitCode(IntEnum):
    """Process exit codes. Documented in README.md and docs/troubleshooting.md."""

    OK = 0
    GENERAL_ERROR = 1
    INVALID_INPUT = 2
    MISSING_DEPENDENCY = 3
    MPD_UNREACHABLE = 4
    SOURCE_UNAVAILABLE = 5
    NOT_FOUND = 6


class StreamBridgeError(Exception):
    """Base class for every error raised by StreamBridge."""

    exit_code: ExitCode = ExitCode.GENERAL_ERROR

    def __init__(self, message: str, *, hint: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint

    def user_message(self) -> str:
        if self.hint:
            return f"{self.message}\n  Hint: {self.hint}"
        return self.message


class ConfigError(StreamBridgeError):
    """Malformed or unusable configuration."""


class ValidationError(StreamBridgeError):
    """User input failed validation (bad video id, bad search type, ...)."""

    exit_code = ExitCode.INVALID_INPUT


class DependencyError(StreamBridgeError):
    """A required external program is missing or too old."""

    exit_code = ExitCode.MISSING_DEPENDENCY


class MpdError(StreamBridgeError):
    """MPD could not be reached or rejected a command."""

    exit_code = ExitCode.MPD_UNREACHABLE


class SourceUnavailableError(StreamBridgeError):
    """The online source could not provide a usable audio source.

    Covers unavailable items, region locks, bot checks, rate limits and
    format-extraction failures. ``upstream_message`` carries the extractor
    error text so the user sees what actually happened.
    """

    exit_code = ExitCode.SOURCE_UNAVAILABLE

    def __init__(
        self,
        message: str,
        *,
        upstream_message: str | None = None,
        hint: str | None = None,
    ) -> None:
        super().__init__(message, hint=hint)
        self.upstream_message = upstream_message

    def user_message(self) -> str:
        parts = [self.message]
        if self.upstream_message:
            parts.append(f"upstream reported: {self.upstream_message}")
        if self.hint:
            parts.append(f"Hint: {self.hint}")
        return "\n  ".join(parts)


class RateLimitError(StreamBridgeError):
    """Local rate limit tripped; protects against accidental request storms."""


class NotFoundError(StreamBridgeError):
    """A requested resource does not exist."""

    exit_code = ExitCode.NOT_FOUND


class AuthorizationError(StreamBridgeError):
    """A request from the network arrived without a valid access token.

    Only raised once LAN access is enabled (``server.allow_lan`` plus
    ``server.access_token``); a loopback install never produces it.
    """

    exit_code = ExitCode.INVALID_INPUT
