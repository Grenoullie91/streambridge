"""PlayerService: the single place that turns MPD commands into JSON.

Driven entirely by the in-memory :class:`FakeMpd`, so the tests are fast,
deterministic and never touch the MPD the developer is listening to.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from conftest import FakeMpd
from streambridge.config import Config, config_from_mapping
from streambridge.errors import StreamBridgeError, ValidationError
from streambridge.library import LibraryStore
from streambridge.models import Track
from streambridge.player import PlayerService, video_id_from_stream_url


def make_track(video_id: str = "5NV6Rdv1a3I", **overrides: object) -> Track:
    fields: dict[str, object] = {
        "id": video_id,
        "title": "Get Lucky",
        "artist": "Daft Punk",
        "duration": 249,
    }
    fields.update(overrides)
    return Track(**fields)  # type: ignore[arg-type]


@pytest.fixture
def player(tmp_path: Path) -> PlayerService:
    config = config_from_mapping(
        {
            "cache": {"directory": str(tmp_path / "cache")},
            "library": {"directory": str(tmp_path / "state")},
        }
    )
    mpd = FakeMpd(config)
    library = LibraryStore(tmp_path / "state")
    return PlayerService(config, mpd=mpd, library=library)


class TestStreamUrlParsing:
    @pytest.mark.parametrize(
        "url",
        [
            "http://127.0.0.1:8787/stream/5NV6Rdv1a3I",
            "http://localhost:1234/stream/5NV6Rdv1a3I",
        ],
    )
    def test_accepts_local_stream_urls(self, url: str) -> None:
        assert video_id_from_stream_url(url) == "5NV6Rdv1a3I"

    @pytest.mark.parametrize(
        "url",
        [
            "",
            "/stream/short",
            "http://evil.example/stream/5NV6Rdv1a3I/../../etc",
            "http://127.0.0.1:8787/stream/5NV6Rdv1a3I?x=1",
            "http://127.0.0.1:8787/stream/../../etc/passwd",
            "file:///etc/passwd",
            "not a url at all",
        ],
    )
    def test_rejects_everything_else(self, url: str) -> None:
        assert video_id_from_stream_url(url) is None


class TestAdd:
    def test_appends_with_metadata(self, player: PlayerService) -> None:
        result = player.add(["5NV6Rdv1a3I"], tracks=[make_track()])
        assert result["added"] == 1
        rows = result["queue"]["items"]
        assert rows[0]["title"] == "Daft Punk - Get Lucky"
        assert rows[0]["video_id"] == "5NV6Rdv1a3I"
        assert rows[0]["duration"] == 249

    def test_play_inserts_at_current_position(self, player: PlayerService) -> None:
        player.add(["5NV6Rdv1a3I", "CCHdMIEGaaM"], tracks=[make_track(), make_track("CCHdMIEGaaM")])
        player.play_id = player.mpd.play_id  # type: ignore[attr-defined]
        player.mpd.play_id(1)
        result = player.add(["a5uQMwRMHcs"], tracks=[make_track("a5uQMwRMHcs")], position=1)
        ids = [row["video_id"] for row in result["queue"]["items"]]
        assert ids == ["a5uQMwRMHcs", "5NV6Rdv1a3I", "CCHdMIEGaaM"]

    def test_play_inserts_a_block_keeping_order(self, player: PlayerService) -> None:
        player.add(["5NV6Rdv1a3I", "CCHdMIEGaaM"], tracks=[make_track(), make_track("CCHdMIEGaaM")])
        result = player.add(
            ["FGBhQbmPwH8", "vJZp6awlL58"],
            tracks=[make_track("FGBhQbmPwH8"), make_track("vJZp6awlL58")],
            position=2,
        )
        ids = [row["video_id"] for row in result["queue"]["items"]]
        assert ids == ["5NV6Rdv1a3I", "FGBhQbmPwH8", "vJZp6awlL58", "CCHdMIEGaaM"]

    def test_play_flag_starts_playback(self, player: PlayerService) -> None:
        player.add(["5NV6Rdv1a3I"], tracks=[make_track()], play=True)
        assert player.mpd.state == "playing"
        assert player.mpd.position == 1

    def test_unknown_id_is_looked_up_not_invented(self, player: PlayerService) -> None:
        # No metadata supplied and no API to resolve it: the failure must be
        # explicit rather than queueing a fabricated entry.
        with pytest.raises(Exception):
            player.add(["5NV6Rdv1a3I"])

    @pytest.mark.parametrize("video_id", ["", "short", "../../etc/passwd", "x" * 40])
    def test_rejects_bad_ids(self, player: PlayerService, video_id: str) -> None:
        with pytest.raises(ValidationError):
            player.add([video_id])

    def test_rejects_empty_and_oversized_batches(self, player: PlayerService) -> None:
        with pytest.raises(ValidationError):
            player.add([])
        with pytest.raises(ValidationError):
            player.add(["5NV6Rdv1a3I"] * 201)

    def test_generation_increments(self, player: PlayerService) -> None:
        before = player.generation
        player.add(["5NV6Rdv1a3I"], tracks=[make_track()])
        assert player.generation > before


class TestQueueMutations:
    @pytest.fixture(autouse=True)
    def _seed(self, player: PlayerService) -> None:
        player.add(
            ["5NV6Rdv1a3I", "CCHdMIEGaaM", "a5uQMwRMHcs"],
            tracks=[make_track(), make_track("CCHdMIEGaaM"), make_track("a5uQMwRMHcs")],
        )

    def test_remove(self, player: PlayerService) -> None:
        result = player.remove(2)
        assert [r["video_id"] for r in result["queue"]["items"]] == [
            "5NV6Rdv1a3I",
            "a5uQMwRMHcs",
        ]

    @pytest.mark.parametrize("position", [0, -1, 4, 99])
    def test_remove_rejects_out_of_range(self, player: PlayerService, position: int) -> None:
        with pytest.raises(ValidationError):
            player.remove(position)

    def test_move_down(self, player: PlayerService) -> None:
        result = player.move(1, 2)
        assert [r["video_id"] for r in result["queue"]["items"]] == [
            "CCHdMIEGaaM",
            "5NV6Rdv1a3I",
            "a5uQMwRMHcs",
        ]

    def test_move_to_same_position_is_a_no_op(self, player: PlayerService) -> None:
        before = player.generation
        result = player.move(2, 2)
        assert [r["video_id"] for r in result["queue"]["items"]][1] == "CCHdMIEGaaM"
        assert player.generation == before

    def test_move_rejects_out_of_range(self, player: PlayerService) -> None:
        with pytest.raises(ValidationError):
            player.move(1, 9)

    def test_clear(self, player: PlayerService) -> None:
        assert player.clear()["length"] == 0
        assert player.mpd.items == []


class TestTransport:
    @pytest.fixture(autouse=True)
    def _seed(self, player: PlayerService) -> None:
        player.add(
            ["5NV6Rdv1a3I", "CCHdMIEGaaM"],
            tracks=[make_track(), make_track("CCHdMIEGaaM")],
            play=True,
        )

    def test_play_without_position(self, player: PlayerService) -> None:
        status = player.play()
        assert status["state"] == "playing"

    def test_play_specific_position(self, player: PlayerService) -> None:
        status = player.play(2)
        assert status["position"] == 2

    def test_play_out_of_range(self, player: PlayerService) -> None:
        with pytest.raises(ValidationError):
            player.play(5)

    def test_play_on_empty_queue(self, player: PlayerService) -> None:
        player.clear()
        with pytest.raises(ValidationError):
            player.play()

    def test_pause_is_idempotent(self, player: PlayerService) -> None:
        assert player.pause()["state"] == "paused"
        # A second pause must not resume, and must not raise.
        assert player.pause()["state"] == "paused"

    def test_pause_when_stopped(self, player: PlayerService) -> None:
        player.stop()
        with pytest.raises(ValidationError):
            player.pause()

    def test_next_and_previous(self, player: PlayerService) -> None:
        player.play(1)
        assert player.next()["position"] == 2
        assert player.previous()["position"] == 1

    def test_next_at_the_end_stops(self, player: PlayerService) -> None:
        player.play(2)
        assert player.next()["state"] == "stopped"

    def test_next_when_stopped_is_refused(self, player: PlayerService) -> None:
        player.stop()
        with pytest.raises(ValidationError):
            player.next()
        with pytest.raises(ValidationError):
            player.previous()

    def test_seek(self, player: PlayerService) -> None:
        assert player.seek(90)["elapsed"] == 90

    @pytest.mark.parametrize("seconds", [-1, 999999, "abc", True, None])
    def test_seek_rejects_bad_input(self, player: PlayerService, seconds: Any) -> None:
        with pytest.raises(ValidationError):
            player.seek(seconds)

    def test_seek_when_stopped(self, player: PlayerService) -> None:
        player.stop()
        with pytest.raises(ValidationError):
            player.seek(10)

    def test_seek_is_clamped_to_the_track(self, player: PlayerService) -> None:
        assert player.seek(9999)["elapsed"] == 249


class TestVolumeAndModes:
    def test_volume(self, player: PlayerService) -> None:
        assert player.volume(42)["volume"] == 42

    @pytest.mark.parametrize("value", [-1, 101, "loud", True, None])
    def test_volume_rejects_bad_input(self, player: PlayerService, value: Any) -> None:
        with pytest.raises(ValidationError):
            player.volume(value)

    def test_mute_and_unmute_restore_the_level(self, player: PlayerService) -> None:
        player.volume(70)
        assert player.mute(True)["muted"] is True
        assert player.mute(True)["volume"] == 0
        assert player.mute(False)["volume"] == 70

    def test_mute_requires_a_bool(self, player: PlayerService) -> None:
        with pytest.raises(ValidationError):
            player.mute("yes")  # type: ignore[arg-type]

    def test_modes(self, player: PlayerService) -> None:
        status = player.set_modes(shuffle=True, repeat=True, repeat_one=False)
        assert status["shuffle"] is True
        assert status["repeat"] is True
        status = player.set_modes(shuffle=False, repeat=False, repeat_one=True)
        assert status["repeat"] is False
        assert status["repeat_one"] is True

    def test_modes_reject_non_bool(self, player: PlayerService) -> None:
        with pytest.raises(ValidationError):
            player.set_modes(shuffle="on")  # type: ignore[arg-type]

    def test_partial_update_leaves_other_modes(self, player: PlayerService) -> None:
        player.set_modes(repeat=True)
        status = player.set_modes(shuffle=True)
        assert status["repeat"] is True
        assert status["shuffle"] is True


class TestStatusShape:
    def test_stopped_status(self, player: PlayerService) -> None:
        status = player.status()
        assert status["state"] == "stopped"
        assert status["current"] is None
        assert status["position"] is None
        assert status["playlist_length"] == 0

    def test_playing_status_describes_the_song(self, player: PlayerService) -> None:
        player.add(["5NV6Rdv1a3I"], tracks=[make_track()], play=True)
        status = player.status()
        assert status["state"] == "playing"
        assert status["current"] is not None
        assert status["current"]["video_id"] == "5NV6Rdv1a3I"
        assert status["duration"] == 249
        assert status["timestamp"] > 0

    def test_status_is_json_serialisable(self, player: PlayerService) -> None:
        import json

        player.add(["5NV6Rdv1a3I"], tracks=[make_track()], play=True)
        json.dumps(player.status())

    def test_history_is_recorded_once_per_song(self, player: PlayerService) -> None:
        player.add(["5NV6Rdv1a3I"], tracks=[make_track()], play=True)
        for _ in range(5):
            player.status()
        assert len(player.library.history()) == 1

    def test_history_grows_when_the_song_changes(self, player: PlayerService) -> None:
        player.add(["5NV6Rdv1a3I", "CCHdMIEGaaM"], tracks=[make_track(), make_track("CCHdMIEGaaM")])
        player.play(1)
        player.status()
        player.next()
        player.status()
        assert len(player.library.history()) == 2

    def test_queue_cache_avoids_repeated_queries(self, player: PlayerService) -> None:
        player.add(["5NV6Rdv1a3I"], tracks=[make_track()])
        before = len(player.mpd.calls)
        for _ in range(5):
            player.queue()
        # A cached queue must not re-run `mpc playlist` for every render.
        assert len(player.mpd.calls) - before == 0


class TestMetadataCache:
    def test_remembered_metadata_survives_a_fresh_queue_read(self, player: PlayerService) -> None:
        player.add(["5NV6Rdv1a3I"], tracks=[make_track()])
        player._invalidate()
        rows = player.queue_rows(fresh=True)
        assert rows[0]["artist"] == "Daft Punk"

    def test_invalid_track_is_not_remembered(self, player: PlayerService) -> None:
        player.remember([make_track("nope")])
        assert player._meta.stats()["entries"] == 0


def test_service_can_be_built_without_an_api() -> None:
    config = Config()
    service = PlayerService(
        config, mpd=FakeMpd(config), library=LibraryStore(config.state_directory)
    )
    # Without an API nothing can be resolved, so the failure must be a typed
    # error the HTTP layer can translate - never a crash.
    with pytest.raises(StreamBridgeError):
        service.add(["5NV6Rdv1a3I"])
