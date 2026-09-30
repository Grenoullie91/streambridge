"""Integrity of the bundled web UI.

The interface ships inside the Python package, so these tests are part of the
normal run. They guard the promises the README makes: no external resources, no
placeholders, working keyboard access, and a JavaScript file that at least
parses.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

from streambridge.api import _STATIC_FILES
from streambridge.config import default_web_dir

WEB = default_web_dir()

# Markers of unfinished work that must never survive into a shipped interface.
# "placeholder" alone is not one of them: the HTML input attribute and the
# .cover__placeholder class are legitimate.
_PLACEHOLDERS = (
    "TODO",
    "FIXME",
    "XXX",
    "Coming soon",
    "coming soon",
    "Lorem ipsum",
    "lorem ipsum",
    "Platzhalter",
    "nicht implementiert",
    "noch nicht fertig",
    "dummy",
)

# Hosts the page must not load anything from. Thumbnails are the single
# exception and are served through the backend's /thumbnail/<id> redirect.
_EXTERNAL_HOSTS = (
    "cdn.",
    "unpkg.com",
    "jsdelivr",
    "cdnjs",
    "googleapis.com/gstatic",
    "fonts.googleapis",
    "ajax.googleapis",
    "bootstrapcdn",
    "cloudflare",
    "gaug.es",
    "google-analytics",
    "googletagmanager",
    "doubleclick",
)


def _button_calls(source: str) -> list[tuple[str, str]]:
    """Return ``(attributes, children)`` for every ``el('button', {...}, ...)``.

    Uses a balanced scan instead of a regex so nested braces and arrow
    functions do not truncate the result.
    """
    out: list[tuple[str, str]] = []
    needle = "el('button', {"
    index = source.find(needle)
    while index != -1:
        cursor = index + len(needle) - 1  # at the opening brace
        depth = 0
        end = cursor
        while end < len(source):
            if source[end] == "{":
                depth += 1
            elif source[end] == "}":
                depth -= 1
                if depth == 0:
                    break
            end += 1
        attrs = source[cursor + 1 : end]
        tail = end + 1
        depth = 1
        while tail < len(source) and depth:
            if source[tail] == "(":
                depth += 1
            elif source[tail] == ")":
                depth -= 1
            tail += 1
        out.append((attrs, source[end + 1 : tail - 1]))
        index = source.find(needle, index + 1)
    return out


def _read(name: str) -> str:
    assert WEB is not None, "web-Verzeichnis nicht gefunden"
    return (WEB / name).read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def index_html() -> str:
    return _read("index.html")


@pytest.fixture(scope="module")
def app_js() -> str:
    return _read("app.js")


@pytest.fixture(scope="module")
def app_css() -> str:
    return _read("app.css")


class TestAssetsExist:
    def test_web_dir_is_present(self) -> None:
        assert WEB is not None and WEB.is_dir(), (
            f"Die Weboberfläche fehlt ({WEB}). Wurde das Paket unvollständig installiert?"
        )

    def test_index_is_present(self) -> None:
        assert (WEB / "index.html").is_file()

    @pytest.mark.parametrize(("name", "content_type"), sorted(set(_STATIC_FILES.values())))
    def test_allowlisted_file_exists(self, name: str, content_type: str) -> None:
        assert (WEB / name).is_file(), f"{name} fehlt, wird aber ausgeliefert"
        assert content_type

    def test_no_unexpected_files(self) -> None:
        """Only the allowlisted files are ever served, so nothing else is needed."""
        on_disk = {
            str(p.relative_to(WEB))
            for p in WEB.rglob("*")
            if p.is_file() and p.suffix not in {".pyc"}
        }
        served = {name for _path, (name, _type) in _STATIC_FILES.items()} | {"index.html"}
        assert on_disk == served, f"unerwartete Dateien im Paket: {on_disk - served}"


class TestSelfContained:
    def test_no_external_resources(self, index_html: str, app_js: str, app_css: str) -> None:
        combined = index_html + app_js + app_css
        for host in _EXTERNAL_HOSTS:
            assert host not in combined, f"externe Ressource gefunden: {host}"

    def test_no_remote_fetches(self, app_js: str) -> None:
        # The browser may only talk to this server. The single allowed
        # exception is a user-initiated link out to YouTube.
        allowed = ("youtube.com/watch", "www.w3.org/2000/svg")
        remote = [
            match
            for match in re.findall(r"""https?://[^\s'"`)]+""", app_js)
            if not any(prefix in match for prefix in allowed)
        ]
        assert remote == [], f"entfernte URLs im JavaScript: {remote}"

    def test_no_inline_script_or_style(self, index_html: str) -> None:
        # The Content-Security-Policy allows 'self' only, so inline code would
        # simply be blocked in the browser.
        assert "<style" not in index_html.lower()
        for match in re.findall(r"<script\b[^>]*>(.*?)</script>", index_html, re.S | re.I):
            assert not match.strip(), "Inline-Skript gefunden; die CSP verbietet es"

    def test_stylesheet_and_script_are_external_files(self, index_html: str) -> None:
        assert '<link rel="stylesheet" href="/app.css">' in index_html
        assert '<script src="/app.js" type="module"></script>' in index_html

    def test_no_external_font(self, app_css: str) -> None:
        assert "@font-face" not in app_css
        assert "@import" not in app_css


