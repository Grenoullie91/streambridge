"""Configuration loading.

Precedence, lowest to highest:

1. built-in defaults
2. ``$XDG_CONFIG_HOME/streambridge/config.toml`` (or ``$STREAMBRIDGE_CONFIG``)
3. ``STREAMBRIDGE_*`` environment variables
4. explicit command line flags

Only the standard library (``tomllib``, Python 3.11+) is used.
"""

from __future__ import annotations

import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from .errors import ConfigError

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8787
DEFAULT_MPD_HOST = "127.0.0.1"
DEFAULT_MPD_PORT = 6600
DEFAULT_SEARCH_LIMIT = 20
DEFAULT_REQUEST_TIMEOUT = 20.0
DEFAULT_RESOLVE_TIMEOUT = 30.0
DEFAULT_INFO_TIMEOUT = 20.0
DEFAULT_CACHE_TTL_SECONDS = 300.0
DEFAULT_CACHE_MAX_ENTRIES = 200
DEFAULT_SEARCH_RATE_PER_MIN = 30
DEFAULT_RESOLVE_RATE_PER_MIN = 60
DEFAULT_LOG_LEVEL = "INFO"

VALID_LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")


def config_home() -> Path:
    """Base directory for user configuration, honouring XDG."""
    base = os.environ.get("XDG_CONFIG_HOME")
    return Path(base) if base else Path.home() / ".config"


def state_home() -> Path:
    """Base directory for persistent runtime state, honouring XDG."""
    base = os.environ.get("XDG_STATE_HOME")
    return Path(base) if base else Path.home() / ".local" / "state"


def cache_home() -> Path:
    """Base directory for caches, honouring XDG."""
    base = os.environ.get("XDG_CACHE_HOME")
    return Path(base) if base else Path.home() / ".cache"


def default_config_path() -> Path:
    """Conventional configuration file location."""
    return config_home() / "streambridge" / "config.toml"


def default_cache_dir() -> Path:
    """Conventional cache directory."""
    return cache_home() / "streambridge"


@dataclass(frozen=True, slots=True)
class Config:
    """Validated runtime configuration. All defaults are generic."""

    # [server]
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    # [search]
    search_limit: int = DEFAULT_SEARCH_LIMIT
    search_rate_per_min: int = DEFAULT_SEARCH_RATE_PER_MIN
    resolve_rate_per_min: int = DEFAULT_RESOLVE_RATE_PER_MIN
    # [timeouts]
    request_timeout: float = DEFAULT_REQUEST_TIMEOUT
    resolve_timeout: float = DEFAULT_RESOLVE_TIMEOUT
    info_timeout: float = DEFAULT_INFO_TIMEOUT
    # [cache]
    # default_factory, not a plain default: the XDG lookup must happen per
    # instance, not once at import time.
    cache_directory: Path = field(default_factory=default_cache_dir)
    cache_ttl_seconds: float = DEFAULT_CACHE_TTL_SECONDS
    cache_max_entries: int = DEFAULT_CACHE_MAX_ENTRIES
    # [mpd]
    mpd_host: str = DEFAULT_MPD_HOST
    mpd_port: int = DEFAULT_MPD_PORT
    # MPD only accepts playlist loads from its own playlist directory.
    # Leave unset to auto-detect; set it when loading is rejected.
    playlist_directory: Path | None = None
    # [youtube]
    extractor_path: str = "yt-dlp"
    cookies_from_browser: str | None = None
    # [logging]
    log_level: str = DEFAULT_LOG_LEVEL

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def stream_url(self, video_id: str) -> str:
        return f"{self.base_url}/stream/{video_id}"


def _as_int(value: Any, key: str) -> int:
    # bool is a subclass of int, but `port = true` is a user mistake.
    if isinstance(value, bool):
        raise ConfigError(f"{key} must be an integer, got {value!r}")
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{key} must be an integer, got {value!r}") from exc


def _as_float(value: Any, key: str) -> float:
    if isinstance(value, bool):
        raise ConfigError(f"{key} must be a number, got {value!r}")
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{key} must be a number, got {value!r}") from exc


