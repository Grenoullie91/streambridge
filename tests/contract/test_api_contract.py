"""Contract tests for the public HTTP surface.

The unit and integration suites test that each piece behaves. These tests
answer a different question: **can something written against the documented
API still work?** A response key that is renamed, a status code that changes, an
error field that disappears — any of those is a breaking change, and a
breaking change should fail here loudly rather than surface as a blank page in
someone's browser.

Nothing here is a duplicate of a behavioural test. Each case pins a *shape*, so
that the behaviour tests are free to keep evolving behind it.
"""

from __future__ import annotations

import dataclasses
import json
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from conftest import INFO_PAYLOAD, SEARCH_PAYLOAD, FakeMpd, FakeRunner
from streambridge.api import ApiService, make_server
from streambridge.config import Config
from streambridge.errors import (
    DependencyError,
    MpdError,
    RateLimitError,
    SourceUnavailableError,
    StreamBridgeError,
    ValidationError,
)
from streambridge.resolver import StreamResolver
from streambridge.youtube import ExtractorClient

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
VIDEO = "5NV6Rdv1a3I"
SECOND = "CCHdMIEGaaM"


class ContractClient:
    """Minimal HTTP client, so the contract is asserted over the wire.

    Deliberately built on urllib rather than reusing the test suite's client:
    this file is asserting what an *outside* program would see, including the
    status line and the headers.
    """

    def __init__(self, base: str) -> None:
        self.base = base

    def call(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        follow: bool = True,
    ) -> tuple[int, Any, dict[str, str]]:
        import urllib.error
        import urllib.request

        data = json.dumps(payload).encode() if payload is not None else None
        request = urllib.request.Request(self.base + path, data=data, method=method)
        request.add_header("Accept", "*/*")
        for key, value in (headers or {}).items():
            request.add_header(key, value)
        # A no-redirect opener, so a 302 can be asserted. Following it would
        # leave the test asserting on the upstream image host's response, which
        # is both a network call and not what the contract is about.
        if not follow:

            class NoRedirect(urllib.request.HTTPRedirectHandler):
                def redirect_request(self, *args: Any, **kwargs: Any) -> None:
                    return None

            opener = urllib.request.build_opener(NoRedirect)
        else:
            opener = urllib.request.build_opener()
        try:
            with opener.open(request, timeout=10) as response:
                body = response.read()
                return response.status, _decode(body, response.headers), dict(response.headers)
        except urllib.error.HTTPError as exc:
            body = exc.read()
            return exc.code, _decode(body, exc.headers), dict(exc.headers)

    def get(self, path: str, **kwargs: Any) -> tuple[int, Any, dict[str, str]]:
        return self.call("GET", path, **kwargs)

    def post(self, path: str, payload: dict[str, Any] | None = None, **kwargs: Any):
        return self.call("POST", path, payload, **kwargs)


def _decode(body: bytes, headers: Any) -> Any:
    if "json" in (headers.get("Content-Type") or ""):
        return json.loads(body) if body else None
    return body.decode("utf-8", "replace")


@pytest.fixture
def server(config: Config, fake_runner: FakeRunner) -> Iterator[ContractClient]:
    fake_runner.add_json("ytsearch", SEARCH_PAYLOAD)
    fake_runner.add_json("watch?v=", INFO_PAYLOAD)
    client = ExtractorClient(config, runner=fake_runner, executable="yt-dlp")
    service = ApiService(
        config,
        client=client,
        resolver=StreamResolver(config, client),
        mpd=FakeMpd(config),  # type: ignore[arg-type]
    )
    httpd = make_server(dataclasses.replace(config, port=0), service=service)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield ContractClient(f"http://127.0.0.1:{httpd.server_address[1]}")
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


