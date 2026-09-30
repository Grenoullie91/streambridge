"""Input validation, display formatting and model round-trips.

The id validators are the primary defence against path traversal and open-proxy
behaviour, so they get the most exhaustive coverage in the suite.
"""

from __future__ import annotations

import pytest

from streambridge.errors import ExitCode, ValidationError
from streambridge.models import (
    MAX_QUERY_LENGTH,
    SearchType,
    StreamInfo,
    Track,
    validate_playlist_id,
    validate_query,
    validate_video_id,
)

VALID_ID = "5NV6Rdv1a3I"


class TestVideoId:
    @pytest.mark.parametrize(
        "value",
        [
            VALID_ID,
            "dQw4w9WgXcQ",
            "abcdefghijk",
            "A1_B2-C3d4E",
            "___________",
        ],
    )
    def test_accepts_valid(self, value: str) -> None:
        assert validate_video_id(value) == value

    @pytest.mark.parametrize(
        "value",
        [
            "",
            "short",
            "waytoolongvideoid",
            "../../../etc/passwd",
            f"{VALID_ID}/../x",
            f"{VALID_ID}?x=1",
            f"{VALID_ID}#frag",
            "5NV6Rdv1a3\nI",
            "5NV6Rdv1a3I\x00",
            f"https://www.youtube.com/watch?v={VALID_ID}",
            "5NV6Rdv1a3I%2f",
            "'; rm -rf /",
            f"{VALID_ID}|whoami",
            "..",
            "1; cat /etc/passwd",
            "5NV6Rdv1a3\x0bI",  # vertical tab: still 12 characters
            "5NV6Rdv1a3I\n--exec rm",  # argument injection attempt
            "%2e%2e%2fetc%2fpasswd",
        ],
    )
    def test_rejects_invalid(self, value: str) -> None:
        with pytest.raises(ValidationError):
            validate_video_id(value)

    def test_strips_surrounding_whitespace(self) -> None:
        assert validate_video_id(f"  {VALID_ID}  ") == VALID_ID

    def test_error_message_carries_hint(self) -> None:
        with pytest.raises(ValidationError) as exc:
            validate_video_id("nope")
        assert exc.value.hint is not None
        assert "11" in exc.value.hint

    def test_exit_code_is_invalid_input(self) -> None:
        with pytest.raises(ValidationError) as exc:
            validate_video_id("nope")
        assert exc.value.exit_code == ExitCode.INVALID_INPUT

    def test_exactly_eleven_characters(self) -> None:
        assert validate_video_id("a" * 11)
        with pytest.raises(ValidationError):
            validate_video_id("a" * 10)
        with pytest.raises(ValidationError):
            validate_video_id("a" * 12)


class TestPlaylistId:
    @pytest.mark.parametrize(
        "value",
        [
            "PLrAXtmErZgOeiKm4sgNOknGvNjby9efdf",
            "UU1234567890abcdefghij",
            "OLAK5uy_abcdefghijklmnopqrstuv",
            "RDabcdefghij",
        ],
    )
    def test_accepts_valid(self, value: str) -> None:
        assert validate_playlist_id(value) == value

    @pytest.mark.parametrize(
        "value",
        ["", "PL", "../../etc/passwd", VALID_ID, "XXshort", "PL;rm -rf /"],
    )
    def test_rejects_invalid(self, value: str) -> None:
        with pytest.raises(ValidationError):
            validate_playlist_id(value)


class TestQuery:
    def test_trims(self) -> None:
        assert validate_query("  daft punk  ") == "daft punk"

    def test_rejects_empty(self) -> None:
        with pytest.raises(ValidationError):
            validate_query("   ")

    def test_rejects_too_long(self) -> None:
        with pytest.raises(ValidationError):
            validate_query("a" * (MAX_QUERY_LENGTH + 1))

    def test_accepts_maximum_length(self) -> None:
        assert validate_query("a" * MAX_QUERY_LENGTH)

    def test_rejects_control_characters(self) -> None:
        # Control characters in a query would be the classic route to argument
        # injection once the value reaches an external program.
        for value in ("a\nb", "a\x00b", "a\rb", "a\x1bb"):
            with pytest.raises(ValidationError):
                validate_query(value)


class TestSearchType:
    def test_documented_types_exist(self) -> None:
        assert {t.value for t in SearchType} == {
            "songs",
            "videos",
            "artists",
            "albums",
            "playlists",
        }

    def test_constructs_from_value(self) -> None:
        assert SearchType("songs") is SearchType.SONGS
        assert SearchType("videos") is SearchType.VIDEOS


