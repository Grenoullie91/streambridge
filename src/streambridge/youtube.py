"""The only module in StreamBridge that talks to the upstream extractor.

Everything else works with :class:`~streambridge.models.Track`,
:class:`~streambridge.models.StreamInfo` and
:class:`~streambridge.models.Playlist`. Replacing the extractor backend means
replacing this module alone.
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import parse_qs, urlsplit

from .config import Config
from .errors import DependencyError, SourceUnavailableError, ValidationError
from .models import (
    Playlist,
    SearchResult,
    SearchType,
    StreamInfo,
    Track,
    validate_playlist_id,
    validate_query,
    validate_video_id,
)
from .proc import CommandRunner, SubprocessRunner, which

log = logging.getLogger("streambridge.youtube")

WATCH_URL = "https://www.youtube.com/watch?v={video_id}"

# Bounded backoff. Retries are never unbounded.
MAX_ATTEMPTS = 3
BACKOFF_SECONDS = (0.8, 2.5)

# Advisory only: versions below this predate the modern extraction path and
# are known to fail with "Precondition check failed".
MIN_EXTRACTOR_VERSION = "2024.01.01"

_UNAVAILABLE_MARKERS = (
    "video unavailable",
    "is unavailable",
    "has been removed",
    "no longer available",
    "account associated with this video has been terminated",
    "private video",
    "members-only",
    "this video is not available",
)
_GEO_MARKERS = (
    "not available in your country",
    "blocked it in your country",
    "geo restricted",
    "not available from your location",
)
_BOTCHECK_MARKERS = (
    "sign in to confirm",
    "sign in to confirm you're not a bot",
    "confirm you're not a bot",
    "rate limit",
    "too many requests",
)


def clean_text(value: Any) -> str | None:
    """Normalise an upstream string field; empty or placeholder values become None."""
    if value is None or isinstance(value, (dict, list, bool)):
        return None
    if isinstance(value, float):
        return None
    text = str(value).strip()
    if not text or text.lower() in {"none", "null", "n/a"}:
        return None
    return text


def as_duration(value: Any) -> int | None:
    """Coerce the many duration shapes into whole seconds."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        seconds = float(value)
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            seconds = float(text)
        except ValueError:
            numbers: list[float] = []
            for part in text.split(":"):
                try:
                    numbers.append(float(part))
                except ValueError:
                    return None
            seconds = 0.0
            for number in numbers:
                seconds = seconds * 60 + number
    else:
        return None
    if seconds <= 0 or seconds > 24 * 3600 * 7:
        return None
    return round(seconds)


