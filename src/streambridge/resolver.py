"""Stream resolution layer.

Sits between the HTTP layer and the extractor and owns:

* per-id resolution locks, so N concurrent requests for the same track trigger
  exactly one upstream call instead of N
* short-lived caching of resolved sources
* re-resolution whenever a cached URL looks expired or a request fails
* local rate limiting to guard against accidental request storms

The HTTP layer never calls the extractor directly.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque

from .cache import STREAM_TTL_SECONDS, TtlCache
from .config import Config
from .errors import RateLimitError, SourceUnavailableError
from .models import StreamInfo, validate_video_id
from .youtube import ExtractorClient

log = logging.getLogger("streambridge.resolver")

# How long a lock holder may hold the lock before waiters give up and try
# themselves rather than queue indefinitely.
_LOCK_WAIT_LIMIT = 1.5
# Upper bound on the lock table for long-running servers.
_MAX_LOCKS = 512


class TokenBucket:
    """Simple sliding-window limiter.

    Deliberately permissive: it exists to stop accidental request storms, not
    to throttle normal listening.
    """

    def __init__(self, limit_per_minute: int) -> None:
        self._limit = max(1, limit_per_minute)
        self._events: deque[float] = deque()
        self._lock = threading.Lock()

    def check(self, what: str) -> None:
        """Raise :class:`RateLimitError` when the budget for this minute is spent."""
        now = time.monotonic()
        with self._lock:
            while self._events and now - self._events[0] > 60.0:
                self._events.popleft()
            if len(self._events) >= self._limit:
                oldest = self._events[0]
                retry_in = max(1.0, 60.0 - (now - oldest))
                raise RateLimitError(
                    f"Local rate limit reached for {what} ({self._limit}/minute).",
                    hint=(
                        f"Retry in about {retry_in:.0f}s, or raise the limit in the configuration."
                    ),
                )
            self._events.append(now)


class StreamResolver:
    """Turns a track id into a currently valid :class:`StreamInfo`."""

    def __init__(self, config: Config, client: ExtractorClient) -> None:
        self._config = config
        self._client = client
        self._cache = TtlCache(max_entries=64, ttl=STREAM_TTL_SECONDS)
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()
        self._limiter = TokenBucket(config.resolve_rate_per_min)

    def _lock_for(self, video_id: str) -> threading.Lock:
        """One lock per id, created on demand.

        A single global lock is explicitly avoided: unrelated tracks must be
        able to resolve in parallel.
        """
        with self._locks_guard:
            lock = self._locks.get(video_id)
            if lock is None:
                lock = threading.Lock()
                self._locks[video_id] = lock
                if len(self._locks) > _MAX_LOCKS:
                    for stale in list(self._locks)[:128]:
                        candidate = self._locks.get(stale)
                        if candidate is not None and not candidate.locked():
                            self._locks.pop(stale, None)
            return lock

    def resolve(self, video_id: str, *, rate_limit: bool = True) -> StreamInfo:
        """Return a usable audio source for *video_id*.

        Concurrent callers for the same id share one upstream call. A cached
        source that fails is discarded so the next attempt re-resolves.
        """
        vid = validate_video_id(video_id)
        if rate_limit:
            self._limiter.check("stream resolution")

        cached = self._cache.get(vid)
        if isinstance(cached, StreamInfo) and not cached.is_probably_expired(time.time()):
            return cached

        lock = self._lock_for(vid)
        acquired = lock.acquire(timeout=_LOCK_WAIT_LIMIT)
        try:
            if acquired:
                # Double check: another thread may have resolved meanwhile.
                cached = self._cache.get(vid)
                if isinstance(cached, StreamInfo) and not cached.is_probably_expired(time.time()):
                    return cached
            try:
                info = self._client.resolve_stream(vid)
            except SourceUnavailableError:
                # Never let a cached-but-dead entry poison future attempts.
                self._cache.invalidate(vid)
                raise
            log.info(
                "resolved %s (format=%s, container=%s, expires=%s)",
                vid,
                info.format_id,
                info.ext,
                "known" if info.expires_at else "unknown",
            )
            self._cache.set(vid, info)
            return info
        finally:
            if acquired:
                lock.release()

    def invalidate(self, video_id: str) -> None:
        """Forget a cached source, forcing re-resolution on next use."""
        self._cache.invalidate(validate_video_id(video_id))

    def stats(self) -> dict[str, int]:
        return self._cache.stats()
