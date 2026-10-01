#!/usr/bin/env bash
# Drive the StreamBridge app on a connected device, for end-to-end testing.
#
# This is a *test harness*, not a UI test framework: it taps coordinates and
# reads the view hierarchy, which is enough to walk the app through the flow
# the README documents and to assert on what the server ends up saying.
#
#   scripts/android-e2e.sh <serial> [base-url] [token]
#
# It assumes a reachable streambridge-server and a booted device or emulator
# with the app installed. Every step prints what it did, so a failure says
# which step failed rather than just "assertion failed".
set -uo pipefail

ADB="${ADB:-$HOME/Android/Sdk/platform-tools/adb}"
SERIAL="${1:?usage: android-e2e.sh <serial> [host] [port] [token]}"
HOST="${2:-10.0.2.2}"
PORT="${3:-8787}"
TOKEN="${4:-}"

PKG="app.streambridge"
ACTIVITY="$PKG/ui.MainActivity"

pass=0
fail=0

info()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
ok()    { printf '\033[1;32m  ✓\033[0m %s\n' "$*"; pass=$((pass + 1)); }
bad()   { printf '\033[1;31m  ✗\033[0m %s\n' "$*"; fail=$((fail + 1)); }
die()   { printf '\033[1;31mFEHLER\033[0m %s\n' "$*" >&2; exit 1; }

a() { "$ADB" -s "$SERIAL" "$@"; }
shell() { a shell "$@"; }

# --- device plumbing --------------------------------------------------------

# Dump the view hierarchy once and answer questions about it, because walking
# 300 nodes with adb calls is slow and this runs dozens of times.
dump() {
  a shell uiautomator dump /sdcard/ui.xml >/dev/null 2>&1
  a shell cat /sdcard/ui.xml 2>/dev/null
}

