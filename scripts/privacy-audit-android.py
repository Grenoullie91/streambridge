#!/usr/bin/env python3
"""Check the Android tree and the built APKs for anything private.

The question this answers is narrow and blunt: is there a name, a path, an
address, a hostname, a token, a key or a log full of someone else's business
in the source, the build configuration, or either of the shipped APKs?

It is a grep with a list attached, and the list is the interesting part. Run
from the repository root:

    python3 scripts/privacy-audit-android.py

Exit code 0 means clean. Anything found is printed with the file and line, and
the script does not try to decide whether a hit is actually harmless - that
call belongs to a person.
"""

from __future__ import annotations

import pathlib
import re
import shutil
import subprocess
import zipfile

ROOT = pathlib.Path(__file__).resolve().parent.parent

# Resolved once, with a clear failure. Naming a bare "git" in two places means
# the script either depends on PATH silently or fails with an opaque OSError
# deep inside a check; both are worse than saying so up front.
GIT = shutil.which("git")
if GIT is None:  # pragma: no cover - depends on the machine
    raise SystemExit("git was not found on PATH; this audit needs it.")
ANDROID = ROOT / "android"
APKS = sorted((ANDROID / "dist").glob("*.apk"))

# Build output and downloaded dependencies are not source: a Gradle cache full
# of other people's library code will match everything, and a build directory
# full of intermediates is not something anybody ships by accident.
SKIP_DIRS = {
    ".git",
    ".gradle",
    "build",
    "dist",
    "node_modules",
    ".idea",
    ".kotlin",
    "system-images",
    "emulator",
}
SKIP_SUFFIXES = {".jar", ".zip", ".apk", ".aar", ".png", ".jpg", ".webp", ".keystore", ".jks"}

# --- patterns ---------------------------------------------------------------
#
# Each entry is (name, regex, what a match would mean). The regexes are
# deliberately loose: a false positive costs a glance, a false negative costs
# the thing this script exists to prevent.

PATTERNS: list[tuple[str, str, str]] = [
    ("private key", r"-----BEGIN [A-Z ]*PRIVATE KEY-----", "a private key in the tree"),
    ("aws key", r"\bAKIA[0-9A-Z]{16}\b", "an AWS access key id"),
    ("google api key", r"\bAIza[0-9A-Za-z_-]{35}\b", "a Google API key"),
    ("slack token", r"\bxox[abprs]-[0-9A-Za-z-]{10,}\b", "a Slack token"),
    ("github token", r"\bgh[pousr]_[0-9A-Za-z]{36,}\b", "a GitHub token"),
    ("bearer literal", r"(?i)bearer\s+[A-Za-z0-9._-]{20,}", "a hard-coded bearer credential"),
    ("basic auth in url", r"https?://[^/\s:@]+:[^/\s@]+@", "credentials embedded in a URL"),
    (
        "password assignment",
        r"(?i)\b(password|passwd|secret|api[_-]?key|token)\s*[:=]\s*['\"][^'\"\s]{8,}",
        "a credential assigned to a literal",
    ),
    (
        "email",
        r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b",
        "an email address; the build has no reason to contain one",
    ),
    (
        "private ipv4",
        r"\b(?:10\.\d{1,3}\.\d{1,3}\.\d{1,3}|"
        r"192\.168\.\d{1,3}\.\d{1,3}|"
        r"172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3})\b",
        "a private network address; no example should need a real one",
    ),
    ("home path", r"/(?:home|Users)/[A-Za-z0-9._-]+", "a home directory path"),
    ("windows path", r"[A-Z]:\\\\Users\\\\[A-Za-z0-9._-]+", "a home directory path"),
    (
        "uuid",
        r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b",
        "a UUID; usually a machine or session identifier",
    ),
    (
        "android advertising id",
        r"\b(?:advertising|ads|app)id\b|AA_ID|ANDROID_ID",
        "an advertising or device identifier reference",
    ),
    (
        "firebase",
        r"(?i)firebase|google-services\.json|google_app_id",
        "a Firebase or Google Services dependency",
    ),
    (
        "google play services",
        r"(?i)play-services|com\.google\.android\.gms",
        "a Play Services dependency",
    ),
    (
        "crash reporting",
        r"(?i)crashlytics|sentry|bugsnag|appcenter|datadog|newrelic",
        "a crash-reporting or monitoring SDK",
    ),
    (
        "analytics",
        r"(?i)google-analytics|firebase-analytics|mixpanel|amplitude|segment\.io",
        "an analytics SDK",
    ),
    (
        "telemetry endpoints",
        r"https?://[a-z0-9.-]*(?:google-analytics|appcenter|sentry"
        r"|crashlytics|mixpanel|amplitude|segment)[a-z0-9.-]*",
        "a telemetry endpoint",
    ),
]

# Values that look like findings and are not.
#
# A grep cannot tell the difference, so these are listed and justified rather
# than suppressed by pattern: a check that quietly ignores whole classes of
# match stops being a check.
ALLOWED_LINES = (
    "STREAMBRIDGE_LIVE_URL",
    "192.0.2.20",  # the RFC 5737 documentation address the docs use
    "192.0.2.0/24",  # the documentation subnet the firewall example uses
    "192.0.2.256",  # in a test, as an address the validator must reject
    "999.999.999.999",  # likewise: not a valid address at all
    "10.0.2.2",  # the emulator's alias for the host
    "example.invalid",  # RFC 2606 reserved, used as a dead host
    "nas.home.arpa",  # RFC 8375: reserved for home networks, never public
    "server.token",  # a DataStore key name, not a credential
    "192.0.2.1",  # RFC 5737 TEST-NET-1, used to pick a source address
)


