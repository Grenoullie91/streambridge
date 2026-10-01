"""HTTP contract of the web-facing API: player, queue, library, assets.

Runs against a real loopback socket with yt-dlp mocked and MPD replaced by the
in-memory fake, so the whole request path is exercised without touching the
network or the developer's MPD.
"""

from __future__ import annotations

import dataclasses
import json
import threading
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator
from typing import Any

import pytest

from conftest import FAKE_EXECUTABLE, INFO_PAYLOAD, SEARCH_PAYLOAD, FakeMpd, FakeRunner
from streambridge.api import ApiService, make_server
from streambridge.config import Config
from streambridge.errors import ValidationError
from streambridge.library import LibraryStore
from streambridge.models import Track, validate_video_id
from streambridge.mpd import MpdClient
from streambridge.player import PlayerService
from streambridge.resolver import StreamResolver
from streambridge.youtube import ExtractorClient

VIDEO = "5NV6Rdv1a3I"
SECOND = "CCHdMIEGaaM"


def track(video_id: str = VIDEO, title: str = "Get Lucky") -> Track:
    return Track(id=video_id, title=title, artist="Daft Punk", duration=249)


class Client:
    """Tiny HTTP client that never follows redirects and never raises on 4xx."""

    def __init__(self, base: str) -> None:
        self.base = base

    def get(self, path: str) -> tuple[int, Any, dict[str, str]]:
        request = urllib.request.Request(
            f"{self.base}{path}", headers={"Accept": "application/json"}
        )
        return self._send(request)

    def post(
        self, path: str, body: dict[str, Any] | None = None
    ) -> tuple[int, Any, dict[str, str]]:
        data = json.dumps(body if body is not None else {}).encode()
        request = urllib.request.Request(
            f"{self.base}{path}",
            data=data,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        return self._send(request)

    def raw(
        self,
        path: str,
        method: str = "GET",
        data: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, Any, dict[str, str]]:
        return self._send(
            urllib.request.Request(
                f"{self.base}{path}", data=data, headers=headers or {}, method=method
            )
        )

    @staticmethod
    def _send(request: urllib.request.Request) -> tuple[int, Any, dict[str, str]]:
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *_a: Any, **_k: Any) -> None:
                return None

        opener = urllib.request.build_opener(NoRedirect)
        try:
            with opener.open(request, timeout=20) as response:
                body = response.read()
                head = dict(response.headers)
                status = response.status
        except urllib.error.HTTPError as exc:
            body = exc.read()
            head = dict(exc.headers or {})
            status = exc.code
        try:
            return status, json.loads(body) if body else None, head
        except json.JSONDecodeError:
            return status, body, head


@pytest.fixture
def api_server(config: Config, fake_runner: FakeRunner) -> Iterator[tuple[Client, FakeMpd]]:
    fake_runner.add_json("ytsearch", SEARCH_PAYLOAD)
    fake_runner.add_json("watch?v=", INFO_PAYLOAD)
    client = ExtractorClient(config, runner=fake_runner, executable=FAKE_EXECUTABLE)
    mpd = FakeMpd(config)
    library = LibraryStore(config.state_directory)
    service = ApiService(
        config,
        client=client,
        resolver=StreamResolver(config, client),
        mpd=mpd,  # type: ignore[arg-type]
        library=library,
    )
    # Port 0: the shared `config` fixture names a fixed port, which would make
    # the second test in this module fail to bind.
    server = make_server(dataclasses.replace(config, port=0), service=service)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield Client(f"http://127.0.0.1:{server.server_address[1]}"), mpd
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


class TestPlayerStatus:
    def test_stopped(self, api_server: tuple[Client, FakeMpd]) -> None:
        http, _ = api_server
        status, body, _ = http.get("/player/status")
        assert status == 200
        assert body["state"] == "stopped"
        assert body["current"] is None
        assert body["playlist_length"] == 0
        assert body["volume"] >= 0
        assert "timestamp" in body

    def test_after_playback(self, api_server: tuple[Client, FakeMpd]) -> None:
        http, mpd = api_server
        http.post("/queue/add", {"ids": [VIDEO], "tracks": [track().to_dict()]})
        mpd.play_id(1)
        _, body, _ = http.get("/player/status")
        assert body["state"] == "playing"
        assert body["current"]["video_id"] == VIDEO
        assert body["duration"] == 249

    def test_json_only(self, api_server: tuple[Client, FakeMpd]) -> None:
        http, _ = api_server
        _, _, headers = http.get("/player/status")
        assert headers["Content-Type"].startswith("application/json")


