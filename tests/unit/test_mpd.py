"""MPD contract: generated M3U, status parsing, error mapping.

The playlist-loading contract is verified against the real behaviour of
MPD 0.23.14 with mpc 0.35: MPD resolves ``load`` against its
``playlist_directory`` and appends the ``.m3u`` suffix itself, so an absolute
path or a name that already carries the extension is rejected with
"Access denied".
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from conftest import FakeRunner
from streambridge import mpd
from streambridge.config import Config, config_from_mapping
from streambridge.errors import DependencyError, MpdError, ValidationError
from streambridge.models import Track
from streambridge.mpd import MpdClient, clean_tag
from streambridge.proc import CompletedRun

pytestmark = pytest.mark.unit

# A real executable stands in for `mpc` so the dependency check passes
# deterministically; all behaviour under test comes from FakeRunner.
FAKE_MPC = "/bin/echo"

SAMPLE = [
    Track(
        id="5NV6Rdv1a3I",
        title="First Song",
        artist="Artist One",
        album="Some Album",
        duration=249,
        webpage_url="https://www.youtube.com/watch?v=5NV6Rdv1a3I",
    ),
    Track(
        id="CCHdMIEGaaM",
        title="Second Song",
        artist="Artist One",
        duration=320,
        webpage_url="https://www.youtube.com/watch?v=CCHdMIEGaaM",
    ),
]


def build(runner: FakeRunner, **overrides: object) -> MpdClient:
    config = config_from_mapping({"mpd": {"host": "127.0.0.1", "port": 6600}, **overrides})  # type: ignore[arg-type]
    return MpdClient(config, runner=runner, executable=FAKE_MPC)


class TestCleanTag:
    def test_strips_newlines(self) -> None:
        assert clean_tag("a\nb") == "a b"

    def test_strips_quotes(self) -> None:
        assert '"' not in clean_tag('say "hi"')

    def test_truncates_long_values(self) -> None:
        assert len(clean_tag("x" * 500)) <= 300

    def test_injection_attempt_neutralised(self) -> None:
        # A title crafted to break out of the EXTINF line must not survive.
        hostile = 'evil"\nhttp://evil.test/x\n#EXTINF:1,'
        cleaned = clean_tag(hostile)
        assert "\n" not in cleaned
        assert '"' not in cleaned

    def test_preserves_ordinary_values(self) -> None:
        assert clean_tag("Some Album") == "Some Album"


class TestGeneratedPlaylist:
    def _capture(self, runner: FakeRunner, tracks: list[Track], tmp_path: Path) -> Path:
        """Run add_tracks and return the generated M3U before it is removed."""
        captured: dict[str, Path] = {}
        original_write = MpdClient.write_playlist

        def spy(self: MpdClient, entries: list[tuple[str, Track]], target_dir: Path) -> Path:
            path = original_write(self, entries, target_dir)
            captured["path"] = path
            # Copy before the finally block unlinks the original.
            copy = path.with_suffix(".copy.m3u")
            copy.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
            captured["copy"] = copy
            return path

        MpdClient.write_playlist = spy  # type: ignore[method-assign]
        try:
            client = build(runner, mpd={"playlist_directory": str(tmp_path)})
            runner.add("load", CompletedRun((), 0, "", ""))
            runner.add("play", CompletedRun((), 0, "", ""))
            client.add_tracks(tracks)
        finally:
            MpdClient.write_playlist = original_write  # type: ignore[method-assign]
        return captured["copy"]

    def test_extinf_carries_artist_title_duration(self, runner: FakeRunner, tmp_path: Path) -> None:
        text = self._capture(runner, SAMPLE, tmp_path).read_text(encoding="utf-8")
        assert text.startswith("#EXTM3U")
        assert "#EXTINF:249,Artist One - First Song" in text
        assert "#EXTINF:320,Artist One - Second Song" in text

    def test_stable_local_stream_urls(self, runner: FakeRunner, tmp_path: Path) -> None:
        text = self._capture(runner, SAMPLE, tmp_path).read_text(encoding="utf-8")
        assert "http://127.0.0.1:8787/stream/5NV6Rdv1a3I" in text
        assert "http://127.0.0.1:8787/stream/CCHdMIEGaaM" in text
        # The URL MPD sees must be stable, never a signed upstream URL.
        assert "googlevideo" not in text

    def test_order_preserved(self, runner: FakeRunner, tmp_path: Path) -> None:
        text = self._capture(runner, SAMPLE, tmp_path).read_text(encoding="utf-8")
        assert text.index("5NV6Rdv1a3I") < text.index("CCHdMIEGaaM")

    def test_album_and_title_tags_present(self, runner: FakeRunner, tmp_path: Path) -> None:
        text = self._capture(runner, SAMPLE, tmp_path).read_text(encoding="utf-8")
        assert "#EXT-X-ALBUM:Some Album" in text
        assert "#EXT-X-ALBUMARTIST:Artist One" in text
        assert "#EXT-X-TITLE:First Song" in text

    def test_hostile_title_does_not_break_file(self, runner: FakeRunner, tmp_path: Path) -> None:
        hostile = Track(
            id="5NV6Rdv1a3I",
            title='Bad"\n#EXTINF:-1,evil\nhttp://evil.test',
            duration=10,
            webpage_url="https://www.youtube.com/watch?v=5NV6Rdv1a3I",
        )
        text = self._capture(runner, [hostile], tmp_path).read_text(encoding="utf-8")
        lines = text.splitlines()
        # The payload stays inert: it may remain as visible title text, but it
        # must not create a second stream line or a second EXTINF directive.
        stream_lines = [ln for ln in lines if ln.startswith("http://")]
        assert stream_lines == ["http://127.0.0.1:8787/stream/5NV6Rdv1a3I"]
        assert sum(1 for ln in lines if ln.startswith("#EXTINF:")) == 1
        # No line inflation: header + 4 metadata lines + 1 stream URL.
        assert len(lines) == 1 + 4 + 1

    def test_temp_file_removed_after_use(self, runner: FakeRunner, tmp_path: Path) -> None:
        client = build(runner, mpd={"playlist_directory": str(tmp_path)})
        runner.add("load", CompletedRun((), 0, "", ""))
        client.add_tracks(SAMPLE)
        # The generated file is a short-lived artifact, not litter.
        assert not list(tmp_path.glob("streambridge-*.m3u"))

    def test_load_uses_bare_name_without_extension(
        self, runner: FakeRunner, tmp_path: Path
    ) -> None:
        client = build(runner, mpd={"playlist_directory": str(tmp_path)})
        runner.add("load", CompletedRun((), 0, "", ""))
        client.add_tracks(SAMPLE)
        load_call = next(c for c in runner.calls if "load" in c)
        argument = load_call[load_call.index("load") + 1]
        # MPD appends the suffix itself; a path or ".m3u" is "Access denied".
        assert not argument.endswith(".m3u")
        assert "/" not in argument

    def test_load_rejection_names_the_directory(self, runner: FakeRunner, tmp_path: Path) -> None:
        client = build(runner, mpd={"playlist_directory": str(tmp_path)})
        runner.add("load", CompletedRun((), 1, "", "MPD error: Access denied"))
        with pytest.raises(MpdError) as exc:
            client.add_tracks(SAMPLE)
        assert "playlist_directory" in (exc.value.hint or "")

    def test_empty_track_list_rejected(self, runner: FakeRunner) -> None:
        with pytest.raises(ValidationError):
            build(runner).add_tracks([])

    def test_invalid_id_rejected_before_mpd(self, runner: FakeRunner) -> None:
        bad = Track(id="../etc/passwd", title="x", webpage_url="u")
        with pytest.raises(ValidationError):
            build(runner).add_tracks([bad])
        assert not [c for c in runner.calls if "load" in c]

    def test_play_flag_starts_playback(self, runner: FakeRunner, tmp_path: Path) -> None:
        client = build(runner, mpd={"playlist_directory": str(tmp_path)})
        runner.add("load", CompletedRun((), 0, "", ""))
        runner.add("play", CompletedRun((), 0, "", ""))
        client.add_tracks(SAMPLE, play=True)
        assert any("play" in c for c in runner.calls)


class TestPlaylistDirectory:
    def test_explicit_configuration_wins(self, tmp_path: Path) -> None:
        client = build(FakeRunner(), mpd={"playlist_directory": str(tmp_path / "pl")})
        assert client.playlist_dir() == tmp_path / "pl"

    def test_read_from_mpd_conf(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """mpc 0.35 has no `paths` subcommand, so mpd.conf is the second source."""
        conf = tmp_path / "mpd.conf"
        conf.write_text(
            f'music_directory "/music"\nplaylist_directory  "{tmp_path / "playlists"}"\n',
            encoding="utf-8",
        )
        monkeypatch.setattr(mpd, "_mpd_conf_candidates", lambda: (conf,))
        client = MpdClient(Config(), runner=FakeRunner(), executable=FAKE_MPC)
        assert client.playlist_dir() == tmp_path / "playlists"

    def test_a_commented_out_directive_is_not_used(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        conf = tmp_path / "mpd.conf"
        conf.write_text(f'#playlist_directory "{tmp_path / "wrong"}"\n', encoding="utf-8")
        monkeypatch.setattr(mpd, "_mpd_conf_candidates", lambda: (conf, tmp_path / "absent.conf"))
        client = MpdClient(Config(), runner=FakeRunner(), executable=FAKE_MPC)
        # Unresolvable, and it must say so rather than guess.
        with pytest.raises(MpdError) as excinfo:
            client.playlist_dir()
        assert "playlist_directory" in excinfo.value.user_message()

    def test_refuses_to_guess_when_nothing_can_be_determined(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No silent fallback any more.

        Falling back to the cache directory cannot work: MPD never looks there,
        so every load failed with "No such playlist" while the real cause - an
        unset setting - stayed invisible. Refusing to guess and naming the key
        turns a puzzling failure into a one-line fix.
        """
        monkeypatch.setattr(mpd, "_mpd_conf_candidates", lambda: (tmp_path / "absent.conf",))
        config = config_from_mapping({"cache": {"directory": str(tmp_path / "cache")}})
        client = MpdClient(config, runner=FakeRunner(), executable=FAKE_MPC)
        with pytest.raises(MpdError) as excinfo:
            client.playlist_dir()
        assert "playlist_directory" in excinfo.value.user_message()
        # Nothing was written anywhere on the way to failing.
        assert not (tmp_path / "cache").exists()


