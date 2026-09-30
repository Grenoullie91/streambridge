"""Configuration parsing, defaults, environment overrides and validation.

StreamBridge uses sectioned TOML, so these tests also assert that a typo in a
section or key is reported by name rather than silently ignored.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from streambridge.config import (
    Config,
    config_from_mapping,
    default_cache_dir,
    default_config_path,
    load_config,
)
from streambridge.errors import ConfigError


class TestDefaults:
    def test_builtin_defaults(self) -> None:
        config = config_from_mapping({})
        assert config.host == "127.0.0.1"
        assert config.port == 8787
        assert config.mpd_host == "127.0.0.1"
        assert config.mpd_port == 6600
        assert config.search_limit == 20
        assert config.cookies_from_browser is None
        assert config.playlist_directory is None

    def test_loopback_is_the_default(self) -> None:
        # The service must never bind publicly by accident.
        assert config_from_mapping({}).host == "127.0.0.1"

    def test_defaults_are_generic(self) -> None:
        # No default may embed a path from the author's machine. The cache
        # directory is the only path-like default, and it is derived from
        # $XDG_CACHE_HOME or $HOME at call time.
        config = config_from_mapping({})
        assert config.extractor_path == "yt-dlp"
        assert config.cache_directory == default_cache_dir()
        assert "streambridge" in str(config.cache_directory)

    def test_base_and_stream_url(self) -> None:
        config = config_from_mapping({"server": {"port": 9999}})
        assert config.base_url == "http://127.0.0.1:9999"
        assert config.stream_url("5NV6Rdv1a3I") == "http://127.0.0.1:9999/stream/5NV6Rdv1a3I"

    def test_log_level_normalised(self) -> None:
        assert config_from_mapping({"logging": {"level": "debug"}}).log_level == "DEBUG"


class TestParsing:
    def test_reads_all_documented_sections(self) -> None:
        config = config_from_mapping(
            {
                "server": {"host": "127.0.0.1", "port": 9000},
                "search": {"limit": 5, "rate_per_min": 12},
                "timeouts": {"request": 8.0, "resolve": 12.5, "info": 9.0},
                "cache": {
                    "directory": "/tmp/streambridge-cache",
                    "ttl_seconds": 60.0,
                    "max_entries": 50,
                },
                "mpd": {"host": "localhost", "port": 6601, "playlist_directory": "/tmp/pl"},
                "youtube": {"extractor_path": "yt-dlp", "cookies_from_browser": "firefox"},
                "logging": {"level": "WARNING"},
            }
        )
        assert config.port == 9000
        assert config.search_limit == 5
        assert config.search_rate_per_min == 12
        assert config.request_timeout == 8.0
        assert config.resolve_timeout == 12.5
        assert config.cache_directory == Path("/tmp/streambridge-cache")
        assert config.cache_max_entries == 50
        assert config.mpd_host == "localhost"
        assert config.mpd_port == 6601
        assert config.playlist_directory == Path("/tmp/pl")
        assert config.cookies_from_browser == "firefox"
        assert config.log_level == "WARNING"

    def test_unknown_section_rejected(self) -> None:
        # A misspelled section is a user error worth reporting, not ignoring:
        # silently dropping it would leave the user wondering why the setting
        # has no effect.
        with pytest.raises(ConfigError) as exc:
            config_from_mapping({"servers": {"port": 9000}})
        assert "servers" in str(exc.value)

    def test_unknown_key_rejected(self) -> None:
        with pytest.raises(ConfigError) as exc:
            config_from_mapping({"server": {"prot": 9000}})
        assert "prot" in str(exc.value)

    def test_null_value_uses_default(self) -> None:
        config = config_from_mapping({"youtube": {"cookies_from_browser": None}})
        assert config.cookies_from_browser is None

    def test_section_must_be_a_table(self) -> None:
        with pytest.raises(ConfigError):
            config_from_mapping({"server": "127.0.0.1"})

    def test_cache_directory_expands_user(self) -> None:
        config = config_from_mapping({"cache": {"directory": "~/streambridge"}})
        assert config.cache_directory == Path.home() / "streambridge"

    @pytest.mark.parametrize(
        ("payload", "fragment"),
        [
            ({"server": {"port": "not-a-number"}}, "port"),
            ({"search": {"rate_per_min": 0}}, "rate_per_min"),
            ({"search": {"resolve_rate_per_min": 0}}, "resolve_rate_per_min"),
            ({"server": {"port": 0}}, "port"),
            ({"server": {"port": 70000}}, "port"),
            ({"server": {"port": True}}, "port"),
            ({"search": {"limit": 0}}, "limit"),
            ({"search": {"limit": 500}}, "limit"),
            ({"timeouts": {"resolve": -1}}, "timeouts.resolve"),
            ({"timeouts": {"request": 0}}, "timeouts.request"),
            ({"logging": {"level": "TRACE"}}, "level"),
            ({"cache": {"max_entries": 0}}, "max_entries"),
            ({"cache": {"ttl_seconds": -5}}, "ttl_seconds"),
            ({"mpd": {"port": 99999}}, "port"),
            ({"youtube": {"extractor_path": "  "}}, "extractor_path"),
        ],
    )
    def test_invalid_values_rejected(self, payload: dict, fragment: str) -> None:
        with pytest.raises(ConfigError) as exc:
            config_from_mapping(payload)
        assert fragment in str(exc.value)


class TestFileLoading:
    def test_loads_toml_file(self, tmp_path: Path) -> None:
        path = tmp_path / "config.toml"
        path.write_text(
            "\n".join(
                [
                    "[server]",
                    "port = 8123",
                    'host = "127.0.0.1"',
                    "",
                    "[search]",
                    "limit = 3",
                    "",
                    "[youtube]",
                    'cookies_from_browser = "brave"',
                ]
            ),
            encoding="utf-8",
        )
        config = load_config(path, environ={})
        assert config.port == 8123
        assert config.search_limit == 3
        assert config.cookies_from_browser == "brave"

    def test_missing_file_uses_defaults(self, tmp_path: Path) -> None:
        assert load_config(tmp_path / "nope.toml", environ={}).port == 8787

    def test_broken_toml_reports_path(self, tmp_path: Path) -> None:
        path = tmp_path / "config.toml"
        path.write_text("port = = 3", encoding="utf-8")
        with pytest.raises(ConfigError) as exc:
            load_config(path, environ={})
        assert str(path) in str(exc.value)

    def test_default_path_uses_xdg(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("XDG_CONFIG_HOME", "/custom/cfg")
        assert default_config_path() == Path("/custom/cfg/streambridge/config.toml")

    def test_config_env_var_selects_file(self, tmp_path: Path) -> None:
        path = tmp_path / "alt.toml"
        path.write_text("[server]\nport = 7777\n", encoding="utf-8")
        assert load_config(environ={"STREAMBRIDGE_CONFIG": str(path)}).port == 7777

    def test_string_path_accepted(self, tmp_path: Path) -> None:
        # Both entry points get their --config value straight from argparse,
        # where every value is a string.
        path = tmp_path / "str.toml"
        path.write_text("[server]\nport = 7654\n", encoding="utf-8")
        assert load_config(str(path), environ={}).port == 7654


class TestEnvironment:
    def test_env_overrides_file(self, tmp_path: Path) -> None:
        path = tmp_path / "config.toml"
        path.write_text("[server]\nport = 7000\n", encoding="utf-8")
        config = load_config(path, environ={"STREAMBRIDGE_PORT": "7001"})
        assert config.port == 7001

    def test_mpd_env_vars(self) -> None:
        config = load_config(
            environ={"STREAMBRIDGE_MPD_HOST": "127.0.0.2", "STREAMBRIDGE_MPD_PORT": "6602"}
        )
        assert config.mpd_host == "127.0.0.2"
        assert config.mpd_port == 6602

    def test_rate_limit_env_vars(self) -> None:
        config = load_config(
            environ={"STREAMBRIDGE_SEARCH_RATE": "12", "STREAMBRIDGE_RESOLVE_RATE": "34"}
        )
        assert config.search_rate_per_min == 12
        assert config.resolve_rate_per_min == 34

    def test_bad_rate_env_value_names_the_variable(self) -> None:
        # A type error is reported at the coercion site, which knows the
        # variable it read.
        with pytest.raises(ConfigError) as exc:
            load_config(environ={"STREAMBRIDGE_SEARCH_RATE": "lots"})
        assert "STREAMBRIDGE_SEARCH_RATE" in str(exc.value)

    def test_out_of_range_env_value_names_the_setting(self) -> None:
        # A range violation is caught by the final validation pass, so the
        # message names the config key rather than the variable.
        with pytest.raises(ConfigError) as exc:
            load_config(environ={"STREAMBRIDGE_SEARCH_RATE": "0"})
        assert "search.rate_per_min" in str(exc.value)

    def test_empty_env_value_ignored(self) -> None:
        assert load_config(environ={"STREAMBRIDGE_PORT": ""}).port == 8787

    def test_bad_env_value_mentions_variable(self) -> None:
        with pytest.raises(ConfigError) as exc:
            load_config(environ={"STREAMBRIDGE_PORT": "abc"})
        assert "STREAMBRIDGE_PORT" in str(exc.value)

    def test_log_level_env(self) -> None:
        assert load_config(environ={"STREAMBRIDGE_LOG_LEVEL": "DEBUG"}).log_level == "DEBUG"

    def test_explicit_overrides_win(self) -> None:
        config = load_config(environ={"STREAMBRIDGE_PORT": "7100"}, overrides={"port": 7200})
        assert config.port == 7200

    def test_none_overrides_ignored(self) -> None:
        assert load_config(environ={}, overrides={"port": None}).port == 8787


class TestConfigSafety:
    def test_public_bind_is_detectable(self) -> None:
        # The value round-trips so the server can refuse it; the refusal itself
        # is asserted in the server tests.
        config = config_from_mapping({"server": {"host": "0.0.0.0"}})
        assert config.host not in ("127.0.0.1", "::1", "localhost")

    def test_frozen(self) -> None:
        config = Config()
        with pytest.raises(Exception):
            config.port = 1  # type: ignore[misc]
