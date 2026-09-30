"""Streaming path end to end: /stream/<id> -> upstream -> client.

The upstream is a real local HTTP server, so Range handling, 206 responses,
Content-Range, header forwarding and the retry on a rejected signature are all
exercised over real sockets. Only the host allowlist is relaxed, and only for
127.0.0.1 - everything else in the code path is the production behaviour.
"""

from __future__ import annotations

import dataclasses
import json
import threading
import urllib.error
import urllib.request
from collections.abc import Iterator
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, ClassVar

import pytest

from conftest import INFO_PAYLOAD, SEARCH_PAYLOAD, FakeRunner
from streambridge.api import ApiService, make_server
from streambridge.config import Config
from streambridge.models import StreamInfo
from streambridge.resolver import StreamResolver
from streambridge.streamer import ALLOWED_HOST_SUFFIXES
from streambridge.youtube import ExtractorClient

VIDEO = "5NV6Rdv1a3I"
PAYLOAD = bytes(range(256)) * 64  # 16 KiB, enough to span several ranges


class FakeUpstream(BaseHTTPRequestHandler):
    """Minimal byte server with correct Range semantics.

    ``state`` is shared with the test: ``fail_first`` makes the first request
    answer 403, which is how YouTube rejects a signature that expired early.
    """

    protocol_version = "HTTP/1.1"
    state: ClassVar[dict[str, Any]] = {"fail_first": 0, "requests": 0, "ranges": []}

    def log_message(self, *_args: Any) -> None:
        return

    def do_HEAD(self) -> None:
        self._serve(head_only=True)

    def do_GET(self) -> None:
        self._serve(head_only=False)

    def _serve(self, *, head_only: bool) -> None:
        type(self).state["requests"] += 1
        if type(self).state["fail_first"] > 0:
            type(self).state["fail_first"] -= 1
            self.send_response(HTTPStatus.FORBIDDEN)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        rng = self.headers.get("Range")
        type(self).state["ranges"].append(rng)
        start, end = 0, len(PAYLOAD) - 1
        partial = False
        if rng and rng.startswith("bytes="):
            raw = rng.removeprefix("bytes=")
            first, _, last = raw.partition("-")
            if first:
                start = int(first)
                end = int(last) if last else end
                partial = True
            elif last:
                start = max(0, len(PAYLOAD) - int(last))
                partial = True
            start = min(start, end)
        chunk = PAYLOAD[start : end + 1]

        self.send_response(HTTPStatus.PARTIAL_CONTENT if partial else HTTPStatus.OK)
        self.send_header("Content-Type", "audio/mp4")
        self.send_header("Content-Length", str(len(chunk)))
        if partial:
            self.send_header("Content-Range", f"bytes {start}-{end}/{len(PAYLOAD)}")
        self.send_header("Accept-Ranges", "bytes")
        # Record what the server sent, so the test can prove StreamInfo.headers
        # really reach the upstream.
        type(self).state["user_agent"] = self.headers.get("User-Agent", "")
        type(self).state["origin"] = self.headers.get("Origin", "")
        self.end_headers()
        if not head_only:
            self.wfile.write(chunk)


class UpstreamFixture:
    def __init__(self) -> None:
        FakeUpstream.state = {
            "fail_first": 0,
            "requests": 0,
            "ranges": [],
            "user_agent": "",
            "origin": "",
        }
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeUpstream)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def port(self) -> int:
        return self.server.server_address[1]

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/videoplayback"

    @property
    def state(self) -> dict[str, Any]:
        return FakeUpstream.state

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


class StubResolver:
    """Returns one prepared source and counts how often it was asked."""

    def __init__(self, url: str) -> None:
        self.url = url
        self.calls = 0
        self.invalidations = 0

    def resolve(self, video_id: str, **_kwargs: Any) -> StreamInfo:
        self.calls += 1
        return StreamInfo(
            video_id=video_id,
            url=self.url,
            mime_type="audio/mp4",
            content_length=len(PAYLOAD),
            expires_at=None,
            # A header the server must forward: this is what makes the
            # production code proxy the bytes instead of redirecting to it.
            headers={
                "User-Agent": "Mozilla/5.0 (streambridge-test)",
                "Origin": "https://www.youtube.com",
            },
        )

    def invalidate(self, _video_id: str) -> None:
        self.invalidations += 1

    def stats(self) -> dict[str, int]:
        return {"entries": 1, "hits": 0, "misses": 0}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_a: Any, **_k: Any) -> None:
        return None