class TestCommands:
    def test_ping_uses_status_not_ping(self, runner: FakeRunner) -> None:
        # `mpc ping` does not exist in mpc 0.35 (it exits 1 even against a
        # healthy server), so liveness must use `mpc status`.
        runner.add("status", CompletedRun((), 0, "[stopped]\n", ""))
        assert build(runner).ping() is True
        assert "ping" not in " ".join(runner.calls[-1])

    def test_ping_false_on_refused(self, runner: FakeRunner) -> None:
        runner.add("status", CompletedRun((), 1, "", "mpc: Connection refused"))
        assert build(runner).ping() is False

    def test_ping_false_without_binary(self) -> None:
        client = MpdClient(
            config_from_mapping({}), runner=FakeRunner(), executable="not-a-real-mpc-xyz"
        )
        assert client.ping() is False

    def test_missing_binary_raises_dependency_error(self) -> None:
        client = MpdClient(
            config_from_mapping({}), runner=FakeRunner(), executable="not-a-real-mpc-xyz"
        )
        with pytest.raises(DependencyError) as exc:
            client.require()
        assert int(exc.value.exit_code) == 3

    def test_connection_refused_maps_to_mpd_error(self, runner: FakeRunner) -> None:
        runner.add("status", CompletedRun((), 1, "", "mpc: Connection refused"))
        with pytest.raises(MpdError) as exc:
            build(runner).status()
        assert "not reachable" in exc.value.message
        assert int(exc.value.exit_code) == 4

    def test_unknown_command_maps_to_mpd_error(self, runner: FakeRunner) -> None:
        runner.add("status", CompletedRun((), 1, "", "Unknown command: status"))
        with pytest.raises(MpdError) as exc:
            build(runner).status()
        assert int(exc.value.exit_code) == 4

    def test_host_and_port_passed(self, runner: FakeRunner) -> None:
        runner.add("status", CompletedRun((), 0, "[stopped]\n", ""))
        build(runner, mpd={"host": "127.0.0.9", "port": 6601}).ping()
        call = runner.calls[-1]
        assert call[call.index("--host") + 1] == "127.0.0.9"
        assert call[call.index("--port") + 1] == "6601"

    def test_add_url(self, runner: FakeRunner) -> None:
        runner.add("add", CompletedRun((), 0, "", ""))
        build(runner).add_url("http://127.0.0.1:8787/stream/5NV6Rdv1a3I")
        assert "http://127.0.0.1:8787/stream/5NV6Rdv1a3I" in runner.calls[-1]

    def test_play_url_appends_then_plays(self, runner: FakeRunner) -> None:
        runner.add("add", CompletedRun((), 0, "", ""))
        runner.add("play", CompletedRun((), 0, "", ""))
        build(runner).play_url("http://127.0.0.1:8787/stream/5NV6Rdv1a3I")
        commands = [" ".join(c) for c in runner.calls]
        assert any("add" in c for c in commands)
        assert any(re.search(r"\bplay\b", c) for c in commands)

    def test_clear_does_not_touch_database(self, runner: FakeRunner) -> None:
        runner.add("clear", CompletedRun((), 0, "", ""))
        build(runner).clear()
        call = " ".join(runner.calls[-1])
        # `clear` empties the queue; the MPD database must never be modified.
        assert "update" not in call
        assert "rm" not in call

    def test_transport_commands_pass_through(self, runner: FakeRunner) -> None:
        client = build(runner)
        # `previous` is the mpc subcommand for "go back"; the others match.
        for method, subcommand in (
            ("next", "next"),
            ("previous", "prev"),
            ("pause", "pause"),
            ("play", "play"),
            ("stop", "stop"),
        ):
            runner.add(subcommand, CompletedRun((), 0, "", ""))
            getattr(client, method)()
            assert runner.calls[-1][-1] == subcommand