def as_bytes(value: Any) -> int | None:
    """Coerce a file size into a positive byte count."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        size = float(value)
    elif isinstance(value, str):
        try:
            size = float(value.strip())
        except ValueError:
            return None
    else:
        return None
    if size <= 0 or size > 1024**4:
        return None
    return int(size)


def parse_year(value: Any) -> int | None:
    """Extract a four digit year from a release date field."""
    text = clean_text(value)
    if text is None:
        return None
    match = re.search(r"(\d{4})", text)
    if not match:
        return None
    year = int(match.group(1))
    return year if 1900 <= year <= 2100 else None


def pick_thumbnail(entry: Mapping[str, Any]) -> str | None:
    """Choose a thumbnail URL from the explicit field or the candidate list."""
    direct = clean_text(entry.get("thumbnail"))
    if direct:
        return direct
    candidates = entry.get("thumbnails")
    if isinstance(candidates, list):
        usable = [t for t in candidates if isinstance(t, Mapping) and clean_text(t.get("url"))]
        if usable:
            usable.sort(key=lambda t: (t.get("preference") or 0, t.get("width") or 0))
            return clean_text(usable[-1].get("url"))
    return None


def track_from_entry(entry: object) -> Track | None:
    """Convert one upstream entry into a :class:`Track`, or None if unusable.

    The parameter is ``object`` on purpose: a malformed result set can contain
    strings, numbers and nulls where objects are expected, and those must be
    skipped rather than raise. The isinstance check below is the guard.

    Rows without a syntactically valid video id are also dropped: channel and
    browse entries legitimately appear in some result sets and are not
    playable tracks.
    """
    if not isinstance(entry, Mapping):
        return None
    raw_id = clean_text(entry.get("id"))
    if not raw_id:
        return None
    try:
        video_id = validate_video_id(raw_id)
    except ValidationError:
        log.debug("Skipping entry with non-track id")
        return None

    title = clean_text(entry.get("title")) or f"({video_id})"
    url = clean_text(entry.get("webpage_url")) or WATCH_URL.format(video_id=video_id)
    return Track(
        id=video_id,
        title=title,
        artist=clean_text(entry.get("artist")) or clean_text(entry.get("track")),
        album=clean_text(entry.get("album")),
        duration=as_duration(entry.get("duration")),
        webpage_url=url,
        thumbnail=pick_thumbnail(entry),
        channel=clean_text(entry.get("channel")) or clean_text(entry.get("channel_id")),
        uploader=clean_text(entry.get("uploader")),
        release_year=parse_year(entry.get("release_date") or entry.get("release_year")),
    )


def classify_error(stderr: str, returncode: int) -> tuple[str, bool]:
    """Return ``(kind, is_transient)`` for an upstream failure.

    ``kind`` is one of ``unavailable``, ``geo``, ``botcheck``, ``no_audio``,
    ``timeout`` or ``unknown``. Decides the user-facing message and whether a
    bounded retry makes sense.
    """
    text = stderr.lower()
    if "requested format is not available" in text or "no video formats found" in text:
        return "no_audio", False
    for marker in _BOTCHECK_MARKERS:
        if marker in text:
            return "botcheck", True
    for marker in _GEO_MARKERS:
        if marker in text:
            return "geo", False
    for marker in _UNAVAILABLE_MARKERS:
        if marker in text:
            return "unavailable", False
    if "timeout" in text or "timed out" in text:
        return "timeout", True
    if returncode == 124:
        return "timeout", True
    return "unknown", False


def error_message_for(kind: str, subject: str | None = None) -> tuple[str, str | None]:
    """Return ``(user message, hint)`` for a classified upstream error."""
    label = f"Track {subject}" if subject else "The stream"
    table = {
        "unavailable": (
            f"{label} is not available (removed, private or region locked).",
            "Check in the upstream service whether the item is still public.",
        ),
        "geo": (
            f"{label} is region locked.",
            "StreamBridge does not bypass geo-restrictions, by design.",
        ),
        "botcheck": (
            f"The upstream service requires confirmation for {label}.",
            "Optionally use --cookies-from-browser, or update yt-dlp.",
        ),
        "no_audio": (
            f"No playable audio format found for {label}.",
            "Update yt-dlp: yt-dlp -U",
        ),
        "timeout": (
            f"Timed out while resolving {label}.",
            "Increase timeouts.resolve in the configuration.",
        ),
    }
    return table.get(kind, (f"Stream could not be resolved ({label}).", None))


def expiry_from_url(url: str) -> float | None:
    """Best-effort signature expiry extracted from a media URL.

    Returns ``None`` when no plausible timestamp is present; the resolver then
    re-resolves on failure instead of trusting a guess.
    """
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    query = parse_qs(parts.query)
    for key in ("expire", "expireei", "e"):
        for raw in query.get(key, []):
            try:
                value = float(raw)
            except (TypeError, ValueError):
                continue
            if value > 1_000_000_000:
                return value
    return None


def pick_audio_format(info: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """Choose the best audio-only format.

    Preference is based on format metadata only - never on a hardcoded format
    id. MP4/AAC is preferred because MPD decodes it without an external
    decoder; WebM/Opus is the fallback.
    """
    formats: Sequence[Any] = info.get("formats") or ()
    audio: list[Mapping[str, Any]] = []
    for fmt in formats:
        if not isinstance(fmt, Mapping):
            continue
        if fmt.get("vcodec") not in (None, "none"):
            continue
        if fmt.get("acodec") in (None, "none"):
            continue
        if not clean_text(fmt.get("url")):
            continue
        audio.append(fmt)
    if not audio:
        return None

    def rank(fmt: Mapping[str, Any]) -> tuple[int, float, int]:
        ext = (clean_text(fmt.get("ext")) or "").lower()
        codec = (clean_text(fmt.get("acodec")) or "").lower()
        preferred = 0 if ext in {"m4a", "mp4"} or codec.startswith("mp4a") else 1
        quality = float(fmt.get("abr") or 0.0) or float(fmt.get("tbr") or 0.0)
        return (preferred, -quality, as_bytes(fmt.get("filesize")) or 0)

    return sorted(audio, key=rank)[0]


def extract_headers(fmt: Mapping[str, Any]) -> dict[str, str]:
    """Return sanitised HTTP headers required by the media URL."""
    raw = fmt.get("http_headers")
    if not isinstance(raw, Mapping):
        return {}
    out: dict[str, str] = {}
    for key, value in raw.items():
        if not isinstance(key, str) or isinstance(value, (dict, list, bool)):
            continue
        text = str(value).strip()
        # Header injection guard: reject anything containing line breaks.
        if text and "\n" not in text and "\r" not in text:
            out[key] = text
    return out


class ExtractorClient:
    """Typed facade over the yt-dlp command line interface.

    Named after the role rather than the tool so a different backend can be
    substituted without touching callers.
    """

    def __init__(
        self,
        config: Config,
        *,
        runner: CommandRunner | None = None,
        executable: str | None = None,
    ) -> None:
        self._config = config
        self._runner = runner or SubprocessRunner()
        self._executable = executable or config.extractor_path

    # -- infrastructure ------------------------------------------------
    def is_available(self) -> bool:
        return which(self._executable) is not None

    def version(self) -> str | None:
        """Return the installed extractor version, or None."""
        path = which(self._executable)
        if path is None:
            return None
        result = self._runner.run([path, "--version"], timeout=10.0)
        if not result.ok:
            return None
        return result.stdout.strip() or None

    def require(self) -> str:
        """Return the extractor path or raise a friendly dependency error."""
        path = which(self._executable)
        if path is None:
            raise DependencyError(
                f"yt-dlp was not found ({self._executable!r}).",
                hint=(
                    "Install with: pipx install yt-dlp  |  apt install yt-dlp, or set "
                    "youtube.extractor_path in the configuration."
                ),
            )
        return path

    def _base_args(self) -> list[str]:
        args = [self.require(), "--no-warnings", "--no-progress"]
        if self._config.cookies_from_browser:
            # Passed as a separate argv entry; never shell-interpolated.
            args += ["--cookies-from-browser", self._config.cookies_from_browser]
        return args

    def _run_json(
        self,
        args: Sequence[str],
        *,
        timeout: float,
        subject: str | None = None,
    ) -> dict[str, Any]:
        """Run the extractor with JSON output and return the parsed object.

        Retries are bounded (3 attempts) and only for transient failures such
        as bot checks or timeouts.
        """
        argv = [*self._base_args(), "--dump-single-json", *args]
        last_kind = "unknown"
        last_message = ""
        for attempt in range(1, MAX_ATTEMPTS + 1):
            result = self._runner.run(argv, timeout=timeout)
            if result.ok and result.stdout.strip():
                try:
                    data = json.loads(result.stdout)
                except json.JSONDecodeError as exc:
                    # Truncated stdout happens when a timeout kills the process
                    # mid-write. Treat as transient, then give up.
                    last_kind = "unknown"
                    last_message = f"invalid JSON: {exc}"
                    log.warning("Extractor produced no valid JSON (attempt %d)", attempt)
                else:
                    if isinstance(data, dict):
                        return data
                    last_kind = "unknown"
                    last_message = "extractor returned no JSON object"
            else:
                kind, transient = classify_error(result.stderr, result.returncode)
                last_kind = kind
                last_message = result.error_summary()
                if not transient:
                    break
                log.info("Transient upstream failure (%s), attempt %d", kind, attempt)

            if attempt < MAX_ATTEMPTS:
                time.sleep(BACKOFF_SECONDS[min(attempt - 1, len(BACKOFF_SECONDS) - 1)])

        message, hint = error_message_for(last_kind, subject)
        raise SourceUnavailableError(message, upstream_message=last_message, hint=hint)

    # -- public API ----------------------------------------------------
    def search(
        self,
        query: str,
        *,
        limit: int | None = None,
        search_type: SearchType = SearchType.SONGS,
    ) -> SearchResult:
        """Search the music-focused catalogue, falling back to general video search.

        Both paths go through the extractor's own ``ytsearch`` abstraction, so
        no upstream-internal API or HTML is parsed here.
        """
        clean = validate_query(query)
        # An explicit 0 must be rejected, not silently replaced by the default,
        # so the fallback is keyed on None rather than falsiness.
        count = min(self._config.search_limit if limit is None else limit, 100)
        if count < 1:
            raise ValidationError("Limit must be at least 1.")

        target = f"{clean} song" if search_type is SearchType.SONGS else clean
        tracks = self._search_tracks(target, count)

        if not tracks and search_type is SearchType.SONGS:
            log.info("Music search returned nothing, falling back to general search")
            tracks = self._search_tracks(clean, count)

        return SearchResult(query=clean, tracks=tuple(tracks), search_type=search_type)

    def _search_tracks(self, query: str, count: int) -> list[Track]:
        data = self._run_json(
            [f"ytsearch{count}:{query}", "--flat-playlist"],
            timeout=self._config.request_timeout,
        )
        raw = data.get("entries")
        entries = (
            [e for e in raw if isinstance(e, Mapping)]
            if isinstance(raw, list)
            else ([data] if data.get("id") else [])
        )
        return [t for t in (track_from_entry(e) for e in entries) if t is not None]

    def get_info(self, video_id: str) -> Track:
        """Fetch full metadata for one track."""
        vid = validate_video_id(video_id)
        data = self._run_json(
            [WATCH_URL.format(video_id=vid), "--skip-download"],
            timeout=self._config.info_timeout,
            subject=vid,
        )
        track = track_from_entry(data)
        if track is None:
            raise SourceUnavailableError(
                f"No metadata available for {vid}.",
                hint="The item may no longer exist.",
            )
        return track

    def resolve_stream(self, video_id: str) -> StreamInfo:
        """Resolve a currently valid audio source for *video_id*."""
        vid = validate_video_id(video_id)
        data = self._run_json(
            [WATCH_URL.format(video_id=vid), "--skip-download"],
            timeout=self._config.resolve_timeout,
            subject=vid,
        )
        return self._stream_info_from(vid, data)

    def _stream_info_from(self, video_id: str, data: Mapping[str, Any]) -> StreamInfo:
        fmt = pick_audio_format(data)
        if fmt is None:
            raise SourceUnavailableError(
                f"No playable audio format found for {video_id}.",
                upstream_message="no audio formats in the extractor response",
                hint="Update yt-dlp: yt-dlp -U",
            )
        url = clean_text(fmt.get("url"))
        if not url:
            raise SourceUnavailableError(
                f"The extractor returned no media URL for {video_id}.",
                hint="Usually a bot check; optionally use --cookies-from-browser.",
            )
        return StreamInfo(
            video_id=video_id,
            url=url,
            mime_type=clean_text(fmt.get("mime_type")) or "audio/mp4",
            content_length=as_bytes(fmt.get("filesize")) or as_bytes(fmt.get("filesize_approx")),
            expires_at=expiry_from_url(url),
            headers=extract_headers(fmt),
            ext=clean_text(fmt.get("ext")),
            acodec=clean_text(fmt.get("acodec")),
            format_id=clean_text(fmt.get("format_id")),
        )

    def playlist(self, playlist_id: str, *, limit: int | None = None) -> Playlist:
        """Fetch a playlist, preserving order, without downloading media."""
        pid = validate_playlist_id(playlist_id)
        data = self._run_json(
            [f"https://www.youtube.com/playlist?list={pid}", "--flat-playlist", "--skip-download"],
            timeout=self._config.info_timeout,
            subject=pid,
        )
        cap = limit or self._config.search_limit * 5
        tracks: list[Track] = []
        for entry in data.get("entries") or []:
            if len(tracks) >= cap:
                break
            if isinstance(entry, Mapping):
                track = track_from_entry(entry)
                if track is not None:
                    tracks.append(track)
        return Playlist(id=pid, title=clean_text(data.get("title")) or pid, tracks=tuple(tracks))
