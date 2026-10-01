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
import hmac
import json
import logging
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
from collections.abc import Iterator
from http import HTTPStatus
from http.client import HTTPResponse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from . import __version__
from .cache import DiskSearchCache, TtlCache, info_cache_key, search_cache_key, track_from_dict
from .config import Config, lan_bind_problem
from .errors import (
    AuthorizationError,
    ConfigError,
    DependencyError,
    MpdError,
    NotFoundError,
    RateLimitError,
    SourceUnavailableError,
    StreamBridgeError,
    ValidationError,
)
from .library import LibraryStore
from .logging import redact
from .models import SearchType, StreamInfo, Track, validate_playlist_id, validate_video_id
from .mpd import MpdClient
from .player import PlayerService
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

# Paths that accept POST. Anything else answers 405 for a POST.
_MUTATING_PATHS = frozenset(
    {
        "/queue/add",
        "/queue/clear",
        "/queue/move",
        "/queue/remove",
        "/player/play",
        "/player/pause",
        "/player/stop",
        "/player/next",
        "/player/previous",
        "/player/seek",
        "/player/volume",
        "/player/mute",
        "/player/modes",
        "/favorites/add",
        "/favorites/remove",
        "/favorites/toggle",
        "/favorites/clear",
        "/history/clear",
    }
)

# Only 11-character ids may appear in these paths. Anything else is rejected
# before it touches the filesystem, a subprocess or the network.
_THUMB_PATH_RE = re.compile(r"^/thumbnail/([A-Za-z0-9_-]{11})$")

