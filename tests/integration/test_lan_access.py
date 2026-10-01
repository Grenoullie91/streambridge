"""LAN access for the Android companion app.

Two things are asserted here, and they are the whole security argument for
putting a player on the network:

* enabling the network is opt-in, and impossible without a shared secret;
* once enabled, a client that is not on this machine must present that secret
  before it can read the queue or move the needle, while the browser UI on
  127.0.0.1 keeps working with no token at all.

The peer address is what the decision turns on, and a loopback socket cannot
produce a non-loopback peer, so most of these drive the handler directly with
a stub address. One test binds a real network interface to prove the socket
path agrees, and skips when the machine has none.
"""

from __future__ import annotations

import json
import socket
import threading
import urllib.error
import urllib.request
from collections.abc import Iterator
from dataclasses import replace
from email.message import Message
from typing import Any

import pytest

from conftest import INFO_PAYLOAD, SEARCH_PAYLOAD, FakeRunner
from streambridge.api import ApiHandler, ApiService, make_server
from streambridge.config import Config, is_lan_bind, is_loopback_host, lan_bind_problem
from streambridge.errors import ValidationError
from streambridge.resolver import StreamResolver
from streambridge.youtube import ExtractorClient

pytestmark = pytest.mark.integration

TOKEN = "s3cret-token-for-the-phone"
VALID_ID = "5NV6Rdv1a3I"


def _build(config: Config, fake_runner: FakeRunner) -> ApiService:
    fake_runner.add_json("ytsearch", SEARCH_PAYLOAD)
    fake_runner.add_json("watch?v=", INFO_PAYLOAD)
    client = ExtractorClient(config, runner=fake_runner, executable="yt-dlp")
    return ApiService(config, client=client, resolver=StreamResolver(config, client))


class _StubHandler(ApiHandler):
    """An ApiHandler with just enough state to answer the authorisation check.

    Instantiating the real handler would open a socket and run
    BaseHTTPRequestHandler.__init__; the decision under test reads exactly
    three things - the peer address, the request headers and the path.
    """

    def __init__(  # deliberately does not call super().__init__()
        self, config: Config, peer: str, path: str = "/player/status", **headers: str
    ) -> None:
        self.service = _StubService(config)
        self.client_address = (peer, 40000)
        self.path = path
        self.headers = Message()
        for name, value in headers.items():
            self.headers[name.replace("_", "-")] = value


class _StubService:
    """Minimal stand-in exposing only the ``config`` the check reads."""

    def __init__(self, config: Config) -> None:
        self.config = config


def _tokenized(config: Config) -> Config:
    return replace(config, allow_lan=True, access_token=TOKEN, host="192.0.2.5")


class TestLoopbackClassification:
    @pytest.mark.parametrize("host", ["127.0.0.1", "127.0.0.2", "::1", "localhost", "LOCALHOST"])
    def test_loopback_spellings(self, host: str) -> None:
        assert is_loopback_host(host)

    @pytest.mark.parametrize(
        "host", ["0.0.0.0", "192.0.2.10", "10.0.0.1", "example.com", "::", "8.8.8.8", ""]
    )
    def test_everything_else_is_a_lan_bind(self, host: str) -> None:
        assert not is_loopback_host(host)


class TestBindPolicy:
    def test_default_config_is_loopback_only(self, config: Config) -> None:
        assert config.allow_lan is False
        assert config.access_token is None
        assert not is_lan_bind(config)
        assert lan_bind_problem(config) is None

    def test_lan_without_opt_in_is_refused(self, config: Config) -> None:
        problem = lan_bind_problem(replace(config, host="192.0.2.5"))
        assert problem is not None
        assert "loopback" in problem
        # The message has to say how to proceed, not just that it failed.
        assert "allow_lan" in problem
        assert "access_token" in problem

    def test_lan_without_token_is_refused(self, config: Config) -> None:
        problem = lan_bind_problem(replace(config, host="192.0.2.5", allow_lan=True))
        assert problem is not None
        assert "access_token" in problem

    def test_lan_with_token_is_allowed(self, config: Config) -> None:
        assert lan_bind_problem(_tokenized(config)) is None

    def test_allow_lan_alone_does_not_change_a_loopback_bind(self, config: Config) -> None:
        # Opting in on a loopback host must stay silent about tokens: there is
        # no network surface to protect.
        assert lan_bind_problem(replace(config, allow_lan=True)) is None

    def test_server_factory_enforces_the_same_rule(self, config: Config) -> None:
        with pytest.raises(ValidationError):
            make_server(replace(config, host="192.0.2.5"))
        with pytest.raises(ValidationError):
            make_server(replace(config, host="192.0.2.5", allow_lan=True))

    def test_config_parser_reads_the_new_keys(self, tmp_path: Any) -> None:
        from streambridge.config import config_from_mapping

        parsed = config_from_mapping(
            {"server": {"host": "10.0.0.9", "allow_lan": True, "access_token": "abc"}}
        )
        assert parsed.allow_lan is True
        assert parsed.access_token == "abc"
        assert lan_bind_problem(parsed) is None

    def test_allow_lan_must_be_a_boolean(self) -> None:
        from streambridge.config import config_from_mapping
        from streambridge.errors import ConfigError

        with pytest.raises(ConfigError):
            config_from_mapping({"server": {"allow_lan": "yes"}})


