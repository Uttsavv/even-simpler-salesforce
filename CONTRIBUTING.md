# Contributing to even-simpler-salesforce

Thanks for helping. This file covers everything needed to land a change: setup, the
checks CI runs, the rules of the codebase, changelog fragments, sign-off and releases.

The design spec is [`big-bang.md`](big-bang.md); architecture decision records live in
[`docs/adr/`](docs/adr/). Read the relevant sections before larger changes, and open an
issue first for anything that adds public API.

## Development setup

You need [uv](https://docs.astral.sh/uv/) and git. uv installs the Python versions it needs.

```sh
git clone https://github.com/Uttsavv/even-simpler-salesforce
cd even-simpler-salesforce
uv sync --all-extras --group dev     # creates .venv with every extra + dev tools
uvx pre-commit install               # ruff + hygiene hooks on every commit
```

Run things inside the environment with `uv run`, e.g. `uv run pytest tests/unit -q`.

## Checks (nox)

All checks are [nox](https://nox.thea.codes/) sessions using uv as the backend. They are
the same commands CI runs.

```sh
uvx nox -db uv                 # default: lint, types, unasync_check, tests (all Pythons)
uvx nox -db uv -s lint         # ruff check + ruff format --check
uvx nox -db uv -s types        # pyright strict
uvx nox -db uv -s tests-3.12   # one Python; add pytest args after --:  -- -k query -x
uvx nox -db uv -s unasync      # regenerate src/even_simpler_salesforce/_sync
uvx nox -db uv -s unasync_check
uvx nox -db uv -s min_deps     # unit + contract tests on httpx 0.27
uvx nox -db uv -s docs         # zensical build --clean -> site/
uvx nox -db uv -s bench        # pytest-benchmark
uvx nox -db uv -s changelog    # preview the next CHANGELOG section
uvx nox -l                     # list sessions
```

Preview the docs locally with
`uvx --from 'zensical>=0.1' --with 'mkdocstrings-python>=2' zensical serve`
(http://localhost:8000).

CI (`.github/workflows/ci.yml`) additionally enforces **≥ 90 % line + branch coverage
overall and 100 % for `core/` and `errors.py`**, runs Python 3.10–3.14 on Linux plus 3.12
on macOS and Windows, and greps fixtures and docs for Salesforce token patterns.

## Codebase rules

These are non-negotiable; review will ask for them.

1. **`_async/` is the source of truth; `_sync/` is generated.** Never edit
   `src/even_simpler_salesforce/_sync/` by hand. Change `_async/`, run
   `nox -s unasync` (it runs `scripts/unasync.py`), and commit both trees. CI fails if
   `_sync/` is stale. `_sync/_compat.py` is the only hand-written file there.
2. **No I/O outside `_async/` / `_sync/`.** `core/`, the `auth/` config classes,
   `errors.py` and `models.py` are pure and shared by both clients.
3. **Public surface = `__all__`** of `even_simpler_salesforce` plus documented public
   submodules (`testing`, `contrib.fastapi`, `contrib.flask`). Everything else is private
   even without an underscore.
4. **Every public change ships with** tests for *both* clients (contract tests are
   parametrised over async/sync), docs (Google-style docstrings + a `docs/` page whose
   code blocks run in `tests/docs`), and a changelog fragment.
5. **Never log secrets.** Tokens, refresh tokens, client secrets and keys are redacted
   in every event.
6. **Recorded fixtures only.** Capture Salesforce responses with the recorder (it
   redacts). Hand-written responses must be marked `synthetic`.
7. **Cite Salesforce.** Behaviour you rely on must be backed by official Salesforce docs;
   put the URL in the test docstring or the ADR.

## Changelog fragments

We use [towncrier](https://towncrier.readthedocs.io/). Every user-visible PR adds one
file to `changelog.d/`:

```
changelog.d/<PR-or-issue-number>.<type>.md
changelog.d/+<short-slug>.<type>.md        # when there is no number yet
```

`<type>` is one of `added`, `changed`, `deprecated`, `removed`, `fixed`, `security`,
`performance`. The content is one or two sentences of Markdown written for users. For
breaking changes (allowed in 0.x minors), use `changed`/`removed` and say how to migrate.

`changelog-check.yml` fails a PR without a fragment. Maintainers can apply the
`skip-changelog` label for changes with no user impact (CI, internal refactors, typos).
Preview with `uvx towncrier build --draft --version X.Y.Z`.

## Developer Certificate of Origin (sign-off)

There is no CLA. You keep the copyright on your contribution and license it under
Apache-2.0 (section 5 of the licence). Every commit must carry a
[DCO](https://developercertificate.org/) sign-off line, which `dco.yml` checks:

```sh
git commit -s -m "fix(query): ..."          # adds Signed-off-by: Your Name <you@example.com>
git rebase --signoff origin/main             # sign off commits you already made
```

## Commit messages

[Conventional Commits](https://www.conventionalcommits.org/) style:
`feat(scope): ...`, `fix(...)`, `docs`, `test`, `refactor`, `perf`, `build`, `ci`,
`chore`. Keep commits small and logical; the PR template has the review checklist.

## Release process (maintainer)

Releases are fully scripted (spec §17.3):

1. On an up-to-date, clean `main`: `uv run scripts/release.py 0.2.0`
   (`--dry-run` first to preview). It validates the version (PEP 440), bumps
   `src/even_simpler_salesforce/_version.py`, runs `towncrier build`, commits with
   sign-off and creates an annotated tag `v0.2.0`. It never pushes.
2. `git push origin HEAD v0.2.0`. The tag triggers `release.yml`: full test matrix,
   lint/types/unasync/benchmarks → `uv build` → PyPI trusted publishing → GitHub Release
   with the changelog section. `docs.yml` republishes `latest` from the new tag.
3. Pre-releases (`0.2.0rc1`) publish to PyPI as pre-releases; their changelog fragments
   are kept for the final release, and the docs stay on `dev`.

Versioning follows SemVer with the 0.x and deprecation policy in spec §17.1.

## Maintainers

Utsav Singh ([@Uttsavv](https://github.com/Uttsavv)) is the **sole maintainer**; no
co-maintainer is planned. To keep that sustainable, everything is automated and
documented: scripted releases, strict CI, Dependabot, and [`AGENTS.md`](AGENTS.md) so AI
agents can do routine maintenance. Please be patient with review times.

## Conduct and security

This project follows the [Code of Conduct](CODE_OF_CONDUCT.md). Report vulnerabilities
privately as described in [SECURITY.md](SECURITY.md), never in public issues.