def _as_str(value: Any, key: str) -> str:
    if not isinstance(value, str):
        raise ConfigError(f"{key} must be a string, got {value!r}")
    return value


def _as_path(value: Any, key: str) -> Path:
    return Path(_as_str(value, key)).expanduser()


# section -> key -> (coercer, Config field)
_SCHEMA: dict[str, dict[str, Any]] = {
    "server": {
        "host": ("host", _as_str),
        "port": ("port", _as_int),
    },
    "search": {
        "limit": ("search_limit", _as_int),
        "rate_per_min": ("search_rate_per_min", _as_int),
        "resolve_rate_per_min": ("resolve_rate_per_min", _as_int),
    },
    "timeouts": {
        "request": ("request_timeout", _as_float),
        "resolve": ("resolve_timeout", _as_float),
        "info": ("info_timeout", _as_float),
    },
    "cache": {
        "directory": ("cache_directory", _as_path),
        "ttl_seconds": ("cache_ttl_seconds", _as_float),
        "max_entries": ("cache_max_entries", _as_int),
    },
    "mpd": {
        "host": ("mpd_host", _as_str),
        "port": ("mpd_port", _as_int),
        "playlist_directory": ("playlist_directory", _as_path),
    },
    "youtube": {
        "extractor_path": ("extractor_path", _as_str),
        "cookies_from_browser": ("cookies_from_browser", _as_str),
    },
    "logging": {
        "level": ("log_level", _as_str),
    },
}

_SECTIONS = tuple(_SCHEMA)


def config_from_mapping(data: Mapping[str, Any]) -> Config:
    """Build a :class:`Config` from parsed TOML data, validating every value."""
    if not isinstance(data, Mapping):
        raise ConfigError("Configuration must be a TOML table")
    unknown = set(data) - set(_SECTIONS)
    if unknown:
        raise ConfigError(
            f"Unknown configuration section(s): {', '.join(sorted(unknown))}. "
            f"Known sections: {', '.join(_SECTIONS)}"
        )

    overrides: dict[str, Any] = {}
    for section, entries in _SCHEMA.items():
        raw_section = data.get(section)
        if raw_section is None:
            continue
        if not isinstance(raw_section, Mapping):
            raise ConfigError(f"[{section}] must be a table")
        unknown_keys = set(raw_section) - set(entries)
        if unknown_keys:
            raise ConfigError(
                f"Unknown key(s) in [{section}]: {', '.join(sorted(unknown_keys))}. "
                f"Known keys: {', '.join(entries)}"
            )
        for key, (field_name, coercer) in entries.items():
            if key in raw_section and raw_section[key] is not None:
                overrides[field_name] = coercer(raw_section[key], f"{section}.{key}")
    return validate_config(replace(Config(), **overrides))


def validate_config(config: Config) -> Config:
    """Enforce invariants regardless of where a value originated."""
    if not 1 <= config.port <= 65535:
        raise ConfigError(f"server.port must be between 1 and 65535, got {config.port}")
    if not 1 <= config.mpd_port <= 65535:
        raise ConfigError(f"mpd.port must be between 1 and 65535, got {config.mpd_port}")
    if config.search_limit < 1:
        raise ConfigError("search.limit must be at least 1")
    if config.search_limit > 100:
        raise ConfigError("search.limit must not exceed 100")
    for name, value in (
        ("search.rate_per_min", config.search_rate_per_min),
        ("search.resolve_rate_per_min", config.resolve_rate_per_min),
    ):
        if value < 1:
            raise ConfigError(f"{name} must be at least 1")
    for name, seconds in (
        ("timeouts.request", config.request_timeout),
        ("timeouts.resolve", config.resolve_timeout),
        ("timeouts.info", config.info_timeout),
    ):
        if seconds <= 0:
            raise ConfigError(f"{name} must be greater than 0")
    if config.cache_ttl_seconds < 0:
        raise ConfigError("cache.ttl_seconds must not be negative")
    if config.cache_max_entries < 1:
        raise ConfigError("cache.max_entries must be at least 1")
    if config.log_level.upper() not in VALID_LOG_LEVELS:
        raise ConfigError(
            f"logging.level must be one of {', '.join(VALID_LOG_LEVELS)}, got {config.log_level!r}"
        )
    if not config.extractor_path.strip():
        raise ConfigError("youtube.extractor_path must not be empty")
    return replace(config, log_level=config.log_level.upper())


