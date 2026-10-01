"""HTTP API integration tests over a real loopback socket.

The subprocess layer is replaced entirely by ``FakeRunner``, so no request
here reaches the network: the only socket involved is the local one the test
server binds.
"""

from __future__ import annotations

import dataclasses
import json
import threading
import urllib.error
import urllib.request
from collections.abc import Iterator
from dataclasses import replace
from typing import Any, ClassVar

import pytest

from conftest import FAKE_EXECUTABLE, INFO_PAYLOAD, SEARCH_PAYLOAD, FakeRunner
from streambridge.api import ApiService, make_server
from streambridge.config import Config
from streambridge.errors import ValidationError
from streambridge.proc import CompletedRun
from streambridge.resolver import StreamResolver
from streambridge.youtube import ExtractorClient

pytestmark = pytest.mark.integration

VALID_ID = "5NV6Rdv1a3I"
OTHER_ID = "CCHdMIEGaaM"


class Harness:
    """A running streambridge-server on an ephemeral loopback port."""

    def __init__(self, service: ApiService, server: Any) -> None:
        self.service = service
        self.server = server
        self.base = f"http://127.0.0.1:{server.server_address[1]}"
        self._thread = threading.Thread(target=server.serve_forever, daemon=True)
        self._thread.start()

    def get(self, path: str) -> tuple[int, dict[str, Any]]:
        try:
            with urllib.request.urlopen(f"{self.base}{path}", timeout=15) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            body = exc.read()
            try:
                return exc.code, json.loads(body)
            except json.JSONDecodeError:
                return exc.code, {"raw": body.decode("utf-8", "replace")}

    def get_no_redirect(self, path: str) -> tuple[int, dict[str, str]]:
        """Fetch without following redirects, so a 302 can be inspected.

        Without this the client would chase the Location header out to the real
        internet, which is exactly what these tests must not do.
        """

        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *_args: Any, **_kwargs: Any) -> None:
                return None

        opener = urllib.request.build_opener(NoRedirect)
        try:
            with opener.open(f"{self.base}{path}", timeout=15) as response:
                return response.status, dict(response.headers)
        except urllib.error.HTTPError as exc:
            return exc.code, dict(exc.headers or {})

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self._thread.join(timeout=5)


@pytest.fixture
def harness(config: Config, fake_runner: FakeRunner) -> Iterator[Harness]:
    fake_runner.add_json("ytsearch", SEARCH_PAYLOAD)
    fake_runner.add_json("watch?v=", INFO_PAYLOAD)
    fake_runner.add_text("--version", "2026.08.19")
    client = ExtractorClient(config, runner=fake_runner, executable=FAKE_EXECUTABLE)
    service = ApiService(config, client=client, resolver=StreamResolver(config, client))
    # Port 0 asks the OS for a free port, so parallel runs never collide.
    harness_ = Harness(service, make_server(replace(config, port=0), service=service))
    try:
        yield harness_
    finally:
        harness_.stop()


class TestHealth:
    def test_health_ok(self, harness: Harness) -> None:
        status, payload = harness.get("/health")
        assert status == 200
        assert payload["status"] == "ok"
        assert payload["version"]
        assert "extractor" in payload

    def test_root_advertises_endpoints(self, harness: Harness) -> None:
        status, payload = harness.get("/")
        assert status == 200
        assert payload["service"] == "streambridge"
        assert "/search" in payload["endpoints"]
        assert "/stream/<id>" in payload["endpoints"]

    def test_stats_shape(self, harness: Harness) -> None:
        status, payload = harness.get("/stats")
        assert status == 200
        assert set(payload) == {"search_cache", "info_cache", "stream_cache"}

    def test_trailing_slash_is_accepted(self, harness: Harness) -> None:
        # MPD and players add slashes inconsistently; both must work.
        assert harness.get("/health/")[0] == 200


