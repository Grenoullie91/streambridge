"""Metadata normalisation: duration formatting and display helpers.

All user-supplied text is untrusted. These helpers guarantee that whatever
comes back is a single line, because the values end up in log records and in
generated M3U files where a stray newline would break the structure.
"""

from __future__ import annotations

import pytest

from streambridge.metadata import (
    UNKNOWN_ALBUM,
    UNKNOWN_DURATION,
    describe,
    format_duration,
    one_line,
    to_extinf_name,
    track_metadata,
)
from streambridge.models import Track

pytestmark = pytest.mark.unit

VALID_ID = "5NV6Rdv1a3I"


class TestFormatDuration:
    @pytest.mark.parametrize(
        ("seconds", "expected"),
        [
            (1, "0:01"),
            (59, "0:59"),
            (60, "1:00"),
            (249, "4:09"),
            (320, "5:20"),
            (3600, "1:00:00"),
            (3723, "1:02:03"),
            (86399, "23:59:59"),
        ],
    )
    def test_formats(self, seconds: int, expected: str) -> None:
        assert format_duration(seconds) == expected

    @pytest.mark.parametrize("value", [None, 0, -1])
    def test_unknown_placeholder(self, value: int | None) -> None:
        assert format_duration(value) == UNKNOWN_DURATION


class TestOneLine:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("plain", "plain"),
            ("a\nb", "a b"),
            ("a\r\nb", "a b"),
            ("a\t\tb", "a b"),
            ("  padded  ", "padded"),
            ("", ""),
        ],
    )
    def test_whitespace_collapsed(self, value: str, expected: str) -> None:
        assert one_line(value) == expected

    def test_never_contains_newline(self) -> None:
        assert "\n" not in one_line("a\n\n\nb")
        assert "\r" not in one_line("a\r\rb")


class TestTrackMetadata:
    def test_all_values_are_non_empty_strings(self) -> None:
        # Downstream consumers must never have to handle None.
        meta = track_metadata(Track(id=VALID_ID, title="First Song"))
        assert all(isinstance(v, str) and v for v in meta.values())
        assert meta["artist"] == "Unknown Artist"
        assert meta["album"] == UNKNOWN_ALBUM
        assert meta["duration"] == UNKNOWN_DURATION

    def test_prefers_real_metadata(self) -> None:
        meta = track_metadata(
            Track(
                id=VALID_ID,
                title="First Song",
                artist="Artist One",
                album="Some Album",
                duration=249,
            )
        )
        assert meta["artist"] == "Artist One"
        assert meta["album"] == "Some Album"
        assert meta["duration"] == "4:09"
        assert meta["display"] == "Artist One - First Song"

    def test_keys_are_stable(self) -> None:
        assert set(track_metadata(Track(id=VALID_ID, title="T"))) == {
            "artist",
            "title",
            "album",
            "duration",
            "display",
        }

    def test_newlines_in_upstream_text_are_collapsed(self) -> None:
        meta = track_metadata(Track(id=VALID_ID, title="Bad\n#EXTINF:1,x", artist="A\r\nB"))
        for value in meta.values():
            assert "\n" not in value
            assert "\r" not in value


class TestDescribe:
    def test_single_line_with_duration(self) -> None:
        track = Track(id=VALID_ID, title="First Song", artist="Artist One", duration=61)
        assert describe(track) == "Artist One - First Song (1:01)"

    def test_never_contains_newline(self) -> None:
        hostile = Track(id=VALID_ID, title="A\nB", artist="C\nD")
        assert "\n" not in describe(hostile)


class TestExtinfName:
    def test_matches_display_name(self) -> None:
        track = Track(id=VALID_ID, title="First Song", artist="Artist One")
        assert to_extinf_name(track) == track.display_name

    def test_single_line(self) -> None:
        track = Track(id=VALID_ID, title="A\nB", artist="Artist One")
        assert "\n" not in to_extinf_name(track)
