"""Logging redaction: no credentials, paths or host identifiers in records."""

from __future__ import annotations

import logging

import pytest

from streambridge.logging import (
    REDACTED,
    RedactingFilter,
    log_level_from_env,
    redact,
    redact_args,
    redact_url,
    setup_logging,
)

pytestmark = pytest.mark.unit


class TestRedact:
    def test_home_path_collapsed(self) -> None:
        # A home path identifies the user, so it becomes $HOME.
        assert redact("/home/someone/.cache/streambridge") == "$HOME/.cache/streambridge"

    def test_macos_home_path_collapsed(self) -> None:
        assert redact("/Users/someone/x") == "$HOME/x"

    def test_email_replaced(self) -> None:
        assert REDACTED in redact("contact me at someone@example.com please")

    def test_ipv4_replaced(self) -> None:
        assert REDACTED in redact("listening on 192.168.1.50:8787")

    def test_uuid_replaced(self) -> None:
        raw = "id 123e4567-e89b-12d3-a456-426614174000"
        assert REDACTED in redact(raw)

    def test_loopback_survives_redaction(self) -> None:
        # The loopback address is identical on every machine, so it identifies
        # nobody, and it is the one address the user needs in order to open the
        # web interface. Redacting it produced a startup log reading
        # "Web UI: http://<REDACTED>:8787/", which is worse than useless.
        assert redact("Web UI: http://127.0.0.1:8787/") == "Web UI: http://127.0.0.1:8787/"
        assert redact("listening on ::1") == "listening on ::1"

    def test_loopback_exception_does_not_hide_a_private_address(self) -> None:
        # The exception must be narrow: a LAN address next to a loopback one is
        # still redacted, and a loopback one embedded in a larger number is not
        # mistaken for permission to keep it.
        assert REDACTED in redact("from 127.0.0.1 to 192.168.1.50:6600")
        assert REDACTED in redact("10.127.0.0.1 reachable")

    def test_ordinary_text_untouched(self) -> None:
        assert redact("search returned 3 results") == "search returned 3 results"

    def test_empty_input(self) -> None:
        assert redact("") == ""

    def test_pathlike_without_user_untouched(self) -> None:
        # A system path is not identifying and redacting it would hurt
        # debuggability.
        assert redact("/usr/bin/yt-dlp") == "/usr/bin/yt-dlp"


class TestRedactUrl:
    def test_query_parameters_counted_not_shown(self) -> None:
        url = "https://rr3.googlevideo.com/videoplayback?expire=1&sig=SECRET&itag=140"
        out = redact_url(url)
        assert "SECRET" not in out
        assert "3 params" in out
        assert out.startswith("https://rr3.googlevideo.com/videoplayback")

    def test_url_without_query(self) -> None:
        assert (
            redact_url("https://rr3.googlevideo.com/videoplayback")
            == "https://rr3.googlevideo.com/videoplayback"
        )

    @pytest.mark.parametrize("value", ["", "not a url", "://", "https://"])
    def test_malformed_input_is_opaque(self, value: str) -> None:
        assert redact_url(value) == REDACTED

    def test_signed_parameters_never_appear(self) -> None:
        url = "https://rr3.googlevideo.com/v?expire=9999&signature=abc123&token=xyz"
        out = redact_url(url)
        assert "abc123" not in out
        assert "xyz" not in out


class TestRedactArgs:
    @pytest.mark.parametrize(
        "flag",
        ["--cookies", "--cookies-from-browser", "--username", "--password", "--video-password"],
    )
    def test_secret_flag_values_hidden(self, flag: str) -> None:
        out = redact_args(["yt-dlp", flag, "SUPERSECRET", "--dump-json"])
        assert "SUPERSECRET" not in " ".join(out)
        assert REDACTED in out

    def test_equals_form_hidden(self) -> None:
        out = redact_args(["yt-dlp", "--cookies-from-browser=firefox:secret"])
        assert "secret" not in out[1]
        assert out[1].startswith("--cookies-from-browser=")

    def test_ordinary_args_untouched(self) -> None:
        args = ["yt-dlp", "--dump-json", "ytsearch5:a test"]
        assert redact_args(args) == args

    def test_path_argument_is_collapsed(self) -> None:
        out = redact_args(["/home/someone/bin/yt-dlp"])
        assert "$HOME" in out[0]


class TestRedactingFilter:
    def test_message_is_cleaned(self, caplog: pytest.LogCaptureFixture) -> None:
        caplog.set_level(logging.INFO, logger="streambridge.test")
        log = logging.getLogger("streambridge.test")
        log.addFilter(RedactingFilter())
        try:
            log.info("connecting to 10.0.0.5 as someone@example.com")
        finally:
            log.removeFilter(RedactingFilter())
        text = caplog.text
        assert "10.0.0.5" not in text
        assert "someone@example.com" not in text

    def test_unredacted_message_passes_through(self, caplog: pytest.LogCaptureFixture) -> None:
        caplog.set_level(logging.INFO, logger="streambridge.test2")
        log = logging.getLogger("streambridge.test2")
        log.addFilter(RedactingFilter())
        try:
            log.info("nothing sensitive here")
        finally:
            log.removeFilter(RedactingFilter())
        assert "nothing sensitive here" in caplog.text


@pytest.fixture
def fresh_logging():
    """Reset the one-shot guard so each test configures logging from scratch."""
    import streambridge.logging as module

    module._configured = False
    previous = list(logging.getLogger().handlers)
    previous_level = logging.getLogger().level
    try:
        yield
    finally:
        logging.getLogger().handlers.clear()
        logging.getLogger().handlers.extend(previous)
        logging.getLogger().setLevel(previous_level)
        module._configured = False


class TestSetup:
    def test_idempotent(self, fresh_logging: None) -> None:
        # Both entry points configure logging; calling twice must not duplicate
        # handlers.
        setup_logging("INFO")
        first = len(logging.getLogger().handlers)
        setup_logging("DEBUG")
        assert len(logging.getLogger().handlers) == first

    def test_verbose_overrides_level(self, fresh_logging: None) -> None:
        setup_logging("INFO", verbose=True)
        assert logging.getLogger().level == logging.DEBUG

    def test_invalid_level_falls_back_to_info(self, fresh_logging: None) -> None:
        setup_logging("NOT-A-LEVEL")
        assert logging.getLogger().level == logging.INFO

    def test_handlers_carry_the_redacting_filter(self, fresh_logging: None) -> None:
        setup_logging("INFO")
        assert all(
            any(isinstance(f, RedactingFilter) for f in handler.filters)
            for handler in logging.getLogger().handlers
        )


class TestLogLevelFromEnv:
    def test_reads_variable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("STREAMBRIDGE_LOG_LEVEL", "DEBUG")
        assert log_level_from_env() == "DEBUG"

    def test_invalid_value_falls_back(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("STREAMBRIDGE_LOG_LEVEL", "LOUD")
        assert log_level_from_env("INFO") == "INFO"

    def test_default_used_when_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("STREAMBRIDGE_LOG_LEVEL", raising=False)
        assert log_level_from_env("WARNING") == "WARNING"
