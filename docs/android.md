# Android app

The Android app is a remote control and a front end for the same server the
browser talks to. It is a client in the full sense: it does not run `yt-dlp`,
does not scrape anything, and does not start a second MPD. Audio is decoded on
the machine running StreamBridge, exactly as it is for the web interface and
for ncmpcpp.

```
    phone                    machine
  +---------+            +-------------------+
  | app     |  HTTP/JSON | streambridge-server|---> yt-dlp ---> upstream
  | (remote | <--------> |                   |
  | control)|            +-------------------+
  +---------+                     |
                                  v
                            MPD --> ncmpcpp --> speakers
```

Because the queue is MPD's queue and the player is MPD's player, all three
front ends see the same state. A track added on the phone appears at the
desktop after a refresh, and pressing play in ncmpcpp moves the bar on the
phone.

## Building

The whole build is one command, and it does not need the Android Studio IDE:

```console
$ ./scripts/build-android.sh
```

That produces both a debug and a signed release APK and copies them to
`android/dist/`:

| File | Size | What it is |
|---|---|---|
| `android/dist/app-release.apk` | ~1,9 MB | minified, shrunk, signed. **This is the one to install.** |
| `android/dist/app-debug.apk` | ~18 MB | unminified, `applicationIdSuffix = .debug`, so it installs alongside the release build. |

Other targets:

```console
$ ./scripts/build-android.sh debug      # debug only
$ ./scripts/build-android.sh release    # release only
$ ./scripts/build-android.sh test       # unit tests and lint
$ ./scripts/build-android.sh clean      # remove build output
```

### What the build needs

- **JDK 17 or newer.** Gradle needs `javac`, so a JRE-only install is not
  enough; the failure message mentions toolchains and not JDKs.
- **Android SDK** with platform 35 and build-tools 35.0.0.
- Network access on the first build, to fetch Gradle, AGP and the
  dependencies. After that the build is offline.

The script finds both automatically (`ANDROID_HOME`, then `local.properties`,
then `~/Android/Sdk`; likewise for the JDK) and writes `sdk.dir` into
`android/local.properties`, which is untracked for exactly that reason.

To build by hand instead:

```console
$ cd android && ./gradlew assembleDebug
$ cd android && ./gradlew assembleRelease
```

### Release signing

No keystore is committed, and no key material belongs in a repository. On the
first release build the script generates one **outside** the tree:

| Path | Holds |
|---|---|
| `~/.streambridge/android-release.jks` | the signing key |
| `~/.gradle/streambridge-release.properties` | the four lines `app/build.gradle.kts` reads |

```properties
storeFile=$HOME/.streambridge/android-release.jks
storePassword=...
keyAlias=streambridge
keyPassword=...
```

**Back both up, and keep them out of anything that syncs.** Android identifies
an app by its signing key: a release built with a different key will not
install over an already-installed copy, and the old one has to be removed
first. To use a key you already have, put your own four lines in that
properties file and the script will use them.

Without a properties file, `assembleRelease` still succeeds and produces an
*unsigned* APK. That is the honest outcome rather than a silent fallback to
somebody else's key.

## Installing

### On the phone

1. Copy `android/dist/app-release.apk` to the phone - over USB, or serve it
   from the machine and open it in the phone's browser.
2. Allow installation from that source when Android asks. Installing an APK by
   hand is always something the user has to approve once; the app has no
   business doing it for them.
3. Open **StreamBridge**.

### From the command line

```console
$ adb install -r android/dist/app-release.apk
```

## Setting it up

The phone and the computer have to be on the same network. That is the whole
arrangement - there is no account, no pairing code, no cloud, and no discovery
protocol to trust.

### 1. Allow network access on the server

Out of the box the server is loopback-only, which is the safe default. Serving
the network needs two things, and the second is not optional:

```toml
# ~/.config/streambridge/config.toml
[server]
host = "0.0.0.0"          # see the note below before changing this
allow_lan = true
access_token = "…"        # required; LAN access without a token is refused
```

Generate a token with:

```console
$ python3 -c 'import secrets; print(secrets.token_urlsafe(24))'
```

Three deliberate choices behind that:

- **`host` is `0.0.0.0`, not a named interface.** This is the part that looks
  wrong and is not: a server bound to one non-loopback address stops serving
  `127.0.0.1`, which takes the browser on this machine down with it. Every
  interface is protected by the same token, and the firewall decides what is
  reachable at all. Bind a named interface instead only if you want the VPN
  or a container bridge left out - and move the browser's URL with it.
