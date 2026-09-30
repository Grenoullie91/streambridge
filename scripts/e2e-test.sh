#!/usr/bin/env bash
# ===================================================================
#  StreamBridge :: full end-to-end test
#
#  Exercises the complete path:
#    search -> streambridge-server -> MPD -> mpc -> ncmpcpp -> audio
#
#  Every step prints PASS/FAIL. At the end the audio flow is proven
#  measurably, by comparing a recording made during playback against one
#  made while stopped.
#
#  Requires: streambridge installed, the user service running, MPD
#  running, and pactl/parec available (PipeWire or PulseAudio).
# ===================================================================
set -uo pipefail

export PATH="$HOME/.local/bin:$PATH"

BASE="${STREAMBRIDGE_BASE:-http://127.0.0.1:8787}"
# Search terms are arguments, not hardcoded, so the script works offline
# against a fixture directory as well as live.
QUERY="${STREAMBRIDGE_QUERY:-big buck bunny}"
SERVICE="${STREAMBRIDGE_SERVICE:-streambridge}"
WORKDIR="${TMPDIR:-/tmp}/streambridge-e2e-$$"

# Point the CLI at a non-default port by setting STREAMBRIDGE_PORT, so
# STREAMBRIDGE_BASE and the client cannot disagree about where the service is.
if [ -n "${STREAMBRIDGE_BASE:-}" ]; then
  export STREAMBRIDGE_PORT="${STREAMBRIDGE_BASE##*:}"
fi

mkdir -p "$WORKDIR"
# shellcheck disable=SC2064  # expand $WORKDIR now, not at trap time
trap "rm -rf '$WORKDIR'" EXIT

# Put MPD into a known state. Random, single and consume change what plays
# next, so a leftover mode from ordinary use makes the results unreproducible.
mpc random off >/dev/null 2>&1
mpc repeat off >/dev/null 2>&1
mpc single off  >/dev/null 2>&1
mpc consume off >/dev/null 2>&1
mpc stop       >/dev/null 2>&1

PASS=0
FAIL=0
declare -a RESULTS

step() {
  STEP_NR="$1"; STEP_TITLE="$2"
  printf '\n\033[1;36m-- %s. %s\033[0m\n' "$STEP_NR" "$STEP_TITLE"
}
ok() {
  printf '   \033[1;32mPASS\033[0m  %s\n' "$1"
  RESULTS+=("$STEP_NR $STEP_TITLE: PASS - $1"); PASS=$((PASS+1))
}
bad() {
  printf '   \033[1;31mFAIL\033[0m  %s\n' "$1"
  RESULTS+=("$STEP_NR $STEP_TITLE: FAIL - $1"); FAIL=$((FAIL+1))
}
note() { printf '   \033[2m%s\033[0m\n' "$1"; }

# Count "resolved <id>" lines the server has logged.
# Reads the systemd journal when the service exists, and a log file when the
# server was started in the foreground with output redirected.
count_resolutions() {
  local logfile="${STREAMBRIDGE_LOGFILE:-}"
  if [ -n "$logfile" ] && [ -r "$logfile" ]; then
    grep -c 'resolved [A-Za-z0-9_-]\{11\}' "$logfile" 2>/dev/null || true
  elif systemctl --user cat "$SERVICE" >/dev/null 2>&1; then
    journalctl --user -u "$SERVICE" --no-pager 2>/dev/null \
      | grep -c 'resolved [A-Za-z0-9_-]\{11\}' || true
  else
    echo 0
  fi
}

# Root mean square of a 16-bit stereo capture. Silence scores near 0;
# real audio scores in the hundreds or thousands.
rms() {
  python3 - "$1" <<'PY'
import array, math, sys
try:
    samples = array.array("h")
    samples.frombytes(open(sys.argv[1], "rb").read())
except Exception:
    print("0"); raise SystemExit
if len(samples) % 2:
    samples = samples[:-1]
n = len(samples) // 2
if not n:
    print("0"); raise SystemExit
total = sum((samples[2*i]**2 + samples[2*i+1]**2) / 2 for i in range(n))
print(f"{math.sqrt(total / n):.1f}")
PY
}

# 1 ------------------------------------------------------------------
step 1 "server is reachable"
# How the server was started is not what is being tested; whether it answers
# is. A foreground start and a systemd service are equally valid here.
if systemctl --user is-active --quiet "$SERVICE" 2>/dev/null; then
  ok "service $SERVICE is active"
