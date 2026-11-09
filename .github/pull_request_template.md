<!-- Thanks for contributing! Keep PRs focused; link the issue they address. -->

## What and why

<!-- What does this change, and why? Fixes #... -->

## Checklist

- [ ] Tests added/updated for **both** clients (async + sync contract tests), not just one
- [ ] Changed `_async/` only and ran `nox -s unasync`; regenerated `_sync/` is committed
- [ ] No I/O added to `core/`, `auth/` config classes, `errors.py` or `models.py`
- [ ] Docs updated for any public change (docstrings + `docs/` page; examples run)
- [ ] Changelog fragment added: `changelog.d/<PR-number>.<type>.md` (or label `skip-changelog`)
- [ ] No secrets: recorded fixtures were redacted by the recorder; hand-written ones are marked `synthetic`
- [ ] Salesforce behaviour is backed by an official docs URL (in the test docstring or ADR)
- [ ] Commits are signed off (`git commit -s`, DCO)
- [ ] `nox` (lint, types, unasync_check, tests) passes locally
