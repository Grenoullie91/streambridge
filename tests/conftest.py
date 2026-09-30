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
from streambridge.models import StreamInfo
from streambridge.proc import CompletedRun

# A syntactically valid id used throughout the fixtures.
SAMPLE_ID = "5NV6Rdv1a3I"
SAMPLE_ID_2 = "CCHdMIEGaaM"


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
    """A config with short timeouts and a temporary cache directory."""
    return config_from_mapping(
        {
            "server": {"port": 18787},
            "cache": {"directory": str(tmp_path / "cache")},
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