class TestDisplayName:
    """display_name is what MPD and ncmpcpp show for a stream entry."""

    def test_artist_not_duplicated_when_title_leads_with_it(self) -> None:
        track = Track(id=VALID_ID, title="Artist One - First Song", artist="Artist One")
        assert track.display_name == "Artist One - First Song"

    def test_artist_prefixed_when_absent_from_title(self) -> None:
        track = Track(id=VALID_ID, title="First Song", artist="Artist One")
        assert track.display_name == "Artist One - First Song"

    def test_case_insensitive_match(self) -> None:
        track = Track(id=VALID_ID, title="ARTIST ONE - First Song", artist="Artist One")
        assert track.display_name == "ARTIST ONE - First Song"

    def test_leading_segment_separators(self) -> None:
        track = Track(id=VALID_ID, title="Artist One | First Song (Remix)", artist="Artist One")
        assert track.display_name == "Artist One | First Song (Remix)"

    def test_falls_back_to_title_without_artist(self) -> None:
        assert Track(id=VALID_ID, title="First Song").display_name == "First Song"

    def test_placeholder_artist_never_leaks_into_name(self) -> None:
        assert "Unknown Artist" not in Track(id=VALID_ID, title="First Song").display_name

    def test_uses_uploader_when_artist_missing(self) -> None:
        track = Track(id=VALID_ID, title="Live Set", uploader="Some Band")
        assert track.display_name == "Some Band - Live Set"

    def test_uses_channel_when_artist_and_uploader_missing(self) -> None:
        track = Track(id=VALID_ID, title="Live Set", channel="Some Channel")
        assert track.display_name == "Some Channel - Live Set"

    def test_is_idempotent(self) -> None:
        track = Track(id=VALID_ID, title="Artist One - First Song", artist="Artist One")
        # Applying the rule again must not stack another prefix.
        assert track.display_name == "Artist One - First Song"

    def test_display_artist_fallbacks(self) -> None:
        assert Track(id=VALID_ID, title="T").display_artist == "Unknown Artist"
        assert (
            Track(id=VALID_ID, title="T", artist="A", uploader="U", channel="C").display_artist
            == "A"
        )

    def test_display_album_placeholder(self) -> None:
        assert Track(id=VALID_ID, title="T").display_album == "Unknown Album"
        assert Track(id=VALID_ID, title="T", album="Album").display_album == "Album"


class TestSerialisation:
    def test_track_roundtrip(self) -> None:
        original = Track(
            id=VALID_ID,
            title="First Song",
            artist="Artist One",
            album="Some Album",
            duration=249,
            webpage_url=f"https://www.youtube.com/watch?v={VALID_ID}",
            thumbnail="https://i.ytimg.com/vi/x/hq.jpg",
            channel="Channel",
            uploader="Uploader",
            release_year=2013,
        )
        # to_dict() uses the wire key "url"; the cache decoder maps it back.
        from streambridge.cache import track_from_dict

        assert track_from_dict(original.to_dict()) == original

    def test_to_dict_has_no_internal_fields(self) -> None:
        payload = Track(id=VALID_ID, title="T").to_dict()
        assert set(payload) == {
            "id",
            "title",
            "artist",
            "album",
            "duration",
            "url",
            "thumbnail",
            "channel",
            "uploader",
            "release_year",
            "source",
        }

    def test_search_result_to_dict_counts(self) -> None:
        from streambridge.models import SearchResult

        result = SearchResult(query="q", tracks=(Track(id=VALID_ID, title="T"),))
        payload = result.to_dict()
        assert payload["count"] == 1
        assert payload["query"] == "q"
        assert payload["type"] == "songs"

    def test_playlist_to_dict(self) -> None:
        from streambridge.models import Playlist

        playlist = Playlist(id="PLabc", title="Mix", tracks=(Track(id=VALID_ID, title="T"),))
        payload = playlist.to_dict()
        assert payload["count"] == 1
        assert payload["title"] == "Mix"


class TestStreamInfoExpiry:
    def test_no_expiry_never_considered_expired(self) -> None:
        info = StreamInfo(
            video_id=VALID_ID, url="https://x.googlevideo.com/a", mime_type="audio/mp4"
        )
        # An unknown expiry must not be guessed: the resolver re-resolves on
        # failure instead of trusting a fabricated timestamp.
        assert info.is_probably_expired(1e12) is False

    def test_past_expiry_is_expired(self) -> None:
        info = StreamInfo(
            video_id=VALID_ID,
            url="https://x.googlevideo.com/a",
            mime_type="audio/mp4",
            expires_at=1000.0,
        )
        assert info.is_probably_expired(1000.0) is True

    def test_expiry_uses_a_margin(self) -> None:
        info = StreamInfo(
            video_id=VALID_ID,
            url="https://x.googlevideo.com/a",
            mime_type="audio/mp4",
            expires_at=1000.0,
        )
        # 20s left is inside the 30s margin, so it counts as expiring soon.
        assert info.is_probably_expired(980.0) is True
        assert info.is_probably_expired(500.0) is False
