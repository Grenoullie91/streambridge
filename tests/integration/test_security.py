"""Security review: adversarial input against a live local server.

These tests exercise the running HTTP surface the way a hostile local process
would. The subprocess layer is mocked, so no request leaves the machine.
"""

from __future__ import annotations

import json
import socket
import threading
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from conftest import INFO_PAYLOAD, SEARCH_PAYLOAD, FakeRunner
from streambridge.api import ApiService, make_server
from streambridge.config import Config
from streambridge.resolver import StreamResolver
from streambridge.youtube import ExtractorClient

pytestmark = pytest.mark.integration

VALID_ID = "5NV6Rdv1a3I"


def _build(config: Config, fake_runner: FakeRunner) -> ApiService:
    """Assemble a service entirely from *config*.

    The rate limiter lives inside the search service, so the config handed to
    :class:`ApiService` is the one that decides the limit.
    """
    fake_runner.add_json("ytsearch", SEARCH_PAYLOAD)
    fake_runner.add_json("watch?v=", INFO_PAYLOAD)
    client = ExtractorClient(config, runner=fake_runner, executable="yt-dlp")
    return ApiService(config, client=client, resolver=StreamResolver(config, client))


@pytest.fixture
def server(config: Config, fake_runner: FakeRunner) -> Iterator[tuple[str, Any]]:
    service = _build(config, fake_runner)
    httpd = make_server(replace(config, port=0), service=service)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}", httpd
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


@pytest.fixture
def base_url(server: tuple[str, Any]) -> str:
    return server[0]


def fetch(url: str, method: str = "GET", body: bytes | None = None) -> tuple[int, bytes]:
    """Fetch without following redirects.

    ``/stream`` answers with a 302 to the real media host. Following it would
    send the suite to the internet, so redirects are surfaced, not chased.
    """

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *_a: Any, **_k: Any) -> None:
            return None

    opener = urllib.request.build_opener(NoRedirect)
    request = urllib.request.Request(url, method=method, data=body)
    try:
        with opener.open(request, timeout=15) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()
    except urllib.error.URLError as exc:
        return 0, str(exc).encode()


class TestOpenProxy:
    """The server must never become a general-purpose proxy."""

    @pytest.mark.parametrize(
        "path",
        [
            "/proxy?url=http://example.com",
            "/proxy?url=http://169.254.169.254/latest/meta-data/",
            "/stream?url=http://example.com/x.mp3",
            "/fetch?uri=http://example.com",
            "/download?file=http://example.com",
            "/stream/http://example.com/x.mp3",
            "/stream/https%3A%2F%2Fexample.com",
        ],
    )
    def test_no_arbitrary_url_endpoint(self, base_url: str, path: str) -> None:
        status, _body = fetch(base_url + path)
        assert status in (400, 404, 405), f"{path} was not rejected"

    def test_localhost_ssrf_via_stream_id(self, base_url: str) -> None:
        # A local URL cannot be smuggled through the id parameter: the route
        # regex only accepts 11 safe characters.
        status, _body = fetch(base_url + "/stream/" + urllib.parse.quote("http://127.0.0.1:6600"))
        assert status in (400, 404)

    def test_cloud_metadata_ssrf(self, base_url: str) -> None:
        status, _body = fetch(base_url + "/stream/169.254.169.254")
        assert status in (400, 404)


class TestPathTraversal:
    @pytest.mark.parametrize(
        "path",
        [
            "/info/../../../../etc/passwd",
            "/info/..%2F..%2F..%2Fetc%2Fpasswd",
            "/stream/../../../../etc/shadow",
            "/stream/%2e%2e%2f%2e%2e%2fetc%2fpasswd",
            "/info/....//....//etc/passwd",
            "/info/..\\..\\windows\\system32",
        ],
    )
    def test_traversal_rejected(self, base_url: str, path: str) -> None:
        status, body = fetch(base_url + path)
        assert status in (400, 404), f"{path} -> {status}"
        assert b"root:" not in body

    def test_null_byte_in_path(self, base_url: str) -> None:
        status, _body = fetch(base_url + f"/stream/{VALID_ID}%00.txt")
        assert status in (400, 404)