class TestTokenEnforcement:
    def test_network_client_without_token_is_refused(self, config: Config) -> None:
        handler = _StubHandler(_tokenized(config), peer="192.0.2.77")
        assert handler._authorised("/player/status") is False

    def test_network_client_with_wrong_token_is_refused(self, config: Config) -> None:
        handler = _StubHandler(
            _tokenized(config), peer="192.0.2.77", Authorization=f"Bearer {TOKEN}x"
        )
        assert handler._authorised("/player/status") is False

    @pytest.mark.parametrize("prefix", ["Bearer", "bearer", "BEARER"])
    def test_bearer_scheme_is_case_insensitive(self, config: Config, prefix: str) -> None:
        handler = _StubHandler(
            _tokenized(config), peer="192.0.2.77", Authorization=f"{prefix} {TOKEN}"
        )
        assert handler._authorised("/player/status") is True

    def test_plain_header_is_accepted(self, config: Config) -> None:
        handler = _StubHandler(_tokenized(config), peer="192.0.2.77", X_Streambridge_Token=TOKEN)
        assert handler._authorised("/player/status") is True

    def test_non_bearer_scheme_is_not_a_fallback(self, config: Config) -> None:
        # "Basic <token>" must not be mistaken for a valid credential, or the
        # check would depend on a client spelling it exactly one way.
        handler = _StubHandler(
            _tokenized(config), peer="192.0.2.77", Authorization=f"Basic {TOKEN}"
        )
        assert handler._authorised("/player/status") is False

    def test_empty_bearer_is_refused(self, config: Config) -> None:
        handler = _StubHandler(_tokenized(config), peer="192.0.2.77", Authorization="Bearer")
        assert handler._authorised("/player/status") is False

    @pytest.mark.parametrize(
        "path",
        ["/player/status", "/queue", "/search?q=x", "/favorites", "/history", f"/info/{VALID_ID}"],
    )
    def test_every_state_reading_path_needs_the_token(self, config: Config, path: str) -> None:
        handler = _StubHandler(_tokenized(config), peer="10.0.0.9", path=path)
        assert handler._authorised(path) is False

    @pytest.mark.parametrize(
        "path", ["/player/play", "/player/volume", "/queue/add", "/favorites/add"]
    )
    def test_mutations_need_the_token_too(self, config: Config, path: str) -> None:
        handler = _StubHandler(_tokenized(config), peer="10.0.0.9", path=path)
        assert handler._authorised(path) is False

    @pytest.mark.parametrize(
        "path",
        [
            "/",
            "/index.html",
            "/app.js",
            "/app.css",
            "/health",
            "/version",
            f"/thumbnail/{VALID_ID}",
        ],
    )
    def test_shell_paths_stay_open(self, config: Config, path: str) -> None:
        # These are what a client needs in order to render "this server wants a
        # token" at all, and none of them expose the queue or the player.
        handler = _StubHandler(_tokenized(config), peer="10.0.0.9", path=path)
        assert handler._authorised(path) is True

    def test_ipv6_loopback_peer_is_trusted(self, config: Config) -> None:
        handler = _StubHandler(_tokenized(config), peer="::1")
        assert handler._authorised("/player/status") is True


class TestNoTokenConfigured:
    def test_everything_is_open_when_no_token_is_set(self, config: Config) -> None:
        # The default install. This is the assertion that keeps the change
        # backwards compatible: no token, no gate, whatever the peer.
        handler = _StubHandler(config, peer="192.0.2.77", path="/player/status")
        assert handler._authorised("/player/status") is True


def _lan_address() -> str | None:
    """A private address on a live interface, or None if there is none."""
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            # No packet is sent; connect() only picks a source address.
            probe.connect(("192.0.2.1", 9))
            address = probe.getsockname()[0]
        finally:
            probe.close()
    except OSError:
        return None
    if address.startswith("127."):
        return None
    return address


class TestOverARealSocket:
    """The decision above, exercised through actual HTTP on a real interface."""

    @pytest.fixture
    def lan_server(self, config: Config, fake_runner: FakeRunner) -> Iterator[str]:
        address = _lan_address()
        if address is None:
            pytest.skip("no non-loopback IPv4 interface available")
        bound = replace(config, host=address, allow_lan=True, access_token=TOKEN, port=0)
        httpd = make_server(bound, service=_build(bound, fake_runner))
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://{address}:{httpd.server_address[1]}"
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=5)

    @staticmethod
    def _get(url: str, token: str | None = None) -> tuple[int, Any]:
        request = urllib.request.Request(url, headers={"Accept": "application/json"})
        if token is not None:
            request.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read() or b"{}")

    def test_missing_token_gets_401(self, lan_server: str) -> None:
        status, payload = self._get(f"{lan_server}/player/status")
        assert status == 401
        assert payload["code"] == "UNAUTHORIZED"

    def test_wrong_token_gets_401(self, lan_server: str) -> None:
        status, _payload = self._get(f"{lan_server}/queue", token="not-the-token")
        assert status == 401

    def test_right_token_is_served(self, lan_server: str) -> None:
        status, payload = self._get(f"{lan_server}/player/status", token=TOKEN)
        assert status == 200
        assert "state" in payload

    def test_health_stays_open_for_probing(self, lan_server: str) -> None:
        status, payload = self._get(f"{lan_server}/health")
        assert status == 200
        assert payload["status"] == "ok"

    def test_token_is_not_echoed_in_the_error(self, lan_server: str) -> None:
        status, payload = self._get(f"{lan_server}/player/status")
        assert status == 401
        assert TOKEN not in json.dumps(payload)


