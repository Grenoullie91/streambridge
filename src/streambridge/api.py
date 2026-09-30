"""HTTP API for streambridge-server.

Read-only endpoints, all bound to loopback:

    GET /              service banner and endpoint list
    GET /health        liveness plus resolved component versions
    GET /search?q=     search the online catalogue
    GET /info/<id>     metadata for one track
    GET /playlist?id=  playlist metadata, order preserved
    GET /stream/<id>   audio stream (direct redirect or proxied relay)
    GET /stats         cache statistics

No endpoint accepts an arbitrary URL. Paths carry only validated 11-character
video ids, which is what keeps this from becoming a general-purpose proxy.
"""

from __future__ import annotations

import contextlib
import json
import logging
import re
import time
import urllib.error
import urllib.parse
from collections.abc import Iterator
from http import HTTPStatus
from http.client import HTTPResponse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from . import __version__
from .cache import DiskSearchCache, TtlCache, info_cache_key, search_cache_key
from .config import Config
from .errors import (
    MpdError,
    RateLimitError,
    SourceUnavailableError,
    StreamBridgeError,
    ValidationError,
)
from .models import SearchType, StreamInfo, Track, validate_playlist_id, validate_video_id
from .resolver import StreamResolver, TokenBucket
from .search import SearchService
from .streamer import can_direct_redirect, is_allowed_upstream, iter_chunks, open_with_headers
from .youtube import ExtractorClient

log = logging.getLogger("streambridge.api")

# Only 11-character ids may appear in a path. Anything else is rejected before
# any filesystem, subprocess or network access happens.
_STREAM_PATH_RE = re.compile(r"^/stream/([A-Za-z0-9_-]{11})$")
_INFO_PATH_RE = re.compile(r"^/info/([A-Za-z0-9_-]{11})$")

# A Range header is forwarded upstream. Anything that is not a simple
# byte range is dropped rather than passed on.
_RANGE_RE = re.compile(r"^bytes=\d*-\d*$")

# Redirect and proxy responses must never be cached by an intermediary.
NO_STORE = "no-store"


def _first(params: dict[str, list[str]], key: str) -> str | None:
    """First value of a query parameter, or None when absent or empty."""
    values = params.get(key)
    if not values:
        return None
    value = values[0]
    return value if value else None