class TestTransport:
    @pytest.fixture(autouse=True)
    def _seed(self, api_server: tuple[Client, FakeMpd]) -> None:
        http, _ = api_server
        http.post(
            "/queue/add",
            {
                "ids": [VIDEO, SECOND],
                "tracks": [track().to_dict(), track(SECOND).to_dict()],
                "play": True,
            },
        )

    def test_pause_and_play(self, api_server: tuple[Client, FakeMpd]) -> None:
        http, _ = api_server
        _, body, _ = http.post("/player/pause")
        assert body["state"] == "paused"
        _, body, _ = http.post("/player/play", {"position": 1})
        assert body["state"] == "playing"
        assert body["position"] == 1

    def test_pause_is_idempotent(self, api_server: tuple[Client, FakeMpd]) -> None:
        http, _ = api_server
        http.post("/player/pause")
        status, body, _ = http.post("/player/pause")
        assert status == 200
        assert body["state"] == "paused"

    def test_stop(self, api_server: tuple[Client, FakeMpd]) -> None:
        http, _ = api_server
        _, body, _ = http.post("/player/stop")
        assert body["state"] == "stopped"

    def test_next_and_previous(self, api_server: tuple[Client, FakeMpd]) -> None:
        http, _ = api_server
        _, body, _ = http.post("/player/next")
        assert body["position"] == 2
        _, body, _ = http.post("/player/previous")
        assert body["position"] == 1

    def test_seek(self, api_server: tuple[Client, FakeMpd]) -> None:
        http, _ = api_server
        _, body, _ = http.post("/player/seek", {"seconds": 60})
        assert body["elapsed"] == 60

    @pytest.mark.parametrize(
        ("path", "payload"),
        [
            ("/player/seek", {}),
            ("/player/seek", {"seconds": -5}),
            ("/player/seek", {"seconds": "middle"}),
            ("/player/volume", {}),
            ("/player/volume", {"volume": 200}),
            ("/player/volume", {"volume": "loud"}),
            ("/player/mute", {"muted": "yes"}),
            ("/player/modes", {"shuffle": 1}),
            ("/player/play", {"position": 99}),
        ],
    )
    def test_rejects_bad_input(
        self, api_server: tuple[Client, FakeMpd], path: str, payload: dict[str, Any]
    ) -> None:
        http, _ = api_server
        status, body, _ = http.post(path, payload)
        assert status == 400, body
        assert body["code"] == "VALIDATION_ERROR"
        assert body["message"]

    def test_volume_and_mute(self, api_server: tuple[Client, FakeMpd]) -> None:
        http, _ = api_server
        _, body, _ = http.post("/player/volume", {"volume": 33})
        assert body["volume"] == 33
        _, body, _ = http.post("/player/mute", {"muted": True})
        assert body["muted"] is True
        _, body, _ = http.post("/player/mute", {"muted": False})
        assert body["volume"] == 33

    def test_modes(self, api_server: tuple[Client, FakeMpd]) -> None:
        http, _ = api_server
        _, body, _ = http.post("/player/modes", {"shuffle": True, "repeat_one": True})
        assert body["shuffle"] is True
        assert body["repeat_one"] is True