class TestStatusParsing:
    def test_playing_state(self, runner: FakeRunner) -> None:
        raw = (
            "Artist One - First Song\n"
            "[playing] #3  0:42/4:09 (17%)\n"
            "volume: 70%   repeat: off   random: off   single: off   consume: off\n"
        )
        runner.add("status", CompletedRun((), 0, raw, ""))
        status = build(runner).status()
        assert status.playing is True
        assert status.track == "#3"
        assert status.volume == "70%"
        assert status.elapsed == "0:42"
        assert status.total == "4:09"

    def test_paused_state(self, runner: FakeRunner) -> None:
        runner.add("status", CompletedRun((), 0, "[paused] #1  0:10/3:30\n", ""))
        status = build(runner).status()
        assert status.paused is True
        assert status.playing is False

    def test_stopped_state(self, runner: FakeRunner) -> None:
        runner.add("status", CompletedRun((), 0, "[stopped]\n", ""))
        assert build(runner).status().stopped is True

    def test_titles_with_brackets_do_not_break_parsing(self, runner: FakeRunner) -> None:
        raw = "Some [Remix] Title\n[playing] #2  1:00/3:00 (33%)\n"
        runner.add("status", CompletedRun((), 0, raw, ""))
        status = build(runner).status()
        assert status.track == "#2"
        assert status.playing is True

    def test_repeat_and_random_flags(self, runner: FakeRunner) -> None:
        runner.add(
            "status",
            CompletedRun((), 0, "[playing] #1  0:00/1:00\nrepeat: on   random: on\n", ""),
        )
        status = build(runner).status()
        assert status.repeat is True
        assert status.random is True

    def test_raw_always_preserved(self, runner: FakeRunner) -> None:
        raw = "anything\n[playing] #1  0:00/1:00 (0%)\n"
        runner.add("status", CompletedRun((), 0, raw, ""))
        assert build(runner).status().raw == raw.strip()

    def test_unparseable_output_does_not_raise(self, runner: FakeRunner) -> None:
        runner.add("status", CompletedRun((), 0, "completely unexpected\n", ""))
        status = build(runner).status()
        assert status.playing is False
        assert "unexpected" in status.raw

    def test_empty_output(self, runner: FakeRunner) -> None:
        runner.add("status", CompletedRun((), 0, "", ""))
        assert build(runner).status().stopped is False

    def test_to_dict_is_json_friendly(self, runner: FakeRunner) -> None:
        runner.add("status", CompletedRun((), 0, "[playing] #1  0:00/1:00\n", ""))
        json.dumps(build(runner).status().to_dict())