class ApiService:
    """All business logic, independent of the HTTP transport.

    Separating this from the handler makes the API unit-testable without
    opening a socket.
    """

    def __init__(
        self,
        config: Config,
        *,
        client: ExtractorClient | None = None,
        resolver: StreamResolver | None = None,
        search_service: SearchService | None = None,
    ) -> None:
        self._config = config
        self.client = client or ExtractorClient(config)
        self.resolver = resolver or StreamResolver(config, self.client)
        # Search requests are rate limited inside SearchService, which shares
        # the caches above. This bucket covers the other metadata endpoint,
        # playlist loading, which costs an upstream call of its own.
        self._playlist_limiter = TokenBucket(config.search_rate_per_min)
        self._search_cache = TtlCache(
            max_entries=config.cache_max_entries, ttl=config.cache_ttl_seconds
        )
        self._info_cache = TtlCache(
            max_entries=config.cache_max_entries, ttl=config.cache_ttl_seconds
        )
        self._disk = DiskSearchCache(
            config.cache_directory,
            ttl=config.cache_ttl_seconds,
            max_entries=config.cache_max_entries,
        )
        self.search_service = search_service or SearchService(
            config, self.client, memory_cache=self._search_cache, disk_cache=self._disk
        )
        self.started_at = time.time()
        self._extractor_version: str | None = None
        self._extractor_checked = False

    # -- helpers -------------------------------------------------------
    @property
    def config(self) -> Config:
        return self._config

    def extractor_version(self) -> str | None:
        """Extractor version, resolved once per process."""
        if not self._extractor_checked:
            self._extractor_version = self.client.version()
            self._extractor_checked = True
        return self._extractor_version

    def health(self) -> dict[str, Any]:
        return {
            "status": "ok",
            "version": __version__,
            "extractor": self.extractor_version() or "not found",
            "uptime_seconds": round(time.time() - self.started_at, 1),
        }

    def stats(self) -> dict[str, Any]:
        return {
            "search_cache": self._search_cache.stats(),
            "info_cache": self._info_cache.stats(),
            "stream_cache": self.resolver.stats(),
        }

    def banner(self) -> dict[str, Any]:
        return {
            "service": "streambridge",
            "version": __version__,
            "endpoints": [
                "/health",
                "/search",
                "/info/<id>",
                "/playlist",
                "/stream/<id>",
                "/stats",
            ],
        }

    # -- search --------------------------------------------------------
    def search(self, query: str, search_type: SearchType, limit: int | None) -> dict[str, Any]:
        """Search, serving from cache when possible.

        The cache key is the search helper's, so a repeat query is answered
        without an upstream call.
        """
        clean = (query or "").strip()
        if not clean:
            raise ValidationError("Parameter 'q' is missing or empty.")
        # limit=0 from a query string is a user error, not a default request.
        count = min(self._config.search_limit if limit is None else limit, 100)
        key = search_cache_key(clean, search_type, count)
        cached = self._search_cache.get(key)
        if isinstance(cached, dict):
            return {**cached, "cached": True}
        result = self.search_service.search(clean, search_type=search_type, limit=count)
        payload = result.to_dict()
        self._search_cache.set(key, payload)
        return {**payload, "cached": False}

    # -- info ----------------------------------------------------------
    def info(self, video_id: str) -> dict[str, Any]:
        vid = validate_video_id(video_id)
        key = info_cache_key(vid)
        cached = self._info_cache.get(key)
        if isinstance(cached, dict):
            return {**cached, "cached": True}
        payload = self.client.get_info(vid).to_dict()
        self._info_cache.set(key, payload)
        return {**payload, "cached": False}

    def track(self, video_id: str) -> Track:
        """Metadata for one track as a model object, reusing the cache."""
        from .cache import track_from_dict

        vid = validate_video_id(video_id)
        cached = self._info_cache.get(info_cache_key(vid))
        if isinstance(cached, dict):
            track = track_from_dict(cached)
            if track is not None:
                return track
        payload = self.info(vid)
        track = track_from_dict(payload)
        if track is None:  # pragma: no cover - info() already validated the id
            raise SourceUnavailableError(f"No metadata available for {vid}.")
        return track

    # -- playlist ------------------------------------------------------
    def playlist(self, playlist_id: str, limit: int | None) -> dict[str, Any]:
        """Resolve playlist metadata. Order is preserved; nothing is downloaded."""
        pid = validate_playlist_id(playlist_id)
        self._playlist_limiter.check("playlist loading")
        return self.client.playlist(pid, limit=limit or self._config.search_limit * 5).to_dict()

    # -- stream --------------------------------------------------------
    def resolve(self, video_id: str) -> StreamInfo:
        """Resolve a source, refusing anything on an untrusted host."""
        vid = validate_video_id(video_id)
        info = self.resolver.resolve(vid)
        if not is_allowed_upstream(info.url):
            log.error("Resolved URL for %s is not on an allowed host; refusing", vid)
            self.resolver.invalidate(vid)
            raise SourceUnavailableError(
                f"Source for {vid} is not on an allowed host.",
                hint="This is a safety feature. Update yt-dlp and retry.",
            )
        return info

    def stream_plan(self, video_id: str) -> tuple[StreamInfo, bool]:
        """Decide between a direct redirect and proxying for one stream request."""
        info = self.resolve(video_id)
        return info, can_direct_redirect(info)