@pytest.fixture
def upstream(monkeypatch: pytest.MonkeyPatch) -> Iterator[UpstreamFixture]:
    # The allowlist normally refuses anything but YouTube media hosts. For this
    # test the upstream is 127.0.0.1, so that single entry is added.
    monkeypatch.setattr(
        "streambridge.streamer.ALLOWED_HOST_SUFFIXES", (*ALLOWED_HOST_SUFFIXES, "127.0.0.1")
    )
    fixture = UpstreamFixture()
    try:
        yield fixture
    finally:
        fixture.close()


@pytest.fixture
def client(config: Config, fake_runner: FakeRunner, upstream: UpstreamFixture) -> Iterator[str]:
    fake_runner.add_json("ytsearch", SEARCH_PAYLOAD)
    fake_runner.add_json("watch?v=", INFO_PAYLOAD)
    ytdlp = ExtractorClient(config, runner=fake_runner, executable="yt-dlp")
    service = ApiService(config, client=ytdlp, resolver=StubResolver(upstream.url))  # type: ignore[arg-type]
    # Port 0: the shared config names a fixed port, which would collide with the
    # other integration modules in a full run.
    server = make_server(dataclasses.replace(config, port=0), service=service)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _open(base: str, path: str, headers: dict[str, str] | None = None) -> tuple[int, bytes, Any]:
    request = urllib.request.Request(f"{base}{path}", headers=headers or {})
    opener = urllib.request.build_opener(NoRedirect)
    try:
        with opener.open(request, timeout=20) as response:
            return response.status, response.read(), response.headers
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), exc.headers


class TestFullStream:
    def test_returns_the_whole_body(self, client: str, upstream: UpstreamFixture) -> None:
        status, body, headers = _open(client, f"/stream/{VIDEO}")
        assert status == 200
        assert body == PAYLOAD
        assert headers["Accept-Ranges"] == "bytes"
        assert headers["Content-Length"] == str(len(PAYLOAD))

    def test_forwards_the_resolver_headers(self, client: str, upstream: UpstreamFixture) -> None:
        status, _, _ = _open(client, f"/stream/{VIDEO}")
        assert status == 200
        # The headers yt-dlp reported must reach the upstream, otherwise the
        # source rejects the request.
        assert upstream.state["user_agent"] == "Mozilla/5.0 (streambridge-test)"
        assert upstream.state["origin"] == "https://www.youtube.com"

    def test_content_type_is_audio(self, client: str) -> None:
        _, _, headers = _open(client, f"/stream/{VIDEO}")
        assert headers["Content-Type"].startswith("audio/mp4")

    def test_stream_url_is_loopback_only(self, client: str) -> None:
        status, _, headers = _open(client, f"/stream/{VIDEO}")
        assert status == 200
        # The client sees our own response, never a Location to YouTube.
        assert "Location" not in headers


class TestRangeRequests:
    def test_open_ended_range(self, client: str) -> None:
        status, body, headers = _open(client, f"/stream/{VIDEO}", {"Range": "bytes=100-"})
        assert status == 206
        assert body == PAYLOAD[100:]
        assert headers["Content-Range"] == f"bytes 100-{len(PAYLOAD) - 1}/{len(PAYLOAD)}"
        assert headers["Content-Length"] == str(len(PAYLOAD) - 100)

    def test_bounded_range(self, client: str) -> None:
        status, body, headers = _open(client, f"/stream/{VIDEO}", {"Range": "bytes=0-1023"})
        assert status == 206
        assert body == PAYLOAD[:1024]
        assert headers["Content-Range"] == f"bytes 0-1023/{len(PAYLOAD)}"

    def test_suffix_range(self, client: str) -> None:
        status, body, headers = _open(client, f"/stream/{VIDEO}", {"Range": "bytes=-512"})
        assert status == 206
        assert body == PAYLOAD[-512:]
        assert (
            headers["Content-Range"]
            == f"bytes {len(PAYLOAD) - 512}-{len(PAYLOAD) - 1}/{len(PAYLOAD)}"
        )

    def test_several_ranges_in_sequence(self, client: str, upstream: UpstreamFixture) -> None:
        """MPD seeks by issuing many range requests; each must be honoured."""
        for start in (0, 4096, 8192, 12288):
            status, body, headers = _open(
                client, f"/stream/{VIDEO}", {"Range": f"bytes={start}-{start + 511}"}
            )
            assert status == 206
            assert body == PAYLOAD[start : start + 512]
            assert headers["Content-Range"].startswith(f"bytes {start}-")
        assert len(upstream.state["ranges"]) >= 4

    def test_garbage_range_is_ignored(self, client: str, upstream: UpstreamFixture) -> None:
        status, body, _ = _open(client, f"/stream/{VIDEO}", {"Range": "bytes=abc"})
        assert status == 200
        assert body == PAYLOAD
        # A malformed header never reaches the upstream.
        assert "bytes=abc" not in upstream.state["ranges"]

    def test_multi_range_header_is_rejected(self, client: str) -> None:
        status, body, _ = _open(client, f"/stream/{VIDEO}", {"Range": "bytes=0-1,5-9"})
        assert status == 200
        assert body == PAYLOAD


