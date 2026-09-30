"""Cache behaviour: TTL, LRU bounds, disk persistence, hostile keys."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from streambridge.cache import (
    STREAM_TTL_SECONDS,
    DiskSearchCache,
    TtlCache,
    cache_filename,
    info_cache_key,
    search_cache_key,
    track_from_dict,
)
from streambridge.errors import ValidationError
from streambridge.models import SearchResult, SearchType, Track

pytestmark = pytest.mark.unit


class TestTtlCache:
    def test_set_and_get(self) -> None:
        cache = TtlCache(max_entries=4, ttl=60)
        cache.set("a", 1)
        assert cache.get("a") == 1

    def test_miss_returns_none(self) -> None:
        assert TtlCache().get("nope") is None

    def test_expired_entry_not_returned(self) -> None:
        cache = TtlCache(max_entries=4, ttl=0.01)
        cache.set("a", 1)
        time.sleep(0.05)
        assert cache.get("a") is None

    def test_ttl_zero_means_no_expiry(self) -> None:
        cache = TtlCache(max_entries=4, ttl=0)
        cache.set("a", 1)
        time.sleep(0.02)
        assert cache.get("a") == 1

    def test_lru_bound_enforced(self) -> None:
        cache = TtlCache(max_entries=3, ttl=60)
        for key in "abcd":
            cache.set(key, key)
        assert len(cache) == 3
        assert cache.get("a") is None  # evicted first

    def test_lru_evicts_least_recently_used(self) -> None:
        cache = TtlCache(max_entries=2, ttl=60)
        cache.set("a", 1)
        cache.set("b", 2)
        cache.get("a")  # refresh a
        cache.set("c", 3)
        assert cache.get("a") == 1
        assert cache.get("b") is None

    def test_invalidate(self) -> None:
        cache = TtlCache()
        cache.set("a", 1)
        cache.invalidate("a")
        assert cache.get("a") is None

    def test_clear(self) -> None:
        cache = TtlCache()
        cache.set("a", 1)
        cache.clear()
        assert len(cache) == 0

    def test_get_or_compute(self) -> None:
        calls: list[int] = []

        def factory() -> int:
            calls.append(1)
            return 42

        cache = TtlCache()
        assert cache.get_or_compute("k", factory) == 42
        assert cache.get_or_compute("k", factory) == 42
        assert len(calls) == 1

    def test_get_or_compute_does_not_cache_none(self) -> None:
        calls: list[int] = []

        def factory() -> None:
            calls.append(1)
            return None

        cache = TtlCache()
        cache.get_or_compute("k", factory)
        cache.get_or_compute("k", factory)
        assert len(calls) == 2

    def test_stats(self) -> None:
        cache = TtlCache()
        cache.set("a", 1)
        cache.get("a")
        cache.get("b")
        stats = cache.stats()
        assert stats == {"entries": 1, "hits": 1, "misses": 1}

    def test_concurrent_access_is_safe(self) -> None:
        cache = TtlCache(max_entries=50, ttl=60)
        errors: list[Exception] = []

        def worker(offset: int) -> None:
            try:
                for i in range(100):
                    key = f"k{(i + offset) % 60}"
                    cache.set(key, i)
                    cache.get(key)
                    len(cache)
            except Exception as exc:  # pragma: no cover - failure path
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        assert not errors
        assert len(cache) <= 50


class TestCacheKeys:
    def test_search_key_is_case_insensitive(self) -> None:
        assert search_cache_key("Artist One", SearchType.SONGS, 20) == search_cache_key(
            "  artist one ", SearchType.SONGS, 20
        )

    def test_search_key_includes_type_and_limit(self) -> None:
        base = search_cache_key("x", SearchType.SONGS, 20)
        assert search_cache_key("x", SearchType.VIDEOS, 20) != base
        assert search_cache_key("x", SearchType.SONGS, 5) != base

    def test_info_key_validates(self) -> None:
        assert info_cache_key("5NV6Rdv1a3I") == "info:5NV6Rdv1a3I"
        with pytest.raises(ValidationError):
            info_cache_key("../evil")

    def test_filename_is_hashed(self) -> None:
        # Hashing means no user input can influence a filename, so path
        # traversal through the cache key is impossible by construction.
        name = cache_filename("../../../../etc/passwd")
        assert "/" not in name
        assert name.endswith(".json")
        assert len(name) == 69  # 64 hex chars + ".json"

    def test_filename_is_stable(self) -> None:
        assert cache_filename("abc") == cache_filename("abc")

    def test_stream_ttl_is_short(self) -> None:
        # Media URLs expire; a cached one must not outlive its usefulness.
        assert 0 < STREAM_TTL_SECONDS <= 300


def sample_result() -> SearchResult:
    return SearchResult(
        query="artist one",
        tracks=(
            Track(
                id="5NV6Rdv1a3I",
                title="First Song",
                artist="Artist One",
                album="Some Album",
                duration=249,
                webpage_url="https://www.youtube.com/watch?v=5NV6Rdv1a3I",
            ),
            Track(id="CCHdMIEGaaM", title="Second Song", duration=320),
        ),
        search_type=SearchType.SONGS,
    )


class TestDiskSearchCache:
    def test_roundtrip(self, tmp_path: Path) -> None:
        cache = DiskSearchCache(tmp_path / "c", ttl=60)
        cache.set("k", sample_result())
        loaded = cache.get("k")
        assert loaded is not None
        assert loaded.query == "artist one"
        assert [t.id for t in loaded.tracks] == ["5NV6Rdv1a3I", "CCHdMIEGaaM"]
        assert loaded.tracks[0].album == "Some Album"
        assert loaded.search_type is SearchType.SONGS

    def test_missing_key(self, tmp_path: Path) -> None:
        assert DiskSearchCache(tmp_path / "c").get("absent") is None

    def test_expired_on_disk(self, tmp_path: Path) -> None:
        cache = DiskSearchCache(tmp_path / "c", ttl=0.01)
        cache.set("k", sample_result())
        time.sleep(0.05)
        assert cache.get("k") is None

    def test_corrupt_file_ignored(self, tmp_path: Path) -> None:
        directory = tmp_path / "c"
        cache = DiskSearchCache(directory, ttl=60)
        cache.set("k", sample_result())
        for path in directory.glob("*.json"):
            path.write_text("{broken", encoding="utf-8")
        assert cache.get("k") is None

    def test_non_dict_payload_ignored(self, tmp_path: Path) -> None:
        directory = tmp_path / "c"
        cache = DiskSearchCache(directory, ttl=60)
        cache.set("k", sample_result())
        for path in directory.glob("*.json"):
            path.write_text("[1, 2, 3]", encoding="utf-8")
        assert cache.get("k") is None

    def test_all_tracks_invalid_ignored(self, tmp_path: Path) -> None:
        directory = tmp_path / "c"
        cache = DiskSearchCache(directory, ttl=60)
        cache.set("k", sample_result())
        for path in directory.glob("*.json"):
            path.write_text('{"results": [{"id": "bad"}]}', encoding="utf-8")
        assert cache.get("k") is None

    def test_unknown_search_type_falls_back(self, tmp_path: Path) -> None:
        directory = tmp_path / "c"
        cache = DiskSearchCache(directory, ttl=60)
        cache.set("k", sample_result())
        for path in directory.glob("*.json"):
            path.write_text(
                '{"query": "q", "type": "from-a-future-version", '
                '"results": [{"id": "5NV6Rdv1a3I", "title": "T"}]}',
                encoding="utf-8",
            )
        loaded = cache.get("k")
        # A newer type name must not invalidate an otherwise usable entry.
        assert loaded is not None
        assert loaded.search_type is SearchType.SONGS

    def test_hostile_key_cannot_escape_directory(self, tmp_path: Path) -> None:
        directory = tmp_path / "cache"
        cache = DiskSearchCache(directory, ttl=60)
        cache.set("../../../../etc/passwd", sample_result())
        # Every written file must live inside the cache directory.
        assert list(directory.glob("*.json"))
        assert not list(tmp_path.parent.glob("passwd*.json"))

    def test_only_stable_fields_persisted(self, tmp_path: Path) -> None:
        directory = tmp_path / "c"
        cache = DiskSearchCache(directory, ttl=60)
        cache.set("k", sample_result())
        raw = json.loads(next(directory.glob("*.json")).read_text(encoding="utf-8"))
        serialised = json.dumps(raw)
        # No media or stream URL may ever be written to disk.
        assert "googlevideo" not in serialised
        assert "/stream/" not in serialised

    def test_prune_bounds_directory(self, tmp_path: Path) -> None:
        directory = tmp_path / "c"
        cache = DiskSearchCache(directory, ttl=60, max_entries=5)
        for i in range(20):
            cache.set(f"key{i}", sample_result())
        assert len(list(directory.glob("*.json"))) <= 5

    def test_clear(self, tmp_path: Path) -> None:
        directory = tmp_path / "c"
        cache = DiskSearchCache(directory, ttl=60)
        cache.set("k", sample_result())
        cache.clear()
        assert not list(directory.glob("*.json"))

    def test_unwritable_directory_is_survivable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cache = DiskSearchCache(tmp_path / "c", ttl=60)

        def refuse(*_a: object, **_k: object) -> None:
            raise OSError("read-only filesystem")

        monkeypatch.setattr(Path, "mkdir", refuse)
        # A cache that cannot be written must never break a search.
        cache.set("k", sample_result())
        assert cache.get("k") is None


class TestTrackFromDict:
    def test_roundtrip(self) -> None:
        original = sample_result().tracks[0]
        assert track_from_dict(original.to_dict()) == original

    def test_missing_optional_fields(self) -> None:
        track = track_from_dict({"id": "5NV6Rdv1a3I", "title": "T"})
        assert track is not None
        assert track.artist is None
        assert track.album is None
        assert track.duration is None
        # A missing URL is reconstructed from the id rather than left empty.
        assert track.webpage_url.endswith("v=5NV6Rdv1a3I")

    def test_invalid_id_rejected(self) -> None:
        assert track_from_dict({"id": "../etc", "title": "x"}) is None
        assert track_from_dict({}) is None
        assert track_from_dict({"id": None, "title": "x"}) is None

    def test_title_defaults_to_id(self) -> None:
        track = track_from_dict({"id": "5NV6Rdv1a3I"})
        assert track is not None
        assert track.title == "5NV6Rdv1a3I"

    def test_bad_duration_coerced(self) -> None:
        track = track_from_dict({"id": "5NV6Rdv1a3I", "title": "T", "duration": "abc"})
        assert track is not None
        assert track.duration is None

    def test_bool_duration_rejected(self) -> None:
        track = track_from_dict({"id": "5NV6Rdv1a3I", "title": "T", "duration": True})
        assert track is not None
        assert track.duration is None

    def test_negative_duration_rejected(self) -> None:
        track = track_from_dict({"id": "5NV6Rdv1a3I", "title": "T", "duration": -5})
        assert track is not None
        assert track.duration is None

    def test_wrong_types_for_optional_fields_rejected(self) -> None:
        track = track_from_dict({"id": "5NV6Rdv1a3I", "title": "T", "artist": 42, "album": ["x"]})
        assert track is not None
        assert track.artist is None
        assert track.album is None

    def test_display_helpers(self) -> None:
        track = track_from_dict(
            {
                "id": "5NV6Rdv1a3I",
                "title": "T",
                "uploader": "Uploader",
                "channel": "Channel",
            }
        )
        assert track is not None
        assert track.display_artist == "Uploader"
        assert track.display_album == "Unknown Album"
