# Development

## Setup

```console
$ git clone https://github.com/Grenoullie91/streambridge
$ cd streambridge
$ make setup
```

`make setup` creates `.venv` and installs the project in editable mode with its
dev dependencies. **The runtime has no third-party dependencies** — the daemon
and the CLI use only the standard library — so the dev extras are all tooling,
never something a user installs.

`scripts/install.sh` takes a `--dev` flag for the same thing, and only
`--dev` puts pytest in the virtual environment. If `make test` reports a
missing pytest, that is what happened: a plain `install.sh` overwrote the
development environment with a runtime-only one.

```console
$ make doctor     # is yt-dlp, mpc and MPD where they should be
$ make verify     # the full gate; this is what CI runs
```

## The layout

```
src/streambridge/
  cli.py         command line client
  server.py      streambridge-server: process lifecycle, signals, logging
  api.py         HTTP transport + ApiService (all business logic)
  player.py      PlayerService: the single owner of MPD queue state
  library.py     favourites and history, the only persistent writes
  mpd.py         validated, shell-free wrapper around `mpc`
  youtube.py     the yt-dlp boundary (ExtractorClient)
  resolver.py    URL resolution, caching, rate limiting
  streamer.py    upstream host allowlist, range handling
  cache.py       in-memory TTL cache and its on-disk mirror
  config.py      layered configuration
  models.py      Track, StreamInfo, and the id validators
  errors.py      typed errors and the exit-code mapping
  web/           the browser interface: three files, no build step
```

The `web/` directory has **no build step and no dependencies**. It is served
byte-for-byte as committed. A change to the UI does not touch the build, and
there is no `node_modules` in a repository whose whole point is that you can
read it.

## Two design rules worth keeping

**1. All logic lives in `ApiService`, not in the handler.** The HTTP handler
translates requests and writes responses; every decision — what is cached, what
is rate limited, what a stream costs — is a method on `ApiService`. That is what
makes the API testable without opening a socket, and it is why most of the
integration tests are really unit tests of a service object.

**2. Validation happens at the boundary, once.** A video id is validated by
`validate_video_id` and every id in the system is the result of that call.
Nothing downstream re-checks, because nothing downstream can be reached with an
unvalidated value. When you add an entry point, validate there and trust
everything after it.

## Adding an HTTP endpoint

1. Add a method to `ApiService`. It returns a plain dict and raises a typed
   `StreamBridgeError`; it does not know that HTTP exists.
2. Add the route to `do_GET` or `do_POST`. For a `POST`, add the path to
   `_MUTATING_PATHS` or it will answer 405.
3. If it reads a body field, use `self._field(body, "name", type)` so a missing
   or wrongly typed field is a 400 and not a 500.
4. Add a test. `tests/integration/test_web_api.py` has a client fixture; a test
   is a few lines.

Do not add a path to `_STATIC_FILES` on the fly. That allowlist *is* the
protection against path traversal — a route that builds a file path from the
request reintroduces the vulnerability the allowlist removes.

## Adding a web UI feature

`src/streambridge/web/app.js` is plain ES modules and the DOM, no framework.
The pattern is a route in the hash, a render function, and `api.get` /
`api.post` for the server.

- New endpoints go in `_STATIC_FILES` in `api.py` if they are files.
- New copy goes in German, matching the rest of the UI. API messages and logs
  stay English.
- Keep it keyboard reachable and labelled. The accessibility attributes are not
  decoration, and a control without one is a bug.

Then run `make browser`.

## Style

- `ruff check` and `ruff format` decide formatting. Do not hand-format.
- `mypy` is strict. `# type: ignore` needs a reason in a comment.
- Comments explain **why**. The code already says what it does. A comment that
  restates the next line is noise, and a comment that saves the next reader
  from re-deriving a non-obvious constraint is the whole point.
- Docstrings say what the invariant is and what breaks without it, not what the
  function does.

## Dependencies

Adding a runtime dependency needs a strong reason. The project installs with
the standard library alone, and that property is worth real money: it means no
supply chain, no resolver, and a package that still works in five years.

The few things that could tempt you:

| Want | Use instead |
| --- | --- |
| An HTTP client | `urllib.request` — see `streamer.py` |
| A TOML parser | `tomllib`, standard since 3.11 |
| A test runner | `pytest`, dev-only |
| Anything for the UI | The DOM |

## Before you open a pull request

```console
$ make verify
```

Then check the pull request template. The one that catches contributors most
often is the privacy audit: it scans the whole history, so a stray absolute
path from your own machine in a commit message will fail the build.