# Bundled UI assets: URL path -> (file name, content type).
#
# An explicit allowlist, not a path built from the request: nothing the client
# sends can influence which file is opened, which removes path traversal by
# construction. A containment check in _serve_file is the second layer.
_STATIC_FILES: dict[str, tuple[str, str]] = {
    "/app.css": ("app.css", "text/css; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/manifest.webmanifest": ("manifest.webmanifest", "application/manifest+json"),
    "/assets/logo.svg": ("assets/logo.svg", "image/svg+xml"),
    "/assets/icon-192.svg": ("assets/icon-192.svg", "image/svg+xml"),
    "/assets/icon-512.svg": ("assets/icon-512.svg", "image/svg+xml"),
    "/assets/favicon-16.png": ("assets/favicon-16.png", "image/png"),
    "/assets/favicon-32.png": ("assets/favicon-32.png", "image/png"),
    "/assets/favicon-48.png": ("assets/favicon-48.png", "image/png"),
    "/assets/favicon-96.png": ("assets/favicon-96.png", "image/png"),
    "/assets/favicon-192.png": ("assets/favicon-192.png", "image/png"),
}

# Paths that only answer GET. A POST here gets 405 rather than 404 so the
# distinction between "wrong method" and "wrong path" stays meaningful.
_GET_ONLY_PATHS = frozenset(
    {
        "/",
        "/health",
        "/version",
        "/search",
        "/playlist",
        "/stats",
        "/player/status",
        "/queue",
        "/favorites",
        "/history",
        *_STATIC_FILES,
    }
)

# Sent with every response. The UI loads no third-party script, style or
# font, so 'self' plus the upstream image host is the complete policy.
# Without 'unsafe-inline' and 'unsafe-eval' it is also load-bearing: injected
# markup could neither place a script nor set an event handler.
CONTENT_SECURITY_POLICY = (
    "default-src 'self'; "
    "img-src 'self' data: https://i.ytimg.com https://*.ytimg.com; "
    "style-src 'self'; script-src 'self'; connect-src 'self'; "
    "font-src 'self'; object-src 'none'; base-uri 'none'; "
    "form-action 'none'; frame-ancestors 'none'"
)

# Cover images are served by redirect, never proxied: the URL is built from an
# already validated id, so nothing the client sent reaches the Location header
# and no image bytes pass through this process.
THUMBNAIL_TEMPLATE = "https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"

# Server-sent events tuning. The bound matters: a forgotten browser tab would
# otherwise pin a thread and an MPD query per second indefinitely.
SSE_INTERVAL = 1.0
SSE_HEARTBEAT = 15.0
SSE_MAX_SECONDS = 900.0
SSE_MAX_CLIENTS = 8

# Upstream statuses worth one more attempt with a freshly resolved URL. Signed
# media URLs can already be rejected a second after they were handed out, and
# a single retry turns most of those into a working stream.
RETRY_UPSTREAM = frozenset({403, 404, 410})

# JSON bodies are small: a handful of ids and a few strings. A larger one is a
# mistake or an attack, and is rejected before it is parsed.
MAX_BODY_BYTES = 64 * 1024
MAX_BATCH_IDS = 200


# Stable, machine-readable error codes.
#
# The class name is an implementation detail of this Python package and may be
# renamed; the code is part of the HTTP contract, so a client can branch on it
# without string-matching messages. "error" keeps the class name for humans and
# for anything already relying on it.
_ERROR_CODES: dict[type, str] = {
    ValidationError: "VALIDATION_ERROR",
    AuthorizationError: "UNAUTHORIZED",
    NotFoundError: "NOT_FOUND",
    RateLimitError: "RATE_LIMITED",
    SourceUnavailableError: "UPSTREAM_ERROR",
    MpdError: "MPD_ERROR",
    DependencyError: "DEPENDENCY_MISSING",
    ConfigError: "CONFIG_ERROR",
}


def _error_code(exc: StreamBridgeError) -> str:
    for kind, code in _ERROR_CODES.items():
        if isinstance(exc, kind):
            return code
    return "INTERNAL_ERROR"


def _loggable_path(raw: str) -> str:
    """The request path, without its query string.

    The path is the part that says *what* was refused and is worth keeping, but
    a query string is attacker-controlled and may carry anything at all, so it
    is dropped rather than redacted. What is left still goes through
    :func:`redact`, because a path can contain an address or an email.
    """
    from urllib.parse import urlsplit

    try:
        parts = urlsplit(raw)
    except ValueError:  # pragma: no cover - urlsplit is very permissive
        return "<unparsable path>"
    if parts.query:
        return f"{redact(parts.path)}?…"
    return redact(parts.path)


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
        mpd: MpdClient | None = None,
        library: LibraryStore | None = None,
        player: PlayerService | None = None,
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
        # One MpdClient for the whole process. The player holds a reference to
        # the same object, so a queued row and a status query can never go to
        # two different clients with two different ideas of the queue.
        self.mpd = mpd or MpdClient(config)
        # The store owns its directory. It is created on demand, not at import
        # or startup: a read-only or unused install must not need a writable
        # state directory just to serve the API.
        self.library = library or LibraryStore(config.state_directory)
        # Resolved tracks, keyed by id. Resolving costs a yt-dlp call and the
        # UI asks for the same video repeatedly: a queue row, the favourite
        # badge and the now-playing panel all want it.
        self._track_cards: dict[str, Track] = {}
        self.player = player or PlayerService(config, mpd=self.mpd, library=self.library, api=self)
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
        payload: dict[str, Any] = {
            "service": "streambridge",
            "version": __version__,
            "endpoints": [
                "/health",
                "/version",
                "/search",
                "/info/<id>",
                "/playlist",
                "/stream/<id>",
                "/thumbnail/<id>",
                "/player/status",
                "/queue",
                "/favorites",
                "/history",
                "/events",
                "/stats",
            ],
        }
        # The UI's own address, so a client that only ever reads the banner
        # still learns where the browser page lives. Present only when the
        # assets really are on disk: a key that always exists and points
        # nowhere is worse than an absent one.
        if self._config.web_enabled:
            payload["web_ui"] = self._config.base_url + "/"
        return payload

    def version(self) -> dict[str, Any]:
        """Name, version and extractor version.

        Kept tiny and cached: the UI polls it on load, and it must stay cheap
        enough for that.
        """
        return {
            "service": "streambridge",
            "version": __version__,
            "extractor": self.extractor_version() or "not found",
        }

    def cached_track(self, video_id: str) -> Track | None:
        """Metadata for *video_id* only if it is already in memory.

        Never contacts yt-dlp. The player calls this to decorate queue rows and
        the now-playing bar, which run on every poll: a poll that resolved
        metadata would put a subprocess in front of a one-second status query.
        An unresolvable id is not an error here, it is simply a miss.
        """
        try:
            vid = validate_video_id(video_id)
        except ValidationError:
            return None
        memo = self._track_cards.get(vid)
        if memo is not None:
            return memo
        payload = self._info_cache.get(info_cache_key(vid))
        if not isinstance(payload, dict):
            return None
        return track_from_dict(payload)

    def _memoise(self, track: Track) -> None:
        """Remember a resolved track for cached_track.

        The single-entry-per-id memo lives in memory only. The info cache
        already persists to disk, so a second copy on disk would only add a way
        for the two to disagree.
        """
        self._track_cards[track.id] = track

    def _enrich(
        self,
        entries: list[dict[str, Any]],
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Fill in a stored track's metadata from the cache, best effort.

        A stored favourite must still render when the upstream is unreachable
        or the entry was written by an older version, so the stored title wins
        over nothing but never over fresher data, and a failure is not fatal.
        """
        for entry in entries[:limit] if limit else entries:
            track = entry.get("track")
            if not isinstance(track, dict):
                continue
            video_id = track.get("id")
            if not isinstance(video_id, str):
                continue
            card = self.cached_track(video_id)
            if card is not None:
                track.update(card.to_dict())
            track.setdefault("thumbnail", self.thumbnail_url(video_id))
        return entries

    def favorites(self) -> dict[str, Any]:
        """Stored favourites, newest first, with the id list for fast lookups."""
        entries = self._enrich(self.library.favorites())
        return {
            "items": entries,
            "length": len(entries),
            # Sent alongside the items so the UI can test membership without
            # walking the list for every row it paints.
            "ids": [str(item["track"]["id"]) for item in entries],
        }

    def history(self, limit: int = 50) -> dict[str, Any]:
        """Recently played tracks, most recent first."""
        entries = self._enrich(self.library.history(), limit=limit)
        return {"items": entries, "length": len(entries)}

    def favorite_ids(self) -> list[str]:
        return [str(item["track"]["id"]) for item in self.library.favorites()]

    def thumbnail_url(self, video_id: str) -> str:
        return THUMBNAIL_TEMPLATE.format(video_id=validate_video_id(video_id))

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
        """Metadata for one track as a model object, reusing the cache.

        Resolves on a miss. The result is memoised, because the callers that
        reach for this are all on a request path that the UI hits repeatedly.
        """
        cached = self.cached_track(video_id)
        if cached is not None:
            return cached
        vid = validate_video_id(video_id)
        payload = self.info(vid)
        track = track_from_dict(payload)
        if track is None:  # pragma: no cover - info() already validated the id
            raise SourceUnavailableError(
                f"No metadata available for {vid}.",
                hint="The video may have been removed.",
            )
        self._memoise(track)
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
    def end_headers(self) -> None:
        """Append the response-wide security headers, then flush.

        Overriding the single flush point rather than adding headers per code
        path means *every* response carries them: JSON, static assets, SSE, the
        404 fallback, 405s and proxied streams alike. A new route added later
        cannot forget.
        """
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        # The UI needs no third-party origin and does not frame anything.
        self.send_header("Content-Security-Policy", CONTENT_SECURITY_POLICY)
        self.send_header("Cross-Origin-Opener-Policy", "same-origin")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("Permissions-Policy", "geolocation=(), microphone=(), camera=()")
        super().end_headers()

    # -- authorisation --------------------------------------------------
    #
    # Only relevant once server.allow_lan is on. With no access_token
    # configured every request is allowed, which is exactly the behaviour of
    # a loopback-only install and keeps this a no-op for the default setup.

    #: Paths that must answer without a token even for network clients.
    #:
    #: The static shell and the cover images, because that is all a client
    #: needs in order to render the "this server needs a token" prompt; and
    #: /health plus /version, which carry nothing but the service version and
    #: let a client tell "wrong address" apart from "needs a token" before it
    #: has one. Everything that can read the queue or move the needle is
    #: protected.
    _TOKEN_EXEMPT = frozenset({"/", "/index.html", "/health", "/version", "/app.css", "/app.js"})

    def _token_exempt(self, path: str) -> bool:
        if path in self._TOKEN_EXEMPT:
            return True
        return path.startswith("/assets/") or _THUMB_PATH_RE.match(path) is not None

    def _presented_token(self) -> str | None:
        """The token the client sent, or None.

        Two spellings are accepted: the standard ``Authorization: Bearer``
        and a plain header, because an Android client and a browser both
        find one of them more convenient than writing the other.
        """
        authorization = (self.headers.get("Authorization") or "").strip()
        if authorization:
            scheme, _, value = authorization.partition(" ")
            if scheme.lower() == "bearer":
                candidate = value.strip()
                if candidate:
                    return candidate
            else:
                # A non-Bearer scheme is a malformed attempt, not a fallback.
                return None
        plain = (self.headers.get("X-Streambridge-Token") or "").strip()
        return plain or None

    def _peer_is_loopback(self) -> bool:
        try:
            return self.client_address[0].startswith("127.") or self.client_address[0] in (
                "::1",
                "localhost",
            )
        except (AttributeError, IndexError, TypeError):  # pragma: no cover - defensive
            # A missing peer address is not a reason to hand out access.
            return False

    def _authorised(self, path: str) -> bool:
        """Decide access for *path* before any work is done on the request.

        Ordering matters: the token is only consulted at all when one is
        configured, and only for clients that are not on this machine. That
        is what keeps the browser UI on 127.0.0.1 working unchanged while the
        same port serves the phone over the network.
        """
        expected = self.service.config.access_token
        if not expected:
            return True
        if self._peer_is_loopback():
            return True
        if self._token_exempt(path):
            return True
        presented = self._presented_token()
        if presented is None:
            return False
        # Constant-time: a token is a shared secret, and a length-oracle or
        # early-exit comparison would leak it byte by byte over the network.
        return hmac.compare_digest(presented.encode("utf-8"), expected.encode("utf-8"))

    def _check_authorised(self, path: str) -> None:
        """Refuse the request unless *path* is open to this client.

        Raises :class:`AuthorizationError`, which the shared error path turns
        into a 401. Called at the top of both do_GET and do_POST, before any
        routing, so a token-less network client cannot reach state it should
        not see even by guessing paths.

        A refusal is logged, at WARNING and without the address. This is the
        one request that is both security-relevant and the one a user needs to
        see: without it, a phone that cannot connect leaves nothing at all in
        the journal, and the only way to find out whether it arrived is to
        guess.
        """
        if self._authorised(path):
            return
        log.warning(
            "Refused %s %s from a network client: no valid access token",
            self.command,
            _loggable_path(self.path),
        )
        raise AuthorizationError(
            "This server requires an access token.",
            hint="Send it as 'Authorization: Bearer <token>'.",
        )

    def _read_body(self) -> dict[str, Any]:
        """Parse a JSON request body, refusing oversized or malformed input.

        The length is checked before reading and the read is capped, so an
        announced-large or lying Content-Length cannot make this allocate
        without bound.
        """
        if self.headers.get("Transfer-Encoding", "").lower().strip() == "chunked":
            # No Content-Length to bound this with, and nothing in the API
            # streams a request body.
            raise ValidationError(
                "Chunked request bodies are not supported.",
                hint="Send a JSON body with a Content-Length header.",
            )
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError as exc:
            raise ValidationError("Content-Length is not a number.") from exc
        if length < 0:
            raise ValidationError("Content-Length must not be negative.")
        if length > MAX_BODY_BYTES:
            raise ValidationError(
                f"Request body is too large (limit {MAX_BODY_BYTES} bytes).",
                hint="Send at most a few hundred video ids.",
            )
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        if len(raw) != length:
            raise ValidationError("Request body ended unexpectedly.")
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ValidationError(f"Request body is not valid JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise ValidationError("Request body must be a JSON object.")
        return data

    def _send_bytes(
        self,
        body: bytes,
        content_type: str,
        *,
        status: HTTPStatus = HTTPStatus.OK,
        cache_control: str = NO_STORE,
        etag: str | None = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache_control)
        if etag is not None:
            self.send_header("ETag", etag)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _not_found(self, path: str) -> None:
        self._send_json(
            {
                "code": "NOT_FOUND",
                "error": "NotFoundError",
                "message": f"Unknown path: {path}",
            },
            HTTPStatus.NOT_FOUND,
        )

    def _method_not_allowed(self, path: str) -> None:
        """405, with the Allow header so the client knows what is accepted."""
        allow = "GET, HEAD, POST" if path in _MUTATING_PATHS else "GET, HEAD"
        self.send_response(HTTPStatus.METHOD_NOT_ALLOWED)
        body = json.dumps(
            {
                "code": "METHOD_NOT_ALLOWED",
                "error": "MethodNotAllowed",
                "message": f"Use {allow} for {path}.",
            }
        ).encode("utf-8")
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Allow", allow)
        self.send_header("Cache-Control", NO_STORE)
        self.end_headers()
        self.wfile.write(body)

    def _wants_html(self) -> bool:
        """True when the client asked for a web page rather than JSON.

        Content negotiation on "/": a browser (Accept includes text/html) gets
        the UI, an API client or curl (Accept: */*) gets the JSON banner. Both
        stay first-class, and neither needs a separate port or a flag.
        """
        return "text/html" in self.headers.get("Accept", "").lower()

    def log_message(self, fmt: str, *args: Any) -> None:
        """Access log, at DEBUG.

        The peer is included only when this handler is *not* quiet, which the
        server sets when the log level is DEBUG. Without that there is no way
        to tell "the phone never reached us" from "the phone reached us and
        was refused", which is the first question anyone asks when a client
        cannot connect. The address goes through the same redaction as every
        other log line, so turning this on cannot start writing client IPs to
        the journal.
        """
        if self.quiet:
            log.debug("http %s", fmt % args)
        else:
            log.debug("%s - %s", redact(self.address_string()), fmt % args)

    def _send_json(self, payload: dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", NO_STORE)
        self.end_headers()
        self.wfile.write(body)

    @staticmethod
    def _status_for(exc: StreamBridgeError) -> HTTPStatus:
        """The HTTP status for a typed error.

        A method rather than an inline dict so the mapping can be pinned by a
        test. A missing mpc belongs to SERVICE_UNAVAILABLE, not to 500: it is
        the service unable to do its job because a dependency is absent, the
        same condition as MPD being down. Reporting 500 sends a reader hunting
        for a fault in this server when the answer is `apt install mpd-client`.
        """
        return {
            ValidationError: HTTPStatus.BAD_REQUEST,
            AuthorizationError: HTTPStatus.UNAUTHORIZED,
            RateLimitError: HTTPStatus.TOO_MANY_REQUESTS,
            SourceUnavailableError: HTTPStatus.BAD_GATEWAY,
            MpdError: HTTPStatus.SERVICE_UNAVAILABLE,
            DependencyError: HTTPStatus.SERVICE_UNAVAILABLE,
        }.get(type(exc), HTTPStatus.INTERNAL_SERVER_ERROR)

    def _error(self, exc: StreamBridgeError) -> None:
        """Map a typed error onto a status code and a JSON body.

        The body carries the message and optional hint but never a traceback
        or an internal path.
        """
        status = self._status_for(exc)
        payload: dict[str, Any] = {
            "code": _error_code(exc),
            "error": type(exc).__name__,
            "message": exc.message,
        }
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
            self._check_authorised(path)
            if path == "/":
                # A browser gets the UI, an API client the machine-readable
                # banner. Both are documented; neither needs a flag.
                if self._wants_html() and self.service.config.web_enabled:
                    self._serve_static("/")
                    return
                self._send_json(self.service.banner())
                return
            if path == "/health":
                self._send_json(self.service.health())
                return
            if path == "/version":
                self._send_json(self.service.version())
                return
            if path == "/player/status":
                self._send_json(self.service.player.status())
                return
            if path == "/queue":
                self._send_json(self.service.player.queue())
                return
            if path == "/favorites":
                self._send_json(self.service.favorites())
                return
            if path == "/history":
                limit = self._parse_int(_first(params, "limit")) or 50
                self._send_json(self.service.history(limit))
                return
            if path == "/events":
                self._serve_events()
                return
            static = _STATIC_FILES.get(path)
            if static is not None:
                self._serve_static(path)
                return
            if path == "/index.html":
                self._serve_static("/")
                return
            thumb_match = _THUMB_PATH_RE.match(path)
            if thumb_match:
                self._send_thumbnail(thumb_match.group(1))
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
            self._not_found(path)
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

    def do_HEAD(self) -> None:
        """Answer HEAD for every GET path.

        Serving HEAD as a GET that suppresses the body keeps the two in step
        automatically: a new GET route is HEAD-able without being listed twice.
        """
        self.do_GET()

    def do_POST(self) -> None:
        """Mutations. Every one of them changes MPD or local state only.

        Nothing here fetches a URL from the request. Track identity is a
        validated 11-character id, and the stream URL handed to MPD is always
        built by this process from its own host and port. That is what keeps
        this endpoint from being a generic proxy.
        """
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        try:
            self._check_authorised(path)
            if path not in _MUTATING_PATHS:
                # A known GET-only path answers 405 so the client can tell
                # "wrong method" from "wrong path" and read the Allow header.
                known = (
                    path in _GET_ONLY_PATHS
                    or path in _STATIC_FILES
                    or path == "/index.html"
                    or _THUMB_PATH_RE.match(path) is not None
                )
                if known:
                    self._method_not_allowed(path)
                else:
                    self._not_found(path)
                return
            body = self._read_body()
            # A local shorthand: the player and library are the only state
            # these routes touch, and both are already validated on the way in.
            player = self.service.player
            library = self.service.library

            if path == "/queue/add":
                # The one route that carries a list. The cap is enforced here
                # as well as in the player, because a 10 MB id array should be
                # refused before it is walked.
                ids = body.get("ids")
                if not isinstance(ids, list) or not ids:
                    raise ValidationError(
                        "'ids' must be a non-empty list of video ids.",
                        hint='Example: {"ids": ["dQw4w9WgXcQ"]}',
                    )
                if len(ids) > MAX_BATCH_IDS:
                    raise ValidationError(f"At most {MAX_BATCH_IDS} ids per request.")
                result = player.add(
                    [str(value) for value in ids],
                    play=bool(body.get("play", False)),
                    position=body.get("position"),
                    tracks=self._tracks_from(body.get("tracks")),
                )
                self._send_json(result, HTTPStatus.CREATED)
                return
            if path == "/queue/clear":
                self._send_json(player.clear())
                return
            if path == "/queue/remove":
                self._send_json(player.remove(self._field(body, "position", int)))
                return
            if path == "/queue/move":
                self._send_json(
                    player.move(
                        self._field(body, "from", int),
                        self._field(body, "to", int),
                    )
                )
                return
            if path == "/player/play":
                position = body.get("position")
                self._send_json(player.play(int(position) if position is not None else None))
                return
            if path == "/player/pause":
                self._send_json(player.pause())
                return
            if path == "/player/stop":
                self._send_json(player.stop())
                return
            if path == "/player/next":
                self._send_json(player.next())
                return
            if path == "/player/previous":
                self._send_json(player.previous())
                return
            if path == "/player/seek":
                self._send_json(player.seek(self._field(body, "seconds", float)))
                return
            if path == "/player/volume":
                self._send_json(player.volume(self._field(body, "volume", float)))
                return
            if path == "/player/mute":
                self._send_json(player.mute(self._field(body, "muted", bool)))
                return
            if path == "/player/modes":
                # "shuffle" is the UI's word for MPD's random flag; "repeat_one"
                # is MPD's single mode. Translating here keeps MPD's vocabulary
                # out of the HTTP contract.
                # set_modes already answers with the fresh status, which is
                # what the UI paints straight after a mode toggle.
                self._send_json(
                    player.set_modes(
                        shuffle=body.get("shuffle"),
                        repeat=body.get("repeat"),
                        repeat_one=body.get("repeat_one"),
                    )
                )
                return
            if path in ("/favorites/add", "/favorites/remove", "/favorites/toggle"):
                track = self.service.track(self._field(body, "id", str))
                if path == "/favorites/add":
                    favorite = library.add_favorite(track)
                elif path == "/favorites/remove":
                    favorite = not library.remove_favorite(track.id)
                else:
                    favorite = library.toggle_favorite(track)
                self._send_json(
                    {
                        "favorite": favorite,
                        "id": track.id,
                        "title": track.title,
                        "action": "added" if favorite else "removed",
                    }
                )
                return
            if path == "/favorites/clear":
                self._send_json({"removed": library.clear_favorites()})
                return
            if path == "/history/clear":
                self._send_json({"removed": library.clear_history()})
                return
            self._not_found(path)  # pragma: no cover - table above is exhaustive
        except StreamBridgeError as exc:
            self._error(exc)
        except (KeyError, TypeError, ValueError) as exc:
            # A missing or non-numeric field is a client mistake, not a crash.
            self._error(ValidationError(f"Invalid request: {exc}"))
        except (BrokenPipeError, ConnectionResetError):  # pragma: no cover
            log.debug("Client closed the connection")
        except Exception as exc:  # pragma: no cover - safety net
            log.exception("Unexpected error while handling POST %s", path)
            self._send_json(
                {"error": "internal_error", "message": str(exc)[:300]},
                HTTPStatus.INTERNAL_SERVER_ERROR,
            )

    @staticmethod
    def _field(body: dict[str, Any], name: str, kind: type) -> Any:
        """Read one required field, converting it or raising a clean 400.

        A missing or wrongly typed field is a client mistake, so it gets the
        same typed ValidationError path as everything else instead of a 500.
        """
        if name not in body or body[name] is None:
            raise ValidationError(
                f"'{name}' is required.",
                hint=f"Expected a JSON {kind.__name__} field named '{name}'.",
            )
        value = body[name]
        if kind is bool:
            if not isinstance(value, bool):
                raise ValidationError(f"'{name}' must be true or false.")
            return value
        if kind is int:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValidationError(f"'{name}' must be a number.")
            return int(value)
        if kind is float:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValidationError(f"'{name}' must be a number.")
            return float(value)
        if not isinstance(value, str) or not value:
            raise ValidationError(f"'{name}' must be a non-empty string.")
        return value

    def _tracks_from(self, raw: Any) -> list[Track] | None:
        """Rebuild Track objects from client-supplied metadata, best effort.

        Only metadata the caller already displayed is accepted, and every id is
        re-validated. An unparseable or unknown entry is dropped rather than
        failing the request: the player resolves anything it does not get.
        """
        if not isinstance(raw, list):
            return None
        tracks: list[Track] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            track = track_from_dict(item)
            if track is not None:
                tracks.append(track)
        return tracks or None

    def _send_thumbnail(self, video_id: str) -> None:
        """Redirect to the upstream cover image.

        The id is validated by the path regex and the template takes no other
        input, so the Location header cannot be steered. Redirecting rather
        than proxying keeps image traffic and its failures off this process.
        """
        self.send_response(HTTPStatus.FOUND)
        self.send_header("Location", THUMBNAIL_TEMPLATE.format(video_id=video_id))
        self.send_header("Cache-Control", "public, max-age=86400")
        self.end_headers()

    def _serve_static(self, path: str) -> None:
        """Serve one bundled UI asset from the allowlist."""
        directory = self.service.config.web_directory
        if directory is None:
            self._not_found(path)
            return
        name = "index.html" if path == "/" else _STATIC_FILES[path][0]
        target = (Path(directory) / name).resolve()
        try:
            # Second layer behind the allowlist: refuse anything that escaped
            # the web root, whether by symlink or by an odd mount.
            target.relative_to(Path(directory).resolve())
        except ValueError:
            log.error("Refusing to serve %s: outside the web root", target)
            self._error(ValidationError("Asset path is not permitted."))
            return
        try:
            stat = target.stat()
            body = target.read_bytes()
        except OSError:
            self._error(SourceUnavailableError(f"Asset unavailable: {path}"))
            return
        if path in ("/", "/index.html"):
            content_type = "text/html; charset=utf-8"
        else:
            content_type = _STATIC_FILES[path][1]

        # Assets are revalidated, never blindly reused. They are not
        # fingerprinted in the URL, so a returning browser must be told when
        # they changed - otherwise an upgrade leaves a stale app shell behind.
        # "no-cache" means the browser may store the file but must ask; the
        # ETag below makes that a 304 with no body instead of another 60 kB.
        etag = f'"{stat.st_mtime_ns:x}-{len(body):x}"'
        if self.headers.get("If-None-Match", "").strip() == etag:
            self.send_response(HTTPStatus.NOT_MODIFIED)
            self.send_header("ETag", etag)
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            return
        self._send_bytes(body, content_type, etag=etag, cache_control="no-cache")

    def _serve_events(self) -> None:
        """Stream player state as server-sent events.

        A single ticker, no per-event work beyond one status query. A heartbeat
        every SSE_HEARTBEAT seconds keeps proxies from closing an idle stream,
        and the hard cap plus the client limit keep a forgotten tab from pinning
        a thread and an MPD query per second forever.
        """
        service = self.service
        if not service.config.web_enabled:
            self._not_found("/events")
            return
        server = self.server
        if isinstance(server, _LocalServer) and not server.acquire_sse_slot():
            # Refused rather than queued: this is a state feed, and a client
            # that cannot get one falls back to polling on its own.
            self._send_json(
                {
                    "error": "too_many_streams",
                    "message": f"Too many live event streams (limit {SSE_MAX_CLIENTS}).",
                    "hint": "Fall back to polling /player/status.",
                },
                HTTPStatus.SERVICE_UNAVAILABLE,
            )
            return
        try:
            self._pump_events(service)
        finally:
            if isinstance(server, _LocalServer):
                server.release_sse_slot()

    def _pump_events(self, service: ApiService) -> None:
        """Write state frames until the deadline, the client goes, or time is up."""
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", NO_STORE)
        # Without this nginx and friends buffer the whole stream and the UI
        # sees nothing until the connection ends.
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        self.close_connection = True

        deadline = time.monotonic() + SSE_MAX_SECONDS
        next_beat = time.monotonic() + SSE_HEARTBEAT
        try:
            while time.monotonic() < deadline:
                try:
                    payload = service.player.status()
                except StreamBridgeError as exc:
                    # One bad MPD moment ends the stream with a named event
                    # rather than an exception: the UI can fall back to
                    # polling, and a reconnect still works.
                    self._write_event(
                        "error",
                        {"error": type(exc).__name__, "message": exc.message},
                    )
                    return
                self._write_event("state", payload)
                if time.monotonic() >= next_beat:
                    self._write_event("heartbeat", {"at": round(time.time(), 1)})
                    next_beat = time.monotonic() + SSE_HEARTBEAT
                time.sleep(SSE_INTERVAL)
        except (BrokenPipeError, ConnectionResetError, OSError):
            log.debug("SSE client disconnected")
        else:
            self._write_event("timeout", {"seconds": SSE_MAX_SECONDS})

    def _write_event(self, event: str, data: dict[str, Any]) -> None:
        """Write one SSE frame, flushed.

        The event name is from a fixed set and the payload is JSON, so no
        newline in a value can corrupt the framing.
        """
        frame = f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
        self.wfile.write(frame.encode("utf-8"))
        self.wfile.flush()

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
        # A signed media URL can already be rejected a second after it was
        # handed out, which is routine rather than exceptional. Resolving once
        # more and trying again turns most of those into a working stream, and
        # it only ever happens before the first byte is sent, so no response
        # has been committed and the retry stays invisible to the client.
        last_status: int | None = None
        for attempt in (1, 2):
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

            try:
                source = self._open_proxied(info, range_header)
            except urllib.error.HTTPError as exc:
                last_status = exc.code
                log.warning("Upstream HTTP %s for %s (attempt %d)", exc.code, video_id, attempt)
                if attempt == 1 and exc.code in RETRY_UPSTREAM:
                    continue
                self._send_json(
                    {
                        "code": "UPSTREAM_ERROR",
                        "error": "upstream_error",
                        "message": f"Upstream HTTP {exc.code}",
                    },
                    HTTPStatus.BAD_GATEWAY,
                )
                return
            except SourceUnavailableError as exc:
                self._send_json(
                    {
                        "code": "UPSTREAM_ERROR",
                        "error": "upstream_unreachable",
                        "message": exc.message,
                    },
                    HTTPStatus.BAD_GATEWAY,
                )
                return

            try:
                self._relay(source, info)
            finally:
                # Always close the upstream socket, including on client abort.
                with contextlib.suppress(Exception):
                    source.close()
            return

        log.debug("Giving up on %s after a retry (last status %s)", video_id, last_status)

    def _open_proxied(self, info: StreamInfo, range_header: str | None) -> HTTPResponse:
        """Open the upstream source.

        Raises :class:`urllib.error.HTTPError` for an upstream status and
        :class:`SourceUnavailableError` when the host cannot be reached. The
        caller decides whether to retry, because only it knows whether a
        response has already been sent.
        """
        extra = {"Range": range_header} if range_header else None
        try:
            return open_with_headers(info.url, info.headers, extra=extra)
        except urllib.error.HTTPError:
            # The cached URL is not good any more. Drop it so a retry, or the
            # next request, resolves a fresh one.
            self.service.resolver.invalidate(info.video_id)
            raise
        except (urllib.error.URLError, ValueError, OSError) as exc:
            self.service.resolver.invalidate(info.video_id)
            raise SourceUnavailableError(
                f"Upstream unreachable for {info.video_id}.",
                hint=str(exc)[:200],
            ) from exc

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
        except (BrokenPipeError, ConnectionResetError, TimeoutError, OSError):
            # The player closed the connection or stopped reading: track
            # changed, seek, stop, or a stalled client hit the socket timeout.
            # All are normal endings for a stream. The upstream response is
            # closed by the caller's finally block, so no descriptor leaks and
            # no child process survives.
            #
            # TimeoutError is a subclass of OSError, so it is named for clarity
            # rather than because it needs naming.
            log.info("Client disconnected from %s after %d bytes", info.video_id, written)
        log.debug("Streamed %s: %d bytes", info.video_id, written)


class _LocalServer(ThreadingHTTPServer):
    """Threaded server with a listen backlog and a cap on concurrent SSE feeds.

    Kept as a named subclass so the SSE budget is enforced in one place rather
    than at every call site.
    """

    # A larger listen backlog than the stdlib default of 5: a browser opening
    # the app fires a burst of asset requests plus an SSE reconnect, and 5 is
    # small enough that a burst can be refused outright, which shows up as a
    # failed asset load rather than merely a slow page.
    request_queue_size = 128
    allow_reuse_address = True

    def handle_error(self, request: Any, client_address: Any) -> None:
        """Log a handler crash without the stdlib's full traceback noise.

        socketserver prints an unhandled exception as a traceback at ERROR.
        For a media server the common case is not a bug: a player seeking or
        switching track drops the connection mid-response, and a browser closing
        a tab does the same. Those are normal endings, and a traceback per skip
        buries the crashes that would matter. Genuinely unexpected errors still
        get the traceback, at ERROR.
        """
        exc = sys.exc_info()[1]
        if isinstance(exc, (BrokenPipeError, ConnectionResetError, TimeoutError)):
            log.debug("Client %s disconnected", client_address)
            return
        log.error("Unhandled error serving %s", client_address, exc_info=exc)

    # Concurrent SSE feeds. Incremented and decremented around the stream; a
    # simple counter is enough because the bound is what matters, not identity.
    sse_clients = 0
    sse_lock = threading.Lock()

    def acquire_sse_slot(self) -> bool:
        """Reserve a slot for one SSE feed, or refuse when the budget is spent."""
        with self.sse_lock:
            if self.sse_clients >= SSE_MAX_CLIENTS:
                return False
            self.sse_clients += 1
            return True

    def release_sse_slot(self) -> None:
        with self.sse_lock:
            if self.sse_clients > 0:
                self.sse_clients -= 1


def make_server(
    config: Config,
    *,
    service: ApiService | None = None,
    log_access: bool = False,
) -> ThreadingHTTPServer:
    """Create a threaded HTTP server on the configured address.

    Loopback is the default and stays unrestricted.

    ``log_access`` turns on per-request logging with the (redacted) peer
    address. The caller decides, because the caller is what knows the log
    level; inferring it here from the logger's state would work only if
    logging happened to be configured first, which is a fragile thing to rely
    on. Serving a network address
    is an explicit opt-in (``server.allow_lan``) and additionally requires
    ``server.access_token``; the rule itself lives in
    :func:`streambridge.config.lan_bind_problem` so the CLI enforces the same
    thing before it gets here.
    """
    problem = lan_bind_problem(config)
    if problem is not None:
        raise ValidationError(problem, hint="Set server.host to 127.0.0.1 for desktop-only use.")
    api = service or ApiService(config)
    # Access logging is opt-in. On, each request is logged with its redacted
    # peer, which is the only way to answer "did my phone even get through?".
    # Off by default, because a client IP is exactly the kind of thing this
    # project does not write down unless asked to.
    handler = type(
        "BoundApiHandler",
        (ApiHandler,),
        {"service": api, "quiet": not log_access},
    )
    # A backlog is what the listen queue is set to. The default of 5 is small
    # enough that a burst of browser requests plus an SSE reconnect can be
    # refused outright, which surfaces as a failed asset load rather than a
    # slow page.
    # SSE handlers hold their thread for up to SSE_MAX_SECONDS, and the whole
    # point of that cap is to be enforced, so it has to be a real limit.
    server = _LocalServer((config.host, config.port), handler)
    server.daemon_threads = True
    return server


def iter_relay(source: HTTPResponse) -> Iterator[bytes]:
    """Public alias used by tests to drive a relay without a socket."""
    return iter_chunks(source)
