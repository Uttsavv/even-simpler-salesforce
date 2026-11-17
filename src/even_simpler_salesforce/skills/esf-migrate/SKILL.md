---
name: esf-migrate
description: >-
  Migrate Python code from simple-salesforce to even-simpler-salesforce with the esf migrate
  codemod: dry run, JSON report, --write, then resolve every esf-migrate TODO and REVIEW marker
  by its rule id until esf migrate --check passes. Use it when a project imports
  simple_salesforce, or when the user asks to "migrate from simple-salesforce", "replace
  simple_salesforce", "run esf migrate", "fix esf-migrate TODO markers" or "port sf.Lead.create
  calls".
---

# Migrate from simple-salesforce

`esf migrate` rewrites what it can prove is safe. It marks everything else with a comment and
lists it in a JSON report. Your job is to finish the marked sites, one rule at a time.

## Install the codemod

```sh
pip install "even-simpler-salesforce[migrate]"   # adds libcst
esf migrate --help
```

## Run it in this order

1. Start from a clean git working tree, so that `git diff` shows only the codemod's changes.
2. Find the names that hold a client: `grep -rn "Salesforce(" src/` and the attributes that
   store it, such as `self.sf_client`.
3. Do a dry run. It prints a diff and a summary and changes nothing:

    ```sh
    esf migrate src/ tests/ --client-names sf,sf_client,admin_client
    ```

4. Write the report and read it:

    ```sh
    esf migrate src/ tests/ --client-names sf,sf_client --report migrate.json
    ```

5. Apply the rewrites and insert the markers:

    ```sh
    esf migrate src/ tests/ --client-names sf,sf_client --write --report migrate.json
    ```

6. Resolve the markers (below). Run the project's tests after each rule.
7. Check that nothing is left. Exit code `0` means done:

    ```sh
    esf migrate src/ tests/ --check
    ```

`--client-names` matches the last attribute of the client expression, so `sf_client` also
matches `self.sf_client`. It turns most review items into plain rewrites. A second run on
migrated code changes nothing.

| Flag | Effect |
|---|---|
| *(none)* | dry run: diff and summary, no files changed |
| `--write` | apply rewrites and insert markers |
| `--check` | change nothing; exit `1` while a marker or an unconverted pattern remains |
| `--report FILE` | write the JSON report; `-` for stdout |
| `--client-names a,b` | names that hold a client |
| `--rules r1,r2` | run only these rules |
| `--exclude GLOB,...` | skip matching paths |

Exit codes: `0` nothing left, `1` review or TODO items left (or rewrites pending in a dry run),
`2` usage error or a file that doesn't parse.

## Resolve the markers

The codemod puts one comment above each statement that needs a person:

```text
# esf-migrate: TODO[create-return] create() now returns the id (str), not a dict; check how the result is used
res = await sf_client.sobject("Task").create(data)
```

- `TODO[rule]`: the codemod couldn't convert the code. The message says what to do.
- `REVIEW[rule]`: the codemod converted the code. Check that the result is right.

Work through the report grouped by `rule`. Each finding has `file`, `line`, `rule`, `status`,
`message`, `suggestion` and `docs_url`. With `--write`, the positions are in the rewritten file.
Fix the code, then delete the marker. For a false positive, replace the marker with
`# esf-migrate: ignore[<rule>]`.

| Rule | What to do |
|---|---|
| `login` | choose a login flow; `StaticToken(access_token, instance_url)` replaces `session_id=` and `instance=`; there is no username and password login |
| `create-return` | `create()` returns the id as a `str`; replace `r["id"]` with `r`, and `r["success"]` checks with error handling |
| `return-value` | `update()` and `delete()` return `None`; `upsert()` returns `UpsertResult(id, created)` |
| `upsert-path` | pass the external id field and the value as two arguments |
| `query-result` | `query()` returns a `QueryResult`: `.records`, `.total_size`, `.done`, `.cursor` |
| `query-more` | replace the `nextRecordsUrl` loop with `query_iter()`, or pass `page.cursor` to `query_more()` |
| `query-all` | the result is a list of rows; `res["totalSize"]` becomes `len(res)` |
| `format-soql` | `{name}` and `{:like}` placeholders: build the query with `esf.soql()` and `esf.soql_like()` |
| `exceptions` | map to the error classes; `SalesforceMalformedRequest` splits into `RequestError`, `RecordError` and specific classes; `SalesforceExpiredSession` handlers can often go, because the client refreshes |
| `client-internals` | `session_id`, `headers`, `session`, `base_url`: use `sf.request(...)`; for links, use `sf.instance_url` |
| `client-methods` | `quick_search`, `search`, `limits`, `is_sandbox` changed; follow the message |
| `mock-sobject` | replace `MagicMock` chains with `esf.testing.FakeSalesforce` (see the esf-testing skill) |
| `not-a-client` | confirm that the object is a client, or ignore the rule; then pass `--client-names` |
| `unsupported` | Bulk v1, `bulk2`, `toolingexecute`, `mdapi`, `describe_layout`, `headers=`: rewrite by hand with `sf.bulk`, `sf.tooling`, `sf.metadata` or `sf.request` |

The `docs_url` of each finding links to its rule. Read it when the message isn't enough.

## Migrated code

This is what the result looks like:

```python
import os

import even_simpler_salesforce as esf

auth = esf.StaticToken(os.environ["SF_ACCESS_TOKEN"], os.environ["SF_INSTANCE_URL"])
with esf.Salesforce(auth) as sf:
    task_id = sf.sobject("Lead").create({"LastName": "Doe", "Company": "Acme"})  # was res["id"]
    page = sf.query("SELECT Id, Name FROM Lead WHERE Id = {}", task_id)  # was format_soql(...)
    print(page.total_size, page.records[0]["Name"])  # was res["totalSize"], res["records"]
    rows = list(sf.query_iter("SELECT Id FROM Lead"))  # was query_all(q)["records"]
```

## Rules for you

- Don't change behaviour while you migrate. If a change needs a design decision, such as the
  login flow, ask the user.
- Never put values into SOQL with an f-string. Use `{}` placeholders.
- Don't add `--check` to CI until it passes locally.

## More

- Codemod reference: https://raw.githubusercontent.com/Uttsavv/even-simpler-salesforce/main/docs/migrate.md
- Migration guide: https://raw.githubusercontent.com/Uttsavv/even-simpler-salesforce/main/docs/migrating-from-simple-salesforce.md
- Index of all docs pages: https://raw.githubusercontent.com/Uttsavv/even-simpler-salesforce/main/docs/llms.txt
