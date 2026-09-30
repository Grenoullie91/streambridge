"""Allow ``python -m streambridge`` in addition to the console scripts."""

from __future__ import annotations

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