class TestEnvironmentOverrides:
    """STREAMBRIDGE_* variables, which arrive as text.

    The LAN opt-in is the one boolean a user is likely to set from a systemd
    unit rather than a config file, and a coercer that only accepted a real
    bool would reject it there while accepting it in TOML.
    """

    @pytest.mark.parametrize("raw", ["true", "TRUE", "1", "yes", "on", " true "])
    def test_truthy_spellings(self, raw: str) -> None:
        from streambridge.config import _as_env_bool

        assert _as_env_bool(raw, "X") is True

    @pytest.mark.parametrize("raw", ["false", "FALSE", "0", "no", "off"])
    def test_falsy_spellings(self, raw: str) -> None:
        from streambridge.config import _as_env_bool

        assert _as_env_bool(raw, "X") is False

    @pytest.mark.parametrize("raw", ["", "maybe", "2", "truthy"])
    def test_anything_else_is_refused_by_name(self, raw: str) -> None:
        from streambridge.config import _as_env_bool
        from streambridge.errors import ConfigError

        with pytest.raises(ConfigError) as exc:
            _as_env_bool(raw, "STREAMBRIDGE_ALLOW_LAN")
        assert "STREAMBRIDGE_ALLOW_LAN" in exc.value.message

    def test_the_environment_can_turn_lan_access_on(self, tmp_path: Any) -> None:
        from streambridge.config import load_config

        config = load_config(
            environ={
                "STREAMBRIDGE_HOST": "192.0.2.5",
                "STREAMBRIDGE_ALLOW_LAN": "true",
                "STREAMBRIDGE_ACCESS_TOKEN": "s3cret",
            },
            overrides={},
        )
        assert config.allow_lan is True
        assert config.access_token == "s3cret"
        assert lan_bind_problem(config) is None

    def test_the_environment_alone_cannot_open_the_network(self) -> None:
        from streambridge.config import load_config

        config = load_config(environ={"STREAMBRIDGE_HOST": "192.0.2.5"}, overrides={})
        assert lan_bind_problem(config) is not None


class TestMpdStillReachesTheStream:
    """The failure mode LAN access would otherwise introduce.

    When the server listens on a network address, the URL written into the
    MPD queue must still be loopback. MPD fetches the audio itself and sends
    no token, so a queue entry holding the network address is refused with a
    401 and playback stops - while the browser, sitting on the same machine
    and talking to the loopback exemption, looks perfectly healthy.

    This is the kind of bug that passes every unit test and only shows up
    once a phone has been connected, so it gets its own.
    """

    def test_a_loopback_bind_advertises_its_own_address(self, config: Config) -> None:
        assert config.stream_base_url == f"http://127.0.0.1:{config.port}"
        assert config.stream_url("5NV6Rdv1a3I") == (
            f"http://127.0.0.1:{config.port}/stream/5NV6Rdv1a3I"
        )

    @pytest.mark.parametrize("host", ["192.0.2.20", "0.0.0.0", "::"])
    def test_a_network_bind_still_advertises_loopback(self, config: Config, host: str) -> None:
        # `config` here is only used for its port; the host is what matters.
        bound = replace(config, host=host, allow_lan=True, access_token="t")
        assert not is_loopback_host(host)
        assert bound.stream_base_url == f"http://127.0.0.1:{bound.port}"
        assert bound.stream_url("5NV6Rdv1a3I").startswith("http://127.0.0.1:")

    def test_the_advertised_url_never_carries_a_token(self, config: Config) -> None:
        # There is nowhere to put one: MPD is handed a URL, and a token in a
        # playlist would end up in MPD's database and in ncmpcpp's queue view.
        bound = replace(config, host="192.0.2.20", allow_lan=True, access_token="a-token")
        assert "a-token" not in bound.stream_url("5NV6Rdv1a3I")
        assert "?" not in bound.stream_url("5NV6Rdv1a3I")

    def test_the_client_base_url_is_still_the_listening_address(self, config: Config) -> None:
        # The other half of the same distinction: clients address the socket
        # that is actually listening.
        bound = replace(config, host="192.0.2.20", allow_lan=True, access_token="t")
        assert bound.base_url == f"http://192.0.2.20:{bound.port}"
        assert bound.stream_base_url != bound.base_url

    def test_the_port_is_preserved(self, config: Config) -> None:
        bound = replace(config, host="192.0.2.20", port=9999, allow_lan=True, access_token="t")
        assert bound.stream_base_url == "http://127.0.0.1:9999"
