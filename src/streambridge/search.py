"""Search service.

Separates "what to search for" from "how the extractor is invoked", so the
CLI, the API and the tests can all search without touching yt-dlp directly.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping

from .cache import DiskSearchCache, TtlCache, track_from_dict
from .config import Config
from .errors import ValidationError
from .models import Playlist, SearchResult, SearchType, Track, validate_query
from .resolver import TokenBucket
from .youtube import ExtractorClient

log = logging.getLogger("streambridge.search")


class SearchService:
    """Query the upstream catalogue with caching and rate limiting."""

    def __init__(
        self,
        config: Config,
        client: ExtractorClient,
        *,
        memory_cache: TtlCache | None = None,
        disk_cache: DiskSearchCache | None = None,
    ) -> None:
        self._config = config
        self._client = client
        self._limiter = TokenBucket(config.search_rate_per_min)
        self._memory = memory_cache or TtlCache(
            max_entries=config.cache_max_entries, ttl=config.cache_ttl_seconds
        )
        self._disk = disk_cache or DiskSearchCache(
            config.cache_directory,
            ttl=config.cache_ttl_seconds,
            max_entries=config.cache_max_entries,
        )

    def search(
        self,
        query: str,
        *,
        search_type: SearchType = SearchType.SONGS,
        limit: int | None = None,
    ) -> SearchResult:
        """Return matching tracks, served from cache when possible."""
        clean = validate_query(query)
        # An explicit 0 is a user error, not a request for the default, so the
        # fallback keys on None rather than falsiness.
        count = min(self._config.search_limit if limit is None else limit, 100)
        if count < 1:
            raise ValidationError("limit must be at least 1")
        key = f"search:{search_type.value}:{count}:{clean.lower()}"

        # The budget is charged before the cache is consulted: the limiter
        # guards the endpoint, not the upstream call. Checking it only on a
        # miss would let a loop of identical queries bypass the guard.
        self._limiter.check("search")

        cached = self._memory.get(key)
        if isinstance(cached, SearchResult):
            return cached

        disk = self._disk.get(key)
        if disk is not None:
            self._memory.set(key, disk)
            return disk

        result = self._client.search(clean, limit=count, search_type=search_type)
        self._memory.set(key, result)
        self._disk.set(key, result)
        log.info("search returned %d results for %r", len(result.tracks), clean)
        return result

    def tracks(
        self,
        query: str,
        *,
        search_type: SearchType = SearchType.SONGS,
        limit: int | None = None,
    ) -> list[Track]:
        """Convenience wrapper returning only the track list."""
        return list(self.search(query, search_type=search_type, limit=limit).tracks)

    def playlist(self, playlist_id: str, *, limit: int | None = None) -> Playlist:
        """Return playlist metadata without downloading any media."""
        self._limiter.check("playlist loading")
        cap = self._config.search_limit * 5 if limit is None else limit
        return self._client.playlist(playlist_id, limit=cap)

    @staticmethod
    def tracks_from_payload(payload: Mapping[str, object]) -> list[Track]:
        """Rebuild tracks from an API payload, skipping unusable rows.

        Callers pass decoded JSON, which is untrusted: ``results`` may be
        absent, null, or a list of scalars rather than objects.
        """
        rows = payload.get("results")
        if not isinstance(rows, list):
            return []
        out: list[Track] = []
        for item in rows:
            if isinstance(item, dict):
                track = track_from_dict(item)
                if track is not None:
                    out.append(track)
        return out
