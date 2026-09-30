# Contributing

## Setup

```bash
git clone https://github.com/OWNER/streambridge
cd streambridge
pip install -e ".[dev]"
```

## Before you open a pull request

```bash
ruff check . && ruff format --check . && mypy && pytest
./scripts/privacy-audit.sh
```

All five must pass. The last one is not optional: this repository is public,
and a single leaked home path or token is a real incident.

## The invariants

These are the properties the project is built on. A change that breaks one
needs a very good reason and a discussion first.

1. **Loopback only, no URL-accepting endpoint.** The server refuses a
   non-loopback bind, and no endpoint takes a URL. Every path segment is a
   validated 11-character id.
2. **The upstream allowlist is a suffix match.** A substring match lets
   `evilgooglevideo.com` through.
3. **No shell, ever.** Arguments are lists, `shell=False`, control characters
   rejected before they reach an argument.
4. **Every external call is bounded.** A timeout, and the whole process group
   killed when it fires. Retries capped.
5. **No media URL is ever written to disk.** They expire; a stale one in a
   cache is the failure this project exists to prevent.
6. **No personal data in the repository.** Enforced by the audit script.
7. **No new runtime dependency** without a discussion. The standard library has
   covered everything so far.

## Tests

A change without a test is not finished. Match the layer:

| Kind of change | Where the test goes |
|---|---|
| Validation, parsing, formatting | `tests/unit/` |
| HTTP surface, CLI behaviour | `tests/integration/` |
| Something only the real upstream can prove | `tests/live/`, marked `live` |

Assert behaviour, not implementation. `test_proc.py` proves argument safety by
running a command with `; touch <canary>` and asserting the file does not
appear, rather than by reading the source.

The suite must stay offline. If your test needs the network, it belongs in
`tests/live/` behind `-m live`.

## Style

Follow the surrounding code. `ruff` enforces formatting, import order, naming
and a large set of correctness rules; `mypy --strict` covers the source.

Comments explain why, not what. If a line needs a comment to say what it does,
rename something instead.

## Documentation

User-visible changes need a documentation change in the same pull request.
`docs/verification.md` states what is checked and, more importantly, what is
not. If your change affects either list, update it.

## Commits and pull requests

- One logical change per commit.
- Write the subject as an imperative sentence under 70 characters.
- Explain the reasoning in the body when it is not obvious from the diff.
- Reference the issue the change closes.
