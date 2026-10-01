"""Configuration loading.

Precedence, lowest to highest:

1. built-in defaults
2. ``$XDG_CONFIG_HOME/streambridge/config.toml`` (or ``$STREAMBRIDGE_CONFIG``)
3. ``STREAMBRIDGE_*`` environment variables
4. explicit command line flags

Only the standard library (``tomllib``, Python 3.11+) is used.
"""

from __future__ import annotations

import ipaddress
import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from .errors import ConfigError

# Names accepted as "this machine". Kept for callers that only need the
# literal comparison; use is_loopback_host() for anything that may be an
# address literal such as 127.0.0.2 or ::1.
LOOPBACK_HOSTS = ("127.0.0.1", "::1", "localhost")


def is_loopback_host(host: str) -> bool:
    """True when *host* can only ever be reached from this machine.

    Hostnames are accepted for the two loopback spellings people actually
    type. Everything else has to parse as an IP address in a loopback range,
    which keeps 127.0.0.2 and ::1 in the trusted set without trusting
    arbitrary names that DNS could point anywhere.
    """
    candidate = (host or "").strip().lower()
    if candidate in LOOPBACK_HOSTS:
        return True
    try:
        return ipaddress.ip_address(candidate).is_loopback
    except ValueError:
        return False


DEFAULT_HOST = "127.0.0.1"
# The address MPD is told to use, regardless of what the server listens on.
LOCALHOST = "127.0.0.1"
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
DEFAULT_VOLUME = 80

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


def default_state_dir() -> Path:
    """Persistent state directory (favourites, play history).

    Kept apart from the cache on purpose: the cache may be discarded at any
    time, and a user's favourites must survive that.
    """
    return state_home() / "streambridge"


def default_web_dir() -> Path | None:
    """Directory holding the bundled web UI, or None when it is missing.

    The assets ship inside the package, so the location is derived from
    ``__file__`` rather than configured: there is no supported way to point
    it elsewhere, which keeps the static file server free of user-controlled
    roots. ``web_directory`` exists for unusual layouts (a distro package
    that splits files across directories).
    """
    return Path(__file__).resolve().parent / "web"


@dataclass(frozen=True, slots=True)
class Config:
    """Validated runtime configuration. All defaults are generic."""

    # [server]
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    # Opt-in LAN access for the companion mobile app. False keeps the historic
    # posture: loopback only, unreachable from the network.
    allow_lan: bool = False
    # Shared secret required from non-loopback clients when allow_lan is set.
    # Loopback clients stay exempt so the browser UI on this machine keeps
    # working with no token at all.
    access_token: str | None = None
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
    # [library] persistent user data: favourites and play history.
    state_directory: Path = field(default_factory=default_state_dir)
    # [server] bundled web assets. None disables the UI.
    web_directory: Path | None = field(default_factory=default_web_dir)
    # [player] fallback until MPD reports a volume of its own.
    default_volume: int = DEFAULT_VOLUME
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
        """The URL a client on this machine uses to reach the server."""
        return f"http://{self.host}:{self.port}"

    @property
    def stream_base_url(self) -> str:
        """The base URL written into the MPD queue, which is not always the
        address the server listens on.

        MPD fetches the audio itself, and it runs here, on this machine. So
        when the listening socket is a network address - because
        ``server.allow_lan`` is on so a phone can connect - the URL handed to
        MPD still points at loopback.

        Getting this wrong is silent and fatal: a queue entry holding the
        network address is fetched by MPD with no token attached, the server
        answers 401, and playback stops with "Failed to decode" on a player
        that is otherwise perfectly reachable. The token protects the network
        face; MPD must not be routed through it.
        """
        if is_loopback_host(self.host):
            return self.base_url
        return f"http://{LOCALHOST}:{self.port}"

    @property
    def web_enabled(self) -> bool:
        """True when the bundled web UI is present on disk."""
        directory = self.web_directory
        return directory is not None and (Path(directory) / "index.html").is_file()

    def stream_url(self, video_id: str) -> str:
        return f"{self.stream_base_url}/stream/{video_id}"


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


def _as_bool(value: Any, key: str) -> bool:
    """A TOML boolean.

    TOML gives a real bool, and a string where a bool belongs is a mistake
    worth reporting rather than guessing at.
    """
    if not isinstance(value, bool):
        raise ConfigError(f"{key} must be a boolean (true/false), got {value!r}")
    return value


def _as_env_bool(value: Any, key: str) -> bool:
    """A boolean that arrived from the environment, where everything is text.

    Strict on purpose: only the five spellings people actually type are
    accepted, so `allow_lan=yes` fails with a message naming the variable
    instead of silently reading as false and leaving the phone unable to
    connect with no explanation anywhere.
    """
    if isinstance(value, bool):
        return value
    if not isinstance(value, str):
        raise ConfigError(f"{key} must be a boolean (true/false), got {value!r}")
    lowered = value.strip().lower()
    if lowered in ("1", "true", "yes", "on"):
        return True
    if lowered in ("0", "false", "no", "off"):
        return False
    raise ConfigError(f"{key} must be one of true/false/1/0/yes/no/on/off, got {value!r}")


# section -> key -> (coercer, Config field)
_SCHEMA: dict[str, dict[str, Any]] = {
    "server": {
        "host": ("host", _as_str),
        "port": ("port", _as_int),
        "web_directory": ("web_directory", _as_path),
        "allow_lan": ("allow_lan", _as_bool),
        "access_token": ("access_token", _as_str),
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
    "library": {
        "directory": ("state_directory", _as_path),
    },
    "player": {
        "default_volume": ("default_volume", _as_int),
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


def is_lan_bind(config: Config) -> bool:
    """True when *config* asks for a bind address reachable from the network."""
    return not is_loopback_host(config.host)


def lan_bind_problem(config: Config) -> str | None:
    """Describe why *config* may not be served, or None when it is fine.

    Split out of the two call sites that need the same decision (the server
    factory and the CLI) so the rule lives in one place:

    * loopback is always allowed, exactly as before;
    * a network address needs ``server.allow_lan``;
    * a network address additionally needs a token, so enabling LAN access
      can never silently publish an unauthenticated player on the network.
    """
    if not is_lan_bind(config):
        return None
    if not config.allow_lan:
        return (
            f"streambridge-server listens on loopback only, not {config.host!r}.\n"
            "  Set server.allow_lan = true to serve the local network (for example for the\n"
            "  Android app), and set server.access_token to a shared secret at the same time."
        )
    if not config.access_token:
        return (
            f"Refusing to serve {config.host!r} without server.access_token.\n"
            "  LAN access requires a shared secret: anyone who can reach this port could\n"
            "  otherwise control the player. Generate one with:\n"
            "    python3 -c 'import secrets; print(secrets.token_urlsafe(24))'"
        )
    return None


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
    if not 0 <= config.default_volume <= 100:
        raise ConfigError(
            f"player.default_volume must be between 0 and 100, got {config.default_volume}"
        )
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
    "STREAMBRIDGE_STATE_DIR": ("state_directory", _as_path, "library.directory"),
    "STREAMBRIDGE_WEB_DIR": ("web_directory", _as_path, "server.web_directory"),
    "STREAMBRIDGE_ALLOW_LAN": ("allow_lan", _as_env_bool, "server.allow_lan"),
    "STREAMBRIDGE_ACCESS_TOKEN": ("access_token", _as_str, "server.access_token"),
    "STREAMBRIDGE_DEFAULT_VOLUME": ("default_volume", _as_int, "player.default_volume"),
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