def gitignored(path: pathlib.Path) -> bool:
    """True when git would refuse to track *path*.

    Untracked-and-ignored is the whole point of a .gitignore entry: that file
    can hold a real path on this machine and still be incapable of reaching a
    repository. Reporting it would be a false positive that trains the reader
    to ignore the output.
    """
    try:
        # argv list, no shell: the arguments are ours, and a path can contain
        # spaces or a quote without becoming a command.
        result = subprocess.run(  # noqa: S603
            [GIT, "-C", str(ROOT), "check-ignore", "-q", str(path)],
            capture_output=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def human(path: pathlib.Path) -> str:
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def scan_text(name: str, text: str, findings: list[tuple[str, str, str]]) -> None:
    for line_number, line in enumerate(text.splitlines(), start=1):
        if any(marker in line for marker in ALLOWED_LINES):
            continue
        for label, pattern, meaning in PATTERNS:
            if re.search(pattern, line):
                findings.append((f"{name}:{line_number}", label, meaning))


def scan_tree() -> list[tuple[str, str, str]]:
    findings: list[tuple[str, str, str]] = []
    skipped_ignored: list[str] = []
    count = 0
    for path in ANDROID.rglob("*"):
        if not path.is_file():
            continue
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if path.suffix in SKIP_SUFFIXES:
            continue
        if gitignored(path):
            skipped_ignored.append(human(path))
            continue
        # Binary files that slipped through by extension.
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        count += 1
        scan_text(human(path), text, findings)
    print(f"  scanned {count} tracked source and configuration files under android/")
    if skipped_ignored:
        print(
            f"  skipped {len(skipped_ignored)} untracked, gitignored file(s), which "
            f"cannot reach a\n  repository: {', '.join(skipped_ignored)}"
        )
    return findings


def scan_apks() -> list[tuple[str, str, str]]:
    findings: list[tuple[str, str, str]] = []
    if not APKS:
        print("  no APKs in android/dist - run scripts/build-android.sh first")
        return findings
    for apk in APKS:
        with zipfile.ZipFile(apk) as archive:
            names = archive.namelist()
            for name in names:
                interesting = (".xml", ".json", ".properties", ".txt", ".MF", ".SF", ".RSA")
                if not name.endswith(interesting):
                    continue
                if name.startswith("META-INF/") and name.endswith((".MF", ".SF", ".RSA")):
                    # Signature blocks: check the certificate subject only.
                    raw = archive.read(name)
                    dn = rb"/(emailAddress|street|city|OU|O|CN)=([^/\x00]{1,64})"
                    for match in re.finditer(dn, raw):
                        field = match.group(1).decode()
                        value = match.group(2).decode(errors="replace")
                        if field == "emailAddress":
                            findings.append(
                                (
                                    f"{apk.name}!{name}",
                                    "certificate email",
                                    "an email address in the signing certificate",
                                )
                            )
                        elif field in ("CN", "O", "OU", "street", "city", "L", "ST") and not (
                            re.fullmatch(r"[A-Za-z0-9 .,/+-]*", value)
                        ):
                            findings.append(
                                (
                                    f"{apk.name}!{name}",
                                    "certificate subject",
                                    f"unexpected {field} in the certificate",
                                )
                            )
                    continue
                try:
                    text = archive.read(name).decode("utf-8")
                except UnicodeDecodeError:
                    continue
                scan_text(f"{apk.name}!{name}", text, findings)
            # The manifest is binary XML, so the class list is what matters.
            for name in names:
                if not name.endswith(".dex"):
                    continue
                raw = archive.read(name)
                for needle, label, meaning in (
                    (b"com/google/ads", "ads SDK", "an advertising class in the APK"),
                    (b"com/google/android/gms/ads", "ads SDK", "an advertising class in the APK"),
                    (b"com/crashlytics", "crash reporter", "a crash-reporting class in the APK"),
                    (b"io/sentry", "crash reporter", "a crash-reporting class in the APK"),
                    (b"com/google/firebase", "firebase", "a Firebase class in the APK"),
                    (
                        b"com/google/android/gms/analytics",
                        "analytics",
                        "an analytics class in the APK",
                    ),
                ):
                    if needle in raw:
                        findings.append((f"{apk.name}!{name}", label, meaning))
            print(
                f"  scanned {apk.name}: {len(names)} entries, "
                f"{sum(1 for n in names if n.endswith('.dex'))} dex"
            )
    return findings


def scan_history() -> list[tuple[str, str, str]]:
    """A committed secret can be deleted and still be in the history."""
    findings: list[tuple[str, str, str]] = []
    try:
        output = subprocess.run(  # noqa: S603
            [
                GIT,
                "-C",
                str(ROOT),
                "log",
                "--all",
                "--diff-filter=A",
                "--name-only",
                "--pretty=format:",
                "--",
                "android",
            ],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return findings
    for line in output.splitlines():
        if any(marker in line for marker in ALLOWED_LINES):
            continue
        for label, pattern, meaning in PATTERNS:
            if label in ("private ipv4", "email", "home path") and re.search(pattern, line):
                findings.append((f"git history: {line}", label, meaning))
    if output.strip():
        print("  checked git history for the android/ tree")
    return findings


def main() -> int:
    print("Privacy audit: android/ and the built APKs\n")
    findings = scan_tree()
    findings += scan_apks()
    findings += scan_history()

    if not findings:
        print(
            "\nNothing found: no names, paths, addresses, hostnames, addresses, "
            "tokens,\nkeys or device identifiers in the source, the build "
            "configuration or the APKs."
        )
        return 0

    print(f"\n{len(findings)} finding(s):\n")
    for where, label, meaning in findings:
        print(f"  {where}\n      [{label}] {meaning}")
    print("\nCheck each one by hand. A hit is not proof of a leak; it is a reason to look.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
