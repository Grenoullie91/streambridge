# Test fixtures

Recorded upstream responses, used wherever a test needs a realistic `yt-dlp`
payload. They are plain JSON so a test can read one and immediately see what it
is looking at.

| File | What it is |
| --- | --- |
| `search_results.json` | A `ytsearch` result with three entries and a mix of complete and missing metadata |
| `track_info.json` | A `watch?v=` result with three audio-only formats in different containers and codecs |

Both are synthetic. The video ids are the well-known public test id and its
companion, the hostnames are the upstream CDN, and no value is copied from a
real account, a real session or a real person's listening.

## Rules for changing these

- **No personal data, ever.** Not a real id, not a real title, not a real
  channel. `make privacy` scans this directory like everything else.
- **No live URLs.** Where a URL carries an `expire` parameter it is far in the
  future, so a test that accidentally reaches one gets a clear error instead of
  a slow timeout. A URL without an expiry cannot resolve at all, because the
  signature is incomplete. `tests/contract/` asserts this.
- **Keep the awkward cases.** A fixture that is only tidy is a fixture that
  tests less. `search_results.json` deliberately includes an entry with a null
  duration and one with a missing channel, because real responses have those and
  the code has to survive them.
