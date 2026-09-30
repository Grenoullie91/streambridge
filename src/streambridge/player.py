"""Player and queue control - the single place that talks to MPD.

Every player action in the application funnels through :class:`PlayerService`.
The HTTP layer, the web UI and the CLI never speak to MPD directly, and no
MPD command is ever constructed in JavaScript. That gives one place to add
validation, locking and error translation.

Responsibilities
----------------
* translate MPD's 1-based positions and text formats into plain JSON
* serialise mutating commands so a burst of clicks cannot interleave
* enrich queue rows with metadata the server already knows
* remember what is playing, so the local history can be updated
* keep a short-lived queue cache so polling stays cheap
"""

from __future__ import annotations

import logging
import re
import threading
import time
from typing import TYPE_CHECKING, Any

from .cache import TtlCache
from .config import Config
from .errors import MpdError, ValidationError
from .library import LibraryStore
from .models import Track, validate_video_id
from .mpd import MpdClient, MpdStatus, QueueEntry

if TYPE_CHECKING:  # pragma: no cover - import cycle only matters for typing
    from .api import ApiService

log = logging.getLogger("streambridge.player")

# The queue only changes through this service, so a short cache is safe and
# keeps the status poller from spawning an `mpc` process per second.
_QUEUE_TTL = 2.0
# How often history entries are written for the same song, at most.
_HISTORY_MIN_INTERVAL = 5.0
# Bound on the in-memory metadata map.
_META_TTL = 7 * 24 * 3600.0
_META_MAX = 256
# Background metadata lookups: never more than this many at a time, and never
# a long backlog, so a 200-entry queue cannot trigger a yt-dlp storm.
_MAX_CONCURRENT_ENRICH = 2
_MAX_PENDING_ENRICH = 40

_STREAM_PATH_RE = re.compile(r"^/stream/([A-Za-z0-9_-]{11})$")


def video_id_from_stream_url(url: str) -> str | None:
    """Recover the video id from a local ``/stream/<id>`` URL.

    Only URLs of exactly that shape are accepted - no query, no fragment, no
    extra path segment - and the result is re-validated, so a hostile queue
    entry can neither escape nor inject anything.
    """
    if not url:
        return None
    try:
        from urllib.parse import urlsplit

        parts = urlsplit(url)
    except ValueError:
        return None
    if parts.query or parts.fragment:
        return None
    match = _STREAM_PATH_RE.match(parts.path)
    if not match:
        return None
    try:
        return validate_video_id(match.group(1))
    except ValidationError:
        return None