class ApiHandler(BaseHTTPRequestHandler):
    """HTTP transport. All logic lives in :class:`ApiService`."""

    server_version = f"streambridge/{__version__}"
    sys_version = ""  # never advertise the interpreter build
    protocol_version = "HTTP/1.1"

    # Seconds a client may hold a connection without sending a request.
    #
    # socketserver applies this to the request socket, so a client that connects
    # and then goes quiet is dropped instead of pinning a thread and its buffer
    # forever. Without it this handler is the only unbounded resource in the
    # server: the upstream side is already bounded by SOCKET_TIMEOUT.
    #
    # Generous on purpose. A large proxied stream is a long-lived response, and
    # this bounds the gap *between* requests, not the transfer itself.
    timeout = 30.0

    service: ApiService  # injected by make_server
    quiet: bool = True

    # -- plumbing ------------------------------------------------------
    def log_message(self, fmt: str, *args: Any) -> None:
        if not self.quiet:
            log.debug("%s - %s", self.address_string(), fmt % args)
        else:
            log.debug("http %s", fmt % args)

    def _send_json(self, payload: dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", NO_STORE)
        self.end_headers()
        self.wfile.write(body)

    def _error(self, exc: StreamBridgeError) -> None:
        """Map a typed error onto a status code and a JSON body.

        The body carries the message and optional hint but never a traceback
        or an internal path.
        """
        status = {
            ValidationError: HTTPStatus.BAD_REQUEST,
            RateLimitError: HTTPStatus.TOO_MANY_REQUESTS,
            SourceUnavailableError: HTTPStatus.BAD_GATEWAY,
            MpdError: HTTPStatus.SERVICE_UNAVAILABLE,
        }.get(type(exc), HTTPStatus.INTERNAL_SERVER_ERROR)
        payload: dict[str, Any] = {"error": type(exc).__name__, "message": exc.message}
        if exc.hint:
            payload["hint"] = exc.hint
        if isinstance(exc, SourceUnavailableError) and exc.upstream_message:
            payload["upstream"] = exc.upstream_message
        self._send_json(payload, status)

    # -- routing -------------------------------------------------------
    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        params = urllib.parse.parse_qs(parsed.query)
        try:
            if path == "/":
                self._send_json(self.service.banner())
                return
            if path == "/health":
                self._send_json(self.service.health())
                return
            if path == "/stats":
                self._send_json(self.service.stats())
                return
            if path == "/search":
                self._send_json(
                    self.service.search(
                        _first(params, "q") or "",
                        self._parse_type(_first(params, "type") or "songs"),
                        self._parse_int(_first(params, "limit")),
                    )
                )
                return
            if path == "/playlist":
                self._send_json(
                    self.service.playlist(
                        _first(params, "id") or "",
                        self._parse_int(_first(params, "limit")),
                    )
                )
                return
            info_match = _INFO_PATH_RE.match(path)
            if info_match:
                self._send_json(self.service.info(info_match.group(1)))
                return
            if path.startswith("/stream/"):
                match = _STREAM_PATH_RE.match(path)
                if not match:
                    # Rejected before any resolution attempt.
                    raise ValidationError(
                        "Invalid video id in /stream.",
                        hint="Expected exactly one 11-character video id.",
                    )
                self._handle_stream(match.group(1))
                return
            self._send_json(
                {"error": "not_found", "message": f"Unknown path: {path}"},
                HTTPStatus.NOT_FOUND,
            )
        except StreamBridgeError as exc:
            self._error(exc)
        except (BrokenPipeError, ConnectionResetError):  # pragma: no cover
            log.debug("Client closed the connection")
        except Exception as exc:  # pragma: no cover - safety net
            log.exception("Unexpected error while handling %s", path)
            self._send_json(
                {"error": "internal_error", "message": str(exc)[:300]},
                HTTPStatus.INTERNAL_SERVER_ERROR,
            )

    @staticmethod
    def _parse_type(raw: str) -> SearchType:
        try:
            return SearchType(raw)
        except ValueError as exc:
            allowed = ", ".join(t.value for t in SearchType)
            raise ValidationError(
                f"Unknown search type: {raw!r}", hint=f"Allowed: {allowed}"
            ) from exc

    @staticmethod
    def _parse_int(raw: str | None) -> int | None:
        if raw is None or raw == "":
            return None
        try:
            value = int(raw)
        except ValueError as exc:
            raise ValidationError(f"'limit' must be a number, got {raw!r}") from exc
        if value < 1 or value > 100:
            raise ValidationError("'limit' must be between 1 and 100.")
        return value

    # -- streaming -----------------------------------------------------
    def _handle_stream(self, video_id: str) -> None:
        info, redirect = self.service.stream_plan(video_id)

        if redirect:
            # Preferred: the player fetches the bytes itself and this server
            # stays out of the data path entirely.
            self.send_response(HTTPStatus.FOUND)
            self.send_header("Location", info.url)
            self.send_header("Content-Type", info.mime_type)
            self.send_header("Cache-Control", NO_STORE)
            self.end_headers()
            log.debug("Redirected %s straight to the media URL", video_id)
            return

        range_header = self.headers.get("Range")
        if range_header and not _RANGE_RE.match(range_header):
            range_header = None

        source = self._open_proxied(info, range_header)
        if source is None:
            return
        try:
            self._relay(source, info)
        finally:
            # Always close the upstream socket, including on client abort.
            with contextlib.suppress(Exception):
                source.close()

    def _open_proxied(self, info: StreamInfo, range_header: str | None) -> HTTPResponse | None:
        """Open the upstream source, answering the client if that fails."""
        extra = {"Range": range_header} if range_header else None
        try:
            return open_with_headers(info.url, info.headers, extra=extra)
        except urllib.error.HTTPError as exc:
            log.warning("Upstream HTTP %s for %s", exc.code, info.video_id)
            self.service.resolver.invalidate(info.video_id)
            self._send_json(
                {"error": "upstream_error", "message": f"Upstream HTTP {exc.code}"},
                HTTPStatus.BAD_GATEWAY,
            )
            return None
        except (urllib.error.URLError, ValueError, OSError) as exc:
            log.warning("Upstream unreachable for %s: %s", info.video_id, exc)
            self.service.resolver.invalidate(info.video_id)
            self._send_json(
                {"error": "upstream_unreachable", "message": str(exc)[:200]},
                HTTPStatus.BAD_GATEWAY,
            )
            return None

    def _relay(self, source: HTTPResponse, info: StreamInfo) -> None:
        """Forward upstream bytes to the client, preserving range semantics."""
        status = getattr(source, "status", None) or source.getcode()
        headers = source.headers
        content_type = headers.get("Content-Type") or info.mime_type
        content_length = headers.get("Content-Length")
        if status != 206 and content_length is None and info.content_length is not None:
            content_length = str(info.content_length)

        self.send_response(status)
        self.send_header("Content-Type", content_type)
        if content_length:
            self.send_header("Content-Length", content_length)
        if status == 206 and headers.get("Content-Range"):
            self.send_header("Content-Range", headers["Content-Range"])
        self.send_header("Accept-Ranges", headers.get("Accept-Ranges") or "bytes")
        self.send_header("Cache-Control", NO_STORE)
        self.end_headers()

        written = 0
        try:
            for chunk in iter_chunks(source):
                self.wfile.write(chunk)
                written += len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            # The player closed the connection: track changed, seek or stop.
            # The upstream response is closed by the caller's finally block, so
            # no descriptor leaks and no child process survives.
            log.info("Client disconnected from %s after %d bytes", info.video_id, written)
        log.debug("Streamed %s: %d bytes", info.video_id, written)


LOOPBACK_HOSTS = ("127.0.0.1", "::1", "localhost")


def make_server(config: Config, *, service: ApiService | None = None) -> ThreadingHTTPServer:
    """Create a threaded HTTP server on the configured loopback address.

    Refusing any non-loopback bind address is the enforcement point for
    "this is not a public proxy".
    """
    if config.host not in LOOPBACK_HOSTS:
        raise ValidationError(
            f"streambridge-server listens on loopback only, not on {config.host!r}.",
            hint="Set server.host to 127.0.0.1 in the configuration.",
        )
    api = service or ApiService(config)
    handler = type("BoundApiHandler", (ApiHandler,), {"service": api})
    server = ThreadingHTTPServer((config.host, config.port), handler)
    server.daemon_threads = True
    return server


def iter_relay(source: HTTPResponse) -> Iterator[bytes]:
    """Public alias used by tests to drive a relay without a socket."""
    return iter_chunks(source)
