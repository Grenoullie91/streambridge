# Testing

## The short version

```console
$ make verify
```

That runs, in order: `ruff check`, `ruff format --check`, `mypy`, the full
`pytest` suite, and the privacy audit. It is what CI runs and it is what
`scripts/release` refuses to skip. If you are about to open a pull request, that
is the one command to run.

## Layers

| Layer | Command | Needs |
| --- | --- | --- |
| Unit | `make unit` | nothing |
| Integration | `make integration` | nothing |
| Browser (Playwright) | `make browser` | `playwright install chromium` |
| Live playback | `make live` | MPD running, `yt-dlp` installed |
| Full gate | `make verify` | nothing |

## The offline suite is the one that matters

`make test` runs **~800 tests and touches no network and no MPD**. That is not
an optimisation, it is the reason the suite is trustworthy: a test that can be
made to fail by an upstream outage is a test you learn to ignore.

It holds because every external process is faked:

- **`FakeRunner`** stands in for `SubprocessRunner`. A test registers a rule
  ("a search was requested") and asserts on the call, so a test never has to
  hardcode a command line.
- **`FakeMpd`** is an in-memory MPD with MPD's real 1-based positions, its
  wrap-at-the-end behaviour and its `repeat`/`single`/`consume` flags. This is
  what makes player logic testable at all.
- **A local HTTP server** stands in for the upstream on `/stream/<id>`, so
  range requests, rejected signatures and mid-stream disconnects are
  reproducible.

## What the tests actually assert

Not just that things do not crash. The interesting ones:

- `test_full_playback_chain` — search, queue, then **fetch the resulting local
  URL and read the bytes**, with range semantics. This is the test that would
  catch a queue entry that is not actually a working stream URL.
- `test_signature_rejection_is_retried` — an upstream 403 is answered with a
  freshly resolved URL, which is what makes a long listening session survive
  signature expiry.
- `test_path_traversal_is_refused` — a list of traversal payloads
  (`/../config.py`, `/%2e%2e/config.py`, `/app.js/../../config.py`) that must
  all fail. The asset allowlist is why they cannot succeed.
- `test_etag_revalidation` — an unchanged asset answers `304` with no body.
- `test_extinf_tags_cannot_inject_a_directive` — an upstream title containing a
  newline and a quote cannot break out of its `#EXTINF` line.
- `test_history_is_recorded_once_per_song` — history does not double-count
  because a status poll happened twice.

## Testing a change to the player

`FakeMpd` records every call, so a transport test reads as a sequence:

```python
def test_next_at_the_end_stops(player, mpd):
    mpd.add_tracks([track("a"), track("b")])
    player.add(["dQw4w9WgXcQ", "5NV6Rdv1a3I"])
    player.play()
    player.next()
    player.next()
    assert player.status()["state"] == "stopped"
```

## A note on test hygiene

`tests/conftest.py` redirects **both** the cache and the state directory into
`tmp_path`. The library defaults to `$XDG_STATE_HOME/streambridge`, so without
the second redirect every test touching favourites or history would read and
write your real state, and would see files its neighbours left behind. If you
add a fixture, keep it in `tmp_path`.

## Live testing

`make live` is the only thing that needs a working system, and it is optional on
purpose. It verifies real audio, pause and resume, next and previous, HTTP
range requests and the metadata ncmpcpp displays. Run it before a release, not
on every change: it depends on an upstream service, so a failure there is
usually not your commit.
