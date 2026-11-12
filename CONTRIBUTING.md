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

## Writing documentation

The docs should read like a calm colleague showing you how something works: code first,
short sentences, no detours. Every page follows the same shape.

**Page shape**

1. `# Title`: a noun or a task, two to four words (`Records`, `Token stores`).
2. One or two sentences: what this is and when you use it. No history, no caveats.
3. A minimal code example that works when copied.
4. `## ` sections, one per task or option, ordered from most to least common. Each one is a
   short paragraph followed by an example.
5. Reference tables (options, methods, exceptions) after the examples that use them, not
   before.
6. Edge cases, limits and internals last, under headings such as `## Limitations` or
   `## How it works`.

**Tutorial pages**

A tutorial builds one small app from start to finish. It follows the page shape loosely:

- Say what the reader builds and how long it takes. Link to the guides for setup steps, such as
  login, instead of repeating them.
- Mark each step the reader does with a bold line that starts with 👉 and a verb:
  `👉 **Add a route that gets one lead**`. The text between the steps explains what happened.
- After a step, show what the reader sees: a command and its output.
- Show a change to code from an earlier step as a `diff` block, so `tests/docs` doesn't run it twice.
- End with `## Recap`, a list of what the reader can now do, and `## Next steps`.

**Sentences**

These rules take the parts of ASD-STE100 Simplified Technical English that suit developer
docs. They keep the text easy to scan and easy to translate.

- Address the reader as "you". Use simple tenses (present, past, future) and the active
  voice. Write "the client refreshes the token", not "the token is refreshed" or "has been
  refreshed".
- Keep sentences short: at most 20 words for an instruction, 25 for a description.
- One instruction or one idea per sentence. Start an instruction with the verb: "Create one
  client per process."
- Put the condition first: "If Salesforce returns `401`, the client refreshes once."
- Say what to do, then why if it matters: "Create one client per process. Each client owns a
  connection pool."
- At most six sentences in a paragraph, usually one to three. One topic per paragraph, stated
  in its first sentence.
- More than three related items go in a list or a table, not in a sentence.
- Use one term for one thing on every page: always "client", "login flow", "token store",
  "record". No synonyms for variety.
- Write "before you send the request", not "before sending the request".
- At most three nouns in a row. Write "the cache for describe results", not "describe result
  cache entry".
- No parentheses for a second idea: write a second sentence. No em dashes. No slash between
  words in prose ("async and sync", not "async/sync"); paths and code are fine.
- Prefer plain words:

  | Don't write | Write |
  |---|---|
  | utilize, leverage | use |
  | in order to | to |
  | prior to, subsequent to | before, after |
  | in the event that | if |
  | ensure | make sure |
  | is capable of | can |
  | attempt | try |
  | obtain, retrieve | get |
  | additional | more |
  | due to the fact that | because |

- Drop "simply", "just", "easily", "note that", "please" and "basically".
- Bold is for the one word the reader must not miss in a paragraph. Most paragraphs need none.

**Headings**

- Use `##` and `###` only. A page needing `####` should be split.
- Short and parallel: `## Create a record`, `## Update a record`; or the option name in
  code, `` ## `fields` ``.
- No numbering, no trailing punctuation, no question headings.

**Code**

- Every fenced `python` block runs in `tests/docs`, so keep examples real and minimal.
- Show the async client. Add an `=== "Async"` / `=== "Sync"` pair only when the two differ in
  more than `await`.
- Comments explain *why*, or show a result: `# '00Q...'`.

**Admonitions**

- At most one or two per page. `!!! tip` for a shortcut, `!!! warning` for something that
  loses data, leaks a secret or costs money. Never for page intros, status banners or long
  text.
- Put a warning before the step it applies to. Start it with the condition or the command,
  and say what goes wrong: "Don't log `sf.auth.token`. Anyone with the token can act as the
  user."

**Tables**

- For reference data the reader scans: options, defaults, error classes. Not for prose.
- Columns: name, then default or type, then a short description without a full stop.

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
