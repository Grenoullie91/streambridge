# Security and privacy

## Threat model

The asset is the user's listening: what they search for and what they play.
The adversary is any other process on the machine, and the risk of an
accidental leak through logs, cache files or error messages.

Out of scope: a root-level attacker, and anything on the network, because the
service is loopback-only.

## No open proxy

Three independent mechanisms:

1. **Loopback only.** The server refuses to bind `0.0.0.0`, a LAN address or
   any non-loopback host, and says so instead of warning quietly.
2. **No URL-accepting endpoint.** There is no `/proxy`, no `?url=` parameter.
   Path segments are validated as exactly 11 URL-safe characters before any
   filesystem, subprocess or network access. `http://127.0.0.1:6600` in a path
   position is not a URL, it is an invalid id.
3. **Upstream allowlist.** Every fetch and every redirect target is checked
   against a suffix allowlist of known media hosts. Suffix matching, so
   `evilgooglevideo.com` is rejected. Redirect hops are re-validated, so an
   allowed host cannot bounce the server somewhere else.

## Argument safety

Every external program is started through `proc.py`:

- arguments are a list, never a shell string
- `shell=False` always
- control characters are rejected in queries and ids before they get near an
  argument
- a query is always one argv entry, so no splitting is possible

## Resource bounds

| Resource | Bound |
|---|---|
| External call duration | `[timeouts]` in config, default 20s |
| Upstream socket | 20s, every read and connect |
| Client socket idle | 30s, set on the HTTP handler |
| Retry attempts | 3, and only for transient failures |
| Process survival on timeout | killed by process group, including children |
| Memory cache | `cache.max_entries`, LRU |
| Disk cache | pruned to `cache.max_entries` |
| Resolver lock table | 512, pruned |
| Server memory | `MemoryMax=512M` in the shipped unit |
| Server tasks | `TasksMax=128` in the shipped unit |

The client socket timeout closes the last unbounded resource. Without it, a
local client that connects and then goes quiet holds a thread and its buffer
for as long as it likes; with it, `socketserver` drops the connection after 30
seconds. It bounds the gap *between* requests, not a transfer, so a long
proxied stream is unaffected.

## Privacy

### Logs

A redacting filter is attached to the log handler, so it applies to every
record regardless of the call site:

- home paths become `$HOME`
- emails, IPv4 addresses and UUIDs are replaced
- signed media URLs are reduced to origin plus a parameter count
- values after `--cookies`, `--cookies-from-browser`, `--username`,
  `--password` and `--video-password` are replaced

### Cache files

Only stable metadata is persisted: id, title, artist, album, duration, and
public URLs. **Media URLs are never written to disk.** They expire within
hours, and a stale one in a long-lived cache is exactly the failure this
project exists to avoid. Tests assert that no `googlevideo` or `/stream/`
string appears in a written cache file.

### Cache filenames

Cache keys are SHA-256 hashed. No user input reaches the filesystem verbatim,
so path traversal through a key is impossible by construction rather than
blocked by a check.

### No identifiers in the repository

Defaults are generic: `127.0.0.1`, `8787`, `$HOME`, XDG directories, `$USER`.
No personal path, name, email, IP, hostname, token, cookie or machine detail
is committed. See [verification.md](verification.md) for how this is checked.

## Cookies

`youtube.cookies_from_browser` is optional and off by default. When set,
StreamBridge passes only the browser *name* to the extractor as a separate
argument; it never opens or reads a cookie store itself.

Be deliberate about enabling it: it makes your logged-in session available to
the extractor process. It is only needed when the upstream service challenges
anonymous requests, and it is never required for normal use.

## What StreamBridge does not do

- No telemetry, no analytics, no crash reporting, no outbound heartbeat.
- No media is stored. The service is a stream bridge, not a downloader.
- The MPD database is never modified. `clear` empties the queue and nothing
  else.
- No access control is bypassed. Region locks and bot checks are reported as
  errors with a hint, not worked around.

## Local rate limiting

A sliding-window limit guards search and resolution. It exists to stop
accidental request storms from a runaway script, not to throttle listening.
Streaming itself is exempt.

## Reporting a vulnerability

Open a private security advisory on the repository rather than a public
issue. Include the version, your configuration with paths redacted, and the
reproduction steps. You should get an acknowledgement within a week.

## The shipped systemd unit

`systemd/streambridge.service` is hardened: `NoNewPrivileges`,
`ProtectSystem=strict`, `ProtectHome=read-only`, `PrivateTmp`,
`ProtectKernelTunables`, `RestrictSUIDSGID`, `RestrictNamespaces`,
`LockPersonality`, a syscall filter, and a memory cap.

`ReadWritePaths` must include the cache directory and MPD's
`playlist_directory`. That is a functional requirement, not a relaxation: MPD
refuses a playlist load from anywhere else.

`MemoryDenyWriteExecute=false` is required: the extractor binary and some
Python extension modules need to map memory as executable.
