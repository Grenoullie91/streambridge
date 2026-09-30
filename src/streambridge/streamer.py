"""HTTP streaming transport.

Contains no upstream-specific knowledge. It receives a fully resolved
:class:`~streambridge.models.StreamInfo` and moves bytes, transparently
forwarding Range semantics so seeking in players works.

A direct redirect is preferred over proxying: when the resolved URL is
fetchable without special headers, a 302 lets the player pull the bytes
itself and this process stays out of the data path.
"""

from __future__ import annotations

import logging
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator
from http.client import HTTPResponse
from typing import cast
from urllib.parse import urlsplit

from .models import StreamInfo

log = logging.getLogger("streambridge.streamer")

# Upstream servers occasionally stall; bound every socket operation.
SOCKET_TIMEOUT = 20.0
CHUNK_SIZE = 64 * 1024
MAX_REDIRECTS = 3

# Hosts this service is willing to fetch from or redirect to. Anything else
# is refused, which is what keeps the local server from becoming an open proxy.
ALLOWED_HOST_SUFFIXES = (
    ".googlevideo.com",
    ".youtube.com",
    ".ytimg.com",
    ".googleapis.com",
    ".google.com",
    ".gvt1.com",
)

# Headers that are safe to hand to a third-party player: they carry no
# credential and no session state.
#
# Kept deliberately narrow. A wider list would route more requests through a
# 302 instead of the proxy, which was measured to be worse: with a redirect
# MPD stalled at 0:00 with no audio, while the same source proxied played
# through in real time. Keeping the server in the data path costs a little
# throughput and buys predictable playback.
_ORDINARY_HEADERS = {
    "user-agent",
    "accept",
    "accept-encoding",
    "range",
}

# Headers that carry a credential or session state. Present here only as
# documentation of the complement of _ORDINARY_HEADERS: anything in this set
# must never be handed to a third-party player, so a source requiring one is
# proxied instead.
_SECRET_HEADERS = {"cookie", "authorization", "proxy-authorization", "set-cookie"}


def is_allowed_upstream(url: str) -> bool:
    """True only for known media hosts over http(s).

    Suffix matching, not substring matching: ``evilgooglevideo.com`` is
    rejected.
    """
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    if parts.scheme not in ("http", "https"):
        return False
    host = (parts.hostname or "").lower()
    if not host or "." not in host:
        return False
    return any(host.endswith(suffix) for suffix in ALLOWED_HOST_SUFFIXES)


def can_direct_redirect(info: StreamInfo) -> bool:
    """Decide whether a 302 straight to the media URL is safe.

    Redirecting is only safe when no special headers are required. If the
    source needs a cookie or authorisation header, the server must proxy.
    """
    if not is_allowed_upstream(info.url):
        return False
    return not any(k.lower() not in _ORDINARY_HEADERS for k in info.headers)


def _build_opener() -> urllib.request.OpenerDirector:
    # Redirects are followed manually so every hop can be re-validated
    # against the host allowlist.
    return urllib.request.build_opener()


def open_with_headers(
    url: str,
    headers: dict[str, str],
    *,
    method: str = "GET",
    extra: dict[str, str] | None = None,
) -> HTTPResponse:
    """Open *url*, following redirects manually with allowlist checks."""
    opener = _build_opener()
    current = url
    request_headers = dict(headers)
    if extra:
        request_headers.update(extra)
    for _ in range(MAX_REDIRECTS + 1):
        if not is_allowed_upstream(current):
            raise ValueError("upstream host not allowed")
        req = urllib.request.Request(  # noqa: S310 - scheme validated by allowlist
            current, method=method, headers=request_headers
        )
        try:
            # opener.open is typed as returning Any; the cast documents that
            # this specific call yields an HTTPResponse.
            response = cast(HTTPResponse, opener.open(req, timeout=SOCKET_TIMEOUT))
        except urllib.error.HTTPError as exc:
            if exc.code in (301, 302, 303, 307, 308):
                location = exc.headers.get("Location")
                if not location:
                    raise
                current = urllib.parse.urljoin(current, location)
                exc.close()
                continue
            raise
        status = getattr(response, "status", None) or response.getcode()
        if status in (301, 302, 303, 307, 308):
            location = response.headers.get("Location")
            response.close()
            if not location:
                raise ValueError("redirect without location")
            current = urllib.parse.urljoin(current, location)
            continue
        return response
    raise ValueError("too many upstream redirects")


def iter_chunks(source: HTTPResponse) -> Iterator[bytes]:
    """Yield chunks from an already-open upstream response.

    Stops cleanly on timeout or disconnect: both are normal conditions when a
    player skips a track.
    """
    while True:
        try:
            chunk = source.read(CHUNK_SIZE)
        except (TimeoutError, OSError):
            return
        if not chunk:
            return
        yield chunk


def iter_stream(info: StreamInfo, range_header: str | None = None) -> Iterator[bytes]:
    """Yield audio bytes from the resolved source, forwarding the Range header."""
    extra = {"Range": range_header} if range_header else None
    response = open_with_headers(info.url, info.headers, extra=extra)
    try:
        yield from iter_chunks(response)
    finally:
        response.close()


def probe(info: StreamInfo) -> tuple[int, str | None, int | None]:
    """Issue a ranged probe to learn status, content type and length."""
    response = open_with_headers(info.url, info.headers, extra={"Range": "bytes=0-1"})
    try:
        status = getattr(response, "status", None) or response.getcode()
        content_type = response.headers.get("Content-Type")
        raw_length = response.headers.get("Content-Length")
        length = int(raw_length) if raw_length and raw_length.isdigit() else None
        if status == 206:
            content_range = response.headers.get("Content-Range", "")
            if "/" in content_range:
                total = content_range.rsplit("/", 1)[1]
                if total.isdigit():
                    length = int(total)
        return status, content_type, length
    finally:
        response.close()
