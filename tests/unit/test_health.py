"""Environment checks and dependency reporting."""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from conftest import FAKE_EXECUTABLE
from streambridge.config import Config
from streambridge.health import (
    COMPONENTS,
    REQUIRED_PYTHON,
    Check,
    Report,
    audio_backends,
    build_report,
    check_component,
    missing_required,
)

pytestmark = pytest.mark.unit


class TestVersionComparison:
    @pytest.mark.parametrize(
        ("version", "minimum", "expected"),
        [
            ("2026.08.19", "2024.01.01", True),
            ("2024.01.01", "2024.01.01", True),
            ("2023.12.31", "2024.01.01", False),
            ("2026.8.19", "2024.1.1", True),
            ("", "2024.01.01", False),
            ("not-a-version", "2024.01.01", False),
        ],
    )
    def test_compares_numerically(self, version: str, minimum: str, expected: bool) -> None:
        from streambridge.health import _version_at_least

        assert _version_at_least(version, minimum) is expected

    def test_string_comparison_would_be_wrong(self) -> None:
        # "9.0" sorts after "10.0" as a string but is older. This is why the
        # comparison is numeric.
        from streambridge.health import _version_at_least

        assert _version_at_least("9.0.0", "10.0.0") is False


class TestCheckComponent:
    def test_missing_component_reported(self) -> None:
        check = check_component("definitely-not-installed-xyz", "testing", True)
        assert check.ok is False
        assert check.required is True
        assert "missing" in (check.detail or "")

    def test_optional_component_missing_is_not_fatal(self) -> None:
        check = check_component("definitely-not-installed-xyz", "optional", False)
        assert check.ok is False
        assert check.required is False

    def test_present_component_reported(self) -> None:
        check = check_component("sh", "testing", True)
        assert check.ok is True
        assert check.path is not None
        assert Path(check.path).exists()

    def test_to_dict_shape(self) -> None:
        payload = check_component("sh", "testing", True).to_dict()
        assert set(payload) == {"name", "ok", "path", "version", "detail", "required"}


class TestReport:
    def test_ok_when_nothing_required_is_missing(self, config: Config) -> None:
        report = Report(checks=[Check("x", ok=True, required=True)])
        assert report.ok is True

    def test_not_ok_when_a_required_check_fails(self, config: Config) -> None:
        report = Report(checks=[Check("x", ok=False, required=True)])
        assert report.ok is False

    def test_optional_failure_does_not_break_ok(self, config: Config) -> None:
        report = Report(
            checks=[Check("x", ok=True, required=True), Check("y", ok=False, required=False)]
        )
        assert report.ok is True

    def test_to_dict_is_json_friendly(self, config: Config) -> None:
        import json

        payload = Report(checks=[Check("x", ok=True)]).to_dict()
        json.dumps(payload)
        assert payload["status"] == "ok"
        assert payload["version"]

    def test_render_includes_component_names(self, config: Config) -> None:
        report = Report(checks=[Check("yt-dlp", ok=True, path="/usr/bin/yt-dlp")])
        text = report.render()
        assert "yt-dlp" in text
        assert "/usr/bin/yt-dlp" in text
        assert "Result: OK" in text

    def test_render_reports_incomplete(self, config: Config) -> None:
        report = Report(checks=[Check("mpc", ok=False, required=True)])
        assert "Result: INCOMPLETE" in report.render()


class TestBuildReport:
    def test_reports_python_version(self, config: Config) -> None:
        report = build_report(config)
        assert report.python_version
        assert report.python_ok is (report.python_version >= "3.0")

    def test_does_not_probe_network_by_default(self, config: Config) -> None:
        # A dependency check must stay fast and offline.
        report = build_report(config)
        assert report.mpd_reachable is None
        assert report.server_reachable is None

    def test_documented_components_are_checked(self, config: Config) -> None:
        report = build_report(config)
        assert {c.name for c in report.checks} == {name for name, _why, _req in COMPONENTS}

    def test_bind_and_cache_reported(self, config: Config) -> None:
        report = build_report(config)
        assert report.bind == f"http://{config.host}:{config.port}"
        assert report.cache_directory == str(config.cache_directory)
        assert report.mpd_endpoint == f"{config.mpd_host}:{config.mpd_port}"

    def test_missing_required_returns_names(self, config: Config) -> None:
        # A config pointing at a nonexistent extractor must report it.
        import dataclasses

        broken = dataclasses.replace(config, extractor_path="definitely-not-installed-xyz")
        assert "yt-dlp" in missing_required(broken)

    def test_outdated_extractor_gets_a_note(
        self, config: Config, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Both halves need controlling, not just the comparison: the report
        # looks up the configured extractor on PATH, so asserting "present"
        # against a real yt-dlp tests the machine the test runs on rather than
        # the code. A name that resolves everywhere keeps it hermetic.
        named = dataclasses.replace(config, extractor_path=FAKE_EXECUTABLE)
        monkeypatch.setattr("streambridge.health._version_at_least", lambda *a: False)
        # The stub resolves but reports no version, so "outdated" cannot be
        # derived from it - the version has to be supplied, not the binary.
        monkeypatch.setattr(
            "streambridge.youtube.ExtractorClient.version",
            lambda self: "2020.01.01",
        )
        report = build_report(named)
        check = next(c for c in report.checks if c.name == "yt-dlp")
        assert check.ok is True
        assert "update recommended" in (check.detail or "")

    def test_current_extractor_gets_no_note(
        self, config: Config, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("streambridge.health._version_at_least", lambda *a: True)
        report = build_report(config)
        check = next(c for c in report.checks if c.name == "yt-dlp")
        assert "update recommended" not in (check.detail or "")

    def test_unknown_extractor_version_gets_no_note(
        self, config: Config, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # An unreadable version means there is nothing to compare, so no
        # advice is invented. Stubbing the client keeps the real binary's
        # version out of the assertion.
        from streambridge.proc import CompletedRun
        from streambridge.youtube import ExtractorClient

        monkeypatch.setattr(ExtractorClient, "version", lambda self: None)
        report = build_report(dataclasses.replace(config, extractor_path=FAKE_EXECUTABLE))
        check = next(c for c in report.checks if c.name == "yt-dlp")
        assert check.ok is True
        assert check.version is None
        assert "update recommended" not in (check.detail or "")
        assert CompletedRun((), 0, "", "").ok


class TestAudioBackends:
    def test_reports_known_backends(self) -> None:
        found = audio_backends()
        assert set(found) == {"pipewire", "pulseaudio"}
        assert all(isinstance(v, bool) for v in found.values())


class TestRequiredPython:
    def test_floor_is_311(self) -> None:
        # tomllib entered the stdlib in 3.11; there is no stdlib TOML parser
        # before that.
        assert REQUIRED_PYTHON == (3, 11)
