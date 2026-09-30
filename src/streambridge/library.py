"""Local, persistent user library: favorites and play history.

Why this exists
---------------
The web UI needs somewhere to keep favourites and a "recently played" list.
Neither YouTube nor MPD owns that data, and pushing it to a cloud service
would violate this project's privacy rules. The store therefore writes plain
JSON into a local state directory (``$XDG_STATE_HOME/streambridge`` by default,
i.e. ``~/.local/state/streambridge``) and never leaves the machine.

Design notes
------------
* Only *stable* metadata is persisted. No media URLs, no cookies, no
  credentials - a stored favourite is a video id plus its title/artist.
* Writes are atomic (temp file + ``replace``) so a crash mid-write can never
  leave a truncated file behind.
* Every file is read through a strict shape check; a corrupted or hostile
  file degrades to "empty library" instead of crashing the server.
* A hard size cap protects the server from a hand-crafted multi-gigabyte
  file.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import tempfile
import threading
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from .models import Track, validate_video_id

log = logging.getLogger("streambridge.library")

# A library file is metadata only. Anything larger is not something this
# project ever wrote, so it is ignored rather than parsed.
_MAX_FILE_BYTES = 2 * 1024 * 1024
# Upper bound on stored entries. Keeps the JSON small and the UI fast.
MAX_FAVORITES = 500
MAX_HISTORY = 200
# Free-text fields are truncated on write; see _sanitise_text.
_MAX_TEXT = 300

_STORE_VERSION = 1


def _sanitise_text(value: Any, limit: int = _MAX_TEXT) -> str:
    """Coerce an arbitrary JSON value into a short, single-line string."""
    if value is None or isinstance(value, (list, dict, bool)):
        return ""
    if not isinstance(value, str):
        value = str(value)
    # Control characters would break the single-line layout of the UI.
    text = "".join(ch if ch.isprintable() else " " for ch in value)
    return text.strip()[:limit]


def _sanitise_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if 0 <= value <= 24 * 3600 * 7 else None
    if isinstance(value, float) and value.is_integer():
        seconds = int(value)
        return seconds if 0 <= seconds <= 24 * 3600 * 7 else None
    if isinstance(value, str) and value.isdigit():
        seconds = int(value)
        return seconds if 0 <= seconds <= 24 * 3600 * 7 else None
    return None


# Timestamps are unix seconds, not durations: they need their own bounds
# (2000-01-01 .. 2100-01-01) or a valid value would be rejected as "too long".
_MIN_TIMESTAMP = 946_684_800
_MAX_TIMESTAMP = 4_102_444_800


def _sanitise_timestamp(value: Any) -> int:
    """Coerce a stored unix timestamp, falling back to 0 when unusable."""
    if value is None or isinstance(value, bool):
        return 0
    try:
        seconds = int(value)
    except (TypeError, ValueError):
        return 0
    return seconds if _MIN_TIMESTAMP <= seconds <= _MAX_TIMESTAMP else 0


def track_to_stored(track: Track) -> dict[str, Any]:
    """Reduce a :class:`Track` to the fields that are safe to persist."""
    return {
        "id": track.id,
        "title": _sanitise_text(track.title),
        "artist": _sanitise_text(track.artist),
        "album": _sanitise_text(track.album),
        "duration": _sanitise_int(track.duration),
        "url": _sanitise_text(track.webpage_url, 300),
        "thumbnail": _sanitise_text(track.thumbnail, 1000),
        "channel": _sanitise_text(track.channel),
        "uploader": _sanitise_text(track.uploader),
        "release_year": _sanitise_int(track.release_year),
    }


def track_from_stored(data: Any) -> Track | None:
    """Rebuild a :class:`Track` from persisted data, or None if unusable."""
    if not isinstance(data, dict):
        return None
    raw_id = data.get("id")
    if not isinstance(raw_id, str):
        return None
    try:
        video_id = validate_video_id(raw_id)
    except Exception:
        return None

    def opt(key: str) -> str | None:
        value = _sanitise_text(data.get(key))
        return value or None

    duration = _sanitise_int(data.get("duration"))
    year = _sanitise_int(data.get("release_year"))
    if year is not None and not 1900 <= year <= 2100:
        year = None
    webpage = opt("url") or f"https://www.youtube.com/watch?v={video_id}"
    if not webpage.startswith(("https://www.youtube.com/", "https://youtu.be/")):
        webpage = f"https://www.youtube.com/watch?v={video_id}"
    return Track(
        id=video_id,
        title=_sanitise_text(data.get("title")) or video_id,
        artist=opt("artist"),
        album=opt("album"),
        duration=duration,
        webpage_url=webpage,
        thumbnail=opt("thumbnail"),
        channel=opt("channel"),
        uploader=opt("uploader"),
        release_year=year,
    )


class _JsonFile:
    """A tiny, thread-safe, atomically written JSON document."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()

    @property
    def path(self) -> Path:
        return self._path

    def read(self) -> dict[str, Any]:
        """Return the stored document, or an empty one when unusable."""
        with self._lock:
            try:
                if not self._path.is_file():
                    return {}
                if self._path.stat().st_size > _MAX_FILE_BYTES:
                    log.warning("Bibliotheksdatei %s ist zu gross und wird ignoriert", self._path)
                    return {}
                with self._path.open("r", encoding="utf-8") as handle:
                    data = json.load(handle)
            except (OSError, json.JSONDecodeError, ValueError) as exc:
                log.warning("Bibliotheksdatei %s nicht lesbar: %s", self._path, exc)
                return {}
        if not isinstance(data, dict):
            return {}
        return data

    def write(self, document: dict[str, Any]) -> None:
        """Atomically replace the file. Never raises on a full disk."""
        with self._lock:
            try:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                fd, tmp_name = tempfile.mkstemp(
                    dir=str(self._path.parent), prefix=f".{self._path.name}.", suffix=".tmp"
                )
                try:
                    with os.fdopen(fd, "w", encoding="utf-8") as handle:
                        json.dump(document, handle, ensure_ascii=False, indent=2)
                    Path(tmp_name).replace(self._path)
                except BaseException:
                    with contextlib.suppress(OSError):
                        Path(tmp_name).unlink()
                    raise
            except OSError as exc:
                # Persistence is a convenience, never a hard requirement:
                # a read-only home directory must not break playback.
                log.warning("Bibliothek nicht schreibbar (%s): %s", self._path, exc)