class TestQueue:
    def test_add_and_read(self, api_server: tuple[Client, FakeMpd]) -> None:
        http, _ = api_server
        status, body, _ = http.post("/queue/add", {"ids": [VIDEO], "tracks": [track().to_dict()]})
        # 201: the queue really did gain an entry.
        assert status == 201
        assert body["added"] == 1
        _, listed, _ = http.get("/queue")
        assert listed["length"] == 1
        assert listed["items"][0]["video_id"] == VIDEO
        assert listed["items"][0]["title"]

    def test_add_uses_the_local_stream_url(self, api_server: tuple[Client, FakeMpd]) -> None:
        """MPD must always receive a local /stream/<id> URL, never a YouTube one."""
        http, mpd = api_server
        http.post("/queue/add", {"ids": [VIDEO], "tracks": [track().to_dict()]})
        assert mpd.items[0].file.endswith(f"/stream/{VIDEO}")
        assert mpd.items[0].file.startswith("http://127.0.0.1:")
        assert "youtube.com" not in mpd.items[0].file
        assert "googlevideo" not in mpd.items[0].file

    def test_add_many(self, api_server: tuple[Client, FakeMpd]) -> None:
        http, _ = api_server
        _, body, _ = http.post(
            "/queue/add",
            {"ids": [VIDEO, SECOND], "tracks": [track().to_dict(), track(SECOND).to_dict()]},
        )
        assert body["added"] == 2
        assert body["queue"]["length"] == 2

    def test_add_with_play_inserts_at_the_current_song(
        self, api_server: tuple[Client, FakeMpd]
    ) -> None:
        http, _ = api_server
        third = "a5uQMwRMHcs"
        http.post(
            "/queue/add",
            {
                "ids": [VIDEO, SECOND],
                "tracks": [track().to_dict(), track(SECOND).to_dict()],
            },
        )
        http.post("/player/play", {"position": 1})
        _, body, _ = http.post(
            "/queue/add", {"ids": [third], "tracks": [track(third).to_dict()], "position": 1}
        )
        assert [row["video_id"] for row in body["queue"]["items"]] == [third, VIDEO, SECOND]

    def test_remove(self, api_server: tuple[Client, FakeMpd]) -> None:
        http, _ = api_server
        http.post(
            "/queue/add",
            {"ids": [VIDEO, SECOND], "tracks": [track().to_dict(), track(SECOND).to_dict()]},
        )
        _, body, _ = http.post("/queue/remove", {"position": 1})
        assert [row["video_id"] for row in body["queue"]["items"]] == [SECOND]

    def test_remove_out_of_range(self, api_server: tuple[Client, FakeMpd]) -> None:
        http, _ = api_server
        status, body, _ = http.post("/queue/remove", {"position": 5})
        assert status == 400
        assert "outside" in body["message"]

    def test_move(self, api_server: tuple[Client, FakeMpd]) -> None:
        http, _ = api_server
        http.post(
            "/queue/add",
            {
                "ids": [VIDEO, SECOND, "a5uQMwRMHcs"],
                "tracks": [
                    track().to_dict(),
                    track(SECOND).to_dict(),
                    track("a5uQMwRMHcs").to_dict(),
                ],
            },
        )
        _, body, _ = http.post("/queue/move", {"from": 1, "to": 3})
        assert [row["video_id"] for row in body["queue"]["items"]] == [
            SECOND,
            "a5uQMwRMHcs",
            VIDEO,
        ]

    def test_clear(self, api_server: tuple[Client, FakeMpd]) -> None:
        http, _ = api_server
        http.post("/queue/add", {"ids": [VIDEO], "tracks": [track().to_dict()]})
        _, body, _ = http.post("/queue/clear")
        assert body["length"] == 0

    @pytest.mark.parametrize(
        ("path", "payload"),
        [
            ("/queue/add", {}),
            ("/queue/add", {"ids": []}),
            ("/queue/add", {"ids": "one"}),
            ("/queue/add", {"ids": [1, 2]}),
            ("/queue/add", {"ids": ["nope"]}),
            ("/queue/add", {"ids": [VIDEO] * 201}),
            ("/queue/remove", {}),
            ("/queue/remove", {"position": "first"}),
            ("/queue/remove", {"position": True}),
            ("/queue/move", {"from": 1}),
            ("/queue/move", {"from": 1, "to": 0}),
        ],
    )
    def test_rejects_bad_input(
        self, api_server: tuple[Client, FakeMpd], path: str, payload: dict[str, Any]
    ) -> None:
        http, _ = api_server
        status, body, _ = http.post(path, payload)
        assert status == 400, body
        assert body["code"] == "VALIDATION_ERROR"


