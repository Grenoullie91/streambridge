"""Internal data models and input validation.

Upstream extractor output is translated into these types exactly once, at the
:mod:`streambridge.youtube` boundary. No raw dictionaries travel further into
the application.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum

from .errors import ValidationError

# Video ids are 11 URL-safe base64 characters.
_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
# Playlist ids share the character class but are longer.
_PLAYLIST_ID_RE = re.compile(r"^(PL|UU|OLAK5uy_|RD)[A-Za-z0-9_-]{10,}$")

# A search query long enough to be meaningful but short enough to keep the
# upstream request sane.
MAX_QUERY_LENGTH = 200


class SearchType(StrEnum):
    """Search categories exposed by the CLI and the API.

    Extending this enum is the supported way to add new categories.
    """

    SONGS = "songs"
    VIDEOS = "videos"
    ARTISTS = "artists"
    ALBUMS = "albums"
    PLAYLISTS = "playlists"


def validate_video_id(value: str) -> str:
    """Return *value* if it is a syntactically valid video id.

    Rejecting malformed ids here is the primary guard against path traversal
    and open-proxy behaviour: nothing that is not exactly an 11-character id
    ever reaches a URL path or a subprocess argument.
    """
    candidate = (value or "").strip()
    if not _VIDEO_ID_RE.match(candidate):
        raise ValidationError(
            f"Invalid video id: {value!r}",
            hint="A video id consists of exactly 11 characters (A-Z, a-z, 0-9, - and _).",
        )
    return candidate


def validate_playlist_id(value: str) -> str:
    """Return *value* if it is a syntactically valid playlist id."""
    candidate = (value or "").strip()
    if not _PLAYLIST_ID_RE.match(candidate):
        raise ValidationError(
            f"Invalid playlist id: {value!r}",
            hint="Playlist ids usually start with PL, UU, OLAK5uy_ or RD.",
        )
    return candidate


def validate_query(value: str) -> str:
    """Return a trimmed query that is safe to pass to a subprocess argument."""
    candidate = (value or "").strip()
    if not candidate:
        raise ValidationError("Search query must not be empty.")
    if len(candidate) > MAX_QUERY_LENGTH:
        raise ValidationError(f"Search query too long (max. {MAX_QUERY_LENGTH} characters).")
    # Control characters would be the classic route to argument injection when
    # a value is handed to an external program.
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in candidate):
        raise ValidationError("Search query contains control characters.")
    return candidate


@dataclass(frozen=True, slots=True)
class Track:
    """A single search result or queue candidate."""

    id: str
    title: str
    artist: str | None = None
    album: str | None = None
    duration: int | None = None
    webpage_url: str = ""
    thumbnail: str | None = None
    channel: str | None = None
    uploader: str | None = None
    release_year: int | None = None
    source: str = "online"

    def to_dict(self) -> dict[str, object]:
        """JSON-serialisable representation used by the HTTP API and --json."""
        return {
            "id": self.id,
            "title": self.title,
            "artist": self.artist,
            "album": self.album,
            "duration": self.duration,
            "url": self.webpage_url,
            "thumbnail": self.thumbnail,
            "channel": self.channel,
            "uploader": self.uploader,
            "release_year": self.release_year,
            "source": self.source,
        }

    @property
    def display_artist(self) -> str:
        """Best-effort artist: metadata first, then uploader, then channel."""
        for value in (self.artist, self.uploader, self.channel):
            if value:
                return value
        return "Unknown Artist"

    @property
    def display_album(self) -> str:
        """Album label with a clear placeholder when the source has none."""
        return self.album or "Unknown Album"

    @property
    def display_name(self) -> str:
        """Single-line label for players that expose only one text field.

        Online titles frequently already begin with the artist
        ("Artist - Song"). Prefixing the artist unconditionally would produce
        "Artist - Artist - Song", so it is only added when the title does not
        already lead with it.
        """
        title = self.title.strip()
        artist = self.display_artist.strip()
        if not artist or artist == "Unknown Artist":
            return title
        if title.lower().startswith(artist.lower()):
            return title
        # Separators that mark a leading "Artist - Title" pattern. The en dash
        # is included because upstream titles use it as often as the hyphen.
        for separator in (" - ", " | ", " ~ ", " – "):  # noqa: RUF001 - intentional
            if title.lower().split(separator, 1)[0].strip() == artist.lower():
                return title
        return f"{artist} - {title}"


@dataclass(frozen=True, slots=True)
class Playlist:
    """A resolved playlist, order preserved."""

    id: str
    title: str
    tracks: tuple[Track, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "title": self.title,
            "count": len(self.tracks),
            "tracks": [t.to_dict() for t in self.tracks],
        }


@dataclass(frozen=True, slots=True)
class StreamInfo:
    """A resolved, currently valid media source.

    ``expires_at`` is ``None`` when the upstream source does not expose a
    usable expiry. In that case the resolver treats every failure as
    "re-resolve" instead of trusting a guess.
    """

    video_id: str
    url: str
    mime_type: str
    content_length: int | None = None
    expires_at: float | None = None
    headers: dict[str, str] = field(default_factory=dict)
    ext: str | None = None
    acodec: str | None = None
    format_id: str | None = None

    def is_probably_expired(self, now: float, margin: float = 30.0) -> bool:
        """True when the URL is known to be expired or about to expire."""
        if self.expires_at is None:
            return False
        return self.expires_at - margin <= now


@dataclass(frozen=True, slots=True)
class SearchResult:
    """A page of search results plus the query that produced them."""

    query: str
    tracks: tuple[Track, ...]
    search_type: SearchType = SearchType.SONGS

    def to_dict(self) -> dict[str, object]:
        return {
            "query": self.query,
            "type": self.search_type.value,
            "count": len(self.tracks),
            "results": [t.to_dict() for t in self.tracks],
        }
