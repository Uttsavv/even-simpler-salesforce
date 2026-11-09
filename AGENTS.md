# AGENTS.md

Rules for AI agents (and humans) working in this repository. They are binding: a change
that breaks one of them will not be merged. See also [CONTRIBUTING.md](CONTRIBUTING.md).

## Before you start

- **Read the spec first:** [`big-bang.md`](big-bang.md), at least the sections for the
  area you touch, plus §3 (layout), §5 (architecture) and §8 (errors). Then read the ADRs
  in `docs/adr/`. If your change contradicts a decision (`D*`/`O*` in §2), stop and raise
  it in an issue instead of working around it.
- **Verify Salesforce behaviour against official Salesforce documentation**, not memory or
  blog posts. Add the source URL to the ADR or to the docstring of the test that relies
  on it.

## Layout rules (spec §3)

| Path | Rule |
|---|---|
| `src/even_simpler_salesforce/_async/` | Hand-written async I/O code. **The only place that awaits.** Source of truth for both clients. |
| `src/even_simpler_salesforce/_sync/` | **Generated** by `scripts/unasync.py`. Never edit by hand (except the hand-written `_sync/_compat.py`). |
| `core/`, `auth/` config classes, `errors.py`, `models.py` | **No I/O.** Pure functions/data shared unchanged by both clients. |
| `__init__.py` `__all__` + documented submodules (`observability`, `testing`, `contrib.fastapi`, `contrib.flask`) | The public SemVer surface. Everything else is private, even without `_`. |
| `tests/unit` | No network, no httpx mocking. |
| `tests/contract` | respx + recorded responses, parametrised over **both** clients. |
| `tests/fixtures/recorded/` | Redacted recorded responses, one directory per API area. |
| `tests/sandbox` | Real-org tests, marker `sandbox`, opt-in only. |
| `docs/` | Source pages; every fenced `python` block is executed by `tests/docs`. `site/` is build output (gitignored). |
| `changelog.d/` | towncrier fragments. Do not edit `CHANGELOG.md` by hand. |

## The `_async` → `_sync` rule

1. Edit only `_async/` (or `_async/_compat.py` / `_sync/_compat.py` for primitive swaps).
2. Run `uvx nox -db uv -s unasync`.
3. Commit the regenerated `_sync/` in the same commit as the `_async/` change.
4. `uvx nox -db uv -s unasync_check` must pass (CI runs it).

## Every public change needs

- **tests for both clients** (async and sync contract tests), plus unit tests for any
  `core/` logic, keeping 100 % coverage of `core/` and `errors.py` and ≥ 90 % overall;
- **docs**: Google-style docstrings on every public symbol and an updated `docs/` page
  with runnable examples, written to the style in
  [CONTRIBUTING.md](CONTRIBUTING.md#writing-documentation);
- **a changelog fragment**: `changelog.d/<PR-or-issue>.<type>.md` (or `+<slug>.<type>.md`),
  type ∈ `added changed deprecated removed fixed security performance`;
- a deprecation path (`EsfDeprecationWarning` for at least one minor) before removing
  public API (spec §17.1).

## Secrets and fixtures

- **Never log secrets.** Access/refresh tokens, client secrets, private keys and session
  IDs must be redacted in every event, exception message and debug line.
- **Record, don't invent.** Capture new Salesforce responses with the recorder (it
  redacts). Hand-written responses are allowed only when marked `synthetic` (in the
  fixture and the test).
- Never commit anything matching `00D[A-Za-z0-9]{12,15}!`; CI and pre-commit reject it.

## Running checks

```sh
uv sync --all-extras --group dev
uvx nox -db uv                   # lint, types, unasync_check, tests (3.10-3.14)
uvx nox -db uv -s tests-3.12 -- tests/unit -x   # fast loop
uvx nox -db uv -s lint           # or: uv run ruff check . && uv run ruff format --check .
uvx nox -db uv -s types          # pyright strict
uvx nox -db uv -s docs           # zensical build --clean
uvx nox -db uv -s min_deps       # httpx 0.27
uvx towncrier build --draft --version X.Y.Z
```

Run at least `lint`, `types`, `unasync_check` and the tests for the area you changed
before declaring work done. Report failures honestly; do not weaken tests, coverage
thresholds, pyright strictness or ruff rules to make a check pass.

## Commit conventions

- Conventional Commits: `feat(scope):`, `fix(...)`, `docs`, `test`, `refactor`, `perf`,
  `build`, `ci`, `chore`. Small, logical commits; imperative subject ≤ 72 chars.
- Every commit carries a DCO sign-off (`git commit -s`); `dco.yml` checks it.
- Agent-authored commits also end with a `Co-Authored-By:` trailer naming the agent.
- Do not push to `main`, force-push shared branches, create tags, or run
  `scripts/release.py` unless the maintainer asks you to.

## Scope guard

- General-purpose only (D4): nothing specific to one app or org.
- One way per task (D29): a new public method or parameter must do something no existing
  public API can do, or replace code users write by hand and often get wrong. No aliases,
  shortcut parameters or format converters; check D29's rejected list before proposing one.
- No SOAP `login()`, no zeep, no attribute magic (D10). SOAP is used only internally for
  lead convert/merge (D14).
- Core depends on httpx only; anything else is an optional extra (O7).

## Licensing

- This is an original Apache-2.0 implementation of our own spec and ideas. Never copy code,
  docstrings or tests from projects with an incompatible or unclear licence (including
  simple-salesforce and aiosalesforce). Read other libraries for behaviour only; write your own.
- Third-party text that is compatible (e.g. the Contributor Covenant, CC BY 4.0) keeps its
  attribution. If you are unsure about a source, don't use it; flag it in the PR instead.