class TestRetryOnRejectedSignature:
    def test_retries_once_after_403(
        self, client: str, upstream: UpstreamFixture, config: Config
    ) -> None:
        upstream.state["fail_first"] = 1
        status, body, _ = _open(client, f"/stream/{VIDEO}")
        assert status == 200, "ein einzelner 403 darf die Wiedergabe nicht killen"
        assert body == PAYLOAD
        assert upstream.state["requests"] == 2

    def test_gives_up_after_the_second_failure(
        self, client: str, upstream: UpstreamFixture
    ) -> None:
        upstream.state["fail_first"] = 5
        status, _, _ = _open(client, f"/stream/{VIDEO}")
        assert status == 502
        # Exactly two attempts, then the client gets a clean 502.
        assert upstream.state["requests"] == 2


class TestErrors:
    def test_invalid_id_never_reaches_the_network(
        self, client: str, upstream: UpstreamFixture
    ) -> None:
        status, _, _ = _open(client, "/stream/too-short")
        assert status == 400
        assert upstream.state["requests"] == 0

    @pytest.mark.parametrize(
        "path", ["/stream/../etc/passwd", "/stream/abcdefghijk12345", "/stream/%2e%2e%2f"]
    )
    def test_hostile_paths_are_refused(self, client: str, path: str) -> None:
        status, _, _ = _open(client, path)
        assert status in (400, 404)


def test_full_playback_chain(
    config: Config,
    fake_runner: FakeRunner,
    upstream: UpstreamFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Search -> result -> queue -> MPD URL -> stream bytes.

    MPD itself is stubbed (the FakeMpd from conftest), so this asserts the part
    that belongs to streambridge: the queue entry really is a local stream URL, and
    fetching that URL delivers audio with working range semantics.
    """
    from conftest import FakeMpd
    from streambridge.library import LibraryStore

    monkeypatch.setattr(
        "streambridge.streamer.ALLOWED_HOST_SUFFIXES", (*ALLOWED_HOST_SUFFIXES, "127.0.0.1")
    )
    fake_runner.add_json("ytsearch", SEARCH_PAYLOAD)
    fake_runner.add_json("watch?v=", INFO_PAYLOAD)
    ytdlp = ExtractorClient(config, runner=fake_runner, executable="yt-dlp")
    mpd = FakeMpd(config)
    service = ApiService(
        config,
        client=ytdlp,
        resolver=StreamResolver(config, ytdlp),
        mpd=mpd,  # type: ignore[arg-type]
        library=LibraryStore(config.state_directory),
    )
    server = make_server(dataclasses.replace(config, port=0), service=service)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        # 1. Suche
        request = urllib.request.Request(f"{base}/search?q=Get+Lucky")
        with urllib.request.urlopen(request, timeout=20) as response:
            results = json.loads(response.read())["results"]
        assert results

        # 2. Ergebnis in die Queue
        first = results[0]
        body = json.dumps({"ids": [first["id"]], "play": True}).encode()
        request = urllib.request.Request(
            f"{base}/queue/add",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=20) as response:
            queued = json.loads(response.read())
        assert queued["added"] == 1

        # 3. MPD hält eine lokale Stream-URL
        entry = mpd.items[0]
        assert entry.file.startswith("http://127.0.0.1:")
        assert entry.file.endswith(f"/stream/{first['id']}")

        # 4. Status
        with urllib.request.urlopen(f"{base}/player/status", timeout=20) as response:
            status = json.loads(response.read())
        assert status["state"] == "playing"
        assert status["current"]["video_id"] == first["id"]

        # 5. Der Stream liefert Bytes - genau die, die MPD abrufen würde.
        #    Der Stub-Resolver zeigt dabei auf den lokalen Testserver.
        service.resolver = StubResolver(upstream.url)  # type: ignore[assignment]
        code, payload, headers = _open(base, f"/stream/{entry.file.rsplit('/', 1)[1]}")
        assert code == 200
        assert payload == PAYLOAD
        assert headers["Accept-Ranges"] == "bytes"

    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