- **A token is mandatory.** The server refuses to start on a network address
  without one, so turning on LAN access can never quietly publish an
  unauthenticated player to everyone at the coffee shop.
- **Loopback stays open.** A request from the machine itself is trusted, so the
  browser on the desktop needs no token and keeps working unchanged. The token
  is checked for everyone else, in constant time.

Via the environment, which is what a systemd unit uses:

```ini
Environment=STREAMBRIDGE_HOST=0.0.0.0
Environment=STREAMBRIDGE_ALLOW_LAN=true
Environment=STREAMBRIDGE_ACCESS_TOKEN=…
```

Put the token in `~/.config/streambridge/secrets.env` (mode `600`), not in the
unit file, if the unit is ever read by something you do not control.

A systemd unit sets `Environment=`, and that overrides `config.toml`. So a
drop-in is the honest place for this, leaving the unit in the repository
untouched:

```console
$ mkdir -p ~/.config/systemd/user/streambridge.service.d
$ echo "STREAMBRIDGE_ACCESS_TOKEN=$(python3 -c 'import secrets;print(secrets.token_urlsafe(24))')" \
    > ~/.config/streambridge/secrets.env
$ chmod 600 ~/.config/streambridge/secrets.env
```

```ini
# ~/.config/systemd/user/streambridge.service.d/lan.conf
[Service]
Environment=STREAMBRIDGE_HOST=0.0.0.0
Environment=STREAMBRIDGE_ALLOW_LAN=true
EnvironmentFile=%h/.config/streambridge/secrets.env
```

```console
$ systemctl --user daemon-reload && systemctl --user restart streambridge
$ ss -tln | grep 8787
$ curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8787/queue      # 200, local
$ curl -s -o /dev/null -w '%{http_code}\n' http://192.0.2.20:8787/queue      # 401, needs the token
```

To undo it: delete the drop-in, `daemon-reload`, restart. The server is back
on loopback only.

### 2. Firewall

The port only has to be reachable from the local network, never from the
internet. With ufw:

```console
# 192.0.2.0/24 below stands for your own subnet - find it with `ip route`.
$ sudo ufw allow from 192.0.2.0/24 to any port 8787 proto tcp
$ sudo ufw enable
```

If the machine is reachable from outside through a router's port forward, undo
that forward: this service assumes it is only on a network you control.

### 3. Find the machine's address

```console
$ ip -4 addr show scope global        # Linux
$ ipconfig getifaddr en0              # macOS
```

`hostname -I` works too. Use the address the phone can actually reach - on a
machine with both Ethernet and Wi-Fi it is not always the same one.

### 4. Enter it in the app

| Field | Value |
|---|---|
| Server-Adresse | `192.0.2.20` (a paste of `http://192.0.2.20:8787/` also works) |
| Port | `8787` |
| Zugriffstoken | the token from step 1, if you set one |

Tap **Verbinden**. The address is remembered on the phone, and nowhere else.
Nothing is backed up: a different phone on a different network has a different
address, and the token is a credential.

The app does not guess. It does not scan the network, and it does not talk to
anything but the address you gave it - see the note on discovery below.

### 5. If it says "Zugriffstoken fehlt"

The app distinguishes the two failures that look alike from the outside, because
they need opposite things from you:

| Message | Meaning | What to do |
|---|---|---|
| **Zugriffstoken fehlt** | The server answered, and refused the request | Tap **Token eingeben** and paste the token. The token field comes up focused and unmasked. |
| **Server nicht erreichbar** | Nothing answered at that address | Check Wi-Fi, whether the computer is on, whether the service is running, and the address. |

This distinction exists because getting it wrong is expensive. A server that
answers `401` is reachable, and the four checks under *Server nicht erreichbar*
are all true of it; the only thing wrong is the key, and nothing on that screen
would have told you so.

**Erneut versuchen** is deliberately not offered for a `401`. Repeating the
same request without the token gets the same refusal, so the button would only
look busy.

To check from a shell which of the two you have:

```bash
$ curl -s -o /dev/null -w '%{http_code}\n' http://192.0.2.20:8787/queue
401                       # reachable, needs a token
$ curl -s -o /dev/null -w '%{http_code}\n' -H "Authorization: Bearer $TOKEN" \
      http://192.0.2.20:8787/queue
200                       # token correct
```

