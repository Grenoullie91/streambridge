# StreamBridge

Search online music, play it through MPD. Browser, terminal, or any MPD client.

StreamBridge is a small local bridge. It puts a private HTTP service between
your player and the upstream catalogue, so that MPD, ncmpcpp and any other
client see stable local URLs instead of short-lived, signed media links that
expire mid-track.

```
                        +-- browser UI      :8787
search  ->  streambridge-server  ->  MPD  ->  mpc  ->  ncmpcpp  ->  speakers
  CLI           loopback only         queue             terminal UI
                    |
                    +-- yt-dlp: resolves a track to a current audio source
```

## The web interface

Start the service and open <http://127.0.0.1:8787/>:

```console
$ systemctl --user start streambridge
```

A single page with **Start**, **Search**, **Queue**, **Favourites** and
**History** views. Full transport control, queue editing by drag and drop,
live state over server-sent events, and a fallback to polling when the stream
is unavailable.

It is three static files served by the daemon itself. **No build step, no
framework, no dependencies**, and no third-party origin: the strict
`Content-Security-Policy` it is served with allows nothing but `self`, which is
what makes an injected upstream title unable to execute anything.

```
src/streambridge/web/
  index.html   app.css   app.js   manifest.webmanifest   assets/
```

If you would rather stay in the terminal, everything the browser can do is also
a command:

```console
$ streambridge search "big buck bunny" --limit 5
$ streambridge play 5NV6Rdv1a3I
$ streambridge queue
$ streambridge volume 60
$ streambridge favorite 5NV6Rdv1a3I
```

## Why

- **No Python dependencies.** The HTTP server, TOML config and CLI are standard
  library only. `yt-dlp` and MPD do the heavy lifting, and you already run MPD.
- **Loopback only.** The server refuses to bind anything but `127.0.0.1`. It is
  not a proxy: no endpoint accepts a URL, and every path segment is a
  validated 11-character video id.
- **Nothing identifying in the logs.** Home paths collapse to `$HOME`; emails,
  IPv4 addresses, UUIDs and signed media parameters are redacted.
- **Nothing expires in a database.** Stream URLs are cached for two minutes,
  never written to disk, and re-resolved on any failure.

## Requirements