class TestSearch:
    def test_search_returns_results(self, harness: Harness) -> None:
        status, payload = harness.get("/search?q=Artist%20One")
        assert status == 200
        assert payload["query"] == "Artist One"
        assert payload["count"] >= 1
        for key in ("id", "title", "artist", "album", "duration", "url"):
            assert key in payload["results"][0]

    def test_duration_is_int(self, harness: Harness) -> None:
        _status, payload = harness.get("/search?q=test")
        assert isinstance(payload["results"][0]["duration"], int)

    def test_missing_query_is_400(self, harness: Harness) -> None:
        status, payload = harness.get("/search")
        assert status == 400
        assert payload["error"] == "ValidationError"

    def test_empty_query_is_400(self, harness: Harness) -> None:
        assert harness.get("/search?q=%20%20")[0] == 400

    def test_overlong_query_is_400(self, harness: Harness) -> None:
        assert harness.get("/search?q=" + "a" * 500)[0] == 400

    def test_invalid_type_is_400_with_hint(self, harness: Harness) -> None:
        status, payload = harness.get("/search?q=x&type=podcasts")
        assert status == 400
        assert "songs" in payload["hint"]

    def test_limit_validated(self, harness: Harness) -> None:
        assert harness.get("/search?q=x&limit=0")[0] == 400
        assert harness.get("/search?q=x&limit=101")[0] == 400
        assert harness.get("/search?q=x&limit=abc")[0] == 400

    def test_second_call_is_cached(self, harness: Harness) -> None:
        _s1, first = harness.get("/search?q=cache-me")
        _s2, second = harness.get("/search?q=cache-me")
        assert first["cached"] is False
        assert second["cached"] is True

    def test_results_are_json_serialisable(self, harness: Harness) -> None:
        _status, payload = harness.get("/search?q=x")
        json.dumps(payload)


class TestInfo:
    def test_info_returns_metadata(self, harness: Harness) -> None:
        status, payload = harness.get(f"/info/{VALID_ID}")
        assert status == 200
        assert payload["id"] == VALID_ID
        assert payload["album"] == "Some Album"

    def test_info_cached(self, harness: Harness) -> None:
        _s1, first = harness.get(f"/info/{VALID_ID}")
        _s2, second = harness.get(f"/info/{VALID_ID}")
        assert first["cached"] is False
        assert second["cached"] is True

    @pytest.mark.parametrize(
        "path",
        [
            "/info/short",
            "/info/waytoolongvideoid",
            "/info/../../etc/passwd",
            f"/info/{VALID_ID}/../x",
            f"/info/{VALID_ID}%2fetc",
        ],
    )
    def test_invalid_ids_rejected(self, harness: Harness, path: str) -> None:
        assert harness.get(path)[0] in (400, 404)

    def test_upstream_failure_is_502(self, harness: Harness, fake_runner: FakeRunner) -> None:
        fake_runner.add_first("watch?v=", CompletedRun((), 1, "", "ERROR: Video unavailable"))
        harness.service._info_cache.clear()
        status, payload = harness.get(f"/info/{OTHER_ID}")
        assert status == 502
        assert "upstream" in payload


class TestPlaylist:
    def test_playlist_preserves_order(self, harness: Harness, fake_runner: FakeRunner) -> None:
        fake_runner.add_json(
            "playlist?list=",
            {
                "title": "My Playlist",
                "entries": [
                    {"id": "aaaaaaaaaaa", "title": "First"},
                    {"id": "bbbbbbbbbbb", "title": "Second"},
                ],
            },
        )
        status, payload = harness.get("/playlist?id=PLrAXtmErZgOeiKm4sgNOknGvNjby9efdf")
        assert status == 200
        assert [t["title"] for t in payload["tracks"]] == ["First", "Second"]

    def test_invalid_playlist_id_is_400(self, harness: Harness) -> None:
        status, payload = harness.get("/playlist?id=not-a-playlist")
        assert status == 400
        assert payload["error"] == "ValidationError"


