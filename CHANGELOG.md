# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

**Android app** — a native client for the phone, in `android/`. Kotlin and
Jetpack Compose, no dependency-injection framework, and no second player: it
drives the same server and therefore the same MPD as the browser and ncmpcpp.

- Home, Search, Player, Queue and Favourites, plus a Settings screen and an
  offline state that names the four things that are actually wrong.
- Search through the server, so the phone never talks to the upstream
  catalogue and holds no account, cookie or token of any kind.
- Lock-screen, notification-shade and headset transport control through a
  media session. Forwarded to the server exactly as the on-screen button is.
- Server address, port and optional token stored on the device in DataStore;
  nothing is backed up and nothing leaves the handset.
- Adaptive launcher icon, themed-icon and status-bar layers, all generated
  from the brand artwork by `scripts/make-android-icons.py`.
- `scripts/build-android.sh` builds debug and a signed release APK, and
  generates the signing key outside the repository on first use.
- 90 JVM unit tests, plus two opt-in suites that run the real HTTP client
  against a running server - including one that starts playback and pulls
  bytes off the stream endpoint.

**Optional LAN access** — so a phone on the same network can reach the server.

- `server.allow_lan` and `server.access_token`. Both are required together:
  the server refuses to start on a network address without a token, so
  network access cannot be switched on by accident.
- The token is compared in constant time, and only for clients that are not on
  this machine - a browser on `127.0.0.1` keeps working with no token.
- `/health` and `/version` stay reachable without one, so a client can tell
  "nothing is listening" from "listening, but not for you".
- The web interface asks for the token in a small dialog and remembers it, so
  it also works from a browser on the network.
- Documented in `docs/security.md`, with the firewall rule, in
  `docs/android.md`.

### Changed

- The launcher icon, the status-bar icon and the browser favicon are now
  generated from one source image, so the Android app and the web interface
  cannot drift apart. Same URLs, same content types, new artwork.
- `STREAMBRIDGE_ALLOW_LAN` accepts `true`/`false`/`1`/`0`/`yes`/`no`/`on`/`off`
  from the environment. It previously required a real boolean, which no
  environment variable can be, so the documented way to enable LAN access from
  a systemd unit did not work.

### Fixed

- A response that stalled partway through was reported as a raw socket
  exception instead of a typed timeout, because the body was read outside the
  try block. The app could only say "something went wrong" for it.
- A malformed track id is now refused in the client before a path is built
  from it. OkHttp resolves `..` segments while canonicalising a URL, so an
  unchecked id could send a request to a different endpoint on the same
  server.
- The search results header was drawn underneath the first result rather than
  above it.

## [0.1.0] - Unreleased

First public release. Everything below is new; there is no prior version to
differentiate from.

### Added

**Web interface**

- Browser interface served by the daemon at `http://127.0.0.1:8787/`, with
  Start, Search, Queue, Favourites and History views.
- Full transport control: play, pause, stop, next, previous, seek, volume,
  mute, shuffle and repeat (off / all / one).
- Live player state over server-sent events, with automatic fallback to
  polling when the stream is unavailable.
- Queue editing: add, remove, reorder by drag and drop, and clear.
- Search with history, result paging, and bulk queueing.
- Favourites and play history, stored locally only.
- Responsive layout, keyboard shortcuts, and ARIA labelling throughout.

**Player and library**

- `PlayerService`, a single owner of MPD queue state: the UI, the CLI and
  ncmpcpp all see the same queue, and metadata is resolved at most once per
  track.
- `LibraryStore` for favourites and play history, written `0600` under
  `$XDG_STATE_HOME/streambridge`.
- Metadata is injected through a temporary `EXTM3U` playlist, because MPD
  refuses tags on a URL add. This is why ncmpcpp shows real titles and not URLs.

**HTTP API**

- JSON API alongside the UI: `/search`, `/info/<id>`, `/playlist`,
  `/stream/<id>`, `/thumbnail/<id>`, `/player/status`, `/queue`, `/favorites`,
  `/history`, `/events` and `/stats`.
- Stable machine-readable error codes (`VALIDATION_ERROR`, `NOT_FOUND`,
  `RATE_LIMITED`, `UPSTREAM_ERROR`, `MPD_ERROR`) alongside the human message.
- Server-sent events at `/events`, bounded by a per-client duration cap and a
  maximum number of concurrent streams.
- Static assets served from a fixed allowlist, with `ETag` revalidation and a
  strict `Content-Security-Policy` (no `unsafe-inline`, no `unsafe-eval`).
- One automatic retry of a rejected upstream signature with a freshly resolved
  URL, which is what makes long listening sessions reliable.

**Command line**

- `streambridge search`, `add`, `play`, `play-now`, `next`, `previous`, `pause`,
  `stop`, `seek`, `volume`, `queue`, `favorites`, `favorite`, `history`, `clear`.
- Global `--json` for scripting, and `--config` on every subcommand.
- `streambridge doctor` reports the extractor, `mpc`, MPD and the configuration
  in one pass.

**Documentation and packaging**

- Fully annotated example configuration, plus MPD and ncmpcpp examples.
- A browser-driven end-to-end test and a shell end-to-end script that verifies
  real audio, HTTP range requests and ncmpcpp metadata.
- `scripts/install.sh` and `scripts/uninstall.sh` for a user-local install with
  no root.
- `scripts/verify.sh` runs linting, typing, the offline test suite and the
  privacy audit in one command.
- `scripts/privacy-audit.sh` scans the working tree *and every blob in the
  history* for personal data and secrets.
- `scripts/security-audit.sh` asserts the project's security invariants
  directly, so a later change cannot quietly remove one.

### Security

- The server refuses to bind any address other than loopback.
- Upstream media hosts are checked against an allowlist before any byte is
  forwarded, so a crafted id cannot be used to reach an internal address.
- Subprocess calls are shell-free; no upstream title is ever interpolated into
  a command line.
- `EXTINF` tags are sanitised, so an upstream title cannot inject a playlist
  directive.
- Request bodies are size-capped before parsing, and chunked bodies are refused.
- Web UI assets are served from an allowlist with a containment check, which
  removes path traversal by construction.
- Cookies are passed to the extractor as a browser name and never read by
  StreamBridge; see `youtube.cookies_from_browser` in the example config.

[Unreleased]: https://github.com/Grenoullie91/streambridge/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/Grenoullie91/streambridge/releases/tag/v0.1.0
