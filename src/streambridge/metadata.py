"""Metadata normalisation and presentation.

All conversions between raw payloads, :class:`~streambridge.models.Track`
objects and human-readable text live here, so formatting rules are defined in
exactly one place.
"""

from __future__ import annotations

from .models import Track

# Duration placeholder when the source provides none.
UNKNOWN_DURATION = "--:--"
# Fallback album label; avoids showing an empty column in players.
UNKNOWN_ALBUM = "Unknown Album"


def format_duration(seconds: int | None) -> str:
    """Format seconds as ``m:ss`` or ``h:mm:ss``."""
    if not seconds or seconds <= 0:
        return UNKNOWN_DURATION
    minutes, secs = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def track_metadata(track: Track) -> dict[str, str]:
    """Normalised metadata for a track, safe for display.

    Every value is a non-empty string so downstream consumers never have to
    handle ``None``.
    """
    return {
        "artist": one_line(track.display_artist),
        "title": one_line(track.title),
        "album": one_line(track.display_album),
        "duration": format_duration(track.duration),
        "display": one_line(track.display_name),
    }


def one_line(value: str) -> str:
    """Collapse whitespace so a value is safe to interpolate into a line.

    Upstream titles are untrusted text and may contain newlines. A newline in
    a log line or a generated playlist would break the line structure, so all
    user-supplied values pass through here.
    """
    return " ".join(value.split())


def describe(track: Track) -> str:
    """One-line description used in log output and doctor reports."""
    meta = track_metadata(track)
    artist = one_line(meta["artist"])
    title = one_line(meta["title"])
    return f"{artist} - {title} ({meta['duration']})"


def to_extinf_name(track: Track) -> str:
    """Display string written into the EXTINF line of a generated playlist.

    Collapsed to one line here as well as in the M3U writer: the writer's
    sanitiser is the enforcement point, this keeps the value usable anywhere
    else without thinking about it.
    """
    return one_line(track.display_name)
