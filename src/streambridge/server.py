"""streambridge-server: the local HTTP bridge.

Entry point for the long-running service. Everything reusable lives in
:mod:`streambridge.api`; this module owns argument parsing, dependency
validation, signal handling and shutdown.
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
import tomllib
from types import FrameType

from . import __version__
from .api import ApiService, make_server
from .config import Config, load_config
from .errors import ConfigError, DependencyError, StreamBridgeError
from .health import build_report
from .logging import log_level_from_env, setup_logging
from .youtube import ExtractorClient

log = logging.getLogger("streambridge.server")

EXIT_OK = 0
EXIT_CONFIG = 1
EXIT_INVALID_INPUT = 2
EXIT_MISSING_DEPENDENCY = 3

REQUIRED_PYTHON_TEXT = "3.11+"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="streambridge-server",
        description=(
            "Local HTTP bridge between online music sources and MPD. Listens on loopback only."
        ),
    )
    parser.add_argument("--version", action="version", version=f"streambridge {__version__}")
    parser.add_argument("--host", help="Bind address (loopback only)")
    parser.add_argument("--port", type=int, help="Port (default 8787)")
    parser.add_argument("--config", help="Path to config.toml")
    parser.add_argument("--log-level", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    parser.add_argument(
        "--cookies-from-browser",
        help="Browser to read cookies from, e.g. firefox (optional)",
    )
    parser.add_argument("--extractor-path", help="Path to the extractor binary (default: yt-dlp)")
    parser.add_argument(
        "--check",
        action="store_true",
        help="Report the environment and exit",
    )
    return parser


def check_dependencies(config: Config, *, as_json: bool = False) -> int:
    """Print an environment report and return a process exit code."""
    report = build_report(config)
    if as_json:
        import json

        print(json.dumps(report.to_dict(), indent=2, ensure_ascii=False))
    else:
        print(report.render())
    return EXIT_OK if report.ok else EXIT_MISSING_DEPENDENCY


def _load(args: argparse.Namespace) -> Config:
    """Load configuration with command line flags taking precedence."""
    overrides: dict[str, object] = {}
    if args.host:
        overrides["host"] = args.host
    if args.port:
        overrides["port"] = args.port
    if args.cookies_from_browser:
        overrides["cookies_from_browser"] = args.cookies_from_browser
    if args.extractor_path:
        overrides["extractor_path"] = args.extractor_path
    if args.log_level:
        overrides["log_level"] = args.log_level
    return load_config(args.config, overrides=overrides)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    # tomllib is why 3.11 is the floor; checked before anything else so a
    # checkout run on an older interpreter fails with a clear message instead
    # of an ImportError traceback.
    if not hasattr(tomllib, "load"):
        print(
            f"Error: Python {REQUIRED_PYTHON_TEXT} is required (tomllib). "
            f"Running {sys.version.split()[0]}.",
            file=sys.stderr,
        )
        return EXIT_MISSING_DEPENDENCY

    setup_logging(args.log_level or log_level_from_env("INFO"))
    log.info("streambridge-server %s starting", __version__)

    try:
        config = _load(args)
    except ConfigError as exc:
        print(f"Error: {exc.user_message()}", file=sys.stderr)
        return EXIT_CONFIG

    if args.check:
        return check_dependencies(config)

    if config.host not in ("127.0.0.1", "::1", "localhost"):
        print(
            f"Error: streambridge-server binds to loopback only, not {config.host!r}.",
            file=sys.stderr,
        )
        return EXIT_INVALID_INPUT

    client = ExtractorClient(config)
    if not client.is_available():
        try:
            client.require()
        except DependencyError as exc:
            print(f"Error: {exc.user_message()}", file=sys.stderr)
            return int(exc.exit_code)

    try:
        service = ApiService(config, client=client)
        server = make_server(config, service=service)
    except StreamBridgeError as exc:
        print(f"Error: {exc.user_message()}", file=sys.stderr)
        return int(exc.exit_code)
    except OSError as exc:
        print(f"Error: could not bind {config.host}:{config.port}: {exc}", file=sys.stderr)
        return EXIT_CONFIG

    stopping = threading.Event()

    def shutdown(signum: int, _frame: FrameType | None) -> None:
        # A second signal must not start a second shutdown thread.
        if stopping.is_set():
            return
        stopping.set()
        log.info("Signal %s received, shutting down", signum)
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    log.info("listening on http://%s:%d", config.host, config.port)
    log.info("extractor: %s", service.extractor_version() or "not found")
    log.info("cache: %s", config.cache_directory)
    if config.cookies_from_browser:
        # The browser *name* is not secret; the cookies it refers to are.
        log.info("reading cookies from browser: %s", config.cookies_from_browser)

    try:
        server.serve_forever(poll_interval=0.3)
    finally:
        server.server_close()
        log.info("stopped")
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
