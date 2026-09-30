"""Streaming layer: host allowlist, redirect decision, Range handling.

The allowlist is the open-proxy guard, so it gets the most attention here.
Nothing in this file opens a socket; every response is a stand-in object.
"""

from __future__ import annotations

import pytest

from conftest import make_stream_info
from streambridge.models import StreamInfo
from streambridge.streamer import (
    can_direct_redirect,
    is_allowed_upstream,
    iter_stream,
    probe,
)

pytestmark = pytest.mark.unit

VALID_ID = "5NV6Rdv1a3I"


class TestHostAllowlist:
    @pytest.mark.parametrize(
        "url",
        [
            "https://rr3---sn-x.googlevideo.com/videoplayback?expire=1",
            "http://rr1---sn-y.googlevideo.com/videoplayback",
            "https://www.youtube.com/watch?v=5NV6Rdv1a3I",
            "https://i.ytimg.com/vi/x/hq.jpg",
            "https://music.youtube.com/watch?v=5NV6Rdv1a3I",
        ],
    )
    def test_allows_known_media_hosts(self, url: str) -> None:
        assert is_allowed_upstream(url) is True

    @pytest.mark.parametrize(
        "url",
        [
            "http://example.com/x",
            "https://evil.test/proxy",
            "http://169.254.169.254/latest/meta-data/",
            "http://localhost:8787/stream/5NV6Rdv1a3I",
            "file:///etc/passwd",
            "ftp://example.com/x",
            "gopher://example.com/",
            "https://notyoutube.com/videoplayback",
            "https://googlevideo.com.evil.test/x",
            "https://evilgooglevideo.com/x",
            "https://x.googlevideo.com.evil.test/",
            "https://",
            "//googlevideo.com/x",
            "https://localhost/x",
            "",
        ],
    )
    def test_blocks_everything_else(self, url: str) -> None:
        assert is_allowed_upstream(url) is False

    def test_suffix_match_not_substring(self) -> None:
        # A substring match would let "evilgooglevideo.com" through.
        assert is_allowed_upstream("https://evilgooglevideo.com/x") is False
        assert is_allowed_upstream("https://x.googlevideo.com.evil.test/") is False

    def test_hostless_url_rejected(self) -> None:
        assert is_allowed_upstream("https:///path") is False

    def test_port_does_not_affect_host_check(self) -> None:
        assert is_allowed_upstream("https://rr1.googlevideo.com:8443/x") is True


class TestRedirectDecision:
    def test_redirect_when_no_headers_needed(self) -> None:
        info = StreamInfo(
            video_id=VALID_ID,
            url="https://rr3---sn-x.googlevideo.com/videoplayback?expire=1",
            mime_type="audio/mp4",
        )
        assert can_direct_redirect(info) is True

    def test_redirect_with_ordinary_headers(self) -> None:
        # User-Agent and Accept are ordinary defaults, not secrets.
        assert can_direct_redirect(make_stream_info()) is True

    @pytest.mark.parametrize("header", ["Cookie", "Authorization", "Proxy-Authorization"])
    def test_proxy_when_credential_header_needed(self, header: str) -> None:
        # A credential must never be handed to a third-party player.
        info = StreamInfo(
            video_id=VALID_ID,
            url="https://rr3---sn-x.googlevideo.com/videoplayback",
            mime_type="audio/mp4",
            headers={header: "secret-value"},
        )
        assert can_direct_redirect(info) is False

    def test_credential_headers_are_never_ordinary(self) -> None:
        from streambridge.streamer import _ORDINARY_HEADERS, _SECRET_HEADERS

        assert not _ORDINARY_HEADERS & _SECRET_HEADERS

    @pytest.mark.parametrize(
        "header",
        [
            # These carry no credential, but routing them through a redirect
            # measured worse: MPD stalled at 0:00 with no audio, while the
            # proxy path played through in real time. The narrow list is a
            # deliberate trade of throughput for predictable playback.
            "Sec-Fetch-Mode",
            "Accept-Language",
        ],
    )
    def test_unusual_headers_force_the_proxy(self, header: str) -> None:
        info = StreamInfo(
            video_id=VALID_ID,
            url="https://rr3---sn-x.googlevideo.com/videoplayback",
            mime_type="audio/mp4",
            headers={header: "navigate"},
        )
        assert can_direct_redirect(info) is False

    def test_realistic_extractor_headers_use_the_proxy(self) -> None:
        # The exact header set a current extractor returns for an audio format.
        info = StreamInfo(
            video_id=VALID_ID,
            url="https://rr5---sn-x.googlevideo.com/videoplayback?expire=1",
            mime_type="audio/mp4",
            headers={
                # A version-like string, deliberately not a bare 4-part number:
                # that would be indistinguishable from an IPv4 address in the
                # privacy audit.
                "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) Chrome/120",
                "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
                "Accept-Language": "en-us,en;q=0.5",
                "Sec-Fetch-Mode": "navigate",
            },
        )
        assert can_direct_redirect(info) is False

    def test_never_redirect_disallowed_host(self) -> None:
        info = StreamInfo(
            video_id=VALID_ID, url="https://example.com/music.mp3", mime_type="audio/mpeg"
        )
        assert can_direct_redirect(info) is False


