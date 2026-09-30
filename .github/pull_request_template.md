## What does this change?

<!-- One or two sentences. If it fixes an issue, end with "Fixes #123". -->

## Why?

<!-- The problem, not the solution. What breaks without this? -->

## Checklist

- [ ] `make verify` passes (lint, types, tests, privacy audit)
- [ ] New behaviour has a test that fails without it
- [ ] No new runtime dependency — the project installs with the standard library alone
- [ ] `make privacy` is clean, including over the whole history
- [ ] Documentation updated, if the change is visible to a user
- [ ] Commits are focused; unrelated reformatting is in its own commit

## Security

- [ ] If this touches request handling, subprocess use, or the filesystem:
      has the input been validated, and is it still shell-free?

## Testing notes

<!-- Anything a reviewer cannot infer from the diff: a skipped test and why,
     a platform you could not check, an assumption about the environment. -->