elif curl -fsS --max-time 10 "$BASE/health" >/dev/null 2>&1; then
  ok "reachable at $BASE (not managed by systemd as $SERVICE)"
else
  bad "not reachable at $BASE; start it with: systemctl --user start $SERVICE"
fi

# 2 ------------------------------------------------------------------
step 2 "/health"
HEALTH=$(curl -fsS --max-time 10 "$BASE/health" 2>/dev/null)
HSTAT=$(printf '%s' "$HEALTH" | python3 -c 'import json,sys; print(json.load(sys.stdin)["status"])' 2>/dev/null)
HEXT=$(printf '%s' "$HEALTH" | python3 -c 'import json,sys; print(json.load(sys.stdin)["extractor"])' 2>/dev/null)
if [ "$HSTAT" = "ok" ]; then
  ok "status=$HSTAT extractor=$HEXT"
else
  bad "unparseable response: ${HEALTH:0:80}"
fi

# 3 ------------------------------------------------------------------
step 3 "search returns ids"
SEARCH=$(timeout 150 streambridge search "$QUERY" --limit 3 --json 2>/dev/null)
IDS=$(printf '%s' "$SEARCH" | python3 -c \
  'import json,sys; print(" ".join(r["id"] for r in json.load(sys.stdin)["results"]))' 2>/dev/null)
# ncmpcpp shows the display name, not the id, so keep the first title too.
FIRST_TITLE=$(printf '%s' "$SEARCH" | python3 -c \
  'import json,sys; print(json.load(sys.stdin)["results"][0]["title"])' 2>/dev/null)
if [ -n "$IDS" ]; then
  ok "results: $IDS"
else
  bad "search returned nothing"
fi
FIRST=$(printf '%s' "$IDS" | awk '{print $1}')

# 4 ------------------------------------------------------------------
step 4 "select a result interactively"
mpc clear >/dev/null 2>&1; sleep 1
OUT=$(printf '1\n' | timeout 150 streambridge search "$QUERY" --limit 3 --play 2>&1 | tail -1)
# The CLI ends each line with "(playing)" or "(added to the queue)".
if printf '%s' "$OUT" | grep -qE '\((playing|added to the queue)\)'; then
  ok "selection processed"
else
  bad "$OUT"
fi
sleep 4

# 5 ------------------------------------------------------------------
step 5 "MPD queue entry"
QL=$(mpc playlist 2>/dev/null | head -1)
if [ -n "$QL" ]; then
  ok "queue: $QL"
else
  bad "queue is empty"
fi

# 6 ------------------------------------------------------------------
step 6 "MPD status"
ST=$(mpc status 2>/dev/null | sed -n 2p)
if printf '%s' "$ST" | grep -q "playing"; then
  ok "$ST"
else
  bad "$ST"
fi

# 7 ------------------------------------------------------------------
step 7 "audio: sink input registered"
# LC_ALL=C so the output is English: a localised pactl prints "Ziel-Eingabe"
# and the field name never matches, silently turning this into a false pass.
# The input appears a moment after playback starts, so retry briefly rather
# than report a race as a failure.
SINK=0
for _ in 1 2 3 4 5; do
  SINK=$(LC_ALL=C pactl list sink-inputs 2>/dev/null | grep -c 'media.name = "mpd"')
  [ "${SINK:-0}" -ge 1 ] && break
  sleep 1
done
if [ "${SINK:-0}" -ge 1 ]; then
  ok "MPD registered as a sink input"
else
  bad "no MPD sink input (is MPD actually playing?)"
fi

# 8 ------------------------------------------------------------------
step 8 "playback advances"
T1=$(mpc status 2>/dev/null | sed -n 2p | grep -oE '[0-9]+:[0-9]{2}/[0-9]+:[0-9]{2}')
sleep 6
T2=$(mpc status 2>/dev/null | sed -n 2p | grep -oE '[0-9]+:[0-9]{2}/[0-9]+:[0-9]{2}')
if [ "$T1" != "$T2" ] && [ -n "$T2" ]; then
  ok "time advances: $T1 -> $T2"
else
  bad "time is stuck: $T1 -> $T2"
fi

# 9 ------------------------------------------------------------------
step 9 "audio data is measurable"
# Record the default sink's monitor, not a sink input. A sink input exists only
# while MPD is playing, so capturing one before and after the stop compares two
# different devices - and an out-of-range index makes parec fall back to a
# source that has nothing to do with playback. The monitor is the same device
# in both halves of the comparison.
CAPTURE_DEVICE="${STREAMBRIDGE_CAPTURE_DEVICE:---device=@DEFAULT_SINK@.monitor}"
timeout 6 parec "$CAPTURE_DEVICE" --format=s16le --rate=44100 --channels=2 \
  > "$WORKDIR/play.raw" 2>/dev/null