class FakeResponse:
    """Minimal stand-in for an ``http.client.HTTPResponse``."""

    def __init__(
        self,
        chunks: list[bytes],
        status: int = 200,
        headers: dict[str, str] | None = None,
    ) -> None:
        self._chunks = list(chunks)
        self.status = status
        self.headers = headers or {}
        self.closed = False

    def read(self, _size: int) -> bytes:
        return self._chunks.pop(0) if self._chunks else b""

    def getcode(self) -> int:
        return self.status

    def close(self) -> None:
        self.closed = True


def _patch_open(monkeypatch: pytest.MonkeyPatch, response: object) -> None:
    """Replace the upstream opener.

    *response* is either a response object or a callable standing in for
    ``open_with_headers`` itself, so the call signature stays assertable.
    """
    if callable(response):
        monkeypatch.setattr("streambridge.streamer.open_with_headers", response)
        return
    monkeypatch.setattr("streambridge.streamer.open_with_headers", lambda *a, **k: response)


class TestIterStream:
    def test_yields_all_chunks(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_open(monkeypatch, FakeResponse([b"a", b"b", b"c"]))
        assert b"".join(iter_stream(make_stream_info())) == b"abc"

    def test_forwards_range_header(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: dict[str, object] = {}

        def fake_open(url, headers, *, method="GET", extra=None):  # type: ignore[no-untyped-def]
            captured["url"] = url
            captured["headers"] = headers
            captured["extra"] = extra
            return FakeResponse([b"x"])

        _patch_open(monkeypatch, fake_open)
        list(iter_stream(make_stream_info(), "bytes=100-200"))
        assert captured["extra"] == {"Range": "bytes=100-200"}

    def test_no_range_header_sends_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: dict[str, object] = {}

        def fake_open(url, headers, *, method="GET", extra=None):  # type: ignore[no-untyped-def]
            captured["extra"] = extra
            return FakeResponse([b"x"])

        _patch_open(monkeypatch, fake_open)
        list(iter_stream(make_stream_info(), None))
        assert captured["extra"] is None

    def test_response_closed_on_exit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        response = FakeResponse([b"a"])
        _patch_open(monkeypatch, response)
        for _ in iter_stream(make_stream_info()):
            pass
        assert response.closed is True

    def test_stops_cleanly_on_client_abort(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # MPD closing the connection is normal, not an error.
        class AbortingResponse(FakeResponse):
            def read(self, _size: int) -> bytes:
                raise BrokenPipeError("client went away")

        _patch_open(monkeypatch, AbortingResponse([]))
        assert list(iter_stream(make_stream_info())) == []

    def test_survives_socket_timeout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        class TimingOutResponse(FakeResponse):
            def read(self, _size: int) -> bytes:
                raise TimeoutError("read timed out")

        _patch_open(monkeypatch, TimingOutResponse([]))
        assert list(iter_stream(make_stream_info())) == []

    def test_refuses_disallowed_upstream(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def forbidden(*_a: object, **_k: object):
            raise ValueError("upstream host not allowed")

        _patch_open(monkeypatch, forbidden)
        info = StreamInfo(video_id=VALID_ID, url="https://evil.test/x", mime_type="audio/mp4")
        with pytest.raises(ValueError):
            list(iter_stream(info))


class TestProbe:
    def test_reports_status_type_and_length(self, monkeypatch: pytest.MonkeyPatch) -> None:
        response = FakeResponse(
            [b"\x00\x00"],
            status=206,
            headers={"Content-Type": "audio/mp4", "Content-Range": "bytes 0-1/4096"},
        )
        _patch_open(monkeypatch, response)
        status, content_type, length = probe(make_stream_info())
        assert (status, content_type, length) == (206, "audio/mp4", 4096)

    def test_falls_back_to_content_length(self, monkeypatch: pytest.MonkeyPatch) -> None:
        response = FakeResponse([b"\x00\x00"], status=200, headers={"Content-Length": "1234"})
        _patch_open(monkeypatch, response)
        _status, _ctype, length = probe(make_stream_info())
        assert length == 1234

    def test_closes_response(self, monkeypatch: pytest.MonkeyPatch) -> None:
        response = FakeResponse([b"\x00\x00"], status=200, headers={})
        _patch_open(monkeypatch, response)
        probe(make_stream_info())
        assert response.closed is True
