# Security Policy

## Scope

StreamBridge is a **local** tool. It binds to loopback only, refuses to start on
any other address, and has no authentication, because it is designed to be
reachable from one machine and no further.

That design makes the threat model unusually narrow, and worth stating plainly:

| Asset | Exposure |
| --- | --- |
| Your listening history and favourites | Local files, never transmitted |
| Your MPD queue | Reachable by anything that can reach `127.0.0.1:6600` |
| The StreamBridge API | Reachable by anything that can reach `127.0.0.1:8787` |
| Media bytes | Streamed from the upstream source to MPD on this machine |

**There is no account, no cloud component, and no analytics.** Nothing StreamBridge
collects about you leaves your machine, which is why there is no privacy policy
page to read: the design is the guarantee. See [`docs/privacy.md`](docs/privacy.md).

## Reporting a vulnerability

**Do not open a public issue for a security problem.**

Use GitHub's private reporting: *Security* → *Report a vulnerability* on this
repository. If that is unavailable, open an issue that says only *"security
report, please contact me privately"* — no details in the public thread.

Please include:

- what you ran and what you expected instead,
- the reproduction steps,
- the version (`streambridge --version`) and your OS and Python version,
- whether the issue exposes data, escalates privilege, or reaches the network.

You can expect an acknowledgement within a few days and a fix or an explanation
for every well-formed report. Reports are credited unless you prefer otherwise.

### What is in scope

- Anything that lets a remote party reach the API or the MPD control surface.
- SSRF: a crafted request that makes StreamBridge fetch an internal address.
- Path traversal: a request that reads a file outside the bundled web assets.
- Command injection through an upstream title, id or playlist.
- A remote crash, or an unbounded resource a remote party can exhaust.
- A leak of local data (favourites, history, cookies, tokens) into a response or
  a log line.

### What is out of scope

- **Anything requiring an attacker who can already run code as you.** StreamBridge
  has no privilege boundary against a local shell.
- **Unauthenticated access to the loopback ports.** If another local user or a
  compromised browser extension can reach `127.0.0.1:8787`, that is a
  composition of those two facts, not a defect here. Binding to a Unix socket
  instead would close it; see *Hardening* below.
- Denial of service by a local process, which is no harder than killing it.
- Upstream (yt-dlp) and MPD vulnerabilities. Report those upstream; keep
  StreamBridge updated to inherit the fix.

## Hardening

The defaults are chosen for a single-user desktop. To tighten them:

- **Keep MPD on loopback.** `bind_to_address "127.0.0.1"` in `mpd.conf`. MPD's
  port is an unauthenticated queue control surface.
- **Keep StreamBridge on loopback.** It refuses to bind anything else, so this
  is a safety net rather than a setting.
- **Do not expose the port through a tunnel or reverse proxy** unless you
  understand that you are publishing an unauthenticated queue controller.
- **Revalidate the security headers** if you front the UI with nginx or Caddy;
  the strict `Content-Security-Policy` it relies on must survive your config.
- **Treat the state directory as private.** Favourites and history are written
  `0600` under `$XDG_STATE_HOME/streambridge`, and a plain `ls` of your listening
  history is not something to publish.

## Supported versions

Security fixes land on the latest release only. There is no long-term support
branch; this is a small project and a maintenance burden is a liability.
