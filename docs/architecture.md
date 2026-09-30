# Architecture

## The path

```
                 ┌──────────────────────────────────────────────┐
   streambridge   │        streambridge-server (loopback)        │
      (CLI) ─────┤  :8787                                       │
                 │                                              │
   ncmpcpp ──────┤  GET /search      → search.py   → cache       │
   (TUI)         │  GET /info/<id>   → cache        → cache       │
                 │  GET /stream/<id> → resolver    → streamer    │
                 │                        │             │         │
                 │                   locks + cache   allowlist   │
                 └────────────────────────┼─────────────┼─────────┘
                                          │             │
                              ┌───────────▼───┐   ┌─────▼──────────┐
                              │  youtube.py   │   │  upstream      │
                              │  (extractor)  │   │  media host    │
                              └───────────┬───┘   └─────▲──────────┘
                                          │             │
                                   ┌──────▼─────┐       │
                                   │  yt-dlp    │───────┘
                                   └────────────┘
                                          │
                        ┌─────────────────┴──────────────────┐
                        │                                    │
                 ┌──────▼──────┐                      ┌──────▼──────┐
                 │     mpc     │                      │     MPD     │
                 └──────┬──────┘                      └──────┬──────┘
                        │    http://127.0.0.1:8787/stream/<id>│
                        └────────────────────────────────────┘
                                                              │
                                                        audio output
```

## Modules

| Module | Responsibility |
|---|---|
| `models.py` | Data types and input validation. The first gate against traversal and injection. |
| `errors.py` | Typed error hierarchy; every error carries an exit code. |
| `config.py` | Layered configuration: defaults, file, environment, flags. |
| `logging.py` | Redacting log setup. Nothing identifying reaches a record. |
| `proc.py` | The only place that starts a process. `shell=False`, always timed out, always reaped. |
| `youtube.py` | The only module that knows the extractor exists. The backend substitution point. |
| `cache.py` | Bounded memory and disk caches. Cache keys are hashed. |
| `search.py` | Search orchestration: validation, cache, rate limit. |
| `metadata.py` | Display formatting. Untrusted text is collapsed to one line. |
| `resolver.py` | Track id to a currently valid source. Per-id locks, short TTL. |
| `streamer.py` | HTTP transport. Host allowlist, redirect decision, Range handling. |
| `mpd.py` | MPD control through `mpc`. The MPD database is never modified. |
| `api.py` | HTTP transport and routing. All logic sits in `ApiService`. |
| `server.py` | Long-running entry point: flags, signals, shutdown. |
| `cli.py` | Short-lived entry point: output formatting, MPD passthrough. |
| `health.py` | Environment report for `doctor` and `--check`. |

## Design decisions

### One extractor boundary

`youtube.py` is the only module that shells out to the extractor. Everything
above it works with `Track`, `StreamInfo` and `Playlist`. Swapping the backend
means rewriting that one file, and the tests for it are the contract for a
replacement.

### Never an open proxy

Three independent mechanisms, because one is not enough:

1. The server refuses to bind anything but loopback.
2. No endpoint accepts a URL. Path segments are validated 11-character ids
   before anything else happens.
3. Upstream hosts are checked against a suffix allowlist before any fetch or
   redirect. Suffix matching, so `evilgooglevideo.com` does not pass.

### Stable local URLs

MPD sees `http://127.0.0.1:8787/stream/<id>` and never a signed media URL.
Signed URLs expire within hours, which would break a queued track. The bridge
resolves one at request time, so the same queue entry keeps working.

### Redirect first, proxy only when needed

A 302 lets MPD fetch the bytes itself, so the bridge stays out of the data
path. Proxying happens only when the source needs a header that must not be
handed to a third-party client, such as a cookie.

### One upstream call per concurrent request

`StreamResolver` keeps a lock per video id, not one global lock. A hundred
concurrent requests for one track cause exactly one resolution; requests for
different tracks never wait for each other. The lock table is bounded and
pruned.

### Bounded everything

Every external call has a timeout, and on timeout the whole process group is
killed, so a hung extractor leaves neither a child nor a zombie. Retries are
capped at three. Caches are LRU-bounded. The lock table is capped.

### Metadata that survives MPD

MPD reads only `Name` and the duration from an M3U for stream entries; the
separate Artist and Album fields stay empty. So the full display name goes
into that one field, and the artist is not duplicated when the upstream title
already begins with it.

The generated playlist is written into MPD's `playlist_directory`, because
MPD accepts a load from nowhere else, and it is referenced by bare name
because MPD appends `.m3u` itself.

### Errors as data

Every error carries a message, an optional hint and an exit code. The CLI
prints the message; the HTTP layer maps the type to a status code. Neither
ever shows a traceback for an ordinary user mistake.

## Concurrency

`ThreadingHTTPServer` with daemon threads: one per request. Shared state is
either immutable or behind a lock:

- `TtlCache` has an internal lock, and its `stats()` call cannot interleave
  with a mutation.
- `TokenBucket` is a lock-guarded sliding window.
- `StreamResolver` uses per-id locks, with a guarded table for the locks
  themselves.
- `Config` is a frozen dataclass; validation returns a new instance.
