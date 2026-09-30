"""Live tests against the real upstream service.

Never part of the default test run: they need network access and are subject
to upstream rate limits.

    python -m pytest tests/live -m live -v

These tests resolve public metadata for a single well-known, freely licensed
open-movie track and discard it. No copyrighted material is stored in this
repository.
"""

from __future__ import annotations

import pytest

from streambridge.config import config_from_mapping
from streambridge.errors import SourceUnavailableError
from streambridge.models import SearchType
from streambridge.resolver import StreamResolver
from streambridge.streamer import is_allowed_upstream
from streambridge.youtube import ExtractorClient

pytestmark = pytest.mark.live

# Blender Foundation open-movie audio: freely licensed and stable for years.
PUBLIC_VIDEO_ID = "aqz-KE-bpKQ"


@pytest.fixture(scope="module")
def client() -> ExtractorClient:
    config = config_from_mapping({"timeouts": {"request": 60.0, "resolve": 60.0, "info": 60.0}})
    return ExtractorClient(config)


def test_extractor_present() -> None:
    assert ExtractorClient(config_from_mapping({})).is_available(), (
        "yt-dlp is missing - install with: pipx install yt-dlp"
    )


def test_search_returns_results(client: ExtractorClient) -> None:
    result = client.search("big buck bunny", limit=3, search_type=SearchType.SONGS)
    assert result.tracks
    for track in result.tracks:
        assert len(track.id) == 11
        assert track.title
        assert track.webpage_url.startswith("https://")


def test_get_info(client: ExtractorClient) -> None:
    track = client.get_info(PUBLIC_VIDEO_ID)
    assert track.id == PUBLIC_VIDEO_ID
    assert track.title
    assert track.duration and track.duration > 0


def test_resolve_stream(client: ExtractorClient) -> None:
    info = client.resolve_stream(PUBLIC_VIDEO_ID)
    assert info.url.startswith("https://")
    # A resolved URL outside the allowlist would be refused at stream time.
    assert is_allowed_upstream(info.url)
    assert info.acodec and info.acodec != "none"
    assert info.headers


def test_resolver_caches_then_reresolves(client: ExtractorClient) -> None:
    resolver = StreamResolver(config_from_mapping({"timeouts": {"resolve": 60.0}}), client)
    first = resolver.resolve(PUBLIC_VIDEO_ID)
    second = resolver.resolve(PUBLIC_VIDEO_ID)
    assert first.url == second.url
    resolver.invalidate(PUBLIC_VIDEO_ID)
    assert resolver.resolve(PUBLIC_VIDEO_ID).url


def test_unavailable_video_raises(client: ExtractorClient) -> None:
    with pytest.raises(SourceUnavailableError):
        client.resolve_stream("aaaaaaaaaaa")