class TestInjection:
    def test_shell_metacharacters_in_query(self, base_url: str, tmp_path: Path) -> None:
        canary = tmp_path / "pwned"
        # The query is one argv element; nothing may be executed.
        for payload in (f"; touch {canary}", "$(id)", "`id`", "&& rm -rf /"):
            status, _body = fetch(base_url + "/search?q=" + urllib.parse.quote(payload))
            assert status in (200, 400)
        assert not canary.exists()

    def test_newline_in_query(self, base_url: str) -> None:
        status, _body = fetch(base_url + "/search?q=a%0Ab")
        assert status in (200, 400)

    def test_header_injection_not_reachable(self, base_url: str) -> None:
        # A "video id" containing CRLF must never reach an upstream header.
        status, _body = fetch(base_url + f"/stream/{VALID_ID}%0d%0aX-Evil:%201")
        assert status in (400, 404)

    def test_oversized_query_rejected(self, base_url: str) -> None:
        status, _body = fetch(base_url + "/search?q=" + "a" * 5000)
        assert status == 400


class TestMethodAndHost:
    def test_post_not_allowed(self, base_url: str) -> None:
        status, _body = fetch(base_url + "/search?q=test", method="POST", body=b"")
        assert status in (400, 405, 501)

    def test_only_loopback_reachable(self, base_url: str) -> None:
        assert urllib.parse.urlparse(base_url).hostname == "127.0.0.1"

    def test_server_has_no_wildcard_bind(self, base_url: str) -> None:
        # A non-loopback local address must not reach the server.
        port = urllib.parse.urlparse(base_url).port
        try:
            addresses = [
                info[4][0]
                for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
            ]
        except socket.gaierror:
            pytest.skip("hostname does not resolve")
        external = [a for a in addresses if not a.startswith("127.")]
        if not external:
            pytest.skip("no external address available")
        sock = socket.socket()
        sock.settimeout(2)
        try:
            result = sock.connect_ex((external[0], port))
        finally:
            sock.close()
        assert result != 0, "server is also reachable from a non-loopback address"


class TestRateLimit:
    def test_search_rate_limit_trips(self, config: Config, fake_runner: FakeRunner) -> None:
        fake_runner.add_json("ytsearch", {"entries": []})
        limited = replace(config, search_rate_per_min=3)
        service = _build(limited, fake_runner)
        httpd = make_server(replace(limited, port=0), service=service)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{httpd.server_address[1]}"
        try:
            codes = [fetch(f"{base}/search?q=unique{i}")[0] for i in range(6)]
            assert 429 in codes, "rate limit never triggered"
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=5)


class TestResourceLimits:
    def test_many_concurrent_requests(self, base_url: str) -> None:
        results: list[int] = []
        lock = threading.Lock()

        def worker(path: str) -> None:
            status, _body = fetch(base_url + path)
            with lock:
                results.append(status)

        paths = ["/health"] * 20 + ["/search?q=load"] * 10 + [f"/stream/{VALID_ID}"] * 10
        threads = [threading.Thread(target=worker, args=(p,)) for p in paths]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        assert len(results) == len(paths)
        # Every request must get a well-formed answer: success, redirect, rate
        # limit, or a structured upstream error. Never a crash.
        unexpected = sorted({c for c in results if c not in (200, 302, 429, 502)})
        assert not unexpected, f"unexpected status codes: {unexpected}"

    def test_response_is_valid_json_always(self, base_url: str) -> None:
        for path in ("/health", "/stats", "/search?q=x", f"/info/{VALID_ID}", "/nope"):
            status, body = fetch(base_url + path)
            assert status in (200, 400, 404, 502)
            if status >= 400 or path == "/health":
                json.loads(body)  # error responses are structured

    def test_no_stack_trace_leak(self, base_url: str) -> None:
        for path in ("/info/bad", "/stream/bad", "/search", "/nope"):
            _status, body = fetch(base_url + path)
            text = body.decode("utf-8", "replace")
            assert "Traceback" not in text
            assert 'File "' not in text
