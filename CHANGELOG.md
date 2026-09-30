# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

Nothing yet.

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

[Unreleased]: https://github.com/OWNER/streambridge/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/OWNER/streambdae/releases/tag/v0.1.0