class TestLibrary:
    def test_favorite_round_trip(self, api_server: tuple[Client, FakeMpd]) -> None:
        http, _ = api_server
        _, body, _ = http.post("/favorites/toggle", {"id": VIDEO})
        assert body["favorite"] is True
        _, listing, _ = http.get("/favorites")
        assert listing["ids"] == [VIDEO]
        assert listing["items"][0]["track"]["title"]

        _, body, _ = http.post("/favorites/toggle", {"id": VIDEO})
        assert body["favorite"] is False
        _, listing, _ = http.get("/favorites")
        assert listing["ids"] == []

    def test_add_and_remove(self, api_server: tuple[Client, FakeMpd]) -> None:
        http, _ = api_server
        http.post("/favorites/add", {"id": VIDEO})
        _, body, _ = http.post("/favorites/remove", {"id": VIDEO})
        assert body["favorite"] is False

    def test_clear(self, api_server: tuple[Client, FakeMpd]) -> None:
        http, _ = api_server
        http.post("/favorites/add", {"id": VIDEO})
        _, body, _ = http.post("/favorites/clear")
        assert body["removed"] == 1

    def test_history_records_playback(self, api_server: tuple[Client, FakeMpd]) -> None:
        http, mpd = api_server
        http.post("/queue/add", {"ids": [VIDEO], "tracks": [track().to_dict()], "play": True})
        http.get("/player/status")
        _, body, _ = http.get("/history")
        assert body["length"] == 1
        assert body["items"][0]["track"]["id"] == VIDEO
        assert body["items"][0]["played_at"] > 0
        del mpd

    def test_history_clear(self, api_server: tuple[Client, FakeMpd]) -> None:
        http, _ = api_server
        http.post("/queue/add", {"ids": [VIDEO], "tracks": [track().to_dict()], "play": True})
        http.get("/player/status")
        _, body, _ = http.post("/history/clear")
        assert body["removed"] == 1

    @pytest.mark.parametrize("path", ["/favorites/add", "/favorites/remove", "/favorites/toggle"])
    def test_rejects_bad_ids(self, api_server: tuple[Client, FakeMpd], path: str) -> None:
        http, _ = api_server
        for payload in ({"id": "../../etc/passwd"}, {"id": ""}, {}):
            status, body, _ = http.post(path, payload)
            assert status == 400
            assert body["code"] == "VALIDATION_ERROR"


class TestRequestHandling:
    def test_unknown_path(self, api_server: tuple[Client, FakeMpd]) -> None:
        http, _ = api_server
        status, body, _ = http.get("/does-not-exist")
        assert status == 404
        assert body["code"] == "NOT_FOUND"

    def test_method_not_allowed(self, api_server: tuple[Client, FakeMpd]) -> None:
        http, _ = api_server
        for path in ("/search", "/health", "/queue", "/player/status", "/favorites"):
            status, _, headers = http.post(path)
            assert status == 405, path
            assert "GET" in headers.get("Allow", "")

    def test_body_limits(self, api_server: tuple[Client, FakeMpd]) -> None:
        http, _ = api_server
        status, body, _ = http.post("/player/volume", {"volume": 1, "padding": "x" * 70_000})
        assert status == 400
        assert "too large" in body["message"]

    def test_malformed_json(self, api_server: tuple[Client, FakeMpd]) -> None:
        http, _ = api_server
        status, body, _ = http.raw(
            "/player/volume",
            method="POST",
            data=b"{oops",
            headers={"Content-Type": "application/json"},
        )
        assert status == 400
        assert body["code"] == "VALIDATION_ERROR"

    def test_json_array_body_is_refused(self, api_server: tuple[Client, FakeMpd]) -> None:
        http, _ = api_server
        status, _, _ = http.raw(
            "/queue/add",
            method="POST",
            data=b"[1,2,3]",
            headers={"Content-Type": "application/json"},
        )
        assert status == 400

    def test_error_envelope_never_leaks_a_traceback(
        self, api_server: tuple[Client, FakeMpd]
    ) -> None:
        http, _ = api_server
        _, body, _ = http.post("/favorites/toggle", {"id": "bad"})
        assert set(body) <= {"error", "code", "message", "hint", "yt_dlp"}
        assert "Traceback" not in json.dumps(body)


