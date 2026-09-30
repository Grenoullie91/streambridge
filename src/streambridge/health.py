"""Environment checks.

Reports which parts of the toolchain are present and usable, so a user can
tell a missing dependency apart from a misconfigured one. Output is meant to
be read by a human, so it is plain text plus an optional JSON mode.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from . import __version__
from .config import Config
from .proc import which
from .youtube import MIN_EXTRACTOR_VERSION, ExtractorClient

# StreamBridge needs 3.11 for tomllib; there is no stdlib TOML parser before
# that, and adding a dependency for a config file is not worth it.
REQUIRED_PYTHON = (3, 11)

# name -> (why it matters, required)
COMPONENTS: tuple[tuple[str, str, bool], ...] = (
    ("yt-dlp", "search and streaming", True),
    ("mpc", "MPD control from the CLI", True),
    ("mpd", "audio output", False),
    ("ffmpeg", "optional transcoding for players", False),
    ("ncmpcpp", "optional terminal player UI", False),
)


@dataclass(frozen=True, slots=True)
class Check:
    """Outcome of a single component check."""

    name: str
    ok: bool
    path: str | None = None
    version: str | None = None
    detail: str | None = None
    required: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "ok": self.ok,
            "path": self.path,
            "version": self.version,
            "detail": self.detail,
            "required": self.required,
        }


@dataclass(slots=True)
class Report:
    """Aggregated environment report."""

    checks: list[Check] = field(default_factory=list)
    python_version: str = sys.version.split()[0]
    python_ok: bool = True
    bind: str = ""
    cache_directory: str = ""
    mpd_endpoint: str = ""
    mpd_reachable: bool | None = None
    server_reachable: bool | None = None

    @property
    def ok(self) -> bool:
        """True when nothing required is missing."""
        if not self.python_ok:
            return False
        return all(c.ok for c in self.checks if c.required)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": "ok" if self.ok else "incomplete",
            "version": __version__,
            "python": {"version": self.python_version, "ok": self.python_ok},
            "components": [c.to_dict() for c in self.checks],
            "bind": self.bind,
            "cache_directory": self.cache_directory,
            "mpd": {"endpoint": self.mpd_endpoint, "reachable": self.mpd_reachable},
            "server_reachable": self.server_reachable,
        }

    def render(self) -> str:
        """Human-readable report.

        Values are printed verbatim: this output is meant to be pasted into a
        bug report, and a home path or hostname is the reporter's own data.
        The log stream is where redaction applies.
        """
        lines: list[str] = ["StreamBridge environment check", ""]
        python_note = (
            ""
            if self.python_ok
            else f"  (too old, {REQUIRED_PYTHON[0]}.{REQUIRED_PYTHON[1]}+ required)"
        )
        lines.append(f"  Python        : {self.python_version}{python_note}")
        lines.append("")
        for check in self.checks:
            marker = (
                "ok      " if check.ok else ("missing " if check.required else "missing (optional)")
            )
            lines.append(f"  {check.name:<14}: {marker} {check.detail or ''}".rstrip())
            if check.path:
                lines.append(f"  {'':<14}  path: {check.path}")
            if check.version:
                lines.append(f"  {'':<14}  version: {check.version}")
        lines.append("")
        lines.append(f"  Bind          : {self.bind}")
        lines.append(f"  Cache         : {self.cache_directory}")
        lines.append(f"  MPD           : {self.mpd_endpoint}")
        if self.mpd_reachable is not None:
            lines.append(
                f"  MPD status    : {'reachable' if self.mpd_reachable else 'not reachable'}"
            )
        if self.server_reachable is not None:
            lines.append(
                f"  Server        : {'reachable' if self.server_reachable else 'not running'}"
            )
        lines.append("")
        lines.append("Result: " + ("OK" if self.ok else "INCOMPLETE"))
        return "\n".join(lines)


def _version_at_least(version: str, minimum: str) -> bool:
    """Compare dotted version strings without importing packaging."""

    def parts(value: str) -> tuple[int, ...]:
        out: list[int] = []
        for chunk in value.split("."):
            digits = "".join(ch for ch in chunk if ch.isdigit())
            out.append(int(digits) if digits else 0)
        return tuple(out)

    return parts(version) >= parts(minimum)


def check_component(name: str, detail: str, required: bool) -> Check:
    """Locate one component on PATH."""
    path = which(name)
    if path is None:
        return Check(name=name, ok=False, detail=f"missing ({detail})", required=required)
    return Check(name=name, ok=True, path=path, detail=detail, required=required)


def build_report(config: Config, *, probe_network: bool = False) -> Report:
    """Assemble the full report.

    ``probe_network`` controls whether MPD and the server are actually
    contacted. It is off by default so that a pure dependency check stays
    fast and never touches the network.
    """
    report = Report(
        python_ok=sys.version_info >= REQUIRED_PYTHON,
        bind=f"http://{config.host}:{config.port}",
        cache_directory=str(config.cache_directory),
        mpd_endpoint=f"{config.mpd_host}:{config.mpd_port}",
    )

    for name, detail, required in COMPONENTS:
        if name == "yt-dlp":
            client = ExtractorClient(config)
            path = client.require() if _probe_extractor(config) else which(config.extractor_path)
            if path is None:
                report.checks.append(
                    Check(
                        name="yt-dlp",
                        ok=False,
                        detail=f"missing ({detail})",
                        required=True,
                    )
                )
                continue
            version = client.version()
            check = Check(name="yt-dlp", ok=True, path=path, version=version, detail=detail)
            if version and not _version_at_least(version, MIN_EXTRACTOR_VERSION):
                # Advisory: older extractors fail at runtime, not at install.
                check = replace(
                    check,
                    detail=(f"{detail}; older than {MIN_EXTRACTOR_VERSION}, update recommended"),
                )
            report.checks.append(check)
            continue
        report.checks.append(check_component(name, detail, required))

    if probe_network:
        from .api import ApiService
        from .mpd import MpdClient

        try:
            report.server_reachable = ApiService(config).health().get("status") == "ok"
        except Exception:  # pragma: no cover - diagnostics only
            report.server_reachable = False
        report.mpd_reachable = MpdClient(config).ping()

    return report


def _probe_extractor(config: Config) -> bool:
    """True when the extractor binary is present, without raising."""
    return which(config.extractor_path) is not None


def missing_required(config: Config) -> list[str]:
    """Names of required components that are not usable."""
    return [c.name for c in build_report(config).checks if not c.ok and c.required]


def audio_backends() -> dict[str, bool]:
    """Best-effort availability of the common Linux audio servers.

    Used by the docs and the E2E script to pick a capture device; never
    required for the library to function.
    """
    found: dict[str, bool] = {}
    for backend, device in (("pipewire", "/dev/pipewire-0"), ("pulseaudio", "/dev/snd")):
        found[backend] = which(backend) is not None or Path(device).exists()
    return found
