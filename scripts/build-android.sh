#!/usr/bin/env bash
# Build the StreamBridge Android app.
#
#   scripts/build-android.sh              debug and release
#   scripts/build-android.sh debug        debug only
#   scripts/build-android.sh release      release only
#   scripts/build-android.sh test         unit tests
#   scripts/build-android.sh clean        remove build output first
#
# The output lands in android/app/build/outputs/apk/<flavour>/ and is also
# copied to android/dist/ with a stable name, because "which of these eleven
# APKs is the one" is a question nobody should have to answer.
#
# Release signing
# ---------------
# A keystore and its passwords must never enter this repository, so none is
# committed. On the first release build this script creates one *outside* the
# tree, in:
#
#   ~/.streambridge/android-release.jks       the key
#   ~/.gradle/streambridge-release.properties the passwords
#
# app/build.gradle.kts reads the second file. Keep both: the same key has to
# sign every future build, or Android will refuse to install an update over an
# already-installed app. Back them up somewhere safe, and nowhere that syncs.
#
# To use a key you already have, put your own four lines in that properties
# file instead and this script will use them:
#
#   storeFile=/absolute/path/to/keystore.jks
#   storePassword=...
#   keyAlias=...
#   keyPassword=...
set -euo pipefail

readonly ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
readonly ANDROID_DIR="$ROOT/android"
readonly DIST="$ANDROID_DIR/dist"
readonly KEYSTORE="${STREAMBRIDGE_KEYSTORE:-$HOME/.streambridge/android-release.jks}"
readonly PROPERTIES="${STREAMBRIDGE_SIGNING_PROPERTIES:-$HOME/.gradle/streambridge-release.properties}"

# --- output ------------------------------------------------------------------

info()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn()  { printf '\033[1;33mWARN\033[0m %s\n' "$*" >&2; }
die()   { printf '\033[1;31mFEHLER\033[0m %s\n' "$*" >&2; exit 1; }

# The client is a separate repository, so a fresh clone of this one does not
# have it. Say where it comes from and how to get it, rather than letting the
# first `cd android` fail with "no such file or directory".
require_android_dir() {
  [ -d "$ANDROID_DIR" ] && return 0
  printf '\033[1;31mFEHLER\033[0m %s\n' "The Android client is not in this tree." >&2
  printf '\n' >&2
  printf '  It is maintained separately, because it is a Gradle project with its\n' >&2
  printf '  own dependency set and a 175 MB build tree. Check it out next to\n' >&2
  printf '  this repository:\n\n' >&2
  printf '    git clone https://github.com/Grenoullie91/streambridge-android.git \\\n' >&2
  printf '      "$(dirname "$ROOT")/streambridge-android"\n\n' >&2
  printf '  Then re-run this script. See docs/android.md.\n' >&2
  exit 1
}
require_android_dir

# --- toolchain ---------------------------------------------------------------

# The Android SDK. Checked in this order because that is how a machine ends up
# with more than one: the explicit environment variable, the project's
# untracked local.properties, then the conventional home directory.
find_sdk() {
  local candidate
  if [ -n "${ANDROID_HOME:-}" ] && [ -d "${ANDROID_HOME}" ]; then
    printf '%s' "$ANDROID_HOME"
    return 0
  fi
  if [ -n "${ANDROID_SDK_ROOT:-}" ] && [ -d "${ANDROID_SDK_ROOT}" ]; then
    printf '%s' "$ANDROID_SDK_ROOT"
    return 0
  fi
  if [ -f "$ANDROID_DIR/local.properties" ]; then
    candidate="$(sed -n 's/^sdk\.dir=//p' "$ANDROID_DIR/local.properties" | head -1)"
    if [ -n "$candidate" ] && [ -d "$candidate" ]; then
      printf '%s' "$candidate"
      return 0
    fi
  fi
  for candidate in "$HOME/Android/Sdk" "$HOME/android-sdk" /usr/lib/android-sdk; do
    if [ -d "$candidate" ]; then
      printf '%s' "$candidate"
      return 0
    fi
  done
  return 1
}