class PlayerService:
    """Structured, JSON-friendly view of MPD plus the local library."""

    def __init__(
        self,
        config: Config,
        *,
        mpd: MpdClient,
        library: LibraryStore,
        api: ApiService | None = None,
    ) -> None:
        self._config = config
        self.mpd = mpd
        self.library = library
        self._api = api
        self._lock = threading.RLock()
        self._queue_cache: tuple[float, list[QueueEntry]] | None = None
        self._meta = TtlCache(max_entries=_META_MAX, ttl=_META_TTL)
        self._last_played_id: str | None = None
        self._last_played_at = 0.0
        self._last_volume = config.default_volume
        self._generation = 0
        self._pending: set[str] = set()
        self._enrich_slots = threading.Semaphore(_MAX_CONCURRENT_ENRICH)

    # -- metadata cache -------------------------------------------------
    def remember(self, tracks: list[Track]) -> None:
        """Cache metadata so queue rows and the now-playing bar can be enriched
        without another yt-dlp call."""
        for track in tracks:
            try:
                self._meta.set(validate_video_id(track.id), track)
            except ValidationError:
                continue

    def _known(self, video_id: str) -> Track | None:
        cached = self._meta.get(video_id)
        if isinstance(cached, Track):
            return cached
        if self._api is not None:
            track = self._api.cached_track(video_id)
            if track is not None:
                self._meta.set(video_id, track)
                return track
        return None

    def track_for(self, video_id: str) -> Track:
        """Full metadata for *video_id*, resolving through the API if needed."""
        vid = validate_video_id(video_id)
        known = self._known(vid)
        if known is not None:
            return known
        if self._api is None:  # pragma: no cover - always wired in production
            raise MpdError("Metadata is not available right now.")
        return self._api.track(vid)

    # -- queue ----------------------------------------------------------
    def _queue(self, *, fresh: bool = False) -> list[QueueEntry]:
        now = time.monotonic()
        if not fresh and self._queue_cache is not None:
            stamp, entries = self._queue_cache
            if now - stamp < _QUEUE_TTL:
                return entries
        with self._lock:
            entries = self.mpd.queue()
            self._queue_cache = (now, entries)
            return entries

    def _enrich_async(self, video_id: str | None) -> None:
        """Fill in metadata for a queue row in the background.

        A row queued by ncmpcpp, or by an older StreamBridge session, carries no
        metadata in MPD - only the video id. Rather than blocking a status
        poll on yt-dlp, the lookup is queued and the next status update
        (roughly a second later) already shows the real title.
        """
        if not video_id or video_id in self._pending or self._known(video_id) is not None:
            return
        if len(self._pending) >= _MAX_PENDING_ENRICH:
            return
        self._pending.add(video_id)
        with self._enrich_slots:
            if self._enrich_slots.acquire(blocking=False):
                thread = threading.Thread(target=self._enrich_worker, args=(video_id,), daemon=True)
                thread.start()
            else:
                self._pending.discard(video_id)

    def _enrich_worker(self, video_id: str) -> None:
        try:
            track = self.track_for(video_id)
            self.remember([track])
        except Exception as exc:
            # Unavailable videos stay unresolved; the UI falls back to the id.
            log.debug("Metadata for %s could not be loaded later: %s", video_id, exc)
        finally:
            self._enrich_slots.release()
            self._pending.discard(video_id)

    def _invalidate(self) -> None:
        self._queue_cache = None
        self._generation += 1

    @property
    def generation(self) -> int:
        """Bumped on every queue change; lets the UI skip needless re-renders."""
        return self._generation

    def queue_rows(self, *, fresh: bool = False) -> list[dict[str, Any]]:
        """Queue as JSON rows, enriched with locally known metadata."""
        rows: list[dict[str, Any]] = []
        for entry in self._queue(fresh=fresh):
            video_id = video_id_from_stream_url(entry.file)
            track = self._known(video_id) if video_id else None
            if track is None:
                self._enrich_async(video_id)
            title = entry.name
            artist: str | None = None
            if track is not None:
                title = track.display_name
                artist = track.display_artist
            elif not title:
                title = video_id or entry.file
            row: dict[str, Any] = {
                "position": entry.position,
                "song_id": entry.song_id,
                "video_id": video_id,
                "title": title,
                "artist": artist,
                "duration": entry.duration or (track.duration if track else None),
                "playable": video_id is not None,
            }
            if track is not None:
                row["thumbnail"] = track.thumbnail
            rows.append(row)
        return rows

    def queue(self) -> dict[str, Any]:
        rows = self.queue_rows()
        return {"items": rows, "length": len(rows), "generation": self._generation}

    # -- status ---------------------------------------------------------
    def status(self) -> dict[str, Any]:
        """Player state plus the currently loaded song."""
        mpd_status: MpdStatus = self.mpd.status()
        position = mpd_status.position
        current: dict[str, Any] | None = None
        if position is not None:
            entries = self._queue()
            match = next((e for e in entries if e.position == position), None)
            if match is not None:
                video_id = video_id_from_stream_url(match.file)
                track = self._known(video_id) if video_id else None
                if track is None:
                    self._enrich_async(video_id)
                current = {
                    "position": position,
                    "video_id": video_id,
                    "title": (track.display_name if track else match.name)
                    or (video_id or match.file),
                    "artist": track.display_artist if track else None,
                    "duration": match.duration or (track.duration if track else None),
                    "thumbnail": track.thumbnail if track else None,
                }
        volume = mpd_status.volume_percent
        if volume is not None and volume > 0:
            self._last_volume = volume
        payload: dict[str, Any] = {
            "state": mpd_status.state,
            "position": position,
            "current": current,
            "playlist_length": mpd_status.length
            if mpd_status.length is not None
            else len(self._queue()),
            "elapsed": mpd_status.elapsed_seconds or 0,
            "duration": current["duration"] if current else mpd_status.total_seconds,
            "total_seconds": mpd_status.total_seconds,
            "volume": volume if volume is not None else self._last_volume,
            "muted": volume == 0,
            "shuffle": mpd_status.random,
            "repeat": mpd_status.repeat,
            "repeat_one": mpd_status.single,
            "consume": mpd_status.consume,
            "generation": self._generation,
            "timestamp": round(time.time(), 3),
        }
        self._maybe_record_history(payload)
        return payload

    def _maybe_record_history(self, payload: dict[str, Any]) -> None:
        """Append the current song to the local history exactly once per change."""
        current = payload.get("current")
        if not isinstance(current, dict):
            return
        video_id = current.get("video_id")
        if not video_id or payload.get("state") == "stopped":
            return
        now = time.monotonic()
        if video_id == self._last_played_id and now - self._last_played_at < _HISTORY_MIN_INTERVAL:
            return
        self._last_played_id = video_id
        self._last_played_at = now
        try:
            track = self._known(video_id) or Track(
                id=video_id,
                title=str(current.get("title") or video_id),
                artist=current.get("artist"),
                duration=current.get("duration")
                if isinstance(current.get("duration"), int)
                else None,
            )
            self.library.record_play(track)
        except Exception as exc:  # pragma: no cover - history is best effort
            log.debug("History could not be updated: %s", exc)

    # -- queue mutations ------------------------------------------------
    def add(
        self,
        video_ids: list[str],
        *,
        play: bool = False,
        position: int | None = None,
        tracks: list[Track] | None = None,
    ) -> dict[str, Any]:
        """Queue *video_ids*, resolving metadata where the caller has none.

        ``tracks`` may carry metadata the caller already has (a search result,
        a favourite). Every id is validated regardless, so a crafted payload
        cannot inject a URL.
        """
        if not video_ids:
            raise ValidationError("No tracks given.")
        if len(video_ids) > 200:
            raise ValidationError("Too many tracks at once (max. 200).")

        known: dict[str, Track] = {}
        for track in tracks or []:
            try:
                known[validate_video_id(track.id)] = track
            except ValidationError:
                continue

        resolved: list[Track] = []
        for raw in video_ids:
            vid = validate_video_id(raw)
            supplied = known.get(vid)
            resolved.append(supplied if supplied is not None else self.track_for(vid))
        if not resolved:
            raise ValidationError("No valid tracks to add.")

        self.remember(resolved)
        with self._lock:
            before = len(self._queue(fresh=True))
            self.mpd.add_tracks(resolved, play=False)
            added = len(self._queue(fresh=True)) - before
            target: int | None = None
            if position is not None and added > 0:
                # mpc only appends, so a "play this now" request is honoured by
                # loading at the end and then moving the new block into place.
                # Moving forwards with a shifting target keeps the block order.
                self._require_position(position)
                start = before + 1
                for offset in range(min(added, len(resolved))):
                    self.mpd.move(start + offset, position + offset)
                target = position
            self._invalidate()
            if play:
                start_position = target if target is not None else before + 1
                self.mpd.play_id(start_position)
            queue = self.queue()
        return {
            "added": len(resolved),
            "position": target,
            "queue": queue,
        }

    def remove(self, position: int) -> dict[str, Any]:
        pos = self._require_position(position)
        with self._lock:
            self._require_in_queue(pos)
            self.mpd.delete(pos)
            self._invalidate()
            queue = self.queue()
        return {"removed": pos, "queue": queue}

    def move(self, source: int, target: int) -> dict[str, Any]:
        src = self._require_position(source)
        dst = self._require_position(target)
        with self._lock:
            length = len(self._queue(fresh=True))
            if src > length or dst > length:
                raise ValidationError(f"Position is outside the queue (1-{length}).")
            if src != dst:
                self.mpd.move(src, dst)
                self._invalidate()
            queue = self.queue()
        return {"from": src, "to": dst, "queue": queue}

    def clear(self) -> dict[str, Any]:
        with self._lock:
            self.mpd.clear()
            self._invalidate()
        log.info("Queue cleared")
        return self.queue()

    def _require_position(self, position: Any) -> int:
        if isinstance(position, bool) or not isinstance(position, int):
            raise ValidationError("Queue position must be a whole number.")
        if position < 1 or position > 10_000:
            raise ValidationError("Queue position must be between 1 and 10000.")
        return position

    def _require_in_queue(self, position: int) -> int:
        """Check *position* against the real queue.

        ``mpc del 9`` on a three-song queue answers with a bare "song number
        does not exist", which reads like a server fault. Checking here turns it
        into a plain validation error the UI can show.
        """
        length = len(self._queue(fresh=True))
        if position > length:
            raise ValidationError(f"Position is outside the queue (1-{length}).")
        return position

    # -- transport ------------------------------------------------------
    def play(self, position: int | None = None) -> dict[str, Any]:
        with self._lock:
            if position is None:
                if not self._queue(fresh=True):
                    raise ValidationError(
                        "The queue is empty.",
                        hint="Search for a track and add it to the queue first.",
                    )
                self.mpd.play()
            else:
                pos = self._require_position(position)
                self._require_in_queue(pos)
                self.mpd.play_id(pos)
        return self.status()

    def pause(self) -> dict[str, Any]:
        # `mpc pause` is a toggle, so only send it when MPD is actually playing.
        # This makes the API idempotent and avoids a stuck state after a double
        # click or a retried request.
        current = self.mpd.status()
        if current.playing:
            self.mpd.pause()
        elif not current.paused:
            raise ValidationError("Nothing is playing, so there is nothing to pause.")
        return self.status()

    def stop(self) -> dict[str, Any]:
        self.mpd.stop()
        return self.status()

    def next(self) -> dict[str, Any]:
        with self._lock:
            if not self._queue(fresh=True):
                raise ValidationError("The queue is empty.")
            self._require_transport()
            self.mpd.next()
        return self.status()

    def previous(self) -> dict[str, Any]:
        with self._lock:
            if not self._queue(fresh=True):
                raise ValidationError("The queue is empty.")
            self._require_transport()
            self.mpd.previous()
        return self.status()

    def _require_transport(self) -> None:
        """Refuse a skip when nothing is loaded, instead of letting mpc fail.

        MPD answers ``next``/``previous`` on a stopped player with a generic
        protocol error, which would surface as an opaque server fault. The
        queue may well be full, so say what is actually wrong.
        """
        if self.mpd.status().position is None:
            raise ValidationError(
                "Die Wiedergabe ist gestoppt.",
                hint="Start a track first; then it can be skipped.",
            )

    def seek(self, seconds: float) -> dict[str, Any]:
        if isinstance(seconds, bool) or not isinstance(seconds, (int, float)):
            raise ValidationError("'seconds' must be a number.")
        target = float(seconds)
        if target < 0 or target > 24 * 3600:
            raise ValidationError("'seconds' must be between 0 and 86400.")
        current = self.mpd.status()
        if current.stopped or current.position is None:
            raise ValidationError("Nothing is playing, so it cannot be skipped.")
        self.mpd.seek(target)
        return self.status()

    def volume(self, percent: float) -> dict[str, Any]:
        if isinstance(percent, bool) or not isinstance(percent, (int, float)):
            raise ValidationError("'volume' must be a number between 0 and 100.")
        value = round(float(percent))
        if value < 0 or value > 100:
            raise ValidationError("'volume' must be between 0 and 100.")
        self.mpd.volume(value)
        if value > 0:
            self._last_volume = value
        return self.status()

    def mute(self, muted: bool) -> dict[str, Any]:
        """Mute by moving the volume to 0, unmute by restoring the last value."""
        if not isinstance(muted, bool):
            raise ValidationError("'muted' must be true or false.")
        if muted:
            current = self.mpd.status().volume_percent
            if current:
                self._last_volume = current
            self.mpd.volume(0)
        else:
            self.mpd.volume(self._last_volume or self._config.default_volume)
        return self.status()

    def set_modes(
        self,
        *,
        shuffle: bool | None = None,
        repeat: bool | None = None,
        repeat_one: bool | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            if shuffle is not None:
                self._check_bool("shuffle", shuffle)
                self.mpd.random(shuffle)
            if repeat is not None:
                self._check_bool("repeat", repeat)
                self.mpd.repeat(repeat)
            if repeat_one is not None:
                self._check_bool("repeat_one", repeat_one)
                self.mpd.single(repeat_one)
        return self.status()

    @staticmethod
    def _check_bool(name: str, value: Any) -> None:
        if not isinstance(value, bool):
            raise ValidationError(f"'{name}' must be true or false.")
