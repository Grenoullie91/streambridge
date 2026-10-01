# Security and privacy

## Threat model

The asset is the user's listening: what they search for and what they play.
The adversary is any other process on the machine, and the risk of an
accidental leak through logs, cache files or error messages.

Out of scope: a root-level attacker, and anything on the network, because the
service is loopback-only.

## Network access

The default is loopback, and the refusal is enforced twice: `server.host` is
checked when the configuration is loaded, and again in the server factory, so
neither a config file nor a command line flag can get past it by being
unusual.

Putting the server on the network - which the [Android app](android.md) needs -
is opt-in and takes two keys together:

```toml
[server]
host = "192.0.2.20"     # a specific LAN address, not 0.0.0.0
allow_lan = true
access_token = "…"
```

**Why a specific address and not `0.0.0.0`.** Binding every interface also
binds a VPN, a container bridge, a guest interface and anything else the
machine happens to have. Naming one address means the phone works and the
other networks are not part of the decision. Anyone who wants the wildcard can
have it, but they have to type it.

**Why the token is not optional.** With LAN access on and no token, anyone who
can reach the port can play, pause, skip, change the volume and empty the
queue. The server refuses to start in that configuration, so it cannot be
reached by disabling a check that is off by default. Requests from the machine
itself are trusted and need no token, which is what keeps the browser on
`127.0.0.1` working with no changes.

**How the token is compared.** In constant time, with a length the attacker
cannot learn from timing. A shared secret compared with `==` leaks byte by
byte.

**What stays reachable without a token.** `/health` and `/version`, plus the
static shell and the cover images. The first two carry nothing but a version
number and exist so a client can tell "nothing is listening" from "listening,
but you are not allowed yet". The rest is what a client needs in order to draw
the "this server wants a token" prompt at all. Nothing that can read the queue
or move the player is in that set.

**What the token is not.** It is not a per-user account: there is one secret,
it is not rotatable without restarting, and it does not expire. It protects a
service on a network you control from other people on that network. It is not
a substitute for a firewall, and a router port forward would defeat it
entirely - the traffic would be arriving from the internet, not the LAN, and
the loopback exemption would not apply, but neither would anything else you
believe about who is on the other end.

The browser UI handles a token without any JavaScript change beyond the app's
own fetch layer: a 401 opens a small prompt, the value goes into
`localStorage`, and it is sent as `Authorization: Bearer` from then on. It
never leaves the browser.

### Where the token is stored, and what that costs

`localStorage` is the weakest of the usual places, and it is worth being plain
about which property is being traded away.

**What it buys.** A LAN install is usable. Asking for the token again after
every browser restart, on a phone, is the kind of friction that gets a security
feature switched off, and a feature nobody enables protects nobody.

**What it costs.** The value survives a browser restart and is readable by any
script running on the same origin. So:

- An XSS on `http://127.0.0.1:8787` becomes token theft. This is why the UI
  ships a strict `Content-Security-Policy` with neither `unsafe-inline` nor
  `unsafe-eval`, and why exactly one `innerHTML` remains in the codebase.
- A browser extension with access to the page can read it.
- A shared browser profile on a shared machine keeps it.

**The trade is only worth it if you have assessed it.** If your threat model
includes hostile code on your machine — a compromised extension, a shared
profile — store the token per session instead and accept re-entering it. On a
loopback-only install none of this applies: the token field stays empty,
nothing is sent, and no prompt can appear.

**Rotating it.** Changing `access_token` in the configuration and restarting
invalidates every copy, because the client sends the old one and gets a 401.

## No open proxy

Three independent mechanisms:

1. **Loopback unless you say otherwise.** The server refuses to bind
   `0.0.0.0`, a LAN address or any non-loopback host unless `allow_lan` is set,
   and when it is set it refuses to start without a token too. The policy lives
   in one place, `config.lan_bind_problem`, and both the server factory and the
   CLI go through it, so there is no second path that skips the check.
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