# Find the centre of the first node whose text, content-description or
# resource-id matches a pattern. Prints "x y", or nothing if there is no match.
find_node() {
  local pattern="$1" attribute="${2:-text}"
  dump | python3 -c "
import re, sys
pattern = re.compile(sys.argv[1])
attribute = sys.argv[2]
data = sys.stdin.read()
for match in re.finditer(r'<node ([^>]*?)/?>', data):
    attrs = dict(re.findall(r'([a-z-]+)=\"([^\"]*)\"', match.group(1)))
    value = attrs.get(attribute, '')
    if value and pattern.search(value):
        x1, y1, x2, y2 = (int(attrs.get(k, 0)) for k in ('x', 'y', 'width', 'height'))
        if 'bounds' in attrs:
            l, t, r, b = map(int, re.match(r'\[(\d+),(\d+)\]\[(\d+),(\d+)\]', attrs['bounds']).groups())
            print((l + r) // 2, (t + b) // 2)
        else:
            print(x1 + x2 // 2, y1 + y2 // 2)
        break
" "$pattern" "$attribute"
}

has_node() {
  local pattern="$1" attribute="${2:-text}"
  [ -n "$(find_node "$pattern" "$attribute")" ]
}

wait_for_node() {
  local pattern="$1" attribute="${2:-text}" attempts="${3:-20}"
  for _ in $(seq 1 "$attempts"); do
    if has_node "$pattern" "$attribute"; then return 0; fi
    sleep 1
  done
  return 1
}

tap_node() {
  local pattern="$1" attribute="${2:-text}"
  local point
  point="$(find_node "$pattern" "$attribute")" || return 1
  [ -n "$point" ] || return 1
  # shellcheck disable=SC2086
  a shell input tap $point >/dev/null
  sleep 1
}

tap_at() { a shell input tap "$1" "$2" >/dev/null; sleep 1; }

type_text() {
  # adb input text cannot carry spaces or most punctuation; %s is a space and
  # the rest has to be escaped, so quote the whole thing for the device shell.
  local escaped
  escaped="$(printf '%s' "$1" | sed -e 's/ /%s/g' -e "s/&/\\&/g" -e "s/'/\\\\'/g" -e 's/(/\\(/g' -e 's/)/\\)/g' -e 's/</\\</g' -e 's/>/\\>/g' -e 's/;/\\;/g' -e 's/&/\\&/g')"
  a shell input text "$escaped" >/dev/null
  sleep 1
}

clear_field() {
  # Select-all then delete: the reliable way to empty a focused text field.
  a shell input keyevent KEYCODE_MOVE_END >/dev/null
  for _ in $(seq 1 40); do a shell input keyevent KEYCODE_DEL >/dev/null; done
}

screenshot() { a exec-out screencap -p > "$1" 2>/dev/null; }

# --- the flow ---------------------------------------------------------------

info "Gerät: $SERIAL   Server: $HOST:$PORT"

info "1. App starten"
a shell am force-stop "$PKG" >/dev/null 2>&1
a shell am start -n "$PKG/$ACTIVITY" >/dev/null 2>&1
if wait_for_node "Server-Adresse" "" 25; then
  ok "Verbindungsbildschirm erscheint"
else
  bad "Verbindungsbildschirm erscheint nicht"
  screenshot /tmp/e2e-start.png
  exit 1
fi

info "2. Serveradresse eingeben"
# The host field is the first EditText on the screen.
a shell input tap 540 900 >/dev/null
sleep 1
clear_field
type_text "$HOST"
sleep 1
if dump | grep -q "text=\"$HOST\""; then
  ok "Adresse eingetragen: $HOST"
else
  bad "Adresse konnte nicht eingetragen werden"
  screenshot /tmp/e2e-host.png
fi

info "3. Port eingeben"
if tap_node "Port" ""; then
  clear_field
  type_text "$PORT"
  ok "Port eingetragen: $PORT"
else
  bad "Portfeld nicht gefunden"
fi

if [ -n "$TOKEN" ]; then
  info "4. Zugriffstoken eingeben"
  if tap_node "Zugriffstoken" ""; then
    clear_field
    type_text "$TOKEN"
    ok "Token eingetragen"
  else
    bad "Tokenfeld nicht gefunden"
  fi
fi

info "5. Verbinden"
tap_node "Verbinden" "" || bad "Verbinden-Knopf nicht gefunden"
sleep 3
if wait_for_node "Nichts läuft|Home" "" 25; then
  ok " verbunden - Startbildschirm sichtbar"
else
  bad "Verbindung nicht hergestellt"
  screenshot /tmp/e2e-connect-failed.png
  dump > /tmp/e2e-connect-failed.xml
fi

info "6. Suche"
if tap_node "Suche" "" ; then
  sleep 1
  if wait_for_node "Musik suchen" "" 10; then
    ok "Suchansicht geöffnet"
  else
    bad "Suchansicht nicht erreicht"
  fi
  a shell input tap 540 400 >/dev/null
  sleep 1
  type_text "big buck bunny"
  a shell input keyevent KEYCODE_ENTER >/dev/null
  sleep 1
  a shell input keyevent KEYCODE_ENTER >/dev/null
  # The search runs the extractor on the server, which is the slow part.
  if wait_for_node "Treffer" "" 45; then
    ok "Suche liefert Treffer"
  else
    bad "keine Treffer angekommen"
    screenshot /tmp/e2e-search.png
    dump > /tmp/e2e-search.xml
  fi
else
  bad "Navigation zu 'Suche' nicht möglich"
fi

summary() {
  printf '\n'
  if [ "$fail" -eq 0 ]; then
    printf '\033[1;32mALLE %d PRÜFUNGEN BESTANDEN\033[0m\n' "$pass"
  else
    printf '\033[1;31m%d von %d Prüfungen fehlgeschlagen\033[0m\n' "$fail" "$((pass + fail))"
  fi
  return $((fail > 0))
}

summary
