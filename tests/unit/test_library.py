"""Local library: favourites and play history.

Everything here is offline - no yt-dlp, no MPD, no network.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from streambridge.errors import ValidationError
from streambridge.library import (
    MAX_FAVORITES,
    MAX_HISTORY,
    LibraryStore,
    track_from_stored,
    track_to_stored,
)
from streambridge.models import Track


def make_track(video_id: str = "5NV6Rdv1a3I", **overrides: object) -> Track:
    fields: dict[str, object] = {
        "id": video_id,
        "title": "Daft Punk - Get Lucky",
        "artist": "Daft Punk",
        "album": "Random Access Memories",
        "duration": 249,
        "webpage_url": f"https://www.youtube.com/watch?v={video_id}",
        "thumbnail": "https://i.ytimg.com/vi/x/hqdefault.jpg",
    }
    fields.update(overrides)
    return Track(**fields)  # type: ignore[arg-type]


@pytest.fixture
def store(tmp_path: Path) -> LibraryStore:
    return LibraryStore(tmp_path / "state")


class TestRoundTrip:
    def test_stored_track_survives_a_write(self) -> None:
        original = make_track()
        restored = track_from_stored(json.loads(json.dumps(track_to_stored(original))))
        assert restored == original

    def test_missing_fields_get_safe_defaults(self) -> None:
        track = track_from_stored({"id": "5NV6Rdv1a3I"})
        assert track is not None
        assert track.title == "5NV6Rdv1a3I"
        assert track.artist is None
        assert track.webpage_url.endswith("v=5NV6Rdv1a3I")

    @pytest.mark.parametrize("payload", [None, {}, {"id": ""}, {"id": "../etc"}, 42, []])
    def test_unusable_payloads_are_rejected(self, payload: object) -> None:
        assert track_from_stored(payload) is None

    def test_foreign_url_is_replaced(self) -> None:
        track = track_from_stored({"id": "5NV6Rdv1a3I", "url": "https://evil.example/x"})
        assert track is not None
        assert "youtube.com" in track.webpage_url

    def test_control_characters_are_stripped(self) -> None:
        stored = track_to_stored(make_track(title="a\nb\tc"))
        assert "\n" not in stored["title"]
        assert "\t" not in stored["title"]


class TestFavourites:
    def test_add_and_list(self, store: LibraryStore) -> None:
        assert store.add_favorite(make_track()) is True
        items = store.favorites()
        assert len(items) == 1
        assert items[0]["track"]["id"] == "5NV6Rdv1a3I"
        assert items[0]["added_at"] > 1_600_000_000

    def test_add_twice_is_reported(self, store: LibraryStore) -> None:
        store.add_favorite(make_track())
        assert store.add_favorite(make_track()) is False
        assert len(store.favorites()) == 1

    def test_toggle_round_trip(self, store: LibraryStore) -> None:
        assert store.toggle_favorite(make_track()) is True
        assert store.is_favorite("5NV6Rdv1a3I")
        assert store.toggle_favorite(make_track()) is False
        assert not store.is_favorite("5NV6Rdv1a3I")

    def test_remove(self, store: LibraryStore) -> None:
        store.add_favorite(make_track())
        assert store.remove_favorite("5NV6Rdv1a3I") is True
        assert store.remove_favorite("5NV6Rdv1a3I") is False
        assert store.favorites() == []

    def test_newest_first(self, store: LibraryStore) -> None:
        store.add_favorite(make_track("CCHdMIEGaaM"))
        store.add_favorite(make_track("5NV6Rdv1a3I"))
        assert [i["track"]["id"] for i in store.favorites()] == ["5NV6Rdv1a3I", "CCHdMIEGaaM"]

    def test_persists_across_instances(self, tmp_path: Path) -> None:
        directory = tmp_path / "state"
        LibraryStore(directory).add_favorite(make_track())
        assert len(LibraryStore(directory).favorites()) == 1

    def test_invalid_id_never_touches_disk(self, store: LibraryStore) -> None:
        with pytest.raises(ValidationError):
            store.add_favorite(make_track("short"))
        assert store.favorites() == []

    def test_capacity_is_bounded(self, tmp_path: Path) -> None:
        store = LibraryStore(tmp_path / "state", max_favorites=3)
        for index in range(6):
            store.add_favorite(make_track(f"vid{index:08d}"))
        assert len(store.favorites()) == 3

    def test_clear_reports_count(self, store: LibraryStore) -> None:
        store.add_favorite(make_track())
        store.add_favorite(make_track("CCHdMIEGaaM"))
        assert store.clear_favorites() == 2
        assert store.favorites() == []


class TestHistory:
    def test_newest_first_and_deduplicated(self, store: LibraryStore) -> None:
        store.record_play(make_track("CCHdMIEGaaM"))
        store.record_play(make_track("5NV6Rdv1a3I"))
        store.record_play(make_track("CCHdMIEGaaM"))
        ids = [i["track"]["id"] for i in store.history()]
        assert ids == ["CCHdMIEGaaM", "5NV6Rdv1a3I"]
        assert store.history()[0]["played_at"] > 0

    def test_capacity_is_bounded(self, tmp_path: Path) -> None:
        store = LibraryStore(tmp_path / "state", max_history=2)
        for index in range(5):
            store.record_play(make_track(f"vid{index:08d}"))
        assert len(store.history()) == 2

    def test_invalid_track_is_ignored(self, store: LibraryStore) -> None:
        store.record_play(make_track("nope"))
        assert store.history() == []

    def test_clear(self, store: LibraryStore) -> None:
        store.record_play(make_track())
        assert store.clear_history() == 1
        assert store.history() == []


class TestRobustness:
    def test_corrupt_file_degrades_to_empty(self, tmp_path: Path) -> None:
        directory = tmp_path / "state"
        store = LibraryStore(directory)
        store.add_favorite(make_track())
        (directory / "favorites.json").write_text("{not json", encoding="utf-8")
        assert store.favorites() == []

    def test_wrong_shape_degrades_to_empty(self, tmp_path: Path) -> None:
        directory = tmp_path / "state"
        store = LibraryStore(directory)
        store.add_favorite(make_track())
        (directory / "favorites.json").write_text('["a", 1, null]', encoding="utf-8")
        assert store.favorites() == []

    def test_oversized_file_is_ignored(self, tmp_path: Path) -> None:
        directory = tmp_path / "state"
        directory.mkdir(parents=True)
        (directory / "favorites.json").write_text("x" * (3 * 1024 * 1024), encoding="utf-8")
        assert LibraryStore(directory).favorites() == []

    def test_unwritable_directory_does_not_raise(self, tmp_path: Path) -> None:
        if os.geteuid() == 0:  # pragma: no cover - root ignores the mode
            pytest.skip("root umgeht Dateirechte")
        blocked = tmp_path / "blocked"
        blocked.mkdir()
        blocked.chmod(0o500)
        try:
            store = LibraryStore(blocked / "state")
            store.add_favorite(make_track())  # must not raise
            assert store.favorites() == []
        finally:
            blocked.chmod(0o700)

    def test_entries_with_bad_ids_are_skipped(self, tmp_path: Path) -> None:
        directory = tmp_path / "state"
        directory.mkdir(parents=True)
        (directory / "favorites.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "items": [
                        {"id": "../../etc/passwd", "title": "x"},
                        {"id": "5NV6Rdv1a3I", "title": "gut", "added_at": 1700000000},
                        {"id": "CCHdMIEGaaM", "title": "auch gut", "added_at": "nonsense"},
                    ],
                }
            ),
            encoding="utf-8",
        )
        items = LibraryStore(directory).favorites()
        assert [i["track"]["id"] for i in items] == ["5NV6Rdv1a3I", "CCHdMIEGaaM"]
        assert items[0]["added_at"] == 1700000000
        assert items[1]["added_at"] == 0

    def test_plaintext_file_contents_are_metadata_only(self, store: LibraryStore) -> None:
        store.add_favorite(make_track())
        raw = store.favorites_path.read_text(encoding="utf-8")
        assert "googlevideo" not in raw
        assert "5NV6Rdv1a3I" in raw
        # No cookie, token or credential material is ever written.
        for needle in ("cookie", "token", "password", "authorization"):
            assert needle not in raw.lower()

    def test_timestamps_are_current(self, store: LibraryStore) -> None:
        before = int(time.time())
        store.record_play(make_track())
        assert store.history()[0]["played_at"] >= before


def test_limits_are_sane() -> None:
    assert 0 < MAX_FAVORITES <= 5000
    assert 0 < MAX_HISTORY <= 5000