# --------------------------------------------------------------------- reads
class TestReadContract:
    def test_banner_keys(self, server: ContractClient) -> None:
        status, body, _ = server.get("/")
        assert status == 200
        assert set(body) >= {"service", "version", "endpoints"}
        assert body["service"] == "streambridge"
        # The endpoint list is documentation a client can read at runtime.
        assert isinstance(body["endpoints"], list) and body["endpoints"]

    def test_health_keys(self, server: ContractClient) -> None:
        status, body, _ = server.get("/health")
        assert status == 200
        assert set(body) == {"status", "version", "extractor", "uptime_seconds"}

    def test_version_keys(self, server: ContractClient) -> None:
        status, body, _ = server.get("/version")
        assert status == 200
        assert set(body) == {"service", "version", "extractor"}

    def test_search_envelope(self, server: ContractClient) -> None:
        status, body, _ = server.get("/search?q=artist&type=songs&limit=5")
        assert status == 200
        assert "results" in body
        assert isinstance(body["results"], list)
        if body["results"]:
            # A result must be renderable by a client that knows nothing else.
            assert set(body["results"][0]) >= {"id", "title", "duration", "thumbnail", "url"}

    def test_track_info_keys(self, server: ContractClient) -> None:
        status, body, _ = server.get(f"/info/{VIDEO}")
        assert status == 200
        assert body["id"] == VIDEO
        # Track.to_dict() exposes the page URL as "url", not "webpage_url".
        # Pinned here because renaming it is a silent break for every client.
        assert set(body) >= {"id", "title", "duration", "url", "thumbnail"}

    def test_player_status_keys(self, server: ContractClient) -> None:
        status, body, _ = server.get("/player/status")
        assert status == 200
        # Every key a player panel needs, even when nothing is playing.
        assert set(body) >= {
            "state",
            "position",
            "current",
            "playlist_length",
            "elapsed",
            "duration",
            "volume",
            "muted",
            "shuffle",
            "repeat",
            "repeat_one",
            "consume",
        }
        assert body["state"] in {"playing", "paused", "stopped"}

    def test_queue_envelope(self, server: ContractClient) -> None:
        status, body, _ = server.get("/queue")
        assert status == 200
        assert set(body) >= {"items", "length"}
        assert body["length"] == len(body["items"])

    def test_favorites_envelope(self, server: ContractClient) -> None:
        status, body, _ = server.get("/favorites")
        assert status == 200
        # "ids" exists so a client can test membership without walking items.
        assert set(body) >= {"items", "ids", "length"}
        assert body["length"] == len(body["items"]) == len(body["ids"])

    def test_history_envelope(self, server: ContractClient) -> None:
        status, body, _ = server.get("/history")
        assert status == 200
        assert set(body) >= {"items", "length"}

    def test_stats_envelope(self, server: ContractClient) -> None:
        status, body, _ = server.get("/stats")
        assert status == 200
        assert set(body) >= {"search_cache", "info_cache", "stream_cache"}


# -------------------------------------------------------------------- writes
class TestWriteContract:
    def test_queue_add_returns_the_new_queue(self, server: ContractClient) -> None:
        status, body, _ = server.post(
            "/queue/add",
            {"ids": [VIDEO], "tracks": [{"id": VIDEO, "title": "First", "duration": 249}]},
        )
        assert status == 201
        assert body["added"] == 1
        # The whole queue comes back, so a client never has to re-fetch.
        assert set(body) >= {"queue", "added"}
        assert body["queue"]["length"] == 1

    def test_queue_rows_are_renderable(self, server: ContractClient) -> None:
        _, body, _ = server.post("/queue/add", {"ids": [VIDEO]})
        row = body["queue"]["items"][0]
        # A row carries everything the UI needs to draw a track: no second
        # request per row. "song_id" is MPD's own id, needed for the transport
        # commands, and "playable" says whether the row can be started at all.
        assert set(row) >= {
            "position",
            "video_id",
            "song_id",
            "title",
            "artist",
            "duration",
            "thumbnail",
            "playable",
        }

    def test_every_player_command_answers_with_the_status(self, server: ContractClient) -> None:
        server.post("/queue/add", {"ids": [VIDEO], "play": True})
        for path, payload in (
            ("/player/pause", {}),
            ("/player/play", {}),
            ("/player/seek", {"seconds": 5}),
            ("/player/volume", {"volume": 40}),
            ("/player/mute", {"muted": True}),
            ("/player/modes", {"shuffle": True, "repeat": True, "repeat_one": False}),
        ):
            status, body, _ = server.post(path, payload)
            assert status == 200, (path, body)
            # A transport command answers with the resulting state, so the UI
            # paints from one response instead of guessing then re-reading.
            assert "state" in body, path

    def test_favorite_toggle_reports_the_new_state(self, server: ContractClient) -> None:
        status, body, _ = server.post("/favorites/toggle", {"id": VIDEO})
        assert status == 200
        assert body["favorite"] is True
        _, listing, _ = server.get("/favorites")
        assert VIDEO in listing["ids"]


