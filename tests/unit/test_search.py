"""Search service: caching, rate limiting, input validation."""

from __future__ import annotations

import pytest

from conftest import FAKE_EXECUTABLE, SEARCH_PAYLOAD, FakeRunner
from streambridge.cache import DiskSearchCache, TtlCache
from streambridge.config import Config
from streambridge.errors import RateLimitError, ValidationError
from streambridge.models import SearchResult, SearchType
from streambridge.search import SearchService
from streambridge.youtube import ExtractorClient

pytestmark = pytest.mark.unit

VALID_ID = "5NV6Rdv1a3I"
VALID_PLAYLIST = "PLrAXtmErZgOeiKm4sgNOknGvNjby9efdf"


def build(config: Config, runner: FakeRunner) -> tuple[SearchService, FakeRunner]:
    client = ExtractorClient(config, runner=runner, executable=FAKE_EXECUTABLE)
    return SearchService(config, client), runner


class TestSearch:
    def test_returns_results(self, config: Config, fake_runner: FakeRunner) -> None:
        fake_runner.add_json("ytsearch", SEARCH_PAYLOAD)
        service, _runner = build(config, fake_runner)
        result = service.search("Artist One")
        assert isinstance(result, SearchResult)
        assert [t.id for t in result.tracks] == [VALID_ID, "CCHdMIEGaaM"]

    def test_tracks_helper(self, config: Config, fake_runner: FakeRunner) -> None:
        fake_runner.add_json("ytsearch", SEARCH_PAYLOAD)
        service, _runner = build(config, fake_runner)
        assert len(service.tracks("Artist One")) == 2

    def test_query_trimmed(self, config: Config, fake_runner: FakeRunner) -> None:
        fake_runner.add_json("ytsearch", SEARCH_PAYLOAD)
        service, _runner = build(config, fake_runner)
        assert service.search("  Artist One  ").query == "Artist One"

    @pytest.mark.parametrize("query", ["", "   ", "a\nb", "a\x00b", "x" * 500])
    def test_invalid_queries_rejected(
        self, config: Config, fake_runner: FakeRunner, query: str
    ) -> None:
        service, _runner = build(config, fake_runner)
        with pytest.raises(ValidationError):
            service.search(query)
        assert fake_runner.calls == []

    def test_limit_bounds_applied(self, config: Config, fake_runner: FakeRunner) -> None:
        fake_runner.add_json("ytsearch", SEARCH_PAYLOAD)
        service, _runner = build(config, fake_runner)
        service.search("x", limit=5000)
        assert "ytsearch100:" in " ".join(fake_runner.calls[-1])

    def test_zero_limit_rejected(self, config: Config, fake_runner: FakeRunner) -> None:
        service, _runner = build(config, fake_runner)
        with pytest.raises(ValidationError):
            service.search("x", limit=0)

    def test_memory_cache_hit(self, config: Config, fake_runner: FakeRunner) -> None:
        fake_runner.add_json("ytsearch", SEARCH_PAYLOAD)
        service, _runner = build(config, fake_runner)
        service.search("Artist One")
        before = len(fake_runner.calls)
        service.search("Artist One")
        assert len(fake_runner.calls) == before

    def test_cache_key_is_case_insensitive(self, config: Config, fake_runner: FakeRunner) -> None:
        fake_runner.add_json("ytsearch", SEARCH_PAYLOAD)
        service, _runner = build(config, fake_runner)
        service.search("Artist One")
        before = len(fake_runner.calls)
        service.search("artist one")
        # A different case is the same question, so it must hit the cache.
        assert len(fake_runner.calls) == before

    def test_disk_cache_hit(self, config: Config, fake_runner: FakeRunner) -> None:
        fake_runner.add_json("ytsearch", SEARCH_PAYLOAD)
        first, _ = build(config, fake_runner)
        first.search("Disk Hit")

        # A fresh service with only the disk cache still answers.
        client = ExtractorClient(config, runner=fake_runner, executable=FAKE_EXECUTABLE)
        second = SearchService(
            config,
            client,
            memory_cache=TtlCache(ttl=config.cache_ttl_seconds),
            disk_cache=DiskSearchCache(
                config.cache_directory,
                ttl=config.cache_ttl_seconds,
                max_entries=config.cache_max_entries,
            ),
        )
        before = len(fake_runner.calls)
        assert second.search("Disk Hit").tracks
        assert len(fake_runner.calls) == before

    def test_rate_limit_trips(self, config: Config, fake_runner: FakeRunner) -> None:
        import dataclasses

        fake_runner.add_json("ytsearch", SEARCH_PAYLOAD)
        limited = dataclasses.replace(config, search_rate_per_min=2)
        service, _runner = build(limited, fake_runner)
        service.search("one")
        service.search("two")
        with pytest.raises(RateLimitError):
            service.search("three")

    def test_cached_search_still_consumes_rate_budget(
        self, config: Config, fake_runner: FakeRunner
    ) -> None:
        # The limiter guards the endpoint, not the cache lookup, so a cached
        # hit still counts. Otherwise an unbounded loop of cached queries
        # would bypass the guard entirely.
        import dataclasses

        fake_runner.add_json("ytsearch", SEARCH_PAYLOAD)
        limited = dataclasses.replace(config, search_rate_per_min=1)
        service, _runner = build(limited, fake_runner)
        service.search("same")
        with pytest.raises(RateLimitError):
            service.search("same")


class TestPlaylist:
    def test_playlist_loaded(self, config: Config, fake_runner: FakeRunner) -> None:
        fake_runner.add_json(
            "playlist?list=",
            {"title": "Mix", "entries": [{"id": "aaaaaaaaaaa", "title": "T"}]},
        )
        service, _runner = build(config, fake_runner)
        playlist = service.playlist(VALID_PLAYLIST)
        assert playlist.title == "Mix"
        assert len(playlist.tracks) == 1

    def test_playlist_limit_from_config(self, config: Config, fake_runner: FakeRunner) -> None:
        fake_runner.add_json("playlist?list=", {"entries": []})
        service, _runner = build(config, fake_runner)
        service.playlist(VALID_PLAYLIST)
        # Default cap is a multiple of the search limit, not unlimited.
        assert "flat-playlist" in " ".join(fake_runner.calls[-1])

    def test_tracks_from_payload(self) -> None:
        tracks = SearchService.tracks_from_payload(payload_with_results())
        assert [t.id for t in tracks] == [VALID_ID, "CCHdMIEGaaM"]

    def test_tracks_from_empty_payload(self) -> None:
        assert SearchService.tracks_from_payload({}) == []


def payload_with_results() -> dict[str, object]:
    return {
        "results": [
            {"id": VALID_ID, "title": "First Song", "artist": "Artist One", "duration": 249},
            {"id": "CCHdMIEGaaM", "title": "Second Song", "duration": 320},
            {"id": "bad"},
        ]
    }


class TestSearchTypePassThrough:
    def test_type_is_forwarded(self, config: Config, fake_runner: FakeRunner) -> None:
        fake_runner.add_json("ytsearch", SEARCH_PAYLOAD)
        service, _runner = build(config, fake_runner)
        result = service.search("x", search_type=SearchType.ALBUMS)
        assert result.search_type is SearchType.ALBUMS
