"""CLI contract: help, exit codes, JSON output, readable terminal output.

The server is replaced by a stub so no command here reaches the network or a
real MPD.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from streambridge import __version__
from streambridge.cli import (
    ServerClient,
    build_parser,
    format_results,
    main,
    tracks_from_payload,
)
from streambridge.errors import ExitCode, StreamBridgeError, ValidationError
from streambridge.models import SearchType, Track

pytestmark = pytest.mark.integration

VALID_ID = "5NV6Rdv1a3I"
VALID_PLAYLIST = "PLrAXtmErZgOeiKm4sgNOknGvNjby9efdf"


def payload() -> dict[str, Any]:
    return {
        "query": "Artist One",
        "type": "songs",
        "count": 2,
        "results": [
            {
                "id": VALID_ID,
                "title": "First Song",
                "artist": "Artist One",
                "album": "Some Album",
                "duration": 249,
                "url": f"https://www.youtube.com/watch?v={VALID_ID}",
                "thumbnail": None,
                "channel": "Artist One",
                "uploader": "Artist One",
                "release_year": 2013,
            },
            {
                "id": "CCHdMIEGaaM",
                "title": "Second Song",
                "artist": "Artist One",
                "album": None,
                "duration": 320,
                "url": "https://www.youtube.com/watch?v=CCHdMIEGaaM",
                "thumbnail": None,
                "channel": "Artist One",
                "uploader": "Artist One",
                "release_year": 2000,
            },
        ],
    }


HEALTH = {
    "status": "ok",
    "version": __version__,
    "extractor": "2026.08.19",
    "uptime_seconds": 5,
}


class TestParser:
    def test_help_works(self, capsys: pytest.CaptureFixture[str]) -> None:
        with pytest.raises(SystemExit) as exc:
            main(["--help"])
        assert exc.value.code == 0
        assert "streambridge" in capsys.readouterr().out

    def test_version(self, capsys: pytest.CaptureFixture[str]) -> None:
        with pytest.raises(SystemExit) as exc:
            main(["--version"])
        assert exc.value.code == 0
        assert __version__ in capsys.readouterr().out

    def test_no_args_prints_help(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert main([]) == ExitCode.OK
        assert "usage" in capsys.readouterr().out.lower()

    @pytest.mark.parametrize(
        "command",
        [
            "search",
            "add",
            "enqueue",
            "play",
            "play-now",
            "info",
            "playlist",
            "status",
            "health",
            "doctor",
            "clear",
            "next",
            "pause",
            "stop",
        ],
    )
    def test_every_documented_command_parses(self, command: str) -> None:
        # Positional arity differs per command; each must accept its own shape.
        needs_id = {"add", "play", "enqueue", "play-now", "info"}
        needs_query = {"search"}
        needs_playlist = {"playlist"}
        if command in needs_id:
            args = [VALID_ID]
        elif command in needs_query:
            args = ["Artist One"]
        elif command in needs_playlist:
            args = [VALID_PLAYLIST]
        else:
            args = []
        parsed = build_parser().parse_args([command, *args])
        assert parsed.command == command
        assert callable(parsed.func)

    def test_json_flag_available(self) -> None:
        assert build_parser().parse_args(["--json", "status"]).json is True

    def test_global_json_applies_to_subcommand(self) -> None:
        # `--json` before the subcommand must survive; a subparser default of
        # False would otherwise overwrite it.
        parsed = build_parser().parse_args(["--json", "status"])
        assert parsed.json is True

    def test_examples_documented(self, capsys: pytest.CaptureFixture[str]) -> None:
        with pytest.raises(SystemExit):
            main(["--help"])
        out = capsys.readouterr().out
        assert "streambridge search" in out
        assert "streambridge play" in out

    def test_invalid_id_exits_2(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Validation happens before any server or MPD access, so health() is
        # replaced by something that would fail loudly if it were reached.

        def forbidden(self: Any) -> dict[str, Any]:
            raise AssertionError("server must not be contacted for an invalid id")

        monkeypatch.setattr(ServerClient, "health", forbidden)
        assert main(["add", "not-an-id"]) == ExitCode.INVALID_INPUT
        assert "11" in capsys.readouterr().err

    def test_invalid_id_for_play_also_2(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            ServerClient,
            "health",
            lambda self: (_ for _ in ()).throw(AssertionError("should not be reached")),
        )
        assert main(["play", "bad"]) == ExitCode.INVALID_INPUT
        assert capsys.readouterr().err


class TestFormatting:
    def test_contains_title_artist_album_duration_url(self) -> None:
        text = format_results(tracks_from_payload(payload()))
        assert "First Song" in text
        assert "Artist One" in text
        assert "Some Album" in text
        assert "4:09" in text  # 249 seconds
        assert f"https://www.youtube.com/watch?v={VALID_ID}" in text

    def test_numbers_results(self) -> None:
        text = format_results(tracks_from_payload(payload()))
        assert text.startswith("1.")
        assert "2." in text

    def test_minute_formatting(self) -> None:
        assert "5:20" in format_results(tracks_from_payload(payload()))  # 320 seconds

    def test_missing_album_falls_back(self) -> None:
        assert "Unknown Album" in format_results(tracks_from_payload(payload()))

    def test_missing_duration_placeholder(self) -> None:
        text = format_results([Track(id=VALID_ID, title="X", duration=None)])
        assert "--:--" in text

    def test_empty_results_message(self) -> None:
        assert "No results" in format_results([])

    def test_unusable_rows_skipped(self) -> None:
        assert tracks_from_payload({"results": [{"id": "bad"}, "nope", {}]}) == []

    def test_tracks_from_playlist_key(self) -> None:
        tracks = tracks_from_payload({"tracks": payload()["results"]}, key="tracks")
        assert [t.id for t in tracks] == [VALID_ID, "CCHdMIEGaaM"]


class TestOutputModes:
    def test_json_output_is_valid(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(ServerClient, "health", lambda self: HEALTH)
        assert main(["health", "--json"]) == ExitCode.OK
        assert json.loads(capsys.readouterr().out)["status"] == "ok"

    def test_human_output_without_ansi_when_piped(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(ServerClient, "health", lambda self: HEALTH)
        assert main(["health"]) == ExitCode.OK
        out = capsys.readouterr().out
        assert "streambridge-server" in out
        # No escape sequences when stdout is not a terminal.
        assert "\033[" not in out

    def test_health_reports_unreachable(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def boom(self: Any) -> dict[str, Any]:
            raise StreamBridgeError("server not reachable", hint="Start the server")

        monkeypatch.setattr(ServerClient, "health", boom)
        assert main(["health"]) == ExitCode.GENERAL_ERROR
        assert "not reachable" in capsys.readouterr().out

    def test_health_json_on_failure(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def boom(self: Any) -> dict[str, Any]:
            raise StreamBridgeError("server not reachable")

        monkeypatch.setattr(ServerClient, "health", boom)
        assert main(["health", "--json"]) == ExitCode.GENERAL_ERROR
        assert json.loads(capsys.readouterr().out)["status"] == "unreachable"


class TestExitCodes:
    def test_codes_are_distinct_and_documented(self) -> None:
        assert ExitCode.OK == 0
        assert ExitCode.GENERAL_ERROR == 1
        assert ExitCode.INVALID_INPUT == 2
        assert ExitCode.MISSING_DEPENDENCY == 3
        assert ExitCode.MPD_UNREACHABLE == 4
        assert ExitCode.SOURCE_UNAVAILABLE == 5
        assert ExitCode.NOT_FOUND == 6
        assert len({int(c) for c in ExitCode}) == 7


class TestSearchTypes:
    @pytest.mark.parametrize("value", ["songs", "videos", "artists", "albums", "playlists"])
    def test_all_types_accepted_by_parser(self, value: str) -> None:
        assert build_parser().parse_args(["search", "x", "--type", value]).type == value

    def test_default_is_songs(self) -> None:
        assert build_parser().parse_args(["search", "x"]).type == "songs"

    def test_unknown_type_rejected_by_parser(self) -> None:
        with pytest.raises(SystemExit):
            build_parser().parse_args(["search", "x", "--type", "podcasts"])

    def test_search_type_enum_matches_parser(self) -> None:
        assert {t.value for t in SearchType} >= {"songs", "videos"}


class TestServerClientScheme:
    """The client must only ever speak HTTP to a loopback address.

    Bandit flags every urlopen; the useful answer is not a blanket noqa but an
    enforced invariant, so a configuration that somehow produced a file:// base
    fails here instead of reading a local file.
    """

    def test_a_non_http_base_is_refused(self) -> None:
        client = ServerClient("file:///etc/passwd")
        with pytest.raises(ValidationError) as excinfo:
            client._get("/health")
        assert "file" in excinfo.value.user_message()

    def test_a_schemeless_base_is_refused(self) -> None:
        client = ServerClient("127.0.0.1:8787")
        with pytest.raises(ValidationError):
            client._get("/health")
