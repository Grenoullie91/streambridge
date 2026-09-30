# Privacy

StreamBridge has no accounts, no server, and no telemetry. This page is not a
policy with clauses; it is a description of what the code does with your data,
so you can check it against [`src/`](../src).

## The short version

| | |
| --- | --- |
| Data sent to a server operated by someone else | **None.** |
| Account required | **No.** |
| Telemetry, analytics, crash reports | **None.** |
| Data left on your machine | Favourites, play history, a search cache |
| Network connections | The upstream music service, and `127.0.0.1` |

## What is stored on your machine

Two directories, both created with mode `0600`, both under XDG:

- `$XDG_STATE_HOME/streambridge/` — favourites and play history, as JSON. This
  is a record of what you listened to. It is yours; do not publish it.
- `$XDG_CACHE_HOME/streambridge/` — cached search results and track metadata.
  Purely a speed optimisation. Deleting it costs one slow request and nothing
  else.

Both are plain text. There is no encryption because there is nothing to protect
against: the threat model for a local file is a process already running as you.

## What is never stored

- **Audio or video bytes.** Never written to disk, never buffered to a file.
  A stream is a pass-through: bytes arrive from upstream and go straight to MPD.
- **Signed media URLs on disk.** The disk cache holds search results and
  metadata, never a media URL. A signed URL on disk would be a credential for
  that one stream, and it expires in hours anyway.
- **Cookies.** If you set `youtube.cookies_from_browser`, StreamBridge passes
  the *browser name* to `yt-dlp` as an argument and never reads the cookie file
  itself. But note what that does mean: **`yt-dlp` will read your logged-in
  session**, and StreamBridge cannot undo that. It is off by default.

## What leaves your machine

Only two connections, both expected:

1. **To the upstream music service**, to search and to resolve a stream. Those
   requests are made by `yt-dlp`, and they look like any other client of that
   service. The service sees your IP address, as it would for a browser tab.
2. **To `127.0.0.1`**, your own MPD. Never off-machine.

There is no third. `make security` asserts this: it fails the build if a
third-party CDN reference appears in the web UI, and the `Content-Security-Policy`
served with every response allows no origin but `self`.

## The listening-history argument

Here is the part worth being explicit about, because it is the obvious objection
to a tool like this.

Your search queries and your listening history are **not** sent to a server
operated by the StreamBridge authors, because there is no such server. But the
upstream service does observe that *something* on your IP is searching for and
streaming particular tracks, and a metadata service in particular can infer a
lot from that.

StreamBridge cannot fix that, and does not pretend to. What it does is make the
second half of the system — the part that would turn observations into a profile
— yours and local-only:

- history and favourites never leave the machine,
- there is no identifier that ties sessions together,
- there is no account, so there is nothing to leak from a breach,
- the API and the UI bind to loopback and cannot be reached from the network.

If even the upstream observation is unacceptable, this is the wrong tool and you
should not use it. Using local files with MPD has no equivalent at all.

## The web interface

The UI is three static files served by StreamBridge itself, with no third-party
script, style or font, and no analytics. The strict `Content-Security-Policy`
(no `unsafe-inline`, no `unsafe-eval`) is not decoration: it is what makes an
injected upstream title unable to execute anything.

Cover images are a **redirect** to the upstream image host, not a proxy. The
video id is validated before the URL is built, so the redirect cannot be steered
elsewhere — and this is the one place your browser talks to the upstream host
directly.

## Verifying this page

Claims like these are only worth what their evidence is worth. Two ways to check:

```console
$ make privacy     # personal data and secrets, tree and full history
$ make security    # project security invariants
```

Or read the code. The relevant boundaries are small on purpose: `streamer.py`
(host allowlist), `proc.py` (subprocess), `api.py` (request handling), and
`library.py` (the only code that writes anything persistent).