The server logs every refusal as a warning naming the path, so an app that
cannot connect is visible from the desktop:

```bash
$ journalctl --user -u streambridge -f
streambridge.api WARNING Refused GET /player/status from a network client: no valid access token
```

## Using it

Four tabs and a player.

**Home** shows what is playing, what is next, and what you listened to
recently. Tapping the artwork opens the full player.

**Suche** searches through the server, which is where `yt-dlp` runs. The
phone never contacts the upstream catalogue, so there is nothing to sign in
to and no credentials on the phone. Each result offers *Jetzt abspielen*, *Zur
Queue hinzufügen* and *Als Nächstes abspielen*.

**Queue** is MPD's queue. Reorder with the arrows, remove with the bin, or
clear the lot. Every change is a call the server forwards to MPD, so the
desktop sees it immediately.

**Favoriten** is the server's library, not the phone's - the same list ncmpcpp
and the browser show.

The player has play, pause, stop, previous, next, a scrubber, volume, mute,
and the three playback modes. The lock screen and a headset get the same
transport controls through a media notification; a media-button press is
forwarded to the server exactly as the on-screen button would be.

### On the network, honestly

The app speaks plain HTTP, and the traffic is not encrypted. It is meant for a
network you control, between two devices you own, carrying a few JSON calls
and cover images. The alternative - a self-signed certificate the phone has to
be taught to trust - adds a real failure mode, a certificate the user has to
install, in exchange for protection this transport does not need. On a network
you do not control, do not use the app.

The app accepts an `https://` address too: it is simply dropped during parsing
and the connection is made over plain HTTP. That is deliberate - a `https://`
spelling for a server with no certificate would fail later and less clearly -
but it is also why a genuine HTTPS setup needs a proxy in front rather than a
checkbox here.

### Why there is no automatic discovery

The brief this was built from asked for it optionally, and it is not here.
`mDNS` or a UDP broadcast would find the server in the common case, and would
be a real convenience; it would also mean the phone is speaking to every
device on the network on every launch, and that any device on that network
could answer. Manual entry has one failure mode - you type it once - and
attacks none. The address is remembered after the first time, so the
trade-off is one screen, once.

If it is ever added, the useful shape is the same as the token: probe, show
what was found with the address and the name it claims, and let the user
confirm before connecting. Never connect to a name that appeared on its own.

## Privacy

What the app stores, all of it on the device, none of it leaving it:

- the server address and port
- the access token, if you use one
- nothing else

There is no account, no analytics, no crash reporting, no advertising id, no
push, and no third-party SDK of any kind. The dependencies are Kotlin, AndroidX,
OkHttp and Coil; none of them phone home. The two declared permissions are
`INTERNET` - to reach the server - and `ACCESS_NETWORK_STATE`, so the app can
notice that Wi-Fi came back instead of waiting to be told.

Backup and device-transfer are switched off in the manifest. An address is
specific to one network and a token is a credential; neither is worth moving
to another phone.

Cover images are fetched from the server, which redirects to the upstream image
host, rather than straight from the phone to that host. It is a redirect, not
a proxy, so the bytes do not pass through the server - but the phone does not
contact the upstream host on its own initiative either.

## Testing

```console
$ ./scripts/build-android.sh test          # unit tests + lint
$ cd android && ./gradlew testDebugUnitTest lintDebug
```

90 JVM unit tests, no device required:

| File | Covers |
|---|---|
| `ServerAddressTest` | address parsing, and everything that must be rejected |
| `BridgeApiTest` | every endpoint against a real socket, with every failure mode |
| `DomainMappingTest` | wire to UI mapping, cover URLs, playback words |
| `FormatTest` | durations, unknown lengths, relative times |
| `LiveServerTest` | the client against a running server (opt-in) |
| `LivePlaybackTest` | enqueue, play, and whether audio actually flows (opt-in) |

Lint runs with `warningsAsErrors = true`. The build's other check is
`gradle/libs.versions.toml`: versions are pinned, because a build that
silently upgrades a library is not a reproducible one.

The two live suites need a server and are opt-in:

