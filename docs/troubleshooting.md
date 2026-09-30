# Troubleshooting

Start here:

```console
$ streambridge doctor
$ journalctl --user -u streambridge -f
```

`doctor` reports the toolchain and reachability without touching the network
unless you ask for it. The log is redacted, so it is safe to paste into a bug
report.

## Installation and startup

### `yt-dlp was not found`

```
Error: yt-dlp was not found ('yt-dlp').
  Hint: Install with: pipx install yt-dlp  |  apt install yt-dlp
```

Not on `PATH`, or named differently:

```console
$ which yt-dlp
$ ~/.local/bin/yt-dlp --version
```

Fix the `PATH` or point the config at the absolute path:

```toml
[youtube]
extractor_path = "/home/USER/.local/bin/yt-dlp"
```

### The server will not start on a non-loopback address

```
Error: streambridge-server binds to loopback only, not '0.0.0.0'.
```

Intended. MPD connects from the same machine, so there is no reason to listen
further out, and a listening socket on a LAN address would be a much larger
attack surface. If you genuinely need remote access, put StreamBridge behind a
SSH tunnel rather than widening the bind.

### `ModuleNotFoundError: No module named 'tomllib'`

Python is older than 3.11. There is no TOML parser in the standard library
before 3.11, and adding a dependency for a config file is not worth it.

```console
$ python3 --version
```

### Port already in use

```
Error: could not bind 127.0.0.1:8787: Address already in use
```

```console
$ ss -tlnp | grep 8787
```

Either stop the other process or choose a port in the config and, if you use
the shipped unit, in the unit's `Environment=` line too.

### `Unknown configuration section(s)` or `Unknown key(s) in [...]`

A typo. Both the section and the offending key are named, because silently
ignoring a misspelled setting is worse than refusing to start.

## Search

### The search returns nothing

```console
$ streambridge search "some query" --json
```

- Check the rate limit is not tripping: `HTTP 429` means the local per-minute
  budget is spent. See `search.rate_per_min`.
- Check the upstream is reachable at all: `streambridge health`.
- Music-focused search falls back to a general search automatically. Both come
  back empty only if the upstream genuinely has nothing.

### "Sign in to confirm you're not a bot"

The upstream is challenging anonymous requests. Update the extractor first:

```bash
yt-dlp -U
```

If it persists, the optional cookie path exists:

```toml
[youtube]
cookies_from_browser = "firefox"
```

Read [security.md](security.md) first: this makes your logged-in session
available to the extractor process. It is never required for normal use.

### "Requested format is not available" / no playable audio format

The extractor's format knowledge is stale:

```bash
yt-dlp -U
```

## Queueing and playback

### `Error: MPD is not reachable (127.0.0.1:6600)`

```console
$ systemctl --user status mpd
$ mpc status
```

If the packaged user service refuses to start with a permission error on
`/etc/mpd.conf`, see [mpd-config.md](mpd-config.md). That is the most common
cause.

### `MPD did not load the playlist: MPD error: Access denied`

MPD only accepts a playlist load from its own `playlist_directory`, and it
wants a bare name without the `.m3u` suffix. Set the directory explicitly:

```toml
[mpd]
playlist_directory = "~/.config/mpd/playlists"
```

The value must match `playlist_directory` in your `mpd.conf`. If you use the
shipped systemd unit, that path must also be in its `ReadWritePaths` line.

### The title in ncmpcpp is a bare URL

MPD received no metadata. Same cause as above: a wrong `playlist_directory`.
MPD reads only the `Name` field and the duration from a playlist for stream
entries, so there is nothing to fall back on.

### `exception: Decoder plugin 'opus' is unavailable`

Install `ffmpeg`. MPD decodes Opus only with ffmpeg present. Without it,
StreamBridge prefers M4A/AAC, which MPD decodes natively.

### Playback stops after a few minutes

Normally this does not happen: MPD re-requests the URL, the bridge notices the
cached source is dead, discards it and resolves a fresh one. The log shows
this as a re-resolution.

If it still stops, the upstream is refusing repeat connections. Check the log
for a `botcheck` or `geo` classification and act on the hint.

### "Source for … is not on an allowed host"

```
Error: Source for 5NV6Rdv1a3I is not on an allowed host.
  Hint: This is a safety feature. Update yt-dlp and retry.
```

A safety feature working as intended: the resolved URL pointed somewhere
outside the known media hosts, so it was refused. An outdated extractor
mis-parsing a response is the usual cause.

### `local rate limit reached`

The per-minute budget for search or resolution is spent. Wait for the window
to slide, or raise the limit:

```toml
[search]
rate_per_min = 60
```

## Logs and privacy

### I need to share a log

It is already safe: home paths collapse to `$HOME`, and emails, addresses,
UUIDs and signed media parameters are redacted. Still, read it first.

```bash
journalctl --user -u streambridge --since "-10min" --no-pager
```

For more detail:

```bash
$ streambridge-server --log-level DEBUG
```

### The log shows no hostnames

By design. A hostname in a log line is identifying, and redacting it would
make the output less useful than simply not logging it.

## Development

### `mpc: Connection refused` from a unit test

The tests replace the subprocess layer, so this cannot come from them. If you
see it in a manual run, MPD is genuinely not running.

### A test fails only sometimes

Look for a test that binds a real port or starts a real process. `test_proc.py`
deliberately spawns children and takes about six seconds; that is expected.

### `pytest` collects the live tests

It does not: `addopts` deselects them. To run them:

```bash
pytest -m live
```

They contact the real upstream service, which is rate-limited. Do not put them
in a tight loop.

## Still stuck

Open an issue with:

```console
$ streambridge --version
$ streambridge-server --version
$ streambridge doctor
$ journalctl --user -u streambridge --since "-10min" --no-pager
```

Include your `config.toml` with paths shortened to `~`. The log is already
redacted, but read it before pasting.
