#!/usr/bin/env python3
"""Start ncmpcpp in a pseudo-terminal and print what it rendered.

ncmpcpp is a full-screen TUI and refuses to start without a PTY. This script
forks one, waits, collects the screen contents through the ANSI stream, sends
any extra keys, and shuts it down again.

Usage:  ncmpcpp-probe.py <seconds> [key ...]
"""

from __future__ import annotations

import contextlib
import os
import pty
import re
import select
import signal
import sys
import time

ANSI = re.compile(rb"\x1b\[[0-9;?]*[a-zA-Z]|\x1b[()][B0]|\x1b[=>]|\r")

ROWS = 45
COLS = 160


def clean(data: bytes) -> str:
    text = ANSI.sub(b"", data).decode("utf-8", "replace")
    return "\n".join(line.rstrip() for line in text.splitlines())


def _set_window_size(fd: int) -> None:
    try:
        import fcntl
        import struct
        import termios

        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", ROWS, COLS, 0, 0))
    except (ImportError, OSError):
        # Window size is cosmetic; the probe still works without it.
        pass


def main() -> int:
    hold = float(sys.argv[1]) if len(sys.argv) > 1 else 4.0
    keys = sys.argv[2:]

    pid, fd = pty.fork()
    if pid == 0:  # child
        os.environ["TERM"] = "xterm-256color"
        os.environ["LINES"] = str(ROWS)
        os.environ["COLUMNS"] = str(COLS)
        # Deliberately exec a hardcoded name: no user input reaches exec.
        os.execvp("ncmpcpp", ["ncmpcpp"])  # noqa: S606, S607

    _set_window_size(fd)
    chunks: list[bytes] = []

    def pump(duration: float) -> None:
        end = time.monotonic() + duration
        while time.monotonic() < end:
            ready, _, _ = select.select([fd], [], [], 0.2)
            if not ready:
                continue
            try:
                data = os.read(fd, 65536)
            except OSError:
                return
            if not data:
                return
            chunks.append(data)

    pump(hold)
    for key in keys:
        try:
            os.write(fd, key.encode())
        except OSError:
            break
        pump(1.2)

    with contextlib.suppress(ProcessLookupError, OSError):
        os.kill(pid, signal.SIGTERM)
    pump(1.0)
    with contextlib.suppress(ChildProcessError, OSError):
        os.waitpid(pid, os.WNOHANG)

    sys.stdout.write(clean(b"".join(chunks)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
