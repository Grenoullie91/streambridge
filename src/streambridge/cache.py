"""Bounded caches for search results and metadata.

Deliberately caches only stable information:

* an in-memory LRU/TTL cache keeps the server's footprint bounded
* a small on-disk cache makes repeated CLI invocations fast

Media URLs are never persisted. They expire, and a stale URL in a long-lived
database is exactly the failure mode this project exists to avoid.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import tempfile
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .models import SearchResult, SearchType, Track, validate_video_id

log = logging.getLogger("streambridge.cache")

_MAX_JSON_BYTES = 4 * 1024 * 1024
# Stream sources are short lived; keep them barely longer than one track.
STREAM_TTL_SECONDS = 120.0


def cache_filename(key: str) -> str:
    """Hash the cache key so no user input reaches the filesystem verbatim.

    Hashing removes path traversal as a possibility for the cache directory.
    """
    return f"{hashlib.sha256(key.encode('utf-8')).hexdigest()}.json"


def track_from_dict(data: dict[str, Any]) -> Track | None:
    """Rebuild a Track from cached JSON, tolerating missing or broken fields."""
    try:
        video_id = validate_video_id(str(data.get("id", "")))
    except Exception:
        return None

    def optional_int(value: Any) -> int | None:
        return (
            value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None
        )

    def optional_str(value: Any) -> str | None:
        return value if isinstance(value, str) and value else None

    return Track(
        id=video_id,
        title=str(data.get("title") or video_id),
        artist=optional_str(data.get("artist")),
        album=optional_str(data.get("album")),
        duration=optional_int(data.get("duration")),
        webpage_url=optional_str(data.get("url")) or f"https://www.youtube.com/watch?v={video_id}",
        thumbnail=optional_str(data.get("thumbnail")),
        channel=optional_str(data.get("channel")),
        uploader=optional_str(data.get("uploader")),
        release_year=optional_int(data.get("release_year")),
        source=optional_str(data.get("source")) or "online",
    )


class TtlCache:
    """Thread-safe LRU cache with a per-entry TTL."""

    def __init__(self, max_entries: int = 200, ttl: float = 300.0) -> None:
        self._max = max(1, max_entries)
        self._ttl = max(0.0, ttl)
        self._data: OrderedDict[str, tuple[float, Any]] = OrderedDict()
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0

    def get(self, key: str) -> Any | None:
        with self._lock:
            entry = self._data.get(key)
            if entry is None:
                self.misses += 1
                return None
            stored_at, value = entry
            if self._ttl > 0 and (time.monotonic() - stored_at) > self._ttl:
                del self._data[key]
                self.misses += 1
                return None
            self._data.move_to_end(key)
            self.hits += 1
            return value

    def set(self, key: str, value: Any) -> None:
        with self._lock:
            self._data[key] = (time.monotonic(), value)
            self._data.move_to_end(key)
            while len(self._data) > self._max:
                self._data.popitem(last=False)

    def get_or_compute(self, key: str, factory: Callable[[], Any]) -> Any:
        cached = self.get(key)
        if cached is not None:
            return cached
        value = factory()
        if value is not None:
            self.set(key, value)
        return value

    def invalidate(self, key: str) -> None:
        with self._lock:
            self._data.pop(key, None)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._data)

    def stats(self) -> dict[str, int]:
        with self._lock:
            return {"entries": len(self._data), "hits": self.hits, "misses": self.misses}


class DiskSearchCache:
    """Small JSON-backed cache for search results and metadata.

    Only stable fields are written. Stream URLs are explicitly never stored.
    """

    def __init__(self, directory: Path, ttl: float = 300.0, max_entries: int = 200) -> None:
        self._dir = Path(directory).expanduser()
        self._ttl = max(0.0, ttl)
        self._max = max(1, max_entries)

    def _path(self, key: str) -> Path:
        return self._dir / cache_filename(key)

    def get(self, key: str) -> SearchResult | None:
        path = self._path(key)
        try:
            if not path.is_file():
                return None
            if self._ttl > 0 and (time.time() - path.stat().st_mtime) > self._ttl:
                return None
            if path.stat().st_size > _MAX_JSON_BYTES:
                return None
            with path.open("r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, json.JSONDecodeError, ValueError):
            return None
        if not isinstance(data, dict):
            return None

        tracks = []
        for item in data.get("results") or []:
            if isinstance(item, dict):
                track = track_from_dict(item)
                if track is not None:
                    tracks.append(track)
        if not tracks:
            return None

        search_type = SearchType.SONGS
        with contextlib.suppress(ValueError):
            if data.get("type"):
                search_type = SearchType(data["type"])
        return SearchResult(
            query=str(data.get("query") or key),
            tracks=tuple(tracks),
            search_type=search_type,
        )

    def set(self, key: str, result: SearchResult) -> None:
        payload = {
            "query": result.query,
            "type": result.search_type.value,
            "results": [t.to_dict() for t in result.tracks],
        }
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            self._prune()
            fd, tmp_name = tempfile.mkstemp(dir=str(self._dir), prefix=".tmp-", suffix=".json")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(payload, handle, ensure_ascii=False)
                Path(tmp_name).replace(self._path(key))
            except BaseException:
                with contextlib.suppress(OSError):
                    Path(tmp_name).unlink()
                raise
        except OSError as exc:
            log.debug("Disk cache not writable: %s", exc)

    def _prune(self) -> None:
        """Keep the cache directory bounded.

        Called before a write, so one slot is reserved for the entry about to
        be created; otherwise the directory settles at ``max_entries + 1``.
        """
        budget = max(0, self._max - 1)
        try:
            files = sorted(self._dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        except OSError:
            return
        for path in files[budget:]:
            with contextlib.suppress(OSError):
                path.unlink()

    def clear(self) -> None:
        with contextlib.suppress(OSError):
            for path in self._dir.glob("*.json"):
                path.unlink()


def search_cache_key(query: str, search_type: SearchType, limit: int) -> str:
    return f"search:{search_type.value}:{limit}:{query.strip().lower()}"


def info_cache_key(video_id: str) -> str:
    return f"info:{validate_video_id(video_id)}"
