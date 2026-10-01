"""Shared pytest fixtures and fake external processes.

No test in this suite touches the network or a real MPD. Anything external is
replaced by a fake, so the whole suite is deterministic and offline.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from streambridge.config import Config, config_from_mapping
from streambridge.errors import MpdError
from streambridge.models import StreamInfo
from streambridge.mpd import MpdStatus, QueueEntry
from streambridge.proc import CompletedRun

# A syntactically valid id used throughout the fixtures.
SAMPLE_ID = "5NV6Rdv1a3I"
SAMPLE_ID_2 = "CCHdMIEGaaM"

# An executable name that resolves on any POSIX system.
#
# Tests pass a FakeRunner, so no real subprocess runs - but ExtractorClient
# resolves the executable path before it hands anything to the runner. A test
# that asked for "yt-dlp" therefore passed only where yt-dlp was installed,
# and failed on a CI runner that has none. Using a name that always exists
# removes the host from the result; the tests that care about a genuinely
# missing extractor pass their own unresolvable name.
FAKE_EXECUTABLE = "sh"


class FakeRunner:
    """Scripted stand-in for :class:`streambridge.proc.SubprocessRunner`.

    Rules match on substrings of the joined argv, so a test can express intent
    ("a search was requested") without hardcoding a full command line.
    """

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self._rules: list[tuple[str, CompletedRun]] = []
        self.default = CompletedRun((), 0, "{}", "")

    def add(self, match: str, result: CompletedRun) -> None:
        self._rules.append((match, result))

    def add_first(self, match: str, result: CompletedRun) -> None:
        """Register a rule that takes precedence over existing ones."""
        self._rules.insert(0, (match, result))

    def clear_rules(self) -> None:
        self._rules.clear()

    def add_json(self, match: str, payload: Any, returncode: int = 0) -> None:
        self.add(
            match,
            CompletedRun(
                (),
                returncode,
                json.dumps(payload),
                "" if returncode == 0 else "error",
            ),
        )

    def add_text(self, match: str, text: str, returncode: int = 0, stderr: str = "") -> None:
        self.add(match, CompletedRun((), returncode, text, stderr))

    def run(
        self,
        args: Sequence[str],
        *,
        timeout: float,
        stdin_data: str | None = None,
        env: Any = None,
    ) -> CompletedRun:
        self.calls.append([str(a) for a in args])
        joined = " ".join(str(a) for a in args)
        for match, result in self._rules:
            if match in joined:
                return result
        return self.default

    # -- assertions helpers --------------------------------------------
    def calls_matching(self, fragment: str) -> list[list[str]]:
        return [call for call in self.calls if fragment in " ".join(call)]

    def count(self, fragment: str) -> int:
        return len(self.calls_matching(fragment))


@pytest.fixture
def config(tmp_path: Path) -> Config:
    """A config with short timeouts and temporary cache and state directories.

    Both directories must be redirected. The library defaults to
    ``$XDG_STATE_HOME/streambridge``, so without this every test that touches
    favourites or history would read and write the developer's real state, and
    would see the files its neighbours left behind.
    """
    return config_from_mapping(
        {
            "server": {"port": 18787},
            "cache": {"directory": str(tmp_path / "cache")},
            "library": {"directory": str(tmp_path / "state")},
            "timeouts": {"request": 5.0, "resolve": 5.0, "info": 5.0},
        }
    )


@pytest.fixture
def fake_runner() -> FakeRunner:
    return FakeRunner()


@pytest.fixture
def runner(fake_runner: FakeRunner) -> FakeRunner:
    """Alias for tests that inject the fake directly into a constructor."""
    return fake_runner


def make_stream_info(video_id: str = SAMPLE_ID) -> StreamInfo:
    """A StreamInfo pointing at an allowed media host with a far-future expiry."""
    return StreamInfo(
        video_id=video_id,
        url=(
            "https://rr3---sn-x.googlevideo.com/videoplayback?expire=9999999999"
            "&id=abc&itag=140&source=youtube"
        ),
        mime_type='audio/mp4; codecs="mp4a.40.2"',
        content_length=4_000_000,
        expires_at=9999999999.0,
        headers={"User-Agent": "Mozilla/5.0", "Accept": "*/*"},
        ext="m4a",
        acodec="mp4a.40.2",
        format_id="140",
    )


SEARCH_PAYLOAD: dict[str, Any] = {
    "id": None,
    "title": "ytsearch3 result",
    "entries": [
        {
            "id": SAMPLE_ID,
            "title": "Artist One - First Song (Official Audio)",
            "duration": 249.0,
            "uploader": "Artist One",
            "channel": "Artist One",
            "webpage_url": f"https://www.youtube.com/watch?v={SAMPLE_ID}",
            "thumbnails": [
                {
                    "url": f"https://i.ytimg.com/vi/{SAMPLE_ID}/hq.jpg",
                    "width": 480,
                    "preference": 1,
                }
            ],
            "release_date": "20131021",
        },
        {
            "id": SAMPLE_ID_2,
            "title": "Artist One - First Song (Official Video)",
            "duration": 248,
            "artist": "Artist One",
            "album": "Some Album",
            "uploader": "Artist One",
            "webpage_url": f"https://www.youtube.com/watch?v={SAMPLE_ID_2}",
        },
        {
            "id": "too-short-id",
            "title": "invalid id entry",
        },
    ],
}

INFO_PAYLOAD: dict[str, Any] = {
    "id": SAMPLE_ID,
    "title": "Artist One - First Song (Official Audio)",
    "duration": 249,
    "artist": "Artist One",
    "album": "Some Album",
    "uploader": "Artist One",
    "channel": "Artist One",
    "webpage_url": f"https://www.youtube.com/watch?v={SAMPLE_ID}",
    "thumbnail": f"https://i.ytimg.com/vi/{SAMPLE_ID}/maxres.jpg",
    "release_date": "20131021",
    "formats": [
        {
            "format_id": "251",
            "ext": "webm",
            "acodec": "opus",
            "vcodec": "none",
            "abr": 133.69,
            "filesize": 4_155_462,
            "url": "https://rr3---sn-x.googlevideo.com/videoplayback?expire=9999999999&itag=251",
            "mime_type": 'audio/webm; codecs="opus"',
            "http_headers": {"User-Agent": "Mozilla/5.0", "Accept": "*/*"},
        },
        {
            "format_id": "140",
            "ext": "m4a",
            "acodec": "mp4a.40.2",
            "vcodec": "none",
            "abr": 129.496,
            "filesize": 4_025_466,
            "url": "https://rr3---sn-x.googlevideo.com/videoplayback?expire=9999999999&itag=140",
            "mime_type": 'audio/mp4; codecs="mp4a.40.2"',
            "http_headers": {"User-Agent": "Mozilla/5.0", "Accept": "*/*"},
        },
        {
            "format_id": "137",
            "ext": "mp4",
            "acodec": "none",
            "vcodec": "avc1.640028",
            "url": "https://rr3---sn-x.googlevideo.com/videoplayback?itag=137",
        },
    ],
}


class FakeMpd:
    """In-memory stand-in for :class:`streambridge.mpd.MpdClient`.

    Implements exactly the surface :class:`streambridge.player.PlayerService` uses,
    with MPD's 1-based positions and its wrap/stop-at-the-end behaviour, so
    player logic can be tested deterministically and offline.
    """

    def __init__(self, config: Config | None = None) -> None:
        self.config = config or Config()
        self.items: list[QueueEntry] = []
        self.state = "stopped"
        self.position: int | None = None
        self.elapsed = 0
        self.level = 80
        # Prefixed so the flags do not shadow the command methods of the same
        # name - mpc has both a "repeat" flag and a "repeat on" command.
        self.flags = {"repeat": False, "random": False, "single": False, "consume": False}
        self._next_song_id = 1
        self.calls: list[tuple] = []

    # -- helpers -------------------------------------------------------
    def _record(self, *args: object) -> None:
        self.calls.append(args)

    def _song(self, index: int) -> QueueEntry:
        return self.items[index]

    def _current_entry(self) -> QueueEntry | None:
        if self.position is None or not 1 <= self.position <= len(self.items):
            return None
        return self.items[self.position - 1]

    def _require_playing(self) -> QueueEntry:
        entry = self._current_entry()
        if entry is None:
            raise MpdError("MPD: no current song")
        return entry

    # -- queries -------------------------------------------------------
    def ping(self) -> bool:
        return True

    def status(self) -> MpdStatus:
        entry = self._current_entry()
        total = entry.duration if entry else None
        return MpdStatus(
            raw="fake",
            playing=self.state == "playing",
            paused=self.state == "paused",
            stopped=self.state == "stopped",
            track=f"#{self.position}" if self.position else None,
            elapsed=self._fmt(self.elapsed),
            total=self._fmt(total) if total else None,
            repeat=self.flags["repeat"],
            random=self.flags["random"],
            volume=f"{self.level}%",
            single=self.flags["single"],
            consume=self.flags["consume"],
            song_id=entry.song_id if entry else None,
            length=len(self.items),
        )

    @staticmethod
    def _fmt(seconds: int) -> str:
        return f"{seconds // 60}:{seconds % 60:02d}"

    def queue(self) -> list[QueueEntry]:
        return list(self.items)

    # -- commands ------------------------------------------------------
    def add_tracks(self, tracks, *, play: bool = False) -> str:
        self._record("add_tracks", [t.id for t in tracks], play)
        for track in tracks:
            self.items.append(
                QueueEntry(
                    position=len(self.items) + 1,
                    song_id=self._next_song_id,
                    file=self.config.stream_url(track.id),
                    name=track.display_name,
                    duration=track.duration,
                )
            )
            self._next_song_id += 1
        if play and not self.position:
            self.position = 1
            self.state = "playing"
        return ""

    def play(self) -> str:
        self._record("play")
        if not self.items:
            raise MpdError("MPD: empty")
        if self.position is None:
            self.position = 1
        self.state = "playing"
        return ""

    def play_id(self, song: int | str) -> str:
        self._record("play_id", song)
        index = int(song)
        if not 1 <= index <= len(self.items):
            raise MpdError(f"MPD: song number does not exist: {index}")
        self.position = index
        self.elapsed = 0
        self.state = "playing"
        return ""

    def pause(self) -> str:
        self._record("pause")
        if self.state == "playing":
            self.state = "paused"
        elif self.state == "paused":
            self.state = "playing"
        return ""

    def stop(self) -> str:
        self._record("stop")
        self.state = "stopped"
        self.position = None
        self.elapsed = 0
        return ""

    def next(self) -> str:
        self._record("next")
        self._require_playing()
        if self.position < len(self.items):
            self.position += 1
        elif self.flags["repeat"] or self.flags["single"]:
            self.position = 1
        else:
            return self.stop()
        self.elapsed = 0
        self.state = "playing"
        return ""

    def previous(self) -> str:
        self._record("previous")
        self._require_playing()
        if self.position > 1:
            self.position -= 1
        elif self.flags["repeat"] or self.flags["single"]:
            self.position = len(self.items)
        else:
            return self.stop()
        self.elapsed = 0
        self.state = "playing"
        return ""

    def delete(self, song: int | str) -> str:
        self._record("delete", song)
        index = int(song)
        if not 1 <= index <= len(self.items):
            raise MpdError(f"MPD: song number does not exist: {index}")
        self.items.pop(index - 1)
        self._renumber()
        if self.position is not None:
            if self.position > len(self.items):
                self.position = len(self.items) or None
            elif self.position > index:
                self.position -= 1
        return ""

    def move(self, source: int, target: int) -> str:
        self._record("move", source, target)
        if not (1 <= source <= len(self.items) and 1 <= target <= len(self.items)):
            raise MpdError("MPD: song number does not exist")
        entry = self.items.pop(source - 1)
        self.items.insert(target - 1, entry)
        if self.position is not None:
            if self.position == source:
                self.position = target
            elif source < self.position <= target:
                self.position -= 1
            elif target <= self.position < source:
                self.position += 1
        self._renumber()
        return ""

    def clear(self) -> str:
        self._record("clear")
        self.items.clear()
        self.state = "stopped"
        self.position = None
        self.elapsed = 0
        return ""

    def seek(self, seconds: float) -> str:
        self._record("seek", seconds)
        entry = self._require_playing()
        self.elapsed = max(0, int(seconds))
        if entry.duration:
            self.elapsed = min(self.elapsed, entry.duration)
        return ""

    def volume(self, percent: int) -> str:
        self._record("volume", percent)
        self.level = max(0, min(100, int(percent)))
        return ""

    def repeat(self, on: bool) -> str:
        self._record("repeat", on)
        self.flags["repeat"] = on
        return ""

    def random(self, on: bool) -> str:
        self._record("random", on)
        self.flags["random"] = on
        return ""

    def single(self, on: bool) -> str:
        self._record("single", on)
        self.flags["single"] = on
        return ""

    def consume(self, on: bool) -> str:
        self._record("consume", on)
        self.flags["consume"] = on
        return ""

    def _renumber(self) -> None:
        for index, entry in enumerate(self.items, start=1):
            self.items[index - 1] = QueueEntry(
                position=index,
                song_id=entry.song_id,
                file=entry.file,
                name=entry.name,
                duration=entry.duration,
            )