class TestNoPlaceholders:
    @pytest.mark.parametrize("name", ["index.html", "app.js", "app.css"])
    def test_no_placeholder_markers(self, name: str) -> None:
        text = _read(name)
        for marker in _PLACEHOLDERS:
            assert marker not in text, f"{marker!r} in {name}"

    def test_every_button_has_a_label(self, index_html: str) -> None:
        """A button without an accessible name is unusable with a screen reader."""
        for match in re.finditer(r"<button\b([^>]*)>(.*?)</button>", index_html, re.S | re.I):
            attrs, inner = match.group(1), match.group(2)
            has_text = re.sub(r"<svg.*?</svg>", "", inner, flags=re.S).strip() != ""
            has_aria = "aria-label=" in attrs or "aria-labelledby=" in attrs
            assert has_text or has_aria, f"Button ohne Beschriftung: {match.group(0)[:80]}"

    def test_no_wired_but_empty_buttons(self, app_js: str) -> None:
        """Every button is either labelled or carries its label as a child.

        A balanced-brace scan, because a naive regex stops at the first `}`
        inside an arrow function.
        """
        for attrs, children in _button_calls(app_js):
            # An icon on its own is not a name; anything else counts.
            without_icons = re.sub(r"icon\([^()]*(\([^()]*\))?[^()]*\)", "", children)
            has_text_child = without_icons.strip(" ,\n\t") != ""
            labelled = "aria-label" in attrs or "text:" in attrs or has_text_child
            assert labelled, f"Button ohne Beschriftung: {attrs[:90]}"


class TestAccessibility:
    def test_html_is_german_and_declares_a_language(self, index_html: str) -> None:
        assert '<html lang="de">' in index_html

    def test_has_a_skip_link_and_main_landmarks(self, index_html: str) -> None:
        assert 'class="skip-link"' in index_html
        assert "<main" in index_html
        assert "<nav" in index_html
        assert 'role="search"' in index_html

    def test_player_controls_are_labelled(self, index_html: str) -> None:
        for control in (
            "btn-play",
            "btn-next",
            "btn-prev",
            "btn-mute",
            "btn-shuffle",
            "btn-repeat",
        ):
            match = re.search(rf'id="{control}"([^>]*)', index_html)
            assert match, control
            assert "aria-label" in match.group(1), f"{control} hat kein aria-label"

    def test_sliders_expose_range_roles(self, index_html: str) -> None:
        for control in ("seek-track", "volume"):
            match = re.search(rf'id="{control}"([^>]*)', index_html)
            assert match and 'role="slider"' in match.group(1), control

    def test_live_region_for_messages(self, index_html: str) -> None:
        assert 'aria-live="polite"' in index_html

    def test_reduced_motion_is_respected(self, app_css: str) -> None:
        assert "prefers-reduced-motion" in app_css

    def test_focus_is_visible(self, app_css: str) -> None:
        assert ":focus-visible" in app_css

    def test_mobile_layout_rules_exist(self, app_css: str) -> None:
        assert "@media (max-width: 900px)" in app_css
        assert "@media (max-width: 560px)" in app_css


