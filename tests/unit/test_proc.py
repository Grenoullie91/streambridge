"""Process execution: argument safety, timeouts, no zombies, redaction.

These tests spawn real child processes, but only trivial ones (/bin/echo and
short-lived interpreters). Nothing here touches the network.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from streambridge.logging import redact_args
from streambridge.proc import CompletedRun, SubprocessRunner, which

pytestmark = pytest.mark.unit


class TestWhich:
    def test_finds_python(self) -> None:
        assert which(sys.executable) is not None

    def test_missing_program(self) -> None:
        assert which("definitely-not-a-real-program-xyz") is None

    def test_empty_string(self) -> None:
        assert which("") is None

    def test_absolute_path_checked_directly(self) -> None:
        # An absolute path is used as-is; resolve() may normalise symlinks
        # (/usr/bin/python3 -> /usr/bin/python3.12), so compare real paths.
        found = which(sys.executable)
        assert found is not None
        assert Path(found).resolve() == Path(sys.executable).resolve()
        assert which("/nonexistent/path/to/binary") is None

    def test_tilde_expansion(self) -> None:
        assert which("~/definitely-not-here") is None

    def test_non_executable_file_rejected(self, tmp_path: Path) -> None:
        script = tmp_path / "not-executable"
        script.write_text("#!/bin/sh\n", encoding="utf-8")
        script.chmod(0o644)
        assert which(str(script)) is None


class TestRedaction:
    def test_cookie_value_hidden(self) -> None:
        args = ["yt-dlp", "--cookies", "/home/u/cookies.txt", "--dump-json"]
        redacted = redact_args(args)
        assert "/home/u/cookies.txt" not in redacted
        assert "<REDACTED>" in redacted

    def test_equals_form_hidden(self) -> None:
        args = ["yt-dlp", "--cookies-from-browser=firefox:secret"]
        redacted = redact_args(args)
        assert "secret" not in redacted[1]
        assert redacted[1].startswith("--cookies-from-browser=")

    def test_ordinary_args_untouched(self) -> None:
        args = ["yt-dlp", "--dump-json", "ytsearch5:test"]
        assert redact_args(args) == args

    def test_home_path_is_collapsed(self) -> None:
        redacted = redact_args(["/home/someone/bin/yt-dlp"])
        assert "/home/someone" not in " ".join(redacted)
        assert "$HOME" in redacted[0]


class TestSubprocessRunner:
    def test_captures_stdout(self) -> None:
        result = SubprocessRunner().run(["/bin/echo", "hello"], timeout=10)
        assert result.ok
        assert result.stdout.strip() == "hello"

    def test_captures_stderr(self) -> None:
        result = SubprocessRunner().run(
            [sys.executable, "-c", "import sys; sys.stderr.write('bad')"], timeout=10
        )
        assert "bad" in result.stderr

    def test_nonzero_returncode(self) -> None:
        result = SubprocessRunner().run([sys.executable, "-c", "raise SystemExit(3)"], timeout=10)
        assert result.returncode == 3
        assert result.ok is False

    def test_timeout_is_enforced(self) -> None:
        start = time.monotonic()
        result = SubprocessRunner().run(
            [sys.executable, "-c", "import time; time.sleep(30)"], timeout=1.0
        )
        assert result.returncode == 124
        assert "Timeout" in result.stderr
        assert time.monotonic() - start < 10

    def test_timeout_kills_grandchildren(self, tmp_path: Path) -> None:
        """A hung extractor must not leave children behind.

        The grandchild would create a marker file after the parent's timeout.
        If the process group were not killed, the marker would appear.
        """
        marker = tmp_path / "grandchild-survived"
        script = (
            "import subprocess,sys,time;"
            "subprocess.Popen([sys.executable,'-c',"
            f"\"import time;time.sleep(20);open({str(marker)!r},'w').close()\"]);"
            "time.sleep(30)"
        )
        result = SubprocessRunner().run([sys.executable, "-c", script], timeout=1.5)
        assert result.returncode == 124
        time.sleep(3)
        assert not marker.exists()

    def test_arguments_are_not_shell_interpreted(self, tmp_path: Path) -> None:
        canary = tmp_path / "shell-injection"
        # Through a shell, the `;` would terminate the command and the file
        # would be created.
        result = SubprocessRunner().run(["/bin/echo", f"; touch {canary}"], timeout=10)
        assert result.ok
        assert not canary.exists()
        assert ";" in result.stdout

    def test_env_is_passed_through(self) -> None:
        result = SubprocessRunner().run(
            [sys.executable, "-c", "import os; print(os.environ['SB_TEST_VAR'])"],
            timeout=10,
            env={"SB_TEST_VAR": "value-42"},
        )
        assert "value-42" in result.stdout

    def test_stdin_data_is_delivered(self) -> None:
        result = SubprocessRunner().run(
            [sys.executable, "-c", "import sys; sys.stdout.write(sys.stdin.read().upper())"],
            timeout=10,
            stdin_data="hello",
        )
        assert result.stdout == "HELLO"

    def test_args_recorded_on_result(self) -> None:
        result = SubprocessRunner().run(["/bin/echo", "x"], timeout=10)
        assert result.args == ("/bin/echo", "x")

    def test_missing_binary_raises_file_not_found(self) -> None:
        # A missing program is not a failed run: it propagates so the caller
        # can turn it into a DependencyError with install instructions.
        import pytest

        with pytest.raises(FileNotFoundError):
            SubprocessRunner().run(["/nonexistent/binary"], timeout=10)

    def test_unstartable_program_reports_127(self) -> None:
        # A file that exists but cannot be executed is a run failure, not a
        # missing dependency, so it comes back as returncode 127.
        not_executable = Path("/tmp/streambridge-not-executable")
        not_executable.write_text("#!/bin/sh\n", encoding="utf-8")
        not_executable.chmod(0o644)
        try:
            result = SubprocessRunner().run([str(not_executable)], timeout=10)
            assert result.returncode == 127
        finally:
            not_executable.unlink(missing_ok=True)


class TestCompletedRun:
    def test_error_summary_truncated(self) -> None:
        stderr = "\n".join(f"line {i}" for i in range(200))
        assert len(CompletedRun((), 1, "", stderr).error_summary()) <= 700

    def test_error_summary_without_stderr(self) -> None:
        assert "127" in CompletedRun((), 127, "", "").error_summary()

    def test_error_summary_keeps_last_lines(self) -> None:
        stderr = "noisy1\nnoisy2\nnoisy3\nnoisy4\nthe real error"
        summary = CompletedRun((), 1, "", stderr).error_summary()
        assert "the real error" in summary
        assert "noisy1" not in summary

    def test_ok_property(self) -> None:
        assert CompletedRun((), 0, "", "").ok
        assert not CompletedRun((), 1, "", "").ok


class TestNoZombies:
    def test_no_zombies_after_many_runs(self) -> None:
        runner = SubprocessRunner()
        for _ in range(25):
            runner.run(["/bin/true"], timeout=5)
        # Every child was reaped by communicate(); none may linger.
        listing = subprocess.run(  # noqa: S603
            ["/bin/ps", "-o", "stat=", "--ppid", str(os.getpid())],
            capture_output=True,
            text=True,
        )
        zombies = [line for line in listing.stdout.splitlines() if line.strip().startswith("Z")]
        assert not zombies, f"zombie processes: {zombies}"