```console
$ STREAMBRIDGE_LIVE_URL=127.0.0.1:8787 \
  ./gradlew testDebugUnitTest --tests '*Live*'

$ STREAMBRIDGE_LIVE_URL=192.0.2.20:8787 STREAMBRIDGE_LIVE_TOKEN=… \
  STREAMBRIDGE_LIVE_TRACK=5NV6Rdv1a3I \
  ./gradlew testDebugUnitTest --tests '*Live*'
```

`LivePlaybackTest` really does start playback and pull bytes off the stream
endpoint, which is the URL MPD is fetching. It is the only test that changes
what is playing; it puts one track in the queue and restores the volume.

### Driving the app on a device

`scripts/android-e2e.sh <serial> [host] [port] [token]` walks a connected
device or emulator through connect, search and playback, asserting on the view
hierarchy. It is a harness, not a UI test framework - it taps coordinates and
reads `uiautomator dump`, which is enough to walk the flow the way a person
would and to leave behind screenshots when a step fails.

## The icon

The launcher icon, the status-bar icon and the browser favicon all come from
one source image, so the two clients cannot drift apart:

```console
$ python3 scripts/make-android-icons.py \
    android/app/src/main/res \
    src/streambridge/web/assets \
    /path/to/logo.png
```

The mark is the central glyph; the equalizer bars that flank it in the wordmark
lockup are decoration for the icon, because at 48 dp they are about a pixel
wide each. The script writes an adaptive icon (background, foreground and a
monochrome layer for themed icons on Android 13+), a 24dp status-bar
silhouette, legacy square and round bitmaps for API 24 and 25, and the web
assets.

## Layout

```
android/
  settings.gradle.kts, build.gradle.kts, gradle/libs.versions.toml
  gradlew, gradle/                       the wrapper pins Gradle
  app/
    build.gradle.kts                     SDK levels, signing, lint policy
    proguard-rules.pro                   what R8 may not touch, and why
    src/main/AndroidManifest.xml
    src/main/java/app/streambridge/
      StreamBridgeApp.kt                 process entry, image loader, repository
      data/
        ServerAddress.kt                 address parsing, and the path guard
        Domain.kt                        wire types to UI types
        BridgeRepository.kt              the app's only door to the server
        local/SettingsStore.kt           DataStore: address, port, token
        remote/BridgeApi.kt              typed HTTP client
        remote/BridgeException.kt        the closed set of failures
        remote/model/Models.kt           the wire format, field for field
      player/RemotePlaybackService.kt    media session, notification, headset
      ui/
        MainActivity.kt, MainViewModel.kt, AppState.kt, Format.kt
        theme/                           colours, type scale
        components/                      covers, rows, buttons, mini player
        screens/                         connect, home, search, player, queue, …
    src/test/java/…                      the unit and live tests
  dist/                                  built APKs (untracked)
```

### Choices worth knowing about

- **One `ViewModel` for the whole app**, not one per screen. The player, the
  queue, the library and the connection status are one thing, not five, and
  the web UI makes the same choice.
- **`androidx.media`, not Media3.** Media3's session is built around a
  `Player` that owns a timeline and plays something on this device. This app
  plays nothing here - MPD does, on the other machine - so there is no honest
  timeline to give it, and a fabricated one would be precisely the "own
  artificial player state" the design forbids. `androidx.media:media` is the
  library for a session without local playback. Its compat classes still live
  in the `android.support.v4.media` package; that is upstream, not a choice
  here.
- **Polling, not SSE.** The server does have a server-sent-events endpoint, and
  the browser uses it. The app polls instead: every two seconds while playing,
  five while paused, fifteen while stopped, ten in the background, and only
  refetching the queue when the server's `generation` counter says it changed.
  That is four requests a minute in the common case, which is not worth a
  long-lived connection to avoid, and it reconnects on its own after the
  network comes back without a reconnection state machine.
- **Progress is interpolated, state is not.** The bar is advanced by the time
  that passed since the last status reading, while playing, so it moves
  smoothly. It is replaced wholesale by the next reading, and a paused player
  never moves. No local player state exists to disagree with MPD.
- **No DI framework.** The graph is three objects deep and every one of them
  is a concrete class a test can construct. A hand-written container is easier
  to follow here than an annotation processor.
- **Images come from the server, even though the server only redirects.** The
  wire format carries an absolute upstream URL; it is dropped in favour of
  `<server>/thumbnail/<id>` so the phone does not contact the catalogue host on
  its own initiative.