class TestWebAssets:
    def test_root_serves_html_for_a_browser(self, api_server: tuple[Client, FakeMpd]) -> None:
        http, _ = api_server
        status, body, headers = http.raw("/", headers={"Accept": "text/html"})
        assert status == 200
        assert headers["Content-Type"].startswith("text/html")
        html = body.decode() if isinstance(body, bytes) else str(body)
        assert "StreamBridge" in html
        assert '<script src="/app.js"' in html

    def test_root_serves_json_for_a_client(self, api_server: tuple[Client, FakeMpd]) -> None:
        http, _ = api_server
        status, body, headers = http.raw("/", headers={"Accept": "application/json"})
        assert status == 200
        assert headers["Content-Type"].startswith("application/json")
        assert "/player/status" in body["endpoints"]
        assert body["web_ui"].startswith("http://127.0.0.1")

    @pytest.mark.parametrize(
        ("path", "content_type"),
        [
            ("/app.js", "text/javascript"),
            ("/app.css", "text/css"),
            ("/manifest.webmanifest", "application/manifest"),
            ("/assets/logo.svg", "image/svg+xml"),
        ],
    )
    def test_assets_are_served(
        self, api_server: tuple[Client, FakeMpd], path: str, content_type: str
    ) -> None:
        http, _ = api_server
        status, body, headers = http.raw(path)
        assert status == 200
        assert headers["Content-Type"].startswith(content_type)
        assert body
        assert headers["Content-Security-Policy"].startswith("default-src 'self'")

    def test_security_headers(self, api_server: tuple[Client, FakeMpd]) -> None:
        http, _ = api_server
        _, _, headers = http.raw("/app.css")
        assert headers["X-Content-Type-Options"] == "nosniff"
        assert headers["X-Frame-Options"] == "DENY"
        assert headers["Referrer-Policy"] == "no-referrer"

    def test_etag_revalidation(self, api_server: tuple[Client, FakeMpd]) -> None:
        http, _ = api_server
        _, _, headers = http.raw("/app.js")
        etag = headers["ETag"]
        status, _, _ = http.raw("/app.js", headers={"If-None-Match": etag})
        assert status == 304

    @pytest.mark.parametrize(
        "path",
        [
            "/../config.py",
            "/app.js/../../config.py",
            "/%2e%2e/config.py",
            "/assets/../../config.py",
            "/static/app.js",
            "/web/app.js",
        ],
    )
    def test_path_traversal_is_refused(self, api_server: tuple[Client, FakeMpd], path: str) -> None:
        http, _ = api_server
        status, _, _ = http.raw(path)
        assert status == 404
        assert "Config" not in str(status)

    def test_no_external_assets_are_referenced(self, api_server: tuple[Client, FakeMpd]) -> None:
        http, _ = api_server
        for path in ("/app.js", "/app.css"):
            _, body, _ = http.raw(path)
            text = body.decode() if isinstance(body, bytes) else str(body)
            for marker in ("//cdn.", "googleapis.com/gstatic", "unpkg", "jsdelivr", "cdnjs"):
                assert marker not in text, f"{marker} gefunden in {path}"


class TestThumbnails:
    def test_redirects_to_youtube(self, api_server: tuple[Client, FakeMpd]) -> None:
        http, _ = api_server
        status, _, headers = http.raw(f"/thumbnail/{VIDEO}")
        assert status == 302
        assert headers["Location"] == f"https://i.ytimg.com/vi/{VIDEO}/hqdefault.jpg"

    @pytest.mark.parametrize(
        "path", ["/thumbnail/short", "/thumbnail/../../etc", "/thumbnail/abcdefghijk12"]
    )
    def test_rejects_bad_ids(self, api_server: tuple[Client, FakeMpd], path: str) -> None:
        http, _ = api_server
        status, _, _ = http.raw(path)
        assert status in (400, 404)


class TestEvents:
    def test_streams_status_and_stays_usable(self, api_server: tuple[Client, FakeMpd]) -> None:
        http, _ = api_server
        frames: list[bytes] = []
        error: list[BaseException] = []

        def read() -> None:
            try:
                with urllib.request.urlopen(f"{http.base}/events", timeout=8) as response:
                    assert response.headers["Content-Type"].startswith("text/event-stream")
                    deadline = 6
                    while deadline > 0:
                        line = response.readline()
                        if not line:
                            break
                        if line.startswith(b"data:"):
                            frames.append(line)
                            deadline = 0
                            break
            except BaseException as exc:
                error.append(exc)

        thread = threading.Thread(target=read, daemon=True)
        thread.start()
        thread.join(timeout=12)
        assert not error, error
        assert frames, "kein Status-Frame empfangen"
        payload = json.loads(frames[0].split(b"data:", 1)[1])
        assert "state" in payload

    def test_server_survives_a_disconnecting_client(
        self, api_server: tuple[Client, FakeMpd]
    ) -> None:
        http, _ = api_server
        sock = urllib.request.urlopen(f"{http.base}/events", timeout=5)
        sock.close()
        status, _, _ = http.get("/health")
        assert status == 200


def test_mpd_is_only_reachable_through_the_service(config: Config) -> None:
    """The HTTP layer must never build its own MPD client."""
    service = ApiService(config)
    assert isinstance(service.player, PlayerService)
    assert isinstance(service.mpd, MpdClient)
    assert service.player.mpd is service.mpd


def test_queue_url_shape() -> None:
    """`Config.stream_url` is a plain formatter; validation happens upstream.

    The path is what matters for MPD and for the route matcher, so assert the
    exact shape and leave the id validation to validate_video_id.
    """
    config = Config()
    assert config.stream_url(VIDEO) == f"http://127.0.0.1:8787/stream/{VIDEO}"
    assert urllib.parse.urlsplit(config.stream_url(VIDEO)).path == f"/stream/{VIDEO}"
    with pytest.raises(ValidationError):
        validate_video_id("../../etc/passwd")