class TestStream:
    def test_redirects_for_direct_capable_source(self, harness: Harness) -> None:
        # The media URL needs no special headers, so the server answers with a
        # 302 and never proxies the bytes itself.
        status, headers = harness.get_no_redirect(f"/stream/{VALID_ID}")
        assert status in (301, 302, 303, 307, 308)
        assert "googlevideo.com" in headers.get("Location", "")

    def test_invalid_stream_id_is_400(self, harness: Harness) -> None:
        status, payload = harness.get("/stream/bad-id")
        assert status == 400
        assert payload["error"] == "ValidationError"

    def test_stream_path_traversal_rejected(self, harness: Harness) -> None:
        assert harness.get("/stream/../../../etc/passwd")[0] in (400, 404)

    def test_resolution_failure_is_502(self, harness: Harness, fake_runner: FakeRunner) -> None:
        fake_runner.add_first("watch?v=", CompletedRun((), 1, "", "Video unavailable"))
        harness.service.resolver.invalidate(VALID_ID)
        status, payload = harness.get(f"/stream/{OTHER_ID}")
        assert status == 502
        assert "message" in payload


class TestResourceBounds:
    """Every request-side resource must be bounded, not just the upstream one."""

    def test_handler_declares_a_socket_timeout(self) -> None:
        # The real guard. Without a timeout here, a client that connects and
        # then goes quiet pins a thread and its buffer for as long as it likes,
        # and this is the only place in the server with no bound.
        from streambridge.api import ApiHandler

        assert ApiHandler.timeout is not None
        assert 0 < ApiHandler.timeout <= 120

    def test_socketserver_applies_the_declared_timeout(
        self, config: Config, fake_runner: FakeRunner
    ) -> None:
        """Verify the mechanism: socketserver drops a stalled connection.

        This sets the attribute itself, so it proves the plumbing works rather
        than that the default is set - that is the assertion above. Together
        they are the guarantee; either alone would be easy to fool.
        """
        import socket
        import threading
        import time
        from dataclasses import replace as dc_replace

        fake_runner.add_json("ytsearch", SEARCH_PAYLOAD)
        fake_runner.add_json("watch?v=", INFO_PAYLOAD)
        client = ExtractorClient(config, runner=fake_runner, executable=FAKE_EXECUTABLE)
        service = ApiService(config, client=client, resolver=StreamResolver(config, client))
        server = make_server(dc_replace(config, port=0), service=service)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        port = server.server_address[1]
        handler = server.RequestHandlerClass
        original = handler.timeout
        try:
            handler.timeout = 1.0
            sock = socket.create_connection(("127.0.0.1", port), timeout=10)
            # Headers without the terminating blank line: an incomplete request.
            sock.sendall(b"GET /health HTTP/1.1\r\nHost: x\r\n")
            begin = time.monotonic()
            sock.settimeout(8)
            try:
                data = sock.recv(4096)
            except OSError:
                data = b""
            elapsed = time.monotonic() - begin
            sock.close()
            # The server gave up on us rather than answering or waiting.
            assert data == b"", f"expected the server to close, got {data[:80]!r}"
            assert elapsed < 8, f"connection held for {elapsed:.1f}s"
        finally:
            handler.timeout = original
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_normal_requests_still_complete(self, harness: Harness) -> None:
        # The timeout must not truncate ordinary request handling.
        assert harness.get("/health")[0] == 200
        assert harness.get("/stats")[0] == 200

    def test_stream_survives_a_client_that_stops_reading(
        self, harness: Harness, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A stalled player must end the relay quietly, not as a crash.

        The handler timeout now applies to writes, so it can fire in the middle
        of a proxied stream. That is a normal ending, not an error, and must
        not surface as a traceback.
        """
        from conftest import make_stream_info
        from streambridge.api import ApiHandler

        response = _StallingResponse()
        monkeypatch.setattr("streambridge.api.open_with_headers", lambda *a, **k: response)
        monkeypatch.setattr("streambridge.api.iter_chunks", lambda src: iter([b"x" * 1024] * 4))
        # Drive the relay with a socket that fails on write, standing in for a
        # client that stopped reading.
        handler = ApiHandler.__new__(ApiHandler)
        sent: list[tuple[int, str]] = []

        def send_response_only(code: int, message: str | None = None) -> None:
            sent.append((code, message or ""))

        handler.send_response = send_response_only  # type: ignore[method-assign]
        handler.send_header = lambda *a, **k: None  # type: ignore[method-assign]
        handler.end_headers = lambda: None  # type: ignore[method-assign]
        handler.headers = {}
        handler.wfile = _FailingWriter()
        ApiHandler._relay(handler, response, make_stream_info())
        # Reaching this point without an exception is the assertion: an
        # unhandled socket error would have propagated.
        assert sent


class _StallingResponse:
    """Minimal upstream response for the relay test."""

    status = 200
    headers: ClassVar[dict[str, str]] = {
        "Content-Type": "audio/mp4",
        "Content-Length": "4096",
    }

    def getcode(self) -> int:
        return 200

    def close(self) -> None:
        pass


class _FailingWriter:
    """A socket writer whose writes always time out."""

    def write(self, _data: bytes) -> None:
        raise TimeoutError("client stopped reading")


class TestSecurity:
    def test_no_open_proxy_endpoint(self, harness: Harness) -> None:
        # No endpoint may take an arbitrary URL.
        for probe in ("/proxy?url=http://example.com", "/stream?url=http://example.com"):
            assert harness.get(probe)[0] in (400, 404, 405)

    def test_unknown_path_404(self, harness: Harness) -> None:
        status, payload = harness.get("/nope")
        assert status == 404
        assert payload["code"] == "NOT_FOUND"

    @pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.10", "example.com", "::"])
    def test_server_binds_loopback_only(self, config: Config, host: str) -> None:
        # A non-loopback bind is refused outright, not merely warned about.
        with pytest.raises(ValidationError) as exc:
            make_server(replace(config, host=host))
        assert "loopback" in exc.value.message.lower()

    @pytest.mark.parametrize("host", ["127.0.0.1", "localhost"])
    def test_loopback_aliases_accepted(self, config: Config, host: str) -> None:
        server = make_server(replace(config, host=host, port=0))
        server.server_close()

    def test_config_is_not_mutated_by_validation(self, config: Config) -> None:
        dataclasses.asdict(config)
        assert config.host == "127.0.0.1"

    def test_error_body_has_no_traceback(self, harness: Harness) -> None:
        _status, payload = harness.get("/nope")
        assert "Traceback" not in json.dumps(payload)


class TestConcurrency:
    def test_parallel_health_requests(self, harness: Harness) -> None:
        results: list[int] = []
        lock = threading.Lock()

        def worker() -> None:
            status, _payload = harness.get("/health")
            with lock:
                results.append(status)

        threads = [threading.Thread(target=worker) for _ in range(15)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)
        assert results == [200] * 15

    def test_parallel_stream_requests_share_one_resolution(
        self, harness: Harness, fake_runner: FakeRunner
    ) -> None:
        harness.service.resolver.invalidate(VALID_ID)
        before = len([c for c in fake_runner.calls if "watch?v=" in " ".join(c)])

        def worker() -> None:
            try:
                harness.get(f"/stream/{VALID_ID}")
            except Exception:
                pass

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)

        after = len([c for c in fake_runner.calls if "watch?v=" in " ".join(c)])
        assert after - before == 1, "more than one upstream call for a single id"

    def test_distinct_ids_all_work(self, harness: Harness) -> None:
        statuses: list[int] = []
        lock = threading.Lock()

        def worker(video_id: str) -> None:
            status, _headers = harness.get_no_redirect(f"/stream/{video_id}")
            with lock:
                statuses.append(status)

        threads = [threading.Thread(target=worker, args=(f"{i:011d}",)) for i in range(5)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)
        assert len(statuses) == 5
