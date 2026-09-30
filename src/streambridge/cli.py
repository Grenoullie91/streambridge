"""streambridge command line client.

Speaks to streambridge-server over HTTP for search and metadata, and drives MPD
through ``mpc``. Users never need to know the HTTP endpoints.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Sequence
from typing import Any

from . import __version__
from .cache import track_from_dict
from .config import Config, load_config
from .errors import (
    ExitCode,
    SourceUnavailableError,
    StreamBridgeError,
    ValidationError,
)
from .logging import log_level_from_env, setup_logging
from .metadata import format_duration
from .models import SearchType, Track, validate_playlist_id, validate_video_id
from .mpd import MpdClient

# ANSI colour is opt-out via NO_COLOR, and never required for correctness:
# every helper degrades to plain text when stdout is not a terminal.
_COLOR = (
    sys.stdout.isatty()
    and os.environ.get("NO_COLOR") is None
    and os.environ.get("STREAMBRIDGE_NO_COLOR") is None
)


def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _COLOR else text


def bold(text: str) -> str:
    return _c("1", text)


def dim(text: str) -> str:
    return _c("2", text)


def accent(text: str) -> str:
    return _c("38;5;141", text)


def format_track_line(index: int, track: Track) -> str:
    """One search result as a numbered block."""
    head = f"{bold(str(index) + '.')} {bold(track.title)}"
    meta = f"   {dim(track.display_album)} - {dim(track.display_artist)}"
    length = dim(format_duration(track.duration))
    return f"{head}\n{meta}\n   {length}  {dim(track.webpage_url)}\n"


def format_results(tracks: Sequence[Track]) -> str:
    if not tracks:
        return dim("No results.")
    return "\n".join(format_track_line(i, t) for i, t in enumerate(tracks, start=1))


class ServerClient:
    """Minimal HTTP client for the local StreamBridge API."""

    def __init__(self, base_url: str, timeout: float = 30.0) -> None:
        self._base = base_url.rstrip("/")
        self.timeout = timeout

    def _get(self, path: str) -> dict[str, Any]:
        url = f"{self._base}{path}"
        try:
            with urllib.request.urlopen(url, timeout=self.timeout) as response:  # noqa: S310
                raw = response.read()
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")
            try:
                payload = json.loads(body)
            except json.JSONDecodeError:
                payload = {"message": body[:300]}
            if not isinstance(payload, dict):
                payload = {"message": body[:300]}
            message = str(payload.get("message") or f"HTTP {exc.code}")
            hint = payload.get("hint")
            if exc.code == 502:
                raise SourceUnavailableError(
                    message, upstream_message=payload.get("upstream"), hint=hint
                ) from exc
            raise StreamBridgeError(message, hint=hint) from exc
        except urllib.error.URLError as exc:
            raise StreamBridgeError(
                f"streambridge-server is not reachable ({self._base})",
                hint="Start the server:  systemctl --user start streambridge",
            ) from exc
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise StreamBridgeError(
                "streambridge-server returned an invalid JSON response"
            ) from exc
        if not isinstance(data, dict):
            raise StreamBridgeError("Unexpected response from streambridge-server")
        return data

    def health(self) -> dict[str, Any]:
        return self._get("/health")

    def search(
        self, query: str, search_type: SearchType = SearchType.SONGS, limit: int | None = None
    ) -> dict[str, Any]:
        params = {"q": query, "type": search_type.value}
        if limit:
            params["limit"] = str(limit)
        return self._get("/search?" + urllib.parse.urlencode(params))

    def info(self, video_id: str) -> dict[str, Any]:
        return self._get(f"/info/{validate_video_id(video_id)}")

    def playlist(self, playlist_id: str, limit: int | None = None) -> dict[str, Any]:
        params = {"id": validate_playlist_id(playlist_id)}
        if limit:
            params["limit"] = str(limit)
        return self._get("/playlist?" + urllib.parse.urlencode(params))


def tracks_from_payload(payload: dict[str, Any], key: str = "results") -> list[Track]:
    """Rebuild Track objects from a response, skipping unusable rows."""
    out: list[Track] = []
    for item in payload.get(key) or []:
        if not isinstance(item, dict):
            continue
        track = track_from_dict(item)
        if track is not None:
            out.append(track)
    return out


def _print_json(payload: Any) -> None:
    json.dump(payload, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")


def _ensure_server(client: ServerClient) -> None:
    try:
        client.health()
    except StreamBridgeError as exc:
        print(f"Error: {exc.user_message()}", file=sys.stderr)
        raise SystemExit(ExitCode.GENERAL_ERROR) from exc


def _queue_tracks(tracks: list[Track], *, play: bool, config: Config) -> int:
    mpd = MpdClient(config)
    if not mpd.ping():
        print(
            f"Error: MPD is not reachable ({config.mpd_host}:{config.mpd_port}).\n"
            "  Check:  systemctl --user status mpd",
            file=sys.stderr,
        )
        return ExitCode.MPD_UNREACHABLE
    mpd.add_tracks(tracks, play=play)
    action = "playing" if play else "added to the queue"
    for track in tracks:
        print(f"{accent('+')} {track.display_artist} - {track.title} {dim('(' + action + ')')}")
    return ExitCode.OK


def _track_for(video_id: str, client: ServerClient) -> Track:
    payload = client.info(video_id)
    track = track_from_dict(payload)
    if track is None:
        raise ValidationError(f"No metadata received for {video_id}.")
    return track


def _queue_by_ids(ids: list[str], *, play: bool, config: Config) -> int:
    """Resolve ids to tracks, then queue them.

    Every id is validated up front, so a typo is reported without a server
    round-trip and without a partial queue write. Failures that only surface
    during resolution are reported per track, so the rest of a batch still
    gets queued.
    """
    validated: list[str] = []
    for raw in ids:
        try:
            validated.append(validate_video_id(raw))
        except ValidationError as exc:
            print(f"Error: {exc.user_message()}", file=sys.stderr)
            return ExitCode.INVALID_INPUT

    client = ServerClient(config.base_url, config.request_timeout)
    _ensure_server(client)
    tracks: list[Track] = []
    for vid in validated:
        try:
            tracks.append(_track_for(vid, client))
        except StreamBridgeError as exc:
            print(f"Warning: {vid}: {exc.message}", file=sys.stderr)
    if not tracks:
        return ExitCode.SOURCE_UNAVAILABLE
    return _queue_tracks(tracks, play=play, config=config)


# -- commands -----------------------------------------------------------
def cmd_search(args: argparse.Namespace, config: Config) -> int:
    client = ServerClient(config.base_url, config.request_timeout)
    _ensure_server(client)
    payload = client.search(args.query, SearchType(args.type), args.limit)
    tracks = tracks_from_payload(payload)
    if args.json:
        _print_json(payload)
        return ExitCode.OK
    if not tracks:
        print(dim(f"No results for {args.query!r}."), file=sys.stderr)
        return ExitCode.OK
    print(format_results(tracks))

    if args.no_interactive:
        return ExitCode.OK

    # The prompt is only shown on a terminal, but a piped number is still
    # read: `echo 1 | streambridge search "..."` is a legitimate script use.
    if sys.stdin.isatty():
        print(dim("Select (number, Enter = cancel):"), end="  ")
    try:
        answer = input().strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return ExitCode.OK
    if not answer:
        return ExitCode.OK
    try:
        index = int(answer)
    except ValueError:
        print(f"Invalid selection: {answer!r}", file=sys.stderr)
        return ExitCode.INVALID_INPUT
    if not 1 <= index <= len(tracks):
        print(f"Number out of range 1..{len(tracks)}", file=sys.stderr)
        return ExitCode.INVALID_INPUT
    return _queue_tracks([tracks[index - 1]], play=args.play, config=config)


def cmd_add(args: argparse.Namespace, config: Config) -> int:
    return _queue_by_ids(args.id, play=False, config=config)


def cmd_enqueue(args: argparse.Namespace, config: Config) -> int:
    """Alias for add, documented as such."""
    return _queue_by_ids(args.id, play=False, config=config)


def cmd_play(args: argparse.Namespace, config: Config) -> int:
    return _queue_by_ids(args.id, play=True, config=config)


def cmd_play_now(args: argparse.Namespace, config: Config) -> int:
    """Append the track and start playing it, keeping the rest of the queue."""
    mpd = MpdClient(config)
    if not mpd.ping():
        print(
            f"Error: MPD is not reachable ({config.mpd_host}:{config.mpd_port}).",
            file=sys.stderr,
        )
        return ExitCode.MPD_UNREACHABLE
    client = ServerClient(config.base_url, config.request_timeout)
    track = _track_for(validate_video_id(args.id[0]), client)
    mpd.add_tracks([track], play=True)
    print(f"{accent('>')} {track.display_artist} - {track.title}")
    return ExitCode.OK


def cmd_info(args: argparse.Namespace, config: Config) -> int:
    client = ServerClient(config.base_url, config.request_timeout)
    _ensure_server(client)
    payload = client.info(args.id[0])
    if args.json:
        _print_json(payload)
        return ExitCode.OK
    print(bold(str(payload.get("title", ""))))
    print(f"  Artist   : {payload.get('artist') or '-'}")
    print(f"  Album    : {payload.get('album') or '-'}")
    print(f"  Duration : {format_duration(payload.get('duration'))}")
    print(f"  Channel  : {payload.get('channel') or payload.get('uploader') or '-'}")
    print(f"  URL      : {payload.get('url')}")
    print(f"  Stream   : {config.stream_url(args.id[0])}")
    return ExitCode.OK


def cmd_health(args: argparse.Namespace, config: Config) -> int:
    client = ServerClient(config.base_url, min(config.request_timeout, 10.0))
    try:
        payload = client.health()
    except StreamBridgeError as exc:
        if args.json:
            _print_json({"status": "unreachable", "error": exc.message, "hint": exc.hint})
        else:
            print(f"streambridge-server: not reachable ({config.base_url})")
            if exc.hint:
                print(dim(f"  {exc.hint}"))
        return ExitCode.GENERAL_ERROR
    if args.json:
        _print_json(payload)
        return ExitCode.OK
    print(f"streambridge-server  {payload.get('status')}  (version {payload.get('version')})")
    print(f"  Extractor : {payload.get('extractor')}")
    print(f"  Uptime    : {payload.get('uptime_seconds')} s")
    mpd = MpdClient(config)
    print(
        f"  MPD       : {'reachable' if mpd.ping() else 'not reachable'}"
        f" ({config.mpd_host}:{config.mpd_port})"
    )
    return ExitCode.OK


def cmd_status(args: argparse.Namespace, config: Config) -> int:
    mpd = MpdClient(config)
    try:
        status = mpd.status()
    except StreamBridgeError as exc:
        if args.json:
            _print_json({"error": exc.message, "hint": exc.hint})
        else:
            print(f"Error: {exc.user_message()}", file=sys.stderr)
        return ExitCode.MPD_UNREACHABLE
    if args.json:
        _print_json(status.to_dict())
        return ExitCode.OK
    state = "playing" if status.playing else "paused" if status.paused else "stopped"
    print(f"MPD: {state}")
    if status.track:
        print(f"  Track  : {status.track}")
    if status.elapsed:
        print(f"  Time   : {status.elapsed}" + (f" / {status.total}" if status.total else ""))
    print(f"  Repeat : {'on' if status.repeat else 'off'}")
    print(f"  Random : {'on' if status.random else 'off'}")
    return ExitCode.OK


def _mpd_passthrough(args: argparse.Namespace, config: Config) -> int:
    """Forward a no-argument MPD command (next/pause/stop)."""
    mpd = MpdClient(config)
    try:
        out = getattr(mpd, args.command)()
    except StreamBridgeError as exc:
        print(f"Error: {exc.user_message()}", file=sys.stderr)
        return ExitCode.MPD_UNREACHABLE
    if out.strip() and not args.json:
        print(out.strip())
    return ExitCode.OK


def cmd_clear(args: argparse.Namespace, config: Config) -> int:
    if not args.yes:
        print("This empties the MPD queue (online tracks only, no music files).")
        try:
            answer = input("Continue? [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return ExitCode.OK
        if answer not in ("y", "yes"):
            print("Cancelled.")
            return ExitCode.OK
    return _mpd_passthrough(args, config)


def cmd_playlist(args: argparse.Namespace, config: Config) -> int:
    """Load an online playlist into the queue, preserving order."""
    pid = validate_playlist_id(args.playlist_id)
    client = ServerClient(config.base_url, max(config.info_timeout, 60.0))
    _ensure_server(client)
    try:
        payload = client.playlist(pid, args.limit)
    except StreamBridgeError as exc:
        print(f"Error: {exc.user_message()}", file=sys.stderr)
        return ExitCode.SOURCE_UNAVAILABLE

    tracks = tracks_from_payload(payload, key="tracks")
    if args.json:
        _print_json(payload)
        return ExitCode.OK
    if not tracks:
        print(dim("Playlist contains no playable tracks."), file=sys.stderr)
        return ExitCode.SOURCE_UNAVAILABLE
    print(f"{bold(str(payload.get('title', pid)))} - {len(tracks)} tracks")
    return _queue_tracks(tracks, play=False, config=config)


def cmd_doctor(_args: argparse.Namespace, config: Config) -> int:
    """Full environment report, including network probes."""
    from .health import build_report

    report = build_report(config, probe_network=True)
    print(report.render())
    return ExitCode.OK if report.ok else ExitCode.MISSING_DEPENDENCY


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="streambridge",
        description=(
            "Search online music and play it through MPD. Requires a running streambridge-server."
        ),
        epilog=(
            "Examples:\n"
            "  streambridge search 'artist name'\n"
            "  streambridge search 'album' --json\n"
            "  streambridge add dQw4w9WgXcQ\n"
            "  streambridge play dQw4w9WgXcQ\n"
            "  streambridge status\n"
            "  streambridge doctor\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"streambridge {__version__}")
    parser.add_argument("--config", help="Path to config.toml")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    parser.add_argument("-v", "--verbose", action="store_true", help="DEBUG logging")

    # Subcommands inherit --json/--config from the top level. They use
    # default=SUPPRESS so a subparser only sets the attribute when the flag is
    # actually given; otherwise a subparser default of False would overwrite a
    # global `streambridge --json status`.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--json", action="store_true", default=argparse.SUPPRESS, help="Output as JSON"
    )
    common.add_argument("--config", default=argparse.SUPPRESS, help="Path to config.toml")

    sub = parser.add_subparsers(dest="command", metavar="COMMAND")

    def new_command(name: str, **kwargs: object) -> argparse.ArgumentParser:
        return sub.add_parser(name, parents=[common], **kwargs)  # type: ignore[arg-type]

    p = new_command("search", help="Search the online catalogue")
    p.add_argument("query", help="Artist, title or album")
    p.add_argument("--type", default="songs", choices=[t.value for t in SearchType])
    p.add_argument("--limit", type=int, help="Maximum number of results")
    p.add_argument("--no-interactive", action="store_true", help="Do not prompt")
    p.add_argument("--play", action="store_true", help="Play the selection immediately")
    p.set_defaults(func=cmd_search)

    for name, func, helptext in (
        ("add", cmd_add, "Add tracks to the queue"),
        ("enqueue", cmd_enqueue, "Alias for add"),
        ("play", cmd_play, "Add tracks and start playing"),
    ):
        p = new_command(name, help=helptext)
        p.add_argument("id", nargs="+", help="Video id (11 characters)")
        p.set_defaults(func=func)

    p = new_command("play-now", help="Append a track and start it immediately")
    p.add_argument("id", nargs=1, help="Video id (11 characters)")
    p.set_defaults(func=cmd_play_now)

    p = new_command("info", help="Show metadata for a track")
    p.add_argument("id", nargs=1, help="Video id (11 characters)")
    p.set_defaults(func=cmd_info)

    p = new_command("playlist", help="Load a playlist into the queue")
    p.add_argument("playlist_id", help="Playlist id (PL.../UU.../OLAK5uy_...)")
    p.add_argument("--limit", type=int, default=100, help="Maximum number of tracks")
    p.set_defaults(func=cmd_playlist)

    for name, handler, helptext in (
        ("status", cmd_status, "Show the MPD status"),
        ("health", cmd_health, "Show server and system status"),
        ("doctor", cmd_doctor, "Full environment report"),
    ):
        p = new_command(name, help=helptext)
        p.set_defaults(func=handler)

    p = new_command("clear", help="Empty the MPD queue")
    p.add_argument("-y", "--yes", action="store_true", help="Do not ask")
    p.set_defaults(func=cmd_clear)

    for name, helptext in (
        ("next", "Next track"),
        ("pause", "Pause playback"),
        ("stop", "Stop playback"),
    ):
        p = new_command(name, help=helptext)
        p.set_defaults(func=_mpd_passthrough, command=name)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    setup_logging(log_level_from_env("INFO"), verbose=getattr(args, "verbose", False))

    if not getattr(args, "command", None):
        parser.print_help()
        return ExitCode.OK

    try:
        config = load_config(getattr(args, "config", None))
    except StreamBridgeError as exc:
        print(f"Error: {exc.user_message()}", file=sys.stderr)
        return ExitCode.GENERAL_ERROR

    try:
        return int(args.func(args, config))
    except StreamBridgeError as exc:
        print(f"Error: {exc.user_message()}", file=sys.stderr)
        return int(exc.exit_code)
    except KeyboardInterrupt:  # pragma: no cover
        print("\nAborted.", file=sys.stderr)
        return 130
    except BrokenPipeError:  # pragma: no cover
        # Happens when output is piped into e.g. `head`. Not an error.
        return ExitCode.OK


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