class TestScriptIntegrity:
    def test_no_console_noise_in_production(self, app_js: str) -> None:
        # A stray debug statement would spam the console for every user.
        for line in app_js.splitlines():
            stripped = line.strip()
            assert not stripped.startswith("console.log"), stripped
            assert not stripped.startswith("console.debug"), stripped

    def test_no_debugger_statement(self, app_js: str) -> None:
        assert "debugger" not in app_js

    def test_balanced_braces_and_parens(self, app_js: str) -> None:
        # A cheap structural check; the parser below is the real one.
        stripped = re.sub(r"/\*.*?\*/", "", app_js, flags=re.S)
        stripped = re.sub(r"(?m)//.*$", "", stripped)
        stripped = re.sub(r"'(?:\\.|[^'\\])*'", "''", stripped)
        stripped = re.sub(r'"(?:\\.|[^"\\])*"', '""', stripped)
        stripped = re.sub(r"`(?:\\.|[^`\\])*`", "``", stripped)
        for opener, closer in (("{", "}"), ("(", ")"), ("[", "]")):
            assert stripped.count(opener) == stripped.count(closer), opener

    @pytest.mark.skipif(shutil.which("node") is None, reason="node ist nicht installiert")
    def test_parses_with_node(self, app_js: str) -> None:
        assert WEB is not None
        script = WEB / "app.js"
        result = subprocess.run(  # noqa: S603
            ["node", "--check", str(script)],  # noqa: S607
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        del app_js

    @pytest.mark.skipif(shutil.which("node") is None, reason="node ist nicht installiert")
    def test_manifest_is_valid_json(self) -> None:
        import json

        assert WEB is not None
        data = json.loads((WEB / "manifest.webmanifest").read_text(encoding="utf-8"))
        assert data["name"]
        assert data["start_url"] == "/"
        assert all(icon["src"].startswith("/") for icon in data["icons"])


def test_web_dir_can_be_overridden(tmp_path: Path) -> None:
    from dataclasses import replace

    from streambridge.config import Config

    assert Config().web_enabled is True
    assert replace(Config(), web_directory=tmp_path).web_enabled is False
    assert replace(Config(), web_directory=None).web_enabled is False


class TestNoHtmlInjectionSink:
    """The UI must have exactly one innerHTML, and it must take a constant.

    An upstream title is attacker-controlled: anyone can publish a video whose
    title is a payload, and that title is rendered in the search results, the
    queue and the now-playing bar. So text has to reach the DOM as text.

    The single permitted innerHTML is the SVG icon builder, whose argument is
    an ICON constant. This test exists so that adding a second one is a
    deliberate act with a reason attached, rather than something the next
    contributor does to render a bold title.
    """

    def test_only_the_icon_builder_uses_innerhtml(self) -> None:
        assert WEB is not None
        source = (WEB / "app.js").read_text(encoding="utf-8")
        code_lines = [
            line
            for line in source.splitlines()
            if "innerHTML" in line and not line.lstrip().startswith(("*", "//"))
        ]
        assert len(code_lines) == 1, f"unexpected innerHTML use: {code_lines}"
        assert "svg.innerHTML = paths" in code_lines[0]

    def test_the_element_helper_has_no_html_key(self) -> None:
        assert WEB is not None
        source = (WEB / "app.js").read_text(encoding="utf-8")
        assert "node.innerHTML" not in source

    def test_text_is_set_through_textcontent(self) -> None:
        assert WEB is not None
        source = (WEB / "app.js").read_text(encoding="utf-8")
        assert "node.textContent = String(value)" in source

    def test_the_ui_does_not_eval(self) -> None:
        # eval would need 'unsafe-eval' in the CSP, which the server does not
        # send. Together the two must agree, or the UI silently stops working.
        assert WEB is not None
        source = (WEB / "app.js").read_text(encoding="utf-8")
        for dangerous in ("eval(", "new Function("):
            assert dangerous not in source
