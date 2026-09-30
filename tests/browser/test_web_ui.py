#!/usr/bin/env python3
"""End-to-end browser check for the StreamBridge web UI.

Drives the real page in headless Chromium against a real streambridge-server and
verifies the flows a user actually performs. Skipped automatically when
Playwright or its browser is not installed, so the default test run stays
dependency-free.

    streambridge-server &            # or systemctl --user start streambridge-server
    python tests/browser/test_web_ui.py [--base http://127.0.0.1:8787]
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request

BASE_URL = "http://127.0.0.1:8787"


def _api(path: str, payload: dict | None = None, *, attempts: int = 3) -> dict:
    """Call the local API, retrying a refused or reset connection.

    The test process and the browser talk to the server at the same time; a
    single dropped keep-alive connection must not abort the whole run.
    """
    data = json.dumps(payload).encode() if payload is not None else None
    last: Exception | None = None
    for attempt in range(attempts):
        request = urllib.request.Request(
            f"{BASE_URL}{path}",
            data=data,
            headers={"Content-Type": "application/json"} if data else {},
            method="POST" if data else "GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.loads(response.read())
        except (urllib.error.URLError, OSError, ValueError) as exc:
            last = exc
            time.sleep(0.5 * (attempt + 1))
    raise AssertionError(f"{path} nicht erreichbar: {last}")


def _wait_for(predicate, timeout: float = 20.0, interval: float = 0.4):
    """Poll *predicate* until it returns a truthy value or *timeout* elapses."""
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = predicate()
        if last:
            return last
        time.sleep(interval)
    return last


def _reachable() -> bool:
    try:
        _api("/health")
    except (urllib.error.URLError, OSError, ValueError):
        return False
    return True


def main() -> int:
    global BASE_URL

    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default=BASE_URL)
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--shots", default="", help="directory for screenshots")
    args = parser.parse_args()

    BASE_URL = args.base.rstrip("/")

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("Playwright ist nicht installiert - Browser-Test übersprungen.")
        print("  pip install playwright && playwright install chromium")
        return 0

    if not _reachable():
        print(f"streambridge-server antwortet nicht auf {BASE_URL} - Browser-Test übersprungen.")
        return 0

    # Start from a clean, reproducible state: a leftover favourite would make
    # the first toggle a removal instead of an addition.
    _api("/player/stop", {})
    _api("/queue/clear", {})
    _api("/favorites/clear", {})
    _api("/history/clear", {})

    failures: list[str] = []
    console_errors: list[str] = []
    client_errors: list[str] = []

    def ensure_playing(position: int = 1) -> bool:
        """Make sure MPD plays *position*, retrying a stalled or dead stream.

        YouTube rate-limits a test that opens this many streams in a few
        minutes; when that happens MPD stops. That is an upstream condition,
        not a defect, so the test restarts playback instead of failing.
        """
        for _ in range(3):
            status = _api("/player/status")
            if status["state"] == "playing" and status["position"] == position:
                return True
            try:
                _api("/player/play", {"position": position})
            except Exception:
                pass
            if _wait_for(lambda: _api("/player/status")["state"] == "playing", 20):
                return _api("/player/status")["position"] == position
        return False

    def check(label: str, condition: bool, detail: str = "") -> None:
        if condition:
            print(f"  ok    {label}")
        else:
            print(f"  FAIL  {label} {detail}")
            failures.append(label)

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=not args.headed)
        page = browser.new_page(viewport={"width": 1440, "height": 900})

        def on_console(message) -> None:
            if message.type != "error":
                return
            text = message.text
            if "Failed to load resource" in text:
                # A non-2xx response is logged by the browser itself; the HTTP
                # listener below already judges those.
                console_errors.append(text)
            else:
                client_errors.append(text)

        page.on("console", on_console)
        page.on("pageerror", lambda e: client_errors.append(f"pageerror: {e}"))
        page.on("requestfailed", lambda r: client_errors.append(f"requestfailed {r.url}"))
        page.on(
            "response",
            # Only server faults count: a rejected user action is answered with
            # 4xx on purpose and is surfaced as a toast, not a defect.
            lambda r: (
                console_errors.append(f"HTTP {r.status} {r.url}")
                if r.status >= 500 and "/thumbnail/" not in r.url
                else None
            ),
        )

        print("Lade Seite …")
        page.goto(BASE_URL, wait_until="domcontentloaded")
        # A CSS wait, not wait_for_function: the page's CSP forbids eval,
        # which Playwright's polling helper needs.
        page.wait_for_selector('#connection[data-state="live"]', timeout=25_000)
        check("Seite lädt", page.title() != "", page.title())
        check("Sidebar sichtbar", page.is_visible("#sidebar"))
        check("Playerleiste sichtbar", page.is_visible("#player"))
        check("Live-Status", page.get_attribute("#connection", "data-state") == "live")
        check("Menü-Button auf Desktop ausgeblendet", not page.is_visible("#nav-toggle"))
        # Every control must expose an accessible name. Walking the live DOM is
        # the only way to catch the buttons JavaScript creates.
        unnamed = page.evaluate(
            """() => {
                const bad = [];
                for (const b of document.querySelectorAll('button')) {
                    const name = (b.getAttribute('aria-label') || b.textContent || '').trim();
                    if (!name) bad.push(b.outerHTML.slice(0, 90));
                }
                return bad;
            }"""
        )
        check("Alle Buttons haben einen Namen", not unnamed, str(unnamed[:3]))
        if args.shots:
            page.screenshot(path=f"{args.shots}/01-start.png", full_page=True)

        # -- Suche -----------------------------------------------------
        print("Suche …")
        page.fill("#search-input", "massive attack")
        page.press("#search-input", "Enter")
        page.wait_for_selector("#view-search .track", timeout=120_000)
        rows = page.locator("#view-search .track")
        check("Suchergebnisse erscheinen", rows.count() > 0, f"{rows.count()} Treffer")
        check("Thumbnails geladen", page.locator("#view-search .track__art img").count() > 0)
        check(
            "Dauer wird angezeigt",
            (rows.nth(0).locator(".track__duration").text_content() or "").strip() != "0:00",
        )
        if args.shots:
            page.screenshot(path=f"{args.shots}/02-suche.png", full_page=True)

        # -- Zur Queue hinzufügen --------------------------------------
        print("Queue …")
        before = len(_api("/queue")["items"])
        for index in range(3):
            rows.nth(index).locator('[data-action="queue"]').click()
            target = before + index + 1
            check(
                f"Titel {index + 1} hinzugefügt",
                _wait_for(lambda t=target: len(_api("/queue")["items"]) == t, 30),
                f"{len(_api('/queue')['items'])} statt {target}",
            )

        # -- Wiedergabe ------------------------------------------------
        print("Wiedergabe …")
        page.click("#view-search .track:nth-child(1) .track__art")
        playing = _wait_for(lambda: _api("/player/status")["state"] == "playing", timeout=40)
        status = _api("/player/status")
        check("Wiedergabe läuft", playing, status["state"])
        check(
            "Aktueller Titel gesetzt",
            bool(status.get("current")),
            json.dumps(status.get("current")),
        )
        check(
            "Playerleiste zeigt Titel",
            (page.text_content("#now-title") or "").strip() != "Nichts wird abgespielt",
            page.text_content("#now-title"),
        )
        elapsed_one = status["elapsed"]
        # Real audio has to arrive: MPD reports "playing" before the first byte
        # does. YouTube occasionally rejects a signed URL, in which case MPD
        # stalls - retry once before calling it a failure.
        progress = _wait_for(lambda: _api("/player/status")["elapsed"] > elapsed_one, 25)
        if not progress:
            _api("/player/play", {"position": _api("/player/status")["position"] or 1})
            baseline = _api("/player/status")["elapsed"]
            progress = _wait_for(lambda: _api("/player/status")["elapsed"] > baseline, 25)
        check(
            "Audio läuft durch (Fortschritt steigt)",
            progress,
            json.dumps(_api("/player/status"))[:160],
        )
        if args.shots:
            page.screenshot(path=f"{args.shots}/03-wiedergabe.png", full_page=True)

        # -- Pause / Play ---------------------------------------------
        page.click("#btn-play")
        check(
            "Pause funktioniert", _wait_for(lambda: _api("/player/status")["state"] == "paused", 15)
        )
        check(
            "Player zeigt Pause-Icon",
            _wait_for(lambda: page.get_attribute("#btn-play", "data-state") == "paused", 10),
            page.get_attribute("#btn-play", "data-state"),
        )
        page.click("#btn-play")
        check(
            "Play funktioniert", _wait_for(lambda: _api("/player/status")["state"] == "playing", 15)
        )

        # -- Seek ------------------------------------------------------
        before_elapsed = _api("/player/status")["elapsed"]
        box = page.locator("#seek-track").bounding_box()
        page.mouse.click(box["x"] + box["width"] * 0.5, box["y"] + box["height"] / 2)
        after_seek = _wait_for(
            lambda: (
                _api("/player/status")["elapsed"]
                if abs(_api("/player/status")["elapsed"] - before_elapsed) > 3
                else None
            ),
            15,
        )
        check("Seek springt", after_seek is not None, f"{before_elapsed} -> {after_seek}")
        fill_width = lambda: page.locator("#seek-fill").evaluate(  # noqa: E731
            "node => node.style.width"
        )
        check(
            "Fortschrittsbalken folgt",
            _wait_for(lambda: float(fill_width().rstrip("%") or 0) > 20, 10),
            fill_width(),
        )

        # -- Volume ----------------------------------------------------
        vbox = page.locator(".volume__track").bounding_box()
        page.mouse.click(vbox["x"] + vbox["width"] * 0.3, vbox["y"] + vbox["height"] / 2)
        check(
            "Lautstärke regelbar",
            _wait_for(lambda: 10 <= _api("/player/status")["volume"] <= 45, 12),
            str(_api("/player/status")["volume"]),
        )
        page.click("#btn-mute")
        check("Mute funktioniert", _wait_for(lambda: _api("/player/status")["volume"] == 0, 12))
        page.click("#btn-mute")
        check("Unmute funktioniert", _wait_for(lambda: _api("/player/status")["volume"] > 0, 12))

        # -- Next / Previous -------------------------------------------
        # Start from a known place: earlier steps consumed real playing time,
        # so the track may already have run to the end of the queue.
        check("Wiedergabe für Sprungtests bereit", ensure_playing(1))
        _api("/player/seek", {"seconds": 3})
        page.wait_for_timeout(1500)
        first = _api("/player/status")["position"]
        check("Sprungtests starten auf Position 1", first == 1, str(first))
        page.click("#btn-next")
        second = _wait_for(
            lambda: (lambda s: s["position"] if s["position"] != first else None)(
                _api("/player/status")
            ),
            20,
        )
        check("Next funktioniert", second is not None, f"{first} -> {second}")
        page.click("#btn-prev")
        back = _wait_for(
            lambda: (lambda s: s["position"] if s["position"] == first else None)(
                _api("/player/status")
            ),
            20,
        )
        check("Previous funktioniert", back is not None, f"{first} -> {second} -> {back}")

        # -- Modi ------------------------------------------------------
        page.click("#btn-shuffle")
        check("Shuffle schaltet", _wait_for(lambda: _api("/player/status")["shuffle"] is True, 12))
        page.click("#btn-shuffle")
        check("Shuffle aus", _wait_for(lambda: _api("/player/status")["shuffle"] is False, 12))

        # The repeat button cycles through aus -> alle -> einen -> aus.
        page.click("#btn-repeat")
        check(
            "Repeat: alle Titel",
            _wait_for(
                lambda: (
                    _api("/player/status")["repeat"] is True
                    and _api("/player/status")["repeat_one"] is False
                ),
                12,
            ),
            json.dumps({k: _api("/player/status")[k] for k in ("repeat", "repeat_one")}),
        )
        page.click("#btn-repeat")
        check(
            "Repeat: ein Titel",
            _wait_for(lambda: _api("/player/status")["repeat_one"] is True, 12),
            json.dumps({k: _api("/player/status")[k] for k in ("repeat", "repeat_one")}),
        )
        check(
            "Repeat-Button zeigt Modus one",
            _wait_for(lambda: page.get_attribute("#btn-repeat", "data-mode") == "one", 10),
            page.get_attribute("#btn-repeat", "data-mode"),
        )
        page.click("#btn-repeat")
        check(
            "Repeat aus",
            _wait_for(
                lambda: (
                    _api("/player/status")["repeat"] is False
                    and _api("/player/status")["repeat_one"] is False
                ),
                12,
            ),
        )

        # -- Queue-Ansicht ---------------------------------------------
        print("Queue-Ansicht …")
        page.click('.nav__item[data-view="queue"]')
        page.wait_for_timeout(900)
        qrows = page.locator("#view-queue .track")
        check("Queue wird angezeigt", qrows.count() >= 3, f"{qrows.count()} Einträge")
        # Ensure something is loaded so "currently playing" is meaningful.
        ensure_playing(1)
        check(
            "Aktueller Titel markiert",
            _wait_for(lambda: page.locator("#view-queue .track.is-current").count() == 1, 12),
            f"pos={_api('/player/status')['position']} "
            f"markiert={page.locator('#view-queue .track.is-current').count()}",
        )

        # Umsortieren: erster Eintrag nach unten.
        order_before = [i["video_id"] for i in _api("/queue")["items"]]
        check("Queue hat mindestens zwei Einträge", len(order_before) >= 2, str(len(order_before)))
        qrows.nth(0).hover()
        qrows.nth(0).locator('[aria-label="Nach unten"]').click()
        expected = [order_before[1], order_before[0], *order_before[2:]]
        check(
            "Queue umsortierbar",
            _wait_for(lambda: [i["video_id"] for i in _api("/queue")["items"]] == expected, 15),
            f"{order_before} -> {[i['video_id'] for i in _api('/queue')['items']]}",
        )

        # Entfernen
        count_before = len(_api("/queue")["items"])
        page.locator("#view-queue .track").nth(2).hover()
        page.locator("#view-queue .track").nth(2).locator(
            '[aria-label="Aus der Queue entfernen"]'
        ).click()
        check(
            "Queue-Eintrag entfernbar",
            _wait_for(lambda: len(_api("/queue")["items"]) == count_before - 1, 15),
        )
        if args.shots:
            page.screenshot(path=f"{args.shots}/04-queue.png", full_page=True)

        # -- Queue leeren (mit Rückfrage) -------------------------------
        page.click('#view-queue .btn--danger, #view-queue .btn--ghost:has-text("Queue leeren")')
        page.wait_for_timeout(500)
        check("Rückfragedialog erscheint", page.is_visible("#confirm-sheet"))
        page.click("#confirm-ok")
        check("Queue geleert", _wait_for(lambda: len(_api("/queue")["items"]) == 0, 20))
        check(
            "Leere Queue zeigt Hinweis",
            _wait_for(lambda: page.locator("#view-queue .state").count() == 1, 12),
            str(page.locator("#view-queue .state").count()),
        )
        page.click('#view-queue .btn--primary:has-text("Zur Suche")')
        page.wait_for_timeout(700)
        check("Navigation aus leerer Queue heraus", page.is_visible("#view-search"))

        # -- Favoriten --------------------------------------------------
        print("Favoriten …")
        _api(
            "/queue/add",
            {
                "ids": [_api("/history")["items"][0]["track"]["id"]]
                if _api("/history")["items"]
                else []
            },
        )
        page.click('.nav__item[data-view="search"]')
        page.wait_for_timeout(800)
        fav_id = page.locator("#view-search .track").nth(0).get_attribute("data-id")
        check("Suchtreffer hat eine Video-ID", bool(fav_id), str(fav_id))
        fav_button = page.locator("#view-search .track").nth(0).locator('[data-action="favorite"]')
        check(
            "Favorit startet aus",
            fav_button.get_attribute("aria-pressed") == "false",
            fav_button.get_attribute("aria-pressed"),
        )
        fav_button.scroll_into_view_if_needed()
        page.locator("#view-search .track").nth(0).hover()
        fav_button.click()
        check(
            "Favorit sofort sichtbar (optimistisch)",
            _wait_for(lambda: fav_button.get_attribute("aria-pressed") == "true", 10),
            f"aria-pressed={fav_button.get_attribute('aria-pressed')} "
            f"toast={page.locator('.toast__title').all_text_contents()}",
        )
        favs = _wait_for(lambda: fav_id in _api("/favorites")["ids"], 30)
        check("Favorit serverseitig gespeichert", favs, str(_api("/favorites")["ids"]))
        page.click('.nav__item[data-view="favorites"]')
        page.wait_for_timeout(900)
        check(
            "Favoritenansicht zeigt Eintrag",
            _wait_for(lambda: page.locator("#view-favorites .track").count() >= 1, 12),
            str(page.locator("#view-favorites .track").count()),
        )
        page.locator("#view-favorites .track").nth(0).hover()
        page.locator("#view-favorites .track").nth(0).locator('[data-action="favorite"]').click()
        check(
            "Favorit entfernbar",
            _wait_for(lambda: fav_id not in _api("/favorites")["ids"], 25),
            str(_api("/favorites")["ids"]),
        )
        if args.shots:
            page.screenshot(path=f"{args.shots}/05-favoriten.png", full_page=True)

        # -- Verlauf ----------------------------------------------------
        page.click('.nav__item[data-view="history"]')
        page.wait_for_timeout(900)
        check(
            "Verlauf gefüllt",
            page.locator("#view-history .track").count() >= 1,
            str(page.locator("#view-history .track").count()),
        )

        # -- Tastaturkürzel ---------------------------------------------
        print("Tastatur …")
        ensure_playing(1)
        # The shortcut acts on the state the page shows, so wait until the live
        # update has arrived - otherwise a stale "paused" would send /play.
        check(
            "Oberfläche zeigt laufende Wiedergabe",
            _wait_for(lambda: page.get_attribute("#btn-play", "data-state") == "playing", 12),
            page.get_attribute("#btn-play", "data-state"),
        )
        page.evaluate("() => document.activeElement && document.activeElement.blur()")
        page.wait_for_timeout(300)
        page.keyboard.press("Space")
        check(
            "Leertaste pausiert",
            _wait_for(lambda: _api("/player/status")["state"] == "paused", 15),
            str(_api("/player/status")["state"]),
        )
        page.keyboard.press("Space")
        check(
            "Leertaste spielt",
            _wait_for(lambda: _api("/player/status")["state"] == "playing", 15),
            str(_api("/player/status")["state"]),
        )
        seek_before = _api("/player/status")["elapsed"]
        page.keyboard.press("ArrowRight")
        check(
            "Pfeil rechts springt",
            _wait_for(lambda: _api("/player/status")["elapsed"] > seek_before + 2, 12),
            f"{seek_before} -> {_api('/player/status')['elapsed']}",
        )
        page.keyboard.press("/")
        page.wait_for_timeout(500)
        check(
            "Slash fokussiert Suche",
            page.evaluate("document.activeElement && document.activeElement.id") == "search-input",
        )
        page.keyboard.press("Escape")

        # -- Reload -----------------------------------------------------
        print("Reload …")
        page.reload(wait_until="domcontentloaded")
        page.wait_for_selector('#connection[data-state="live"]', timeout=25_000)
        page.wait_for_timeout(1500)
        check(
            "Zustand nach Reload erhalten",
            page.text_content("#now-title").strip() != "Nichts wird abgespielt",
            page.text_content("#now-title"),
        )
        check("Live-Status nach Reload", page.get_attribute("#connection", "data-state") == "live")
        check(
            "Ansicht bleibt erhalten",
            page.is_visible("#view-queue") or page.is_visible("#view-search"),
        )

        # -- Mobile ------------------------------------------------------
        print("Mobile …")
        mobile = browser.new_page(
            viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True
        )
        mobile.goto(BASE_URL, wait_until="domcontentloaded")
        mobile.wait_for_timeout(2500)
        check("Mobile: Menü-Button sichtbar", mobile.is_visible("#nav-toggle"))
        drawer_box = mobile.locator("#sidebar").bounding_box()
        check(
            "Mobile: Sidebar eingeklappt",
            drawer_box is None or drawer_box["x"] + drawer_box["width"] <= 1,
            str(drawer_box),
        )
        check(
            "Mobile: kein horizontaler Overflow",
            mobile.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1"),
            str(mobile.evaluate("document.documentElement.scrollWidth")),
        )
        check("Mobile: Transport-Buttons sichtbar", mobile.is_visible("#btn-play"))

        mobile.click("#nav-toggle")
        mobile.wait_for_timeout(700)
        check("Mobile: Sidebar öffnet", mobile.is_visible("#sidebar .nav__item"))
        if args.shots:
            mobile.screenshot(path=f"{args.shots}/06-mobile.png", full_page=True)
        mobile.click('.nav__item[data-view="queue"]')
        mobile.wait_for_timeout(1000)
        check(
            "Mobile: Navigation schliesst",
            _wait_for(
                lambda: (mobile.locator("#sidebar").bounding_box() or {"x": 0})["x"] + 200 <= 1, 8
            ),
        )

        mobile_row = mobile.locator("#view-queue .track").nth(0)
        check(
            "Mobile: Titel bleibt lesbar",
            len((mobile_row.locator(".track__title").text_content() or "").strip()) > 6,
            repr(mobile_row.locator(".track__title").text_content()),
        )
        check(
            "Mobile: Aktionen passen in eine Zeile",
            mobile.evaluate(
                "() => { const r = document.querySelector('#view-queue .track');"
                " return r ? r.scrollWidth <= r.clientWidth + 1 : false; }"
            ),
            str(mobile.locator("#view-queue .track").nth(0).bounding_box()),
        )
        mobile_row.locator('[data-action="menu"]').click()
        check(
            "Mobile: Kontextmenü bündelt Aktionen",
            _wait_for(lambda: mobile.locator(".menu__item").count() >= 4, 8),
            str(mobile.locator(".menu__item").all_text_contents()),
        )
        if args.shots:
            mobile.screenshot(path=f"{args.shots}/07-mobile-queue.png", full_page=True)
        mobile.keyboard.press("Escape")
        mobile.close()

        # -- Fehlerbehandlung --------------------------------------------
        print("Fehlerbehandlung …")
        page.goto(f"{BASE_URL}/#/search", wait_until="domcontentloaded")
        page.wait_for_timeout(1500)
        page.evaluate(
            """() => {
                const original = window.fetch;
                window.fetch = (url, init) => {
                    if (String(url).includes('/search?')) {
                        return Promise.resolve(new Response(
                            JSON.stringify({error: 'ValidationError', code: 'VALIDATION_ERROR',
                                            message: 'Titel konnte nicht geladen werden.'}),
                            {status: 500, headers: {'Content-Type': 'application/json'}}));
                    }
                    return original(url, init);
                };
            }"""
        )
        page.fill("#search-input", "fehlerfall")
        page.press("#search-input", "Enter")
        page.wait_for_timeout(2500)
        body = page.text_content("#view-search") or ""
        check(
            "Fehler wird verständlich angezeigt",
            "Titel konnte nicht geladen werden." in body,
            body[:120],
        )
        check("Kein Traceback im UI", "Traceback" not in (page.content() or ""))

        # Server komplett weg: die Oberfläche darf nicht einfrieren.
        page.evaluate("() => { window.__origFetch = window.fetch; }")
        page.reload(wait_until="domcontentloaded")
        page.wait_for_timeout(2000)
        check(
            "Oberfläche lädt auch nach Fehler neu",
            page.is_visible("#player") and page.is_visible("#sidebar"),
        )

        real_errors = [
            e
            for e in console_errors
            if "favicon" not in e.lower()
            and "400 (Bad Request)" not in e
            and "404 (Not Found)" not in e
        ]
        check("Keine JavaScript- oder Serverfehler", not real_errors, "; ".join(real_errors[:4]))
        check("Keine unbehandelten Clientfehler", not client_errors, "; ".join(client_errors[:4]))

        browser.close()

    print()
    if failures:
        print(f"{len(failures)} Prüfung(en) fehlgeschlagen:")
        for item in failures:
            print(f"  - {item}")
        return 1
    print("Alle Browser-Prüfungen bestanden.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