RMS_PLAY=$(rms "$WORKDIR/play.raw")
if python3 -c "import sys; sys.exit(0 if float('${RMS_PLAY:-0}') > 10 else 1)"; then
  ok "RMS=${RMS_PLAY} of 32768 - real signal"
else
  bad "RMS=${RMS_PLAY} - no audio data"
fi

# 10 -----------------------------------------------------------------
step 10 "metadata reached MPD"
MET=$(python3 - <<'PY'
import socket
try:
    s = socket.create_connection(("127.0.0.1", 6600), timeout=5)
    f = s.makefile("rwb")
    f.readline()
    f.write(b"currentsong\n"); f.flush()
    out = []
    while True:
        line = f.readline().decode()
        if line.startswith("OK") or not line:
            break
        out.append(line.strip())
    name = next((x.split(": ", 1)[1] for x in out if x.startswith("Name:")), "")
    dur = next((x.split(": ", 1)[1] for x in out if x.startswith("Time:")), "")
    print(f"{name}|{dur}")
except Exception:
    print("|")
PY
)
NAME="${MET%%|*}"; DUR="${MET##*|}"
if [ ${#NAME} -gt 5 ] && printf '%s' "$NAME" | grep -q "http"; then
  # MPD only shows Name for stream entries, so a bare URL here means the
  # playlist load carried no metadata at all.
  bad "only a URL: $NAME"
elif [ ${#NAME} -gt 5 ]; then
  ok "Name='$NAME' Time=${DUR}s"
else
  bad "no metadata (check mpd.playlist_directory)"
fi

# 11 -----------------------------------------------------------------
step 11 "ncmpcpp sees queue and metadata"
# ncmpcpp reads its config from $HOME, so the probe runs in a scratch directory
# to prove the queue is real rather than a leftover file. The script path is
# resolved to an absolute one first, because the probe's cwd changes.
SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
NC=$(cd "$WORKDIR" && timeout 20 python3 "$SCRIPT_DIR/ncmpcpp-probe.py" 4 2>&1 | tr -s 'q' '\n')
if printf '%s' "$NC" | grep -qi "Connected to"; then
  # The queue line carries the item count, and the title appears in the
  # playlist view. Either proves the queue reached the player.
  if printf '%s' "$NC" | grep -qiE "[0-9]+ items?|\(1 item"; then
    ok "connected, queue visible"
  else
    bad "connected, but no item count in the playlist view"
  fi
else
  bad "ncmpcpp reports no connection"
fi
note "first result title: ${FIRST_TITLE:0:60}"

# 12 -----------------------------------------------------------------
step 12 "pause and resume"
mpc pause >/dev/null 2>&1; sleep 1
PA=$(mpc status 2>/dev/null | sed -n 2p)
mpc play  >/dev/null 2>&1; sleep 1
RE=$(mpc status 2>/dev/null | sed -n 2p)
if printf '%s' "$PA" | grep -q "paused" && printf '%s' "$RE" | grep -q "playing"; then
  ok "pause and resume work"
else
  bad "pause='$PA' resume='$RE'"
fi

# 13 -----------------------------------------------------------------
step 13 "next track"
timeout 60 streambridge add "$FIRST" >/dev/null 2>&1
BEFORE=$(mpc status 2>/dev/null | sed -n 2p | grep -oE '#[0-9]+/[0-9]+')
mpc next >/dev/null 2>&1; sleep 5
AFTER=$(mpc status 2>/dev/null | sed -n 2p | grep -oE '#[0-9]+/[0-9]+')
CUR=$(mpc current 2>/dev/null | head -1)
if [ "$BEFORE" != "$AFTER" ]; then
  ok "next: $BEFORE -> $AFTER, now: ${CUR:0:50}"
else
  bad "next had no effect ($BEFORE)"
fi

# 14 -----------------------------------------------------------------
step 14 "stream is re-resolved on demand"
# The resolver caches a source for 120s, so replaying the same id inside that
# window legitimately hits the cache. To exercise the uncached path the server
# must forget it: a restart when it runs as a service, otherwise a fresh id.
# A count that never grows is not a failure on its own - the cache is doing its
# job - so the log is only consulted after a restart actually happened.
mpc stop >/dev/null 2>&1; sleep 1
RESTARTED=0
if systemctl --user cat "$SERVICE" >/dev/null 2>&1; then
  systemctl --user restart "$SERVICE" >/dev/null 2>&1; sleep 5
  RESTARTED=1
fi
BEFORE_N=$(count_resolutions); BEFORE_N=${BEFORE_N:-0}
if [ "$RESTARTED" -eq 1 ]; then
  mpc play >/dev/null 2>&1; sleep 7
  AFTER_N=$(count_resolutions); AFTER_N=${AFTER_N:-0}
  if [ "$AFTER_N" -gt "$BEFORE_N" ]; then
    ok "re-resolved after restart ($BEFORE_N -> $AFTER_N)"
  else
    bad "no re-resolution logged after the restart ($BEFORE_N -> $AFTER_N)"
  fi
else
  # No service to restart, so resolve a genuinely new source instead. A
  # different query is used because the first one is already cached: a cached
  # id legitimately produces no new resolution, which would make the check
  # meaningless rather than failing.
  FRESH_ID=$(timeout 90 streambridge search "planet earth documentary" --limit 1 --json 2>/dev/null \
    | python3 -c 'import json,sys; r=json.load(sys.stdin)["results"]; print(r[0]["id"] if r else "")' 2>/dev/null)
  if [ -z "$FRESH_ID" ]; then
    note "no fresh id available; skipping"
  else
    BEFORE_N=$(count_resolutions); BEFORE_N=${BEFORE_N:-0}
    # Request the stream directly: MPD is irrelevant here, and going through
    # it would add buffering to the measurement.
    CODE=$(curl -s -o /dev/null -w '%{http_code}' --max-time 90 "$BASE/stream/$FRESH_ID" 2>/dev/null)
    AFTER_N=$(count_resolutions); AFTER_N=${AFTER_N:-0}
    if [ "$AFTER_N" -gt "$BEFORE_N" ] && { [ "$CODE" = "200" ] || [ "$CODE" = "302" ]; }; then
      ok "resolved a fresh id on demand ($FRESH_ID -> HTTP $CODE, $BEFORE_N -> $AFTER_N)"
    else
      bad "no on-demand resolution for $FRESH_ID (HTTP $CODE, $BEFORE_N -> $AFTER_N)"
    fi
  fi
fi

# 15 -----------------------------------------------------------------
step 15 "control test: silence while stopped"
# Same device as step 9, so the two readings are comparable. Only the state of
# MPD differs.
mpc stop >/dev/null 2>&1; sleep 2
timeout 6 parec "$CAPTURE_DEVICE" --format=s16le --rate=44100 --channels=2 \
  > "$WORKDIR/stop.raw" 2>/dev/null
RMS_STOP=$(rms "$WORKDIR/stop.raw")
# A single reading could be noise. The playback/silence comparison cannot be.
if python3 -c "
import sys
play, stop = float('${RMS_PLAY:-0}'), float('${RMS_STOP:-0}')
print(f'  delta: +{play - stop:.1f}')
sys.exit(0 if play > stop * 2 and play > 0 else 1)" 2>/dev/null; then
  ok "playback RMS=$RMS_PLAY vs silence RMS=$RMS_STOP - audio flows"
else
  bad "no measurable difference ($RMS_PLAY vs $RMS_STOP)"
fi

# 16 -----------------------------------------------------------------
step 16 "no zombie processes"
MPD_PID=$(pgrep -x mpd | head -1)
Z=$(ps -o stat= --ppid "$MPD_PID" 2>/dev/null | grep -c Z || true); Z=${Z:-0}
# pgrep -c prints "0" and exits 1; do not append another "|| echo 0".
YT=$(pgrep -cf "yt-dlp.*(ytsearch|watch\?v=)" 2>/dev/null || true); YT=${YT:-0}
if [ "$Z" -eq 0 ] && [ "$YT" -le 2 ]; then
  ok "no zombies, running extractors: $YT"
else
  bad "zombies=$Z extractors=$YT"
fi

# --------------------------------------------------------------------
printf '\n\033[1m=== SUMMARY ===\033[0m\n'
for r in "${RESULTS[@]}"; do printf '  %s\n' "$r"; done
printf '\n  passed: \033[1;32m%d\033[0m   failed: \033[1;31m%d\033[0m\n\n' "$PASS" "$FAIL"
exit $(( FAIL > 0 ? 1 : 0 ))
