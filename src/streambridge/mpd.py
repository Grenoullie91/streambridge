"""MPD client.

Talks to MPD through ``mpc``. Talking to a well-known binary is more portable
than reimplementing the MPD wire protocol, and it keeps every call argument
validated and shell-free.

The MPD *database* is never modified. Online tracks live in the running queue
as HTTP URLs pointing at StreamBridge.
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
import shlex
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .config import Config
from .errors import DependencyError, MpdError, ValidationError
from .metadata import to_extinf_name
from .models import Track, validate_video_id
from .proc import CommandRunner, SubprocessRunner, which

log = logging.getLogger("streambridge.mpd")

# Metadata is injected through a temporary EXTM3U playlist.
# Tabs are stripped as well: :meth:`MpdClient.queue` parses one tab-separated
# ``mpc -f`` line per song, and a tab inside a title would shift every later
# column.
_ILLEGAL_IN_TAG = re.compile(r"[\r\n\t\"]")

DEFAULT_TIMEOUT = 20.0

# Field separator for the machine-readable queue dump. _clean_tag guarantees
# it can never occur inside a value.
_QUEUE_SEP = "\t"
# Layout: position, MPD song id, duration, file/URL, display name.
_QUEUE_FORMAT = _QUEUE_SEP.join(("%position%", "%id%", "%time%", "%file%", "%name%"))

# mpc renders times as M:SS or H:MM:SS.
_DURATION_RE = re.compile(r"^(\d+):([0-5]?\d)(?::([0-5]?\d))?$")


def parse_mpd_time(value: str | None) -> int | None:
    """Parse an mpc ``M:SS`` / ``HH:MM:SS`` string into whole seconds.

    Returns None for anything unparseable, including the placeholders mpc
    prints when MPD does not know a duration.
    """
    if not value:
        return None
    text = value.strip()
    if text.startswith("-"):  # MPD reports -1 for "unknown"
        return None
    match = _DURATION_RE.match(text)
    if not match:
        return None
    first, second, third = match.groups()
    if third is None:
        return int(first) * 60 + int(second)
    return int(first) * 3600 + int(second) * 60 + int(third)


def clean_tag(value: str) -> str:
    """Make a string safe for one EXTINF tag.

    Quotes and newlines are stripped so that user-controlled metadata (an
    upstream title) can never break out of the ``#EXTINF:`` line or inject a
    further directive.
    """
    return _ILLEGAL_IN_TAG.sub(" ", value).strip()[:300]


@dataclass(frozen=True, slots=True)
class MpdStatus:
    """Parsed subset of the player status."""

    raw: str
    playing: bool
    paused: bool
    stopped: bool
    track: str | None = None
    elapsed: str | None = None
    total: str | None = None
    repeat: bool = False
    random: bool = False
    volume: str | None = None
    single: bool = False
    consume: bool = False
    song_id: int | None = None
    length: int | None = None

    @property
    def state(self) -> str:
        """Normalised state word: ``playing``, ``paused`` or ``stopped``."""
        if self.playing:
            return "playing"
        if self.paused:
            return "paused"
        return "stopped"

    @property
    def position(self) -> int | None:
        """1-based queue position of the current song, if any."""
        return int(self.track.lstrip("#")) if self.track and self.track.startswith("#") else None

    @property
    def elapsed_seconds(self) -> int | None:
        return parse_mpd_time(self.elapsed)

    @property
    def total_seconds(self) -> int | None:
        return parse_mpd_time(self.total)

    @property
    def volume_percent(self) -> int | None:
        if not self.volume:
            return None
        try:
            value = int(float(self.volume.strip().rstrip("%").strip()))
        except ValueError:
            return None
        return max(0, min(100, value))

    def to_dict(self) -> dict[str, object]:
        return {
            "raw": self.raw,
            "state": self.state,
            "playing": self.playing,
            "paused": self.paused,
            "stopped": self.stopped,
            "track": self.track,
            "position": self.position,
            "song_id": self.song_id,
            "length": self.length,
            "elapsed": self.elapsed,
            "total": self.total,
            "elapsed_seconds": self.elapsed_seconds,
            "total_seconds": self.total_seconds,
            "repeat": self.repeat,
            "random": self.random,
            "single": self.single,
            "consume": self.consume,
            "volume": self.volume,
            "volume_percent": self.volume_percent,
        }


@dataclass(frozen=True, slots=True)
class QueueEntry:
    """One song in the MPD queue, as reported by ``mpc -f``."""

    position: int
    song_id: int
    file: str
    name: str
    duration: int | None

    def to_dict(self) -> dict[str, object]:
        return {
            "position": self.position,
            "song_id": self.song_id,
            "file": self.file,
            "name": self.name,
            "duration": self.duration,
        }


# mpc renders the state line as:  [playing] #3  0:42/4:09 (17%)
# The state tag is bracketed; the song position follows it as "#N".
_STATE_RE = re.compile(r"\[(playing|paused|stopped)\]")
_SONG_RE = re.compile(r"\[(?:playing|paused|stopped)\]\s*#(\d+)(?:/(\d+))?")
_TIME_RE = re.compile(r"(\d+:\d+(?::\d+)?)\s*/\s*(\d+:\d+(?::\d+)?)")
_VOLUME_RE = re.compile(r"^volume:\s*(\S+)", re.MULTILINE)
_SONGID_RE = re.compile(r"^song:\s*(\d+)", re.MULTILINE)


class MpdClient:
    """Thin, validated wrapper around the ``mpc`` command line tool."""

    def __init__(
        self,
        config: Config,
        *,
        runner: CommandRunner | None = None,
        executable: str | None = None,
    ) -> None:
        self._config = config
        self._runner = runner or SubprocessRunner()
        self._executable = executable or "mpc"

    # -- infrastructure ------------------------------------------------
    def require(self) -> str:
        path = which(self._executable)
        if path is None:
            raise DependencyError(
                "mpc was not found.",
                hint="Install with: apt install mpd-client  |  pacman -S mpc",
            )
        return path

    def _args(self, *args: str) -> list[str]:
        return [
            self.require(),
            "--host",
            self._config.mpd_host,
            "--port",
            str(self._config.mpd_port),
            *args,
        ]

    def _run(self, args: list[str], *, timeout: float = DEFAULT_TIMEOUT) -> str:
        try:
            result = self._runner.run(args, timeout=timeout)
        except FileNotFoundError as exc:  # pragma: no cover - covered by require()
            raise DependencyError("mpc was not found.") from exc
        if not result.ok:
            combined = (result.stderr or result.stdout or "").strip()
            lowered = combined.lower()
            if "connection refused" in lowered or "failed to connect" in lowered:
                raise MpdError(
                    f"MPD not reachable at {self._config.mpd_host}:{self._config.mpd_port}.",
                    hint="Is MPD running?  systemctl --user status mpd",
                )
            if "unknown command" in lowered or "not connected" in lowered:
                raise MpdError(
                    f"MPD rejected the command: {combined}",
                    hint="Older MPD version?  mpc --help",
                )
            raise MpdError(f"mpc error: {combined or result.error_summary()}")
        return result.stdout

    # -- queries -------------------------------------------------------
    def ping(self) -> bool:
        """True when MPD answers. Never raises for a missing binary.

        ``mpc ping`` does not exist in every mpc release, so ``mpc status`` is
        used: it is available in all versions and exits non-zero when the
        server is unreachable.
        """
        try:
            self._run(self._args("status"), timeout=10.0)
            return True
        except (MpdError, DependencyError):
            return False

    def status(self) -> MpdStatus:
        return self._parse_status(self._run(self._args("status")))

    @classmethod
    def _parse_status(cls, raw: str) -> MpdStatus:
        """Parse the localised status output defensively.

        Output is localised, so parsing is best effort; the raw text is always
        preserved for display.
        """
        song = _SONG_RE.search(raw)
        times = _TIME_RE.search(raw)
        volume = _VOLUME_RE.search(raw)
        song_id = _SONGID_RE.search(raw)
        return MpdStatus(
            raw=raw.strip(),
            playing="[playing]" in raw,
            paused="[paused]" in raw,
            stopped="[stopped]" in raw,
            track=f"#{song.group(1)}" if song else None,
            elapsed=times.group(1) if times else None,
            total=times.group(2) if times else None,
            repeat="repeat: on" in raw,
            random="random: on" in raw,
            volume=volume.group(1) if volume else None,
            single="single: on" in raw,
            consume="consume: on" in raw,
            song_id=int(song_id.group(1)) if song_id else None,
            length=int(song.group(2)) if song and song.group(2) else None,
        )

    def queue(self) -> list[QueueEntry]:
        """Return the queue as structured entries.

        Uses ``mpc -f`` so the result is machine-readable instead of the
        two-line-per-song human layout. Unparseable lines are skipped rather
        than raising: a display quirk in a future mpc must not break the API.
        """
        raw = self._run([*self._args(), "--format", _QUEUE_FORMAT, "playlist"], timeout=30.0)
        entries: list[QueueEntry] = []
        for line in raw.splitlines():
            if not line.strip():
                continue
            parts = line.split(_QUEUE_SEP)
            if len(parts) < 4:
                continue
            try:
                position = int(parts[0])
                song_id = int(parts[1])
            except ValueError:
                continue
            if position < 1:
                continue
            entries.append(
                QueueEntry(
                    position=position,
                    song_id=song_id,
                    file=parts[3].strip(),
                    name=(_QUEUE_SEP.join(parts[4:]) if len(parts) > 4 else "").strip(),
                    duration=parse_mpd_time(parts[2]),
                )
            )
        return entries

    def playlist(self) -> str:
        return self._run(self._args("playlist"))

    def current(self) -> str:
        return self._run(self._args("current")).strip()

    # -- commands ------------------------------------------------------
    def add_url(self, url: str) -> str:
        return self._run(self._args("add", url))

    def play_url(self, url: str) -> str:
        """Append *url* and start playing it, keeping the existing queue."""
        self.add_url(url)
        return self.play()

    def play_id(self, song_id: int | str) -> str:
        return self._run(self._args("play", str(song_id)))

    def delete(self, song_id: int | str) -> str:
        return self._run(self._args("del", str(song_id)))

    def clear(self) -> str:
        """Empty the queue. Never touches the MPD database."""
        return self._run(self._args("clear"))

    def next(self) -> str:
        return self._run(self._args("next"))

    def previous(self) -> str:
        return self._run(self._args("prev"))

    def pause(self) -> str:
        return self._run(self._args("pause"))

    def play(self) -> str:
        return self._run(self._args("play"))

    def stop(self) -> str:
        return self._run(self._args("stop"))

    # -- transport -----------------------------------------------------
    def seek(self, seconds: float) -> str:
        """Seek the current song to an absolute offset in seconds."""
        return self._run(self._args("seek", str(max(0, int(seconds)))))

    def volume(self, percent: int) -> str:
        """Set the output volume, 0-100."""
        return self._run(self._args("volume", str(max(0, min(100, int(percent))))))

    def _toggle(self, name: str, on: bool) -> str:
        return self._run(self._args(name, "on" if on else "off"))

    def repeat(self, on: bool) -> str:
        return self._toggle("repeat", on)

    def random(self, on: bool) -> str:
        return self._toggle("random", on)

    def single(self, on: bool) -> str:
        """Repeat-one mode: play the current song until told otherwise."""
        return self._toggle("single", on)

    def consume(self, on: bool) -> str:
        return self._toggle("consume", on)

    # -- playlist metadata ---------------------------------------------
    def playlist_dir(self) -> Path:
        """Directory MPD accepts playlist loads from.

        MPD refuses to load a playlist from anywhere but its own
        ``playlist_directory``, so this must match that setting. Resolution
        order:

        1. ``playlist_directory`` from the configuration (explicit, reliable)
        2. ``mpc paths`` (exists in older mpc, absent in 0.35)
        3. the cache directory
        """
        if self._config.playlist_directory is not None:
            return Path(self._config.playlist_directory).expanduser()

        try:
            raw = self._run(self._args("paths"), timeout=10.0)
        except (MpdError, DependencyError):
            raw = ""
        for line in raw.splitlines():
            if line.lower().startswith("playlist:"):
                value = line.split(":", 1)[1].strip()
                if value:
                    expanded = shlex.split(value)[0] if value.startswith("~") else value
                    return Path(expanded).expanduser()

        fallback = Path(self._config.cache_directory).expanduser()
        try:
            fallback.mkdir(parents=True, exist_ok=True)
        except OSError:
            return Path(tempfile.gettempdir())
        return fallback

    def write_playlist(self, entries: list[tuple[str, Track]], target_dir: Path) -> Path:
        """Write an EXTM3U file for *entries* into *target_dir*.

        The file name comes from ``mkstemp`` and is never derived from user
        input, so a crafted title cannot influence the path.
        """
        target_dir.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix="streambridge-", suffix=".m3u", dir=str(target_dir))
        path = Path(name)
        lines = ["#EXTM3U"]
        for url, track in entries:
            seconds = int(track.duration or 0)
            lines.append(f"#EXTINF:{seconds},{clean_tag(to_extinf_name(track))}")
            # Extra comments honour players that map them to real tags. MPD
            # itself only keeps the display string for stream entries.
            lines.append(f"#EXT-X-ALBUMARTIST:{clean_tag(track.display_artist)}")
            lines.append(f"#EXT-X-ALBUM:{clean_tag(track.display_album)}")
            lines.append(f"#EXT-X-TITLE:{clean_tag(track.title)}")
            lines.append(url)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
        return path

    def add_tracks(self, tracks: list[Track], *, play: bool = False) -> str:
        """Queue *tracks* with metadata, preserving order.

        MPD only accepts ``load`` for files inside its ``playlist_directory``,
        and it resolves the argument as a *name without the .m3u suffix*: an
        absolute path or a name that already carries the extension is rejected
        with "Access denied". The generated file is written there and
        referenced by bare name.
        """
        if not tracks:
            raise ValidationError("No tracks to add.")
        entries: list[tuple[str, Track]] = []
        for track in tracks:
            validate_video_id(track.id)
            entries.append((self._config.stream_url(track.id), track))

        target_dir = self.playlist_dir()
        path = self.write_playlist(entries, target_dir)
        name = path.stem  # MPD appends the suffix itself
        try:
            output = self._run(self._args("load", name), timeout=60.0)
            if play:
                self.play()
            return output
        except MpdError as exc:
            raise MpdError(
                f"MPD did not load the playlist: {exc.message}",
                hint=(
                    f"The file must live in MPD's playlist_directory (currently: {target_dir}). "
                    f"Check mpd.conf, or set mpd.playlist_directory in the configuration."
                ),
            ) from exc
        finally:
            with contextlib.suppress(OSError):
                path.unlink()