| Component | Needed for | Install (Debian/Ubuntu) |
|---|---|---|
| Python 3.11+ | everything | `apt install python3` |
| [yt-dlp](https://github.com/yt-dlp/yt-dlp) | search, streaming | `pipx install yt-dlp` or `apt install yt-dlp` |
| MPD | playback | `apt install mpd` |
| mpc | MPD control from the CLI | `apt install mpd-client` |
| ncmpcpp | terminal player UI (optional) | `apt install ncmpcpp` |
| ffmpeg | only for exotic codecs (optional) | `apt install ffmpeg` |

On Arch: `pacman -S python yt-dlp mpd mpc ncmpcpp ffmpeg`.

Check your setup at any time:

```console
$ streambridge doctor
```

## Install

```console
$ pipx install streambridge        # or: pip install --user streambridge
```

From a checkout:

```console
$ pip install -e ".[dev]"
$ streambridge --version
```

## Quick start

1. Make sure MPD is running and configured to play somewhere:

   ```console
   $ systemctl --user status mpd
   $ mpc outputs
   ```

2. Start the bridge (either manually or as a service, see below):

   ```console
   $ streambridge-server
   ```

3. Search and play:

   ```console
   $ streambridge search "artist name"
   1. Artist One - First Song (Official Audio)
      Some Album - Artist One
      4:09  https://www.youtube.com/watch?v=...

   $ streambridge search "artist name" --play 3
   ```

4. Or drive it from a known id:

   ```console
   $ streambridge play dQw4w9WgXcQ
   ```

Anything already in your MPD queue keeps working: StreamBridge only appends
HTTP URLs pointing at itself, and never touches the MPD database.

## Commands

```
streambridge search <query>     search the catalogue, then optionally queue a result
streambridge add <id>...        append tracks to the MPD queue
streambridge enqueue <id>...    alias for add
streambridge play <id>...       append tracks and start playing
streambridge play-now <id>      append one track and start it
streambridge info <id>          show metadata for one track
streambridge playlist <id>      load a playlist, order preserved
streambridge status             current MPD status
streambridge health             server and MPD reachability
streambridge doctor             full environment report
streambridge clear [-y]         empty the MPD queue
streambridge next|pause|stop    transport control
```

Global flags: `--config PATH`, `--json`, `-v/--verbose`.

`--json` works on every read-only command, so the CLI composes cleanly:

```console
$ streambridge search "artist name" --json | jq -r '.results[].id'
```

### Exit codes

| Code | Meaning |
|---|---|
| 0 | success |
| 1 | general error |
| 2 | invalid input (bad id, bad query) |
| 3 | missing dependency |
| 4 | MPD unreachable |
| 5 | source unavailable (removed, region locked, bot check) |
| 6 | not found |

## Configuration

Config lives in `$XDG_CONFIG_HOME/streambridge/config.toml`. Every key is
optional; the defaults work on a fresh install. See
[`config/streambridge.example.toml`](config/streambridge.example.toml) for a fully
commented file and [docs/configuration.md](docs/configuration.md) for details.

```toml
[server]
port = 8787

[mpd]
host = "127.0.0.1"
port = 6600
# Required if MPD rejects playlist loads. Must match mpd.conf.
# playlist_directory = "$HOME/playlists"
```

Environment variables override the file: `STREAMBRIDGE_PORT`,
`STREAMBRIDGE_LOG_LEVEL`, `STREAMBRIDGE_MPD_PORT`, and others. Command line
flags override both.

## Running as a service

```console
$ mkdir -p ~/.config/systemd/user
$ cp systemd/streambridge.service ~/.config/systemd/user/
$ systemctl --user daemon-reload
$ systemctl --user enable --now streambridge
$ systemctl --user status streambridge
```

The unit in this repository is hardened. One setting is likely to need your
attention: `ReadWritePaths` must include MPD's `playlist_directory`, because
MPD only accepts a playlist load from that directory. If queuing fails with
"Access denied", this is the reason.

## HTTP API

Read-only, loopback-only, no endpoint takes a URL:

| Endpoint | Purpose |
|---|---|
| `GET /` | service banner and endpoint list |
| `GET /health` | liveness and resolved versions |
| `GET /search?q=&type=&limit=` | search |
| `GET /info/<id>` | metadata for one track |
| `GET /playlist?id=&limit=` | playlist metadata |
| `GET /stream/<id>` | audio stream: 302 redirect, or a proxied relay |
| `GET /stats` | cache statistics |

`/stream/<id>` prefers a 302 so the player fetches the bytes itself and the
bridge stays out of the data path. It only proxies when the source needs a
header the player cannot be handed safely.

## Troubleshooting

Start with `streambridge doctor` and `journalctl --user -u streambridge -f`.

| Symptom | Cause and fix |
|---|---|
| `mpc: Access denied` when queueing | MPD rejected the playlist. Set `mpd.playlist_directory` to match `mpd.conf`. |
| "Source for … is not on an allowed host" | Safety feature. Update yt-dlp: `yt-dlp -U`. |
| "Sign in to confirm you're not a bot" | Upstream is challenging the client. Set `youtube.cookies_from_browser`. |
| Stream stops after a few minutes | MPD re-requested a dead URL. The bridge re-resolves automatically; check the log. |
| Title shows as a bare URL | MPD got no metadata. Confirm the playlist load succeeded. |

Full reference: [docs/troubleshooting.md](docs/troubleshooting.md).

## Documentation

**Using it**

- [docs/installation.md](docs/installation.md) — install and MPD setup
- [docs/configuration.md](docs/configuration.md) — every config key
- [docs/mpd.md](docs/mpd.md) — how StreamBridge drives MPD
- [docs/mpd-config.md](docs/mpd-config.md) — getting MPD itself running
- [docs/ncmpcpp.md](docs/ncmpcpp.md) — terminal player setup
- [docs/troubleshooting.md](docs/troubleshooting.md) — symptom reference

**Understanding it**

- [docs/architecture.md](docs/architecture.md) — how the layers fit together
- [docs/privacy.md](docs/privacy.md) — what is stored, what leaves the machine
- [SECURITY.md](SECURITY.md) — threat model, and how to report a problem
- [docs/security.md](docs/security.md) — the security properties in detail
- [docs/verification.md](docs/verification.md) — how the tests check behaviour
- [docs/testing.md](docs/testing.md) — the test layers and how to write one

**Working on it**

- [docs/development.md](docs/development.md) — layout, conventions, adding routes
- [CONTRIBUTING.md](CONTRIBUTING.md) — the pull request process

## Development

```console
$ make setup
$ make verify                   # lint, types, tests, privacy audit
```

That is what CI runs. Individually:

```console
$ make test                     # unit + integration, offline
$ ruff check . && ruff format --check . && mypy
$ make privacy                  # tree and full history
$ make live                     # optional: real MPD, real audio
```

The suite is offline by design: the subprocess layer and MPD are both replaced
by fakes, so no test reaches the network or your player. `make live` is the only
command that does, and it is opt-in.

## Legal and ethical use

StreamBridge is a client. It ships no media, no catalogue and no credentials.
Respect the terms of service of the upstream service, the copyright of the
material you access, and the law in your jurisdiction. Do not use it to
redistribute media or to circumvent access controls; region locks and bot
checks are reported as errors, not bypassed.

## License

MIT — see [LICENSE](LICENSE).
