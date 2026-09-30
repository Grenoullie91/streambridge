"""Process execution helpers.

Every external program is started through :class:`SubprocessRunner`, which means:

* arguments are always a list, never a shell string
* a timeout is always enforced
* the child is always reaped, so no zombies survive
* stdout and stderr are captured separately
* on timeout the whole process group is killed
"""

from __future__ import annotations

import contextlib
import logging
import os
import shutil
import signal
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from .logging import redact_args

log = logging.getLogger("streambridge.proc")


@dataclass(frozen=True, slots=True)
class CompletedRun:
    """Result of a finished subprocess."""

    args: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    def error_summary(self, max_lines: int = 4) -> str:
        """Last meaningful stderr lines, for user-facing error messages."""
        lines = [ln.strip() for ln in self.stderr.splitlines() if ln.strip()]
        if not lines:
            return f"exited with code {self.returncode} without a message"
        return " | ".join(lines[-max_lines:])[:600]


class CommandRunner:
    """Interface so tests can substitute process execution."""

    def run(
        self,
        args: Sequence[str],
        *,
        timeout: float,
        stdin_data: str | None = None,
        env: Mapping[str, str] | None = None,
    ) -> CompletedRun:  # pragma: no cover - interface
        raise NotImplementedError


class SubprocessRunner(CommandRunner):
    """Real runner based on :mod:`subprocess` with the shell disabled."""

    def run(
        self,
        args: Sequence[str],
        *,
        timeout: float,
        stdin_data: str | None = None,
        env: Mapping[str, str] | None = None,
    ) -> CompletedRun:
        argv = [str(a) for a in args]
        log.debug("exec %s (timeout=%.1fs)", " ".join(redact_args(argv)), timeout)
        full_env = dict(os.environ)
        if env:
            full_env.update(env)
        try:
            # start_new_session puts the child in its own process group so a
            # timeout can kill it *and* anything it spawned.
            proc = subprocess.Popen(  # noqa: S603 - list argv, shell=False
                argv,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.PIPE if stdin_data is not None else subprocess.DEVNULL,
                text=True,
                errors="replace",
                env=full_env,
                shell=False,
                start_new_session=True,
            )
        except FileNotFoundError:
            raise
        except OSError as exc:
            return CompletedRun(tuple(argv), 127, "", f"start failed: {exc}")

        try:
            stdout, stderr = proc.communicate(input=stdin_data, timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill_process_tree(proc)
            with contextlib.suppress(subprocess.TimeoutExpired):
                stdout, stderr = proc.communicate(timeout=5)
            return CompletedRun(
                tuple(argv),
                124,
                stdout or "",
                (stderr or "") + f"\nTimeout after {timeout:.1f}s",
            )
        finally:
            if proc.poll() is None:  # pragma: no cover - defensive
                _kill_process_tree(proc)
                proc.wait(timeout=5)

        return CompletedRun(tuple(argv), proc.returncode, stdout or "", stderr or "")


def _kill_process_tree(proc: subprocess.Popen[str]) -> None:
    """Terminate the child and its process group, ignoring races."""
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        with contextlib.suppress(ProcessLookupError, OSError):
            proc.terminate()
    with contextlib.suppress(subprocess.TimeoutExpired):
        proc.wait(timeout=3)
        return
    with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
        os.killpg(proc.pid, signal.SIGKILL)
    with contextlib.suppress(subprocess.TimeoutExpired):
        proc.wait(timeout=3)


def which(program: str) -> str | None:
    """Locate *program* on PATH, returning its absolute path or None."""
    if not program:
        return None
    if os.sep in program:
        candidate = Path(program).expanduser().resolve()
        return str(candidate) if candidate.is_file() and os.access(candidate, os.X_OK) else None
    return shutil.which(program)
