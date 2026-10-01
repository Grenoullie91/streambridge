# Configuration

Config file: `$XDG_CONFIG_HOME/streambridge/config.toml`, or
`$XDG_CONFIG_HOME` unset means `~/.config`. A full commented example lives in
[`config/streambridge.example.toml`](../config/streambridge.example.toml).

## Precedence

Lowest to highest:

1. built-in defaults
2. the config file
3. `STREAMBRIDGE_*` environment variables
4. command line flags

```console
$ STREAMBRIDGE_PORT=9000 streambridge-server
$ streambridge-server --port 9001
```

A different file can be selected with `STREAMBRIDGE_CONFIG` or `--config`.

## Strict parsing

Unknown sections and unknown keys are rejected by name:

```
Error: Unknown key(s) in [server]: prot. Known keys: host, port
```

This is deliberate. Silently ignoring a typo leaves you debugging a setting
that never took effect.

## Sections

### `[server]`

| Key | Default | Meaning |
|---|---|---|
| `host` | `127.0.0.1` | Bind address. Loopback unless both `allow_lan` and `access_token` are set. |
| `allow_lan` | `false` | Permit a network bind, so a phone can reach the server. Requires `access_token`. |
| `access_token` | none | Shared secret required from clients that are not on this machine. Loopback clients are exempt. |
| `port` | `8787` | 1..65535 |

### `[search]`

| Key | Default | Meaning |
|---|---|---|
| `limit` | `20` | Default result count, 1..100. |
| `rate_per_min` | `30` | Request budget per minute. |
| `resolve_rate_per_min` | `60` | Separate budget for stream resolution. |

The limits exist to stop accidental request storms, not to throttle normal
listening. Streaming itself is never rate limited; only resolution is, and
playback can opt out.

### `[timeouts]`

| Key | Default | Meaning |
|---|---|---|
| `request` | `20.0` | Search and metadata calls. |
| `resolve` | `30.0` | Stream resolution, which may be slower. |
| `info` | `20.0` | Single-track metadata. |

Every external call is bounded. A hung extractor can never block the server,
and on timeout the whole process group is killed.

### `[cache]`

| Key | Default | Meaning |
|---|---|---|
| `directory` | `$XDG_CACHE_HOME/streambridge` | Search and metadata cache. |
| `ttl_seconds` | `300.0` | Entry lifetime; `0` disables expiry. |
| `max_entries` | `200` | Bound on both the memory and the disk cache. |

Cache keys are SHA-256 hashed, so no user input reaches the filesystem as a
filename and path traversal through a key is impossible by construction.

Only stable metadata is cached. Stream URLs are never written to disk; see
[security.md](security.md).

### `[mpd]`

| Key | Default | Meaning |
|---|---|---|
| `host` | `127.0.0.1` | MPD host. |
| `port` | `6600` | MPD port. |
| `playlist_directory` | auto-detect | Must match `mpd.conf`. See below. |

MPD accepts a playlist load only from its own `playlist_directory`, and it
resolves the argument as a bare name without the `.m3u` suffix. If queuing
fails with "Access denied", set this:

```toml
[mpd]
playlist_directory = "~/.config/mpd/playlists"
```

Detection order when unset: `mpc paths` (absent in mpc 0.35), then the cache
directory.

### `[youtube]`

| Key | Default | Meaning |
|---|---|---|
| `extractor_path` | `yt-dlp` | Extractor binary. |
| `cookies_from_browser` | unset | Browser name, e.g. `firefox`. Only for bot checks. |

The browser name is passed to the extractor as a separate argument;
StreamBridge never reads cookie files itself. Be aware that this makes your
logged-in session available to the extractor. See
[security.md](security.md).

`extractor_path` is the substitution point for a different backend: any
command speaking yt-dlp's command line interface works, and replacing the
extractor means replacing `streambridge/youtube.py` alone.

### `[logging]`

| Key | Default | Meaning |
|---|---|---|
| `level` | `INFO` | `DEBUG`, `INFO`, `WARNING` or `ERROR`. |

Logs go to stderr and pass through a redacting filter. Use `-v` for `DEBUG`.

## Environment variables

| Variable | Maps to |
|---|---|
| `STREAMBRIDGE_CONFIG` | Path to the config file |
| `STREAMBRIDGE_HOST` | `server.host` |
| `STREAMBRIDGE_ALLOW_LAN` | `server.allow_lan` (`true`/`false`/`1`/`0`/`yes`/`no`/`on`/`off`) |
| `STREAMBRIDGE_ACCESS_TOKEN` | `server.access_token` |
| `STREAMBRIDGE_PORT` | `server.port` |
| `STREAMBRIDGE_SEARCH_LIMIT` | `search.limit` |
| `STREAMBRIDGE_REQUEST_TIMEOUT` | `timeouts.request` |
| `STREAMBRIDGE_RESOLVE_TIMEOUT` | `timeouts.resolve` |
| `STREAMBRIDGE_INFO_TIMEOUT` | `timeouts.info` |
| `STREAMBRIDGE_CACHE_DIR` | `cache.directory` |
| `STREAMBRIDGE_CACHE_TTL` | `cache.ttl_seconds` |
| `STREAMBRIDGE_MPD_HOST` | `mpd.host` |
| `STREAMBRIDGE_MPD_PORT` | `mpd.port` |
| `STREAMBRIDGE_PLAYLIST_DIR` | `mpd.playlist_directory` |
| `STREAMBRIDGE_EXTRACTOR_PATH` | `youtube.extractor_path` |
| `STREAMBRIDGE_COOKIES_FROM_BROWSER` | `youtube.cookies_from_browser` |
| `STREAMBRIDGE_LOG_LEVEL` | `logging.level` |
| `NO_COLOR` | Disables ANSI colour in the CLI |

A bad value names the variable it came from:

```
Error: Environment variable STREAMBRIDGE_PORT (server.port): must be an integer, got 'abc'
```

## Exit codes

| Code | Name | Meaning |
|---|---|---|
| 0 | `OK` | success |
| 1 | `GENERAL_ERROR` | unclassified failure |
| 2 | `INVALID_INPUT` | bad id, query or limit |
| 3 | `MISSING_DEPENDENCY` | `yt-dlp` or `mpc` not found |
| 4 | `MPD_UNREACHABLE` | MPD refused the connection or a command |
| 5 | `SOURCE_UNAVAILABLE` | removed, private, region locked, bot check |
| 6 | `NOT_FOUND` | resource does not exist |

Scripts can branch on these instead of parsing messages.
