"""The extractor abstraction: argument safety, parsing, error classification.

This is the only module that knows the upstream tool exists, so these tests
double as the contract for swapping in a different backend.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from conftest import FAKE_EXECUTABLE, INFO_PAYLOAD, SEARCH_PAYLOAD, FakeRunner
from streambridge.config import config_from_mapping
from streambridge.errors import (
    DependencyError,
    SourceUnavailableError,
    ValidationError,
)
from streambridge.youtube import (
    MAX_ATTEMPTS,
    ExtractorClient,
    as_bytes,
    as_duration,
    classify_error,
    clean_text,
    error_message_for,
    expiry_from_url,
    extract_headers,
    pick_audio_format,
    track_from_entry,
)

pytestmark = pytest.mark.unit

VALID_ID = "5NV6Rdv1a3I"
OTHER_ID = "CCHdMIEGaaM"
VALID_PLAYLIST = "PLrAXtmErZgOeiKm4sgNOknGvNjby9efdf"


def make_client(runner: FakeRunner, **overrides: Any) -> ExtractorClient:
    config = config_from_mapping(
        {"timeouts": {"request": 5.0, "resolve": 5.0, "info": 5.0}, **overrides}
    )
    # The executable must be a name that resolves on any machine.
    #
    # The client's require() looks the path up before it hands anything to the
    # runner, so a test that relied on the configured "yt-dlp" passed only
    # where yt-dlp happened to be installed and failed on a CI runner that has
    # none. An explicit extractor_path is honoured, because the tests below
    # that care about a *missing* extractor pass their own unresolvable name
    # and need it to stay missing.
    extractor = config.extractor_path
    if extractor == "yt-dlp":
        extractor = FAKE_EXECUTABLE
    return ExtractorClient(config, runner=runner, executable=extractor)


class TestDependency:
    def test_missing_binary_raises_dependency_error(self) -> None:
        client = make_client(
            FakeRunner(), youtube={"extractor_path": "definitely-not-installed-xyz"}
        )
        with pytest.raises(DependencyError) as exc:
            client.require()
        assert "yt-dlp" in str(exc.value)
        assert int(exc.value.exit_code) == 3

    def test_is_available_false_for_missing(self) -> None:
        client = make_client(
            FakeRunner(), youtube={"extractor_path": "definitely-not-installed-xyz"}
        )
        assert client.is_available() is False

    def test_is_available_true_when_the_executable_resolves(self) -> None:
        # Not "a real extractor", just a resolvable name. Asserting that the
        # development machine has yt-dlp installed is a test of the machine.
        assert make_client(FakeRunner()).is_available() is True

    def test_version_parsed(self) -> None:
        runner = FakeRunner()
        runner.add_text("--version", "2026.08.19\n")
        assert make_client(runner).version() == "2026.08.19"

    def test_version_none_when_command_fails(self) -> None:
        runner = FakeRunner()
        runner.add_text("--version", "", returncode=1, stderr="boom")
        assert make_client(runner).version() is None


class TestSearch:
    def test_returns_typed_tracks(self) -> None:
        runner = FakeRunner()
        runner.add_json("ytsearch", SEARCH_PAYLOAD)
        result = make_client(runner).search("artist one get lucky", limit=3)
        assert result.query == "artist one get lucky"
        assert [t.id for t in result.tracks] == [VALID_ID, OTHER_ID]

    def test_invalid_entries_dropped(self) -> None:
        runner = FakeRunner()
        runner.add_json("ytsearch", SEARCH_PAYLOAD)
        result = make_client(runner).search("artist one", limit=5)
        # "too-short-id" is not a video id and must be filtered out.
        assert all(len(t.id) == 11 for t in result.tracks)

    def test_duration_coerced_to_int(self) -> None:
        runner = FakeRunner()
        runner.add_json("ytsearch", SEARCH_PAYLOAD)
        result = make_client(runner).search("artist one", limit=1)
        assert result.tracks[0].duration == 249
        assert isinstance(result.tracks[0].duration, int)

    def test_metadata_mapped(self) -> None:
        runner = FakeRunner()
        runner.add_json("ytsearch", SEARCH_PAYLOAD)
        second = make_client(runner).search("artist one", limit=3).tracks[1]
        assert second.artist == "Artist One"
        assert second.album == "Some Album"
        assert second.webpage_url.endswith(OTHER_ID)

    def test_thumbnail_picked(self) -> None:
        runner = FakeRunner()
        runner.add_json("ytsearch", SEARCH_PAYLOAD)
        result = make_client(runner).search("artist one", limit=1)
        assert result.tracks[0].thumbnail == f"https://i.ytimg.com/vi/{VALID_ID}/hq.jpg"

    def test_release_year_extracted(self) -> None:
        runner = FakeRunner()
        runner.add_json("ytsearch", SEARCH_PAYLOAD)
        assert make_client(runner).search("artist one", limit=1).tracks[0].release_year == 2013

    def test_empty_query_rejected(self) -> None:
        with pytest.raises(ValidationError):
            make_client(FakeRunner()).search("   ")

    def test_control_characters_rejected(self) -> None:
        with pytest.raises(ValidationError):
            make_client(FakeRunner()).search("artist\x00one")

    def test_limit_bounded_to_100(self) -> None:
        runner = FakeRunner()
        runner.add_json("ytsearch", SEARCH_PAYLOAD)
        make_client(runner).search("x", limit=5000)
        assert "ytsearch100:" in " ".join(runner.calls[-1])

    def test_zero_limit_rejected(self) -> None:
        with pytest.raises(ValidationError):
            make_client(FakeRunner()).search("x", limit=0)

    def test_query_passed_as_single_argument(self) -> None:
        runner = FakeRunner()
        runner.add_json("ytsearch", SEARCH_PAYLOAD)
        make_client(runner).search("a; rm -rf /", limit=1)
        # The whole query is one argv entry, so shell splitting is impossible.
        assert any(a == "ytsearch1:a; rm -rf / song" for a in runner.calls[-1])

    def test_falls_back_to_plain_search(self) -> None:
        runner = FakeRunner()
        # The music-focused query returns nothing; the plain one finds results.
        runner.add_json("ytsearch1:obscure song", {"entries": []})
        runner.add_json("ytsearch1:obscure", SEARCH_PAYLOAD)
        assert make_client(runner).search("obscure", limit=1).tracks

    def test_no_fallback_for_other_search_types(self) -> None:
        runner = FakeRunner()
        runner.add_json("ytsearch", {"entries": []})
        from streambridge.models import SearchType

        result = make_client(runner).search("obscure", limit=1, search_type=SearchType.VIDEOS)
        # Only the songs path retries with a plainer query.
        assert result.tracks == ()
        assert runner.count("ytsearch") == 1

    def test_missing_fields_do_not_crash(self) -> None:
        runner = FakeRunner()
        runner.add_json(
            "ytsearch",
            {
                "entries": [
                    {"id": "aaaaaaaaaaa"},
                    {"id": "bbbbbbbbbbb", "title": "T", "duration": None},
                    {},
                    "not-a-dict",
                    None,
                ]
            },
        )
        result = make_client(runner).search("edge", limit=5)
        # Only the two rows with a usable id survive; an untitled row gets a
        # placeholder rather than raising.
        assert len(result.tracks) == 2
        assert all(t.title for t in result.tracks)

    def test_no_entries_key(self) -> None:
        runner = FakeRunner()
        runner.add_json("ytsearch", {"title": "no entries"})
        assert make_client(runner).search("nothing", limit=3).tracks == ()


class TestInfo:
    def test_returns_track(self) -> None:
        runner = FakeRunner()
        runner.add_json("watch?v=", INFO_PAYLOAD)
        track = make_client(runner).get_info(VALID_ID)
        assert track.id == VALID_ID
        assert track.album == "Some Album"
        assert track.duration == 249

    def test_invalid_id_rejected_before_subprocess(self) -> None:
        runner = FakeRunner()
        with pytest.raises(ValidationError):
            make_client(runner).get_info("not-an-id")
        assert runner.calls == []


class TestResolve:
    def test_prefers_m4a_over_webm(self) -> None:
        runner = FakeRunner()
        runner.add_json("watch?v=", INFO_PAYLOAD)
        info = make_client(runner).resolve_stream(VALID_ID)
        # m4a/aac is preferred because MPD decodes it without ffmpeg.
        assert info.format_id == "140"
        assert info.ext == "m4a"

    def test_falls_back_to_opus(self) -> None:
        payload = json.loads(json.dumps(INFO_PAYLOAD))
        payload["formats"] = [f for f in payload["formats"] if f["ext"] != "m4a"]
        runner = FakeRunner()
        runner.add_json("watch?v=", payload)
        info = make_client(runner).resolve_stream(VALID_ID)
        assert info.ext == "webm"
        assert info.acodec == "opus"

    def test_higher_bitrate_wins_within_a_container(self) -> None:
        payload = json.loads(json.dumps(INFO_PAYLOAD))
        # Two m4a variants: the higher bitrate one must be selected.
        lower = next(f for f in payload["formats"] if f["ext"] == "m4a")
        higher = json.loads(json.dumps(lower))
        higher["format_id"] = "141"
        higher["abr"] = 256
        payload["formats"].append(higher)
        runner = FakeRunner()
        runner.add_json("watch?v=", payload)
        assert make_client(runner).resolve_stream(VALID_ID).format_id == "141"

    def test_video_only_formats_ignored(self) -> None:
        payload = json.loads(json.dumps(INFO_PAYLOAD))
        payload["formats"] = [f for f in payload["formats"] if f["acodec"] == "none"]
        runner = FakeRunner()
        runner.add_json("watch?v=", payload)
        with pytest.raises(SourceUnavailableError) as exc:
            make_client(runner).resolve_stream(VALID_ID)
        assert "audio" in exc.value.message.lower()

    def test_headers_forwarded(self) -> None:
        runner = FakeRunner()
        runner.add_json("watch?v=", INFO_PAYLOAD)
        info = make_client(runner).resolve_stream(VALID_ID)
        assert info.headers["User-Agent"].startswith("Mozilla")

    def test_expiry_parsed_from_url(self) -> None:
        runner = FakeRunner()
        runner.add_json("watch?v=", INFO_PAYLOAD)
        info = make_client(runner).resolve_stream(VALID_ID)
        assert info.expires_at == 9999999999.0
        assert info.is_probably_expired(0.0) is False

    def test_no_expiry_means_never_guessed(self) -> None:
        payload = json.loads(json.dumps(INFO_PAYLOAD))
        for fmt in payload["formats"]:
            fmt["url"] = "https://rr3---sn-x.googlevideo.com/videoplayback?itag=140"
        runner = FakeRunner()
        runner.add_json("watch?v=", payload)
        info = make_client(runner).resolve_stream(VALID_ID)
        assert info.expires_at is None
        # An unknown expiry must not be treated as expired.
        assert info.is_probably_expired(1e12) is False

    def test_content_length_from_filesize(self) -> None:
        runner = FakeRunner()
        runner.add_json("watch?v=", INFO_PAYLOAD)
        assert make_client(runner).resolve_stream(VALID_ID).content_length == 4_025_466

    def test_content_length_from_approx_when_filesize_missing(self) -> None:
        payload = json.loads(json.dumps(INFO_PAYLOAD))
        for fmt in payload["formats"]:
            if fmt["ext"] == "m4a":
                fmt.pop("filesize")
                fmt["filesize_approx"] = 4_000_000
        runner = FakeRunner()
        runner.add_json("watch?v=", payload)
        assert make_client(runner).resolve_stream(VALID_ID).content_length == 4_000_000

    def test_malformed_json_retries_then_fails(self) -> None:
        runner = FakeRunner()
        runner.add_text("watch?v=", "{not json", returncode=0)
        with pytest.raises(SourceUnavailableError) as exc:
            make_client(runner).resolve_stream(VALID_ID)
        assert int(exc.value.exit_code) == 5

    def test_no_url_in_format(self) -> None:
        payload = json.loads(json.dumps(INFO_PAYLOAD))
        for fmt in payload["formats"]:
            fmt.pop("url", None)
        runner = FakeRunner()
        runner.add_json("watch?v=", payload)
        with pytest.raises(SourceUnavailableError):
            make_client(runner).resolve_stream(VALID_ID)


class TestErrorClassification:
    @pytest.mark.parametrize(
        ("stderr", "expected"),
        [
            ("ERROR: Video unavailable", "unavailable"),
            ("ERROR: [youtube] abc: Private video", "unavailable"),
            ("This video is not available in your country", "geo"),
            ("Sign in to confirm you're not a bot", "botcheck"),
            ("ERROR: Requested format is not available", "no_audio"),
            ("something completely different", "unknown"),
        ],
    )
    def test_classify(self, stderr: str, expected: str) -> None:
        kind, _transient = classify_error(stderr, 1)
        assert kind == expected

    def test_botcheck_is_transient(self) -> None:
        kind, transient = classify_error("Sign in to confirm you're not a bot", 1)
        assert (kind, transient) == ("botcheck", True)

    def test_unavailable_is_not_transient(self) -> None:
        kind, transient = classify_error("Video unavailable", 1)
        assert (kind, transient) == ("unavailable", False)

    def test_timeout_code(self) -> None:
        kind, transient = classify_error("", 124)
        assert (kind, transient) == ("timeout", True)

    def test_every_kind_has_a_message_and_hint(self) -> None:
        for kind in ("unavailable", "geo", "botcheck", "no_audio", "timeout", "unknown"):
            message, hint = error_message_for(kind, VALID_ID)
            assert message
            if kind != "unknown":
                assert hint
                assert VALID_ID in message


class TestArgumentSafety:
    def test_cookies_passed_as_separate_argv_entry(self) -> None:
        runner = FakeRunner()
        runner.add_json("ytsearch", SEARCH_PAYLOAD)
        make_client(runner, youtube={"cookies_from_browser": "firefox"}).search("x", limit=1)
        # The point of the test is the argv shape, so it goes through a client
        # whose executable resolves everywhere; see make_client.
        call = runner.calls[-1]
        assert call[call.index("--cookies-from-browser") + 1] == "firefox"

    def test_shell_metacharacters_stay_in_one_argument(self) -> None:
        runner = FakeRunner()
        runner.add_json("ytsearch", SEARCH_PAYLOAD)
        make_client(runner).search("$(whoami) `id` && ls", limit=1)
        for arg in runner.calls[-1]:
            assert "\n" not in arg

    def test_version_flag_queried_separately(self) -> None:
        runner = FakeRunner()
        runner.add_text("--version", "2026.08.19")
        make_client(runner).version()
        assert runner.calls[-1][-1] == "--version"


class TestPlaylist:
    def test_order_preserved(self) -> None:
        runner = FakeRunner()
        runner.add_json(
            "playlist?list=",
            {
                "title": "My Playlist",
                "entries": [
                    {"id": "aaaaaaaaaaa", "title": "First"},
                    {"id": "bbbbbbbbbbb", "title": "Second"},
                    {"id": "ccccccccccc", "title": "Third"},
                ],
            },
        )
        playlist = make_client(runner).playlist(VALID_PLAYLIST)
        assert [t.title for t in playlist.tracks] == ["First", "Second", "Third"]
        assert playlist.title == "My Playlist"

    def test_limit_respected(self) -> None:
        runner = FakeRunner()
        runner.add_json(
            "playlist?list=",
            {"entries": [{"id": f"{i:011d}", "title": f"T{i}"} for i in range(50)]},
        )
        assert len(make_client(runner).playlist(VALID_PLAYLIST, limit=5).tracks) == 5

    def test_invalid_playlist_id_rejected(self) -> None:
        with pytest.raises(ValidationError):
            make_client(FakeRunner()).playlist("not-a-playlist")

    def test_flat_playlist_used(self) -> None:
        # A flat listing avoids probing every entry, which is what makes
        # queueing a large playlist cheap.
        runner = FakeRunner()
        runner.add_json("playlist?list=", {"entries": []})
        make_client(runner).playlist(VALID_PLAYLIST)
        assert "--flat-playlist" in runner.calls[-1]


class TestRetryPolicy:
    def test_bounded_retries_on_transient_error(self) -> None:
        runner = FakeRunner()
        runner.add_text("watch?v=", "", returncode=1, stderr="Sign in to confirm you're not a bot")
        with pytest.raises(SourceUnavailableError):
            make_client(runner).resolve_stream(VALID_ID)
        # Exactly MAX_ATTEMPTS tries, never an unbounded loop.
        assert len(runner.calls) == MAX_ATTEMPTS

    def test_no_retry_on_permanent_error(self) -> None:
        runner = FakeRunner()
        runner.add_text("watch?v=", "", returncode=1, stderr="Video unavailable")
        with pytest.raises(SourceUnavailableError):
            make_client(runner).resolve_stream(VALID_ID)
        assert len(runner.calls) == 1

    def test_upstream_message_surfaced(self) -> None:
        runner = FakeRunner()
        runner.add_text("watch?v=", "", returncode=1, stderr="ERROR: Video unavailable")
        with pytest.raises(SourceUnavailableError) as exc:
            make_client(runner).resolve_stream(VALID_ID)
        assert "Video unavailable" in (exc.value.upstream_message or "")


class TestCoercions:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (None, None),
            ("", None),
            ("   ", None),
            ("none", None),
            ("N/A", None),
            (True, None),
            ({"a": 1}, None),
            (["a"], None),
            ("text", "text"),
        ],
    )
    def test_clean_text(self, value: Any, expected: str | None) -> None:
        assert clean_text(value) == expected

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (249, 249),
            (249.4, 249),
            ("249", 249),
            ("4:09", 249),
            ("1:02:03", 3723),
            (0, None),
            (-5, None),
            (None, None),
            ("abc", None),
            (True, None),
            (10**9, None),  # implausible duration
        ],
    )
    def test_as_duration(self, value: Any, expected: int | None) -> None:
        assert as_duration(value) == expected

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (1000, 1000),
            ("2048", 2048),
            (0, None),
            (-1, None),
            (None, None),
            ("abc", None),
            (True, None),
            (2**50, None),  # implausible size
        ],
    )
    def test_as_bytes(self, value: Any, expected: int | None) -> None:
        assert as_bytes(value) == expected

    @pytest.mark.parametrize(
        ("url", "expected"),
        [
            ("https://x.googlevideo.com/v?expire=9999999999", 9999999999.0),
            ("https://x.googlevideo.com/v?expireei=1234567890", 1234567890.0),
            ("https://x.googlevideo.com/v?itag=140", None),
            ("https://x.googlevideo.com/v?expire=abc", None),
            ("not a url", None),
        ],
    )
    def test_expiry_from_url(self, url: str, expected: float | None) -> None:
        assert expiry_from_url(url) == expected

    def test_headers_with_newlines_dropped(self) -> None:
        # A newline in a header value would allow injecting extra headers.
        headers = extract_headers(
            {"http_headers": {"User-Agent": "ok\r\nX-Injected: 1", "Bad": "a\nb"}}
        )
        assert headers == {}

    def test_headers_non_mapping_ignored(self) -> None:
        assert extract_headers({"http_headers": "nope"}) == {}

    def test_pick_audio_format_none_when_empty(self) -> None:
        assert pick_audio_format({"formats": []}) is None
        assert pick_audio_format({}) is None

    def test_track_from_entry_rejects_non_mapping(self) -> None:
        assert track_from_entry("nope") is None  # type: ignore[arg-type]

    def test_track_from_entry_url_falls_back_to_watch_url(self) -> None:
        track = track_from_entry({"id": VALID_ID, "title": "T"})
        assert track is not None
        assert track.webpage_url == f"https://www.youtube.com/watch?v={VALID_ID}"

    def test_track_from_entry_artist_falls_back_to_track_field(self) -> None:
        track = track_from_entry({"id": VALID_ID, "title": "T", "track": "Artist"})
        assert track is not None
        assert track.artist == "Artist"