# A JDK with a compiler. Gradle needs javac, and a JRE-only install fails with
# a message about toolchains that does not say "install a JDK".
find_java_home() {
  local candidate
  if [ -n "${JAVA_HOME:-}" ] && [ -x "${JAVA_HOME}/bin/javac" ]; then
    printf '%s' "$JAVA_HOME"
    return 0
  fi
  # Every candidate gets a trailing slash, so the loop body can be written
  # once as "${candidate}bin/javac" instead of remembering which entries were
  # written with a slash and which were not. That mistake is silent and cost
  # an afternoon.
  for candidate in "$HOME/tools/jdk21/" "$HOME/tools/jdk/"*/ "$HOME/.jdks/"*/ /usr/lib/jvm/*/; do
    if [ -x "${candidate}bin/javac" ]; then
      printf '%s' "${candidate%/}"
      return 0
    fi
  done
  if command -v javac >/dev/null 2>&1; then
    javac_home="$(dirname "$(dirname "$(readlink -f "$(command -v javac)")")")"
    printf '%s' "$javac_home"
    return 0
  fi
  return 1
}

prepare_toolchain() {
  SDK="$(find_sdk)" || die "Kein Android SDK gefunden. Setze ANDROID_HOME oder lege eines unter ~/Android/Sdk ab."
  export ANDROID_HOME="$SDK"
  export ANDROID_SDK_ROOT="$SDK"

  JAVA_HOME="$(find_java_home)" || die "Kein JDK mit javac gefunden. Installiere ein JDK 17 oder neuer."
  export JAVA_HOME

  info "SDK:  $ANDROID_HOME"
  info "JDK:  $JAVA_HOME ($("$JAVA_HOME/bin/java" -version 2>&1 | head -1))"

  # The wrapper is what pins the Gradle version, so everything below goes
  # through ./gradlew even when a gradle happens to be on the PATH.
  [ -x "$ANDROID_DIR/gradlew" ] || chmod +x "$ANDROID_DIR/gradlew"
  if [ ! -f "$ANDROID_DIR/local.properties" ]; then
    # Untracked by design: it names a directory on this machine.
    printf 'sdk.dir=%s\n' "$ANDROID_HOME" > "$ANDROID_DIR/local.properties"
  fi
}

# --- release signing ---------------------------------------------------------

random_secret() {
  # Base64 without the characters that are awkward in a properties file.
  head -c 48 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 32
}

ensure_signing() {
  if [ -f "$PROPERTIES" ]; then
    info "Signierschlüssel: $PROPERTIES (vorhanden)"
    return 0
  fi
  info "Kein Release-Schlüssel gefunden - erzeuge einen ausserhalb des Repos."
  warn "Store: $KEYSTORE"
  warn "Passwörter: $PROPERTIES"
  warn "Beides sichern! Ohne denselben Schlüssel lässt sich kein Update über eine"
  warn "bereits installierte App installieren."

  local store_password key_password
  store_password="$(random_secret)"
  key_password="$store_password"

  mkdir -p "$(dirname "$KEYSTORE")" "$(dirname "$PROPERTIES")"
  chmod 700 "$(dirname "$KEYSTORE")" "$(dirname "$PROPERTIES")"

  "$JAVA_HOME/bin/keytool" -genkeypair -v \
    -keystore "$KEYSTORE" \
    -storetype PKCS12 \
    -alias streambridge \
    -keyalg RSA -keysize 4096 -validity 10950 \
    -storepass "$store_password" \
    -dname "CN=StreamBridge, O=StreamBridge, C=DE" \
    >/dev/null 2>&1 || die "keytool ist fehlgeschlagen"

  cat > "$PROPERTIES" <<EOF
# Written by scripts/build-android.sh. Never commit this file.
storeFile=$KEYSTORE
storePassword=$store_password
keyAlias=streambridge
keyPassword=$key_password
EOF
  chmod 600 "$PROPERTIES"
  info "Release-Schlüssel erzeugt."
}

# --- packaging ---------------------------------------------------------------

publish() {
  local flavour="$1" pattern="$2"
  local source
  source="$(find "$ANDROID_DIR/app/build/outputs/apk/$flavour" -name "$pattern" -print -quit 2>/dev/null || true)"
  [ -n "$source" ] || return 0
  mkdir -p "$DIST"
  cp "$source" "$DIST/$(basename "$source")"
  local size
  size="$(du -h "$DIST/$(basename "$source")" | cut -f1)"
  info "$flavour: $DIST/$(basename "$source") ($size)"
}

# --- main --------------------------------------------------------------------

flavours="${*:-debug release}"
case "$flavours" in
  clean)
    info "Räume Build-Ausgabe"
    (cd "$ANDROID_DIR" && ./gradlew clean --console=plain -q)
    rm -rf "$DIST"
    exit 0
    ;;
  test)
    prepare_toolchain
    (cd "$ANDROID_DIR" && ./gradlew testDebugUnitTest lintDebug --console=plain)
    exit 0
    ;;
esac

# One Gradle task per requested flavour. Built as a list rather than
# word-splitting "$flavours", so the result is a proper argument list instead
# of one long task name.
tasks=()
for flavour in $flavours; do
  case "$flavour" in
    debug)   tasks+=("assembleDebug") ;;
    release) tasks+=("assembleRelease") ;;
    test)    tasks+=("testDebugUnitTest") ;;
    lint)    tasks+=("lintDebug") ;;
    *) die "Unbekanntes Ziel: $flavour (debug, release, test, lint, clean)" ;;
  esac
done

prepare_toolchain

case "$flavours" in
  *release*) ensure_signing ;;
esac

(cd "$ANDROID_DIR" && ./gradlew "${tasks[@]}" --console=plain)

# Two separate case statements, not one with two branches: a shell `case`
# stops at the first pattern that matches, so `*debug*) ;; *release*)` would
# publish the debug APK and silently skip the release one whenever both were
# asked for - which is the default invocation.
case "$flavours" in
  *debug*) publish debug "app-debug.apk" ;;
esac
case "$flavours" in
  *release*) publish release "app-release.apk" ;;
esac

info "Fertig."