class LibraryStore:
    """Favourites and history, stored locally as JSON.

    Both lists are ordered newest-first and de-duplicated by video id.
    """

    def __init__(
        self,
        directory: Path,
        *,
        max_favorites: int = MAX_FAVORITES,
        max_history: int = MAX_HISTORY,
    ) -> None:
        base = Path(directory).expanduser()
        self._favorites_file = _JsonFile(base / "favorites.json")
        self._history_file = _JsonFile(base / "history.json")
        self._max_favorites = max(1, max_favorites)
        self._max_history = max(1, max_history)
        self._lock = threading.Lock()

    # -- introspection -------------------------------------------------
    @property
    def favorites_path(self) -> Path:
        """Where the favourites are stored. Useful for diagnostics and tests."""
        return self._favorites_file.path

    @property
    def history_path(self) -> Path:
        return self._history_file.path

    # -- favourites ----------------------------------------------------
    def favorites(self) -> list[dict[str, Any]]:
        """Stored favourites, newest first."""
        document = self._favorites_file.read()
        raw = document.get("items")
        items = raw if isinstance(raw, list) else []
        out: list[dict[str, Any]] = []
        seen: set[str] = set()
        for entry in items[: self._max_favorites]:
            track = track_from_stored(entry)
            if track is None or track.id in seen:
                continue
            seen.add(track.id)
            added = entry.get("added_at") if isinstance(entry, dict) else None
            out.append({"track": track.to_dict(), "added_at": _sanitise_timestamp(added)})
        return out

    def is_favorite(self, video_id: str) -> bool:
        vid = validate_video_id(video_id)
        return any(item["track"]["id"] == vid for item in self.favorites())

    def add_favorite(self, track: Track) -> bool:
        """Add *track*. Returns False when it was already a favourite."""
        vid = validate_video_id(track.id)
        with self._lock:
            current = self.favorites()
            if any(item["track"]["id"] == vid for item in current):
                return False
            stored = track_to_stored(track)
            stored["added_at"] = int(time.time())
            self._write_items(self._favorites_file, [stored, *[i["track"] for i in current]])
        return True

    def remove_favorite(self, video_id: str) -> bool:
        vid = validate_video_id(video_id)
        with self._lock:
            current = self.favorites()
            kept = [item["track"] for item in current if item["track"]["id"] != vid]
            if len(kept) == len(current):
                return False
            self._write_items(self._favorites_file, kept)
        return True

    def toggle_favorite(self, track: Track) -> bool:
        """Add or remove *track*; returns True when it is now a favourite."""
        if self.is_favorite(track.id):
            self.remove_favorite(track.id)
            return False
        self.add_favorite(track)
        return True

    def clear_favorites(self) -> int:
        with self._lock:
            count = len(self.favorites())
            self._write_items(self._favorites_file, [])
        return count

    # -- history -------------------------------------------------------
    def history(self) -> list[dict[str, Any]]:
        document = self._history_file.read()
        raw = document.get("items")
        items = raw if isinstance(raw, list) else []
        out: list[dict[str, Any]] = []
        seen: set[str] = set()
        for entry in items[: self._max_history]:
            track = track_from_stored(entry)
            if track is None or track.id in seen:
                continue
            seen.add(track.id)
            played = entry.get("played_at") if isinstance(entry, dict) else None
            out.append({"track": track.to_dict(), "played_at": _sanitise_timestamp(played)})
        return out

    def record_play(self, track: Track) -> None:
        """Push *track* to the front of the history, de-duplicated."""
        try:
            vid = validate_video_id(track.id)
        except Exception:
            return
        with self._lock:
            current = [item["track"] for item in self.history()]
            kept = [item for item in current if item.get("id") != vid]
            stored = track_to_stored(track)
            stored["played_at"] = int(time.time())
            self._write_items(self._history_file, [stored, *kept], limit=self._max_history)

    def record_many(self, tracks: Iterable[Track]) -> None:
        for track in tracks:
            self.record_play(track)

    def clear_history(self) -> int:
        with self._lock:
            count = len(self.history())
            self._write_items(self._history_file, [])
        return count

    # -- internals -----------------------------------------------------
    def _write_items(
        self, target: _JsonFile, items: list[dict[str, Any]], *, limit: int | None = None
    ) -> None:
        cap = limit if limit is not None else self._max_favorites
        target.write({"version": _STORE_VERSION, "items": items[:cap]})

    def clear_all(self) -> None:
        """Wipe both lists. Used by tests and the "clear everything" action."""
        with self._lock:
            self._write_items(self._favorites_file, [])
            self._write_items(self._history_file, [])


def default_state_dir() -> Path:
    """State directory, honouring ``XDG_STATE_HOME``."""
    import os as _os

    base = _os.environ.get("XDG_STATE_HOME")
    root = Path(base) if base else Path.home() / ".local" / "state"
    return root / "streambridge"