_ENV_MAP: dict[str, tuple[str, Any, str]] = {
    "STREAMBRIDGE_HOST": ("host", _as_str, "server.host"),
    "STREAMBRIDGE_PORT": ("port", _as_int, "server.port"),
    "STREAMBRIDGE_SEARCH_LIMIT": ("search_limit", _as_int, "search.limit"),
    "STREAMBRIDGE_SEARCH_RATE": ("search_rate_per_min", _as_int, "search.rate_per_min"),
    "STREAMBRIDGE_RESOLVE_RATE": (
        "resolve_rate_per_min",
        _as_int,
        "search.resolve_rate_per_min",
    ),
    "STREAMBRIDGE_REQUEST_TIMEOUT": ("request_timeout", _as_float, "timeouts.request"),
    "STREAMBRIDGE_RESOLVE_TIMEOUT": ("resolve_timeout", _as_float, "timeouts.resolve"),
    "STREAMBRIDGE_INFO_TIMEOUT": ("info_timeout", _as_float, "timeouts.info"),
    "STREAMBRIDGE_CACHE_DIR": ("cache_directory", _as_path, "cache.directory"),
    "STREAMBRIDGE_CACHE_TTL": ("cache_ttl_seconds", _as_float, "cache.ttl_seconds"),
    "STREAMBRIDGE_MPD_HOST": ("mpd_host", _as_str, "mpd.host"),
    "STREAMBRIDGE_MPD_PORT": ("mpd_port", _as_int, "mpd.port"),
    "STREAMBRIDGE_PLAYLIST_DIR": ("playlist_directory", _as_path, "mpd.playlist_directory"),
    "STREAMBRIDGE_EXTRACTOR_PATH": ("extractor_path", _as_str, "youtube.extractor_path"),
    "STREAMBRIDGE_COOKIES_FROM_BROWSER": (
        "cookies_from_browser",
        _as_str,
        "youtube.cookies_from_browser",
    ),
    "STREAMBRIDGE_LOG_LEVEL": ("log_level", _as_str, "logging.level"),
}


def _env_overrides(environ: Mapping[str, str]) -> dict[str, Any]:
    """Translate STREAMBRIDGE_* variables into config fields."""
    out: dict[str, Any] = {}
    for env_key, (field_name, coercer, config_path) in _ENV_MAP.items():
        raw = environ.get(env_key)
        if raw is None or raw == "":
            continue
        try:
            out[field_name] = coercer(raw, env_key)
        except ConfigError as exc:
            raise ConfigError(
                f"Environment variable {env_key} ({config_path}): {exc.message}"
            ) from exc
    return out


def load_config(
    config_path: str | Path | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    overrides: Mapping[str, Any] | None = None,
) -> Config:
    """Load configuration from file, environment and explicit overrides.

    ``config_path`` accepts a str because both entry points receive one
    straight from argparse, where every value is a string.
    """
    env = environ if environ is not None else os.environ
    path = (
        Path(config_path)
        if config_path
        else Path(env.get("STREAMBRIDGE_CONFIG") or default_config_path())
    )

    data: dict[str, Any] = {}
    if path.is_file():
        try:
            with path.open("rb") as handle:
                data = tomllib.load(handle)
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(f"Configuration file {path} is not valid TOML: {exc}") from exc
        except OSError as exc:
            raise ConfigError(f"Configuration file {path} is not readable: {exc}") from exc

    config = config_from_mapping(data)
    config = replace(config, **_env_overrides(env))
    if overrides:
        config = replace(config, **{k: v for k, v in overrides.items() if v is not None})
    return validate_config(config)
