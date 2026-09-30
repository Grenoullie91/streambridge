"""Logging setup and redaction helpers.

StreamBridge logs must never contain credentials, cookies, tokens, private
URLs, home paths or host identifiers. Everything that could carry such data
passes through :func:`redact` before it reaches a log record.
"""

from __future__ import annotations

import logging
import os
import re
import sys
from collections.abc import Iterable

LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)-18s %(message)s"
DATE_FORMAT = "%Y-%m-%dT%H:%M:%S"

REDACTED = "<REDACTED>"

# Query parameters that carry credentials or session state.
_SECRET_PARAMS = frozenset(
    {
        "authorization",
        "cookie",
        "cred",
        "credential",
        "key",
        "password",
        "secret",
        "session",
        "sig",
        "signature",
        "token",
    }
)

# Command line flags whose *value* must never be logged.
_SECRET_FLAGS = (
    "--cookies",
    "--cookies-from-browser",
    "--username",
    "--password",
    "--video-password",
)

_HOME_RE = re.compile(r"/(?:home|Users)/[^/\s\"']+")
_EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")
_IPV4_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_UUID_RE = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)

_configured = False


def redact(text: str) -> str:
    """Remove identifying material from *text*.

    Home paths collapse to ``$HOME``; emails, IPv4 addresses and UUIDs are
    replaced. Hostnames are not rewritten - they rarely appear in log lines,
    and blanking them would make the output unreadable. Callers that log a
    URL should use :func:`redact_url` instead.
    """
    if not text:
        return text
    result = _HOME_RE.sub("$HOME", text)
    result = _EMAIL_RE.sub(REDACTED, result)
    result = _UUID_RE.sub(REDACTED, result)
    result = _IPV4_RE.sub(REDACTED, result)
    return result


def redact_url(url: str) -> str:
    """Return a loggable form of *url*: origin plus redacted parameters.

    Media URLs carry signed query parameters that encode session state, so
    only the origin and a parameter *count* are kept.
    """
    from urllib.parse import urlsplit

    try:
        parts = urlsplit(url)
    except ValueError:
        return REDACTED
    if not parts.scheme or not parts.netloc:
        return REDACTED
    origin = f"{parts.scheme}://{parts.netloc}{parts.path}"
    count = len([kv for kv in parts.query.split("&") if kv])
    return f"{origin}?{count} params {REDACTED}" if count else origin


def redact_args(args: Iterable[str]) -> list[str]:
    """Replace values that follow a secret-looking flag."""
    out: list[str] = []
    redact_next = False
    for arg in args:
        if redact_next:
            out.append(REDACTED)
            redact_next = False
            continue
        if arg in _SECRET_FLAGS:
            out.append(arg)
            redact_next = True
            continue
        if "=" in arg and arg.split("=", 1)[0] in _SECRET_FLAGS:
            out.append(arg.split("=", 1)[0] + "=" + REDACTED)
            continue
        out.append(redact(arg))
    return out


class RedactingFilter(logging.Filter):
    """Logging filter that redacts every formatted message."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # pragma: no cover - defensive
            return True
        cleaned = redact(message)
        if cleaned != message:
            record.msg = cleaned
            record.args = ()
        return True


def setup_logging(level: str = "INFO", *, verbose: bool = False) -> None:
    """Configure root logging once, for both entry points."""
    global _configured
    if _configured:
        return
    resolved = "DEBUG" if verbose else level.upper()
    numeric = getattr(logging, resolved, logging.INFO)
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter(LOG_FORMAT, DATE_FORMAT))
    handler.addFilter(RedactingFilter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(numeric)
    for noisy in ("urllib3", "yt_dlp", "youtube_dl"):
        logging.getLogger(noisy).setLevel(max(numeric, logging.WARNING))
    _configured = True


def log_level_from_env(default: str = "INFO") -> str:
    raw = os.environ.get("STREAMBRIDGE_LOG_LEVEL", default).upper()
    return raw if raw in ("DEBUG", "INFO", "WARNING", "ERROR") else default