# -------------------------------------------------------------------- errors
class TestErrorContract:
    """The error envelope is a contract too, and the one clients rely on most."""

    def test_error_body_shape(self, server: ContractClient) -> None:
        status, body, _ = server.post("/player/seek", {})
        assert status == 400
        assert set(body) >= {"code", "message"}
        assert body["code"] == "VALIDATION_ERROR"

    def test_known_codes(self, server: ContractClient) -> None:
        cases = {
            "/player/seek": ({"seconds": "soon"}, "VALIDATION_ERROR"),
            "/favorites/toggle": ({"id": "not-a-video-id"}, "VALIDATION_ERROR"),
        }
        for path, (payload, code) in cases.items():
            _, body, _ = server.post(path, payload)
            assert body["code"] == code, (path, body)

    @pytest.mark.parametrize(
        ("error", "expected"),
        [
            # A missing mpc is the service being unable to do its job, not a
            # bug in it. Reporting 500 sends a reader hunting for a fault in
            # this server when the answer is `apt install mpd-client`. Found
            # because a CI runner without mpc produced exactly that.
            (DependencyError("mpc was not found."), 503),
            (MpdError("MPD did not answer."), 503),
            (SourceUnavailableError("upstream is down."), 502),
            (RateLimitError("too many."), 429),
            (ValidationError("bad input."), 400),
        ],
    )
    def test_each_typed_error_maps_to_its_status(
        self, error: StreamBridgeError, expected: int
    ) -> None:
        # Pinned as a table rather than exercised through a real missing
        # dependency, so it holds on a machine that happens to have mpc.
        from streambridge.api import ApiHandler

        assert ApiHandler._status_for(error) == expected

    def test_not_found(self, server: ContractClient) -> None:
        status, body, _ = server.get("/no-such-path")
        assert status == 404
        assert body["code"] == "NOT_FOUND"

    def test_method_not_allowed_carries_allow(self, server: ContractClient) -> None:
        status, _, headers = server.post("/app.js", {})
        assert status == 405
        # Without Allow, a client cannot tell "wrong method" from "wrong path".
        assert "GET" in headers.get("Allow", "")


# ------------------------------------------------------------------- headers
class TestHeaderContract:
    """Security headers are asserted for every path, not just the web UI.

    One forgotten call site is how a CSP quietly stops applying.
    """

    PATHS = ("/health", "/app.js", "/no-such-path", "/thumbnail/" + VIDEO)

    @pytest.mark.parametrize("path", PATHS)
    def test_security_headers_are_always_present(self, server: ContractClient, path: str) -> None:
        # follow=False: the thumbnail answers 302, and the headers under test
        # are the ones StreamBridge sends, not the ones the image host does.
        _, _, headers = server.get(path, follow=False)
        assert headers.get("X-Content-Type-Options") == "nosniff"
        assert headers.get("X-Frame-Options") == "DENY"
        assert headers.get("Referrer-Policy") == "no-referrer"
        csp = headers.get("Content-Security-Policy", "")
        assert "default-src 'self'" in csp
        # A strict CSP is only strict if these are absent.
        assert "unsafe-inline" not in csp
        assert "unsafe-eval" not in csp

    def test_json_responses_are_not_cached(self, server: ContractClient) -> None:
        _, _, headers = server.get("/player/status")
        assert headers.get("Cache-Control") == "no-store"

    def test_thumbnail_redirects_to_the_upstream_host(self, server: ContractClient) -> None:
        status, _, headers = server.get(f"/thumbnail/{VIDEO}", follow=False)
        # A redirect, not a proxy: image bytes never pass through this process,
        # and the Location is built from an already validated id, so nothing the
        # client sent can steer it.
        assert status == 302
        assert headers["Location"] == f"https://i.ytimg.com/vi/{VIDEO}/hqdefault.jpg"


# ------------------------------------------------------------------- fixtures
class TestFixtureIntegrity:
    """The fixtures are shared input, so their validity is itself a contract."""

    def test_search_fixture_is_valid(self) -> None:
        payload = json.loads((FIXTURES / "search_results.json").read_text())
        assert isinstance(payload.get("entries"), list)
        assert payload["entries"], "the fixture must contain at least one entry"
        for entry in payload["entries"]:
            assert "id" in entry and "title" in entry

    def test_info_fixture_is_valid(self) -> None:
        payload = json.loads((FIXTURES / "track_info.json").read_text())
        assert payload["id"] == VIDEO
        assert isinstance(payload["formats"], list) and payload["formats"]
        for entry in payload["formats"]:
            assert "url" in entry, "a format without a url cannot be resolved"

    def test_fixtures_carry_no_live_media_url(self) -> None:
        """No fixture may contain a media URL that could still resolve.

        A test that accidentally reached one would hang, or worse, succeed
        unpredictably. Where an expiry is present it must be far in the future;
        a URL without one cannot resolve at all, because the signature is
        incomplete.
        """
        payload = json.loads((FIXTURES / "track_info.json").read_text())
        for entry in payload["formats"]:
            url = entry["url"]
            if "expire=" in url:
                expire = int(url.split("expire=", 1)[1].split("&", 1)[0])
                assert expire > 4_000_000_000, f"fixture URL may still resolve: {url}"
