# Verification

What is checked, by what, and what is not checked at all. Being explicit about
the gaps is the point of this document.

## The test suite

```console
$ pytest                            # unit + integration, no network
$ pytest -m unit                    # pure logic only
$ pytest -m live                    # contacts the real upstream service
$ pytest --cov=streambridge          # with coverage
```

Live tests are deselected by default via `addopts` in `pyproject.toml`.

### Layers

| Directory | Marker | What it covers |
|---|---|---|
| `tests/unit/` | `unit` | Validation, parsing, caching, locking, formatting |
| `tests/integration/` | `integration` | HTTP server over a real socket, CLI end to end |
| `tests/live/` | `live` | The real upstream service |

The suite is offline by design. `tests/conftest.py` defines `FakeRunner`,
which replaces the entire subprocess layer, so no test reaches the network.
`yt-dlp` is installed in CI only so that dependency *detection* has something
to find; it is never executed by a test.

## What each guarantee is checked by

### Input validation

`tests/unit/test_models.py` — valid ids accepted, and a table of rejected
values including `../../../etc/passwd`, `5NV6Rdv1a3I/../x`, null bytes,
vertical tabs, embedded newlines, `'; rm -rf /` and a full watch URL.

### Argument safety

`tests/unit/test_proc.py` — a real child process is started with
`["/bin/echo", "; touch <canary>"]` and the canary must not exist
afterwards. That is a behavioural check, not an inspection of the source.

### No zombies

`tests/unit/test_proc.py` — a parent spawns a grandchild that would create a
marker file after 20 seconds; after a 1.5s timeout and a 3s wait the marker
must not exist. This proves the process *group* is killed, not just the
direct child. A second test runs 25 processes and asserts `ps` reports no `Z`
state children.

### Concurrency

`tests/unit/test_resolver.py` — 12 threads resolve the same id; the fake runner
must record exactly one upstream call. A separate test makes four different
ids each take 250ms and asserts the total is under 0.8s, which fails if a
global lock serialises them.

`tests/integration/test_api.py` — 8 concurrent HTTP requests for one track
cause exactly one resolution.

### Open-proxy resistance

`tests/integration/test_security.py` — a table of proxy-shaped paths
(`/proxy?url=`, `/stream/http://...`, `/fetch?uri=`, cloud metadata
endpoints) must all be rejected. Path traversal in both raw and percent-encoded
form, and a CRLF-bearing id.

`tests/unit/test_streamer.py` — the host allowlist is exercised with allowed
and refused hosts, including the two suffix-confusion cases
`evilgooglevideo.com` and `x.googlevideo.com.evil.test`.

### Loopback binding

`tests/integration/test_api.py` — `make_server` raises for `0.0.0.0`, a LAN
address, a hostname and `::`. `tests/integration/test_security.py` also
connects to a non-loopback local address and asserts the connection fails.

### M3U injection

`tests/unit/test_mpd.py` — a title crafted as `Bad"\n#EXTINF:-1,evil\nhttp://evil.test`
must produce exactly one `#EXTINF` line and exactly one stream line. The
payload may survive as visible text, but it must not create a second directive
or a second URL.

### The MPD load contract

`tests/unit/test_mpd.py` — the `load` argument must be a bare name with no
slash and no `.m3u` suffix, and the generated file must be gone afterwards.
This is verified against the real behaviour of MPD 0.23.14 with mpc 0.35, where
MPD resolves the argument against its `playlist_directory` and appends the
suffix itself.

### Cache hygiene

`tests/unit/test_cache.py` — after writing a cache entry, the serialised file
must not contain `googlevideo` or `/stream/`. A hostile key
(`../../../../etc/passwd`) must not place a file outside the cache directory.

### Redaction

`tests/unit/test_logging.py` — every redaction rule is asserted directly, and
a live log record is checked to confirm the filter is actually attached to the
handler.

### Error handling

`tests/integration/test_security.py` — no response may contain `Traceback` or
`File "`. Errors are always structured JSON.

### Retry bounds

`tests/unit/test_youtube.py` — a transient failure produces exactly
`MAX_ATTEMPTS` calls; a permanent failure produces exactly one.

## What the test suite does not check

Stated plainly, because a green suite should not be mistaken for more than it
is:

- **That the upstream service keeps working.** That is what the live tests are
  for, and they are opt-in.
- **That real audio reaches your speakers.** The suite never touches an audio
  device. Use `scripts/e2e-test.sh` for that.
- **That MPD accepts the generated playlist on your machine.** The unit tests
  assert the argument shape; only a real MPD proves the load. `e2e-test.sh`
  does that.
- **Performance under sustained load.** Concurrency is verified, throughput is
  not.
- **Behaviour across MPD versions.** The load contract is verified against
  0.23.14. Other versions may differ.

## The end-to-end script

```console
$ ./scripts/e2e-test.sh
```

Runs the full path against real components: search, queue, MPD, ncmpcpp and
the audio output, ending with a measurable proof that audio actually flowed.

It needs: the server installed and running as a user service, MPD running, and
`pactl`/`parec` on PipeWire or PulseAudio. It exits non-zero if any step fails.

The audio check is an A/B comparison: record the sink during playback, record
it again while stopped, and compare RMS levels. A non-zero difference is the
proof. A single reading could be noise; the comparison cannot.

## CI

`.github/workflows/ci.yml` runs on Python 3.11, 3.12 and 3.13:

- `ruff check` and `ruff format --check`
- `mypy` in strict mode
- the test suite
- a wheel build plus a smoke test of both entry points

The `live` job runs only on manual dispatch, because it needs the upstream
service and is rate-limited.

## Privacy audit

The repository must contain no personal data. The check:

```console
$ ./scripts/privacy-audit.sh
```

It greps the tracked files for home directories, email addresses, public IP
addresses, credential-looking key names and absolute paths outside the
repository, and fails on any hit. It is a local, dependency-free check: no
network access and no scanner to install.

Run it before every commit, and read its output. A false positive is a line to
reword; a true positive is a line to remove.
