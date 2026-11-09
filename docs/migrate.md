# `esf migrate`: rewrite simple-salesforce code

`esf migrate` rewrites code written for simple-salesforce (sync `simple_salesforce.Salesforce`
and async `simple_salesforce.aio.AsyncSalesforce`) to even-simpler-salesforce. It changes only what
it can prove is safe. Everything else it finds is **marked** with a comment and listed in a report,
so you (or a coding agent) can finish the job from a checklist.

```sh
pip install "even-simpler-salesforce[migrate]"   # adds libcst

esf migrate src/                          # dry run: prints a diff and a summary, changes nothing
esf migrate src/ --write                  # apply rewrites and insert markers
esf migrate src/ --check                  # CI: exit 1 while anything is left to do
esf migrate src/ --report migrate.json    # machine-readable checklist
```

It keeps comments and formatting (it edits the syntax tree with libcst), and a second run over
migrated code changes nothing.

## Options

| Flag | Effect |
|---|---|
| *(default)* | dry run: prints a unified diff and a summary; changes no files |
| `--write` | applies rewrites and inserts `esf-migrate` markers in place |
| `--report FILE` | writes the [JSON report](#the-report); `-` for stdout |
| `--check` | changes nothing; exit 1 if any marker or unconverted pattern remains, else 0 |
| `--client-names a,b` | names that hold a Salesforce client (the last attribute: `sf_client` matches `self.sf_client`) |
| `--rules r1,r2` | run only these [rules](#rules) |
| `--exclude GLOB,...` | skip paths; `.venv`, `venv`, `site-packages`, `node_modules`, `.git`, `__pycache__`, `.tox` and `.nox` are always skipped |

Exit codes: **0** nothing left to do; **1** items need review or are TODO, or (in a dry run)
rewrites are still pending; **2** a usage error or a file that doesn't parse (it is reported and
the other files are still processed).

## How it finds Salesforce calls

simple-salesforce reaches objects by attribute (`sf.Lead.get(...)`), so the tool looks for the
**shape** `<expr>.<Name>.<method>(...)` where `<method>` is one of simple-salesforce's per-object
methods (`get`, `get_by_custom_id`, `create`, `update`, `upsert`, `delete`, `describe`,
`describe_layout`, `metadata`, `updated`, `deleted`). Any object works: standard, custom
(`__c`), events (`__e`), custom metadata (`__mdt`). Any client expression works: `sf`,
`self.sf_client`, `deps.admin_client`, ...

A site is rewritten as a sure thing when the client is **confirmed**:

1. the last part of the client expression is in `--client-names`, or
2. in the same module, the name is annotated with, assigned from, or returned by a
   simple-salesforce client type or constructor (`sf: AsyncSalesforce`, `sf = Salesforce(...)`,
   `self.sf = sf` where `sf` is such a parameter, a function annotated `-> AsyncSalesforce`).

Sites that aren't confirmed are handled like this:

- **Fallback.** Without `--client-names`, in a module that imports simple-salesforce or holds a
  confirmed client, a shaped site whose object name starts with an upper-case letter is still
  rewritten, but marked [`not-a-client`](#not-a-client) for review.
- **Looks like an sObject.** Otherwise, if the object name has a custom suffix (`__c`, `__e`,
  `__mdt`, `__x`, `__b`, `__kav`) or is a common standard object (`Account`, `Contact`, `Lead`,
  `Opportunity`, `Case`, `Task`, `Event`, `User`, ...), the site is not rewritten but gets a
  `not-a-client` TODO with the suggested rewrite. This also applies with `--client-names` when the
  name doesn't match, so every client-shaped site is either rewritten or marked, and `--check`
  counts it.
- **Anything else** is left alone (`config.Settings.get(...)` is not a Salesforce call).

In test files, mock chains (`X.Lead.create.assert_called_once()`) on sObject-looking names use the
fallback even without the import (tests rarely import simple-salesforce).

**Tip:** pass `--client-names` with the names your code base uses for clients; it removes most
review items.

## Markers

Each finding that needs a person gets one comment on the line above its statement:

```text
# esf-migrate: TODO[create-return] create() now returns the id (str), not a dict; check how the result is used
res = await sf_client.sobject("Task").create(data)
```

- `TODO[...]`: the tool couldn't convert this; the message says what to do.
- `REVIEW[...]`: the tool converted it, but you should check the result.
- Resolve a finding by fixing the code and deleting the marker.
- `# esf-migrate: ignore[<rule>]` on the line above a statement turns that rule off for it (for
  a real false positive). Ignoring `sobject-attr` also turns off the rules that build on it
  (`create-return`, `upsert-path`, `get-by-custom-id`, `return-value`, `not-a-client`); likewise
  `--rules create-return` implies `sobject-attr`.
- A site inside a decorator, a default argument, a base class or a `match` subject gets its marker
  above the decorator, `def`, `class` or `match` line.
- Identical markers on one statement are written once, and reruns never add a marker that is
  already there.

## The report

`--report FILE` writes one JSON object (keys in this order; `esf_version` is the installed
library version):

```json
{
  "version": 1,
  "esf_version": "0.1.0rc1",
  "mode": "write",
  "summary": {"files": 18, "rewritten": 47, "needs_review": 9, "todo": 6, "errors": 0},
  "findings": [
    {
      "file": "app/repository.py",
      "line": 42, "col": 15, "end_line": 42, "end_col": 58,
      "rule": "sobject-attr",
      "status": "rewritten",
      "before": "await self.task_client.Task.update(task_id, body)",
      "after": "await self.task_client.sobject(\"Task\").update(task_id, body)",
      "message": "object accessed through sobject()",
      "suggestion": null,
      "docs_url": "https://uttsavv.github.io/even-simpler-salesforce/latest/migrate/#sobject-attr"
    }
  ],
  "errors": []
}
```

- `status` is `rewritten`, `needs-review` or `todo`.
- With `--write`, line numbers refer to the file **after** the run (markers included); in a dry
  run and with `--check` they refer to the original file. `mode` (`"write"` or `"dry-run"`) says
  which.
- `suggestion` holds replacement code the tool has but can't prove safe.
- String literals longer than 80 characters (multi-line strings included) and anything that looks
  like a secret (session ids, bearer tokens, values of `password`, `secret`, `token`,
  `session_id`, `private_key`, `key` and similar names) are replaced in
  `before`/`after`/`suggestion`.

## Rules

Rule ids are stable: new rules may be added, ids are never renamed.

### `sobject-attr`

`X.Obj.method(...)` → `X.sobject("Obj").method(...)`. **Status:** rewritten.

```diff
- lead = await sf.Lead.get(lead_id)
+ lead = await sf.sobject("Lead").get(lead_id)
```

Method references (`fn = sf.Lead.create`) are rewritten the same way. Assigning to one
(`sf.Lead.create = fake`, monkeypatching) is a TODO: `sobject()` returns a new object each time,
so the patch would not stick. `describe_layout()` and the `headers=` / `raw_response=` keywords
have no equivalent and get an [`unsupported`](#unsupported) TODO.

### `sobject-getattr`

`getattr(X, name)` used as an object (a per-object method is called on it, directly or through
a variable) → `X.sobject(name)`. Needs a confirmed client. **Status:** rewritten.

```diff
- desc = await getattr(sf_client, object_name).describe()
+ desc = await sf_client.sobject(object_name).describe()
```

### `upsert-path`

simple-salesforce took `"Field/value"` as one string; `upsert()` here takes the external id field
and value separately. Literals and f-strings are split on the first literal `/`. **Status:**
rewritten; TODO when the path is a variable or passed as `record_id=`.

```diff
- sf.Account.upsert(f"Ext__c/{code}", data)
+ sf.sobject("Account").upsert("Ext__c", str(code), data)
```

### `get-by-custom-id`

`get_by_custom_id(field, value)` → `get_by_external_id(field, value)`. **Status:** rewritten.

### `create-return`

`create()` returns the new id (`str`), not a dict. When the result is only used as `r["id"]` /
`r.get("id")` in the same function, those become `r` (rewritten). Any other use (passed on,
returned, `r["success"]`, stored) gets a TODO.

```diff
  res = await sf.sobject("Lead").create(data)
- return res["id"]
+ return res
```

### `return-value`

`update()`, `upsert()` and `delete()` returned HTTP status codes in simple-salesforce. Here
`update()`/`delete()` return `None` and `upsert()` returns an `UpsertResult(id, created)`. When
the result is used, the call is marked. **Status:** needs review.

### `stored-ref`

`v = X.Obj` (an object accessor stored in a name) → `v = X.sobject("Obj")` when every use of `v`
in that scope is a per-object method call. Otherwise (stored on an attribute, passed on) a TODO.
Needs a confirmed client.

### `mock-sobject`

In test files (`test_*.py`, `*_test.py`, `conftest.py`, anything under `tests/`), mock
assertions and setup on `X.Obj.method` (`assert_*`, `return_value`, `side_effect`, `call_args*`,
`call_count`, `await_*`, `mock_calls`, assignments) are rewritten like
[`sobject-attr`](#sobject-attr). **Status:** needs review, because
`MagicMock().sobject("Lead")` and `MagicMock().sobject("Task")` return the *same* child mock, so
an assertion no longer proves which object was touched. Prefer
[`esf.testing.FakeSalesforce`](testing.md), which stores records per object.

### `query-all`

`X.query_all(q)` returned a dict (`{"records": [...], "totalSize": ...}`); the replacement is
the list of rows:

- `X.query_all(q)["records"]`, or a variable only used as `res["records"]` /
  `res.get("records", ...)` → `list(X.query_iter(q))` (sync) or
  `[r async for r in X.query_iter(q)]` (async). **Status:** rewritten.
- `X.query_all_iter(q, ...)` → `X.query_iter(q, ...)`; a positional `include_deleted` becomes
  the keyword. **Status:** rewritten.
- Any other use of the result: rewritten to the list, **needs review** (for example, change
  `res["totalSize"]` to `len(res)`).
- An async `query_all(q)` that isn't awaited: TODO.

### `query-result`

`X.query(q)` returns a [`QueryResult`](guides/query.md), not a dict:
`["records"]` / `.get("records", ...)` → `.records`, `["totalSize"]` → `.total_size`,
`["done"]` → `.done`. Other keys (e.g. `["nextRecordsUrl"]`) get a TODO. Needs a confirmed client
(`query` is a common method name).

### `query-more`

`X.query_more(url, True)` takes a URL; `query_more()` here takes the opaque
[`Cursor`](guides/query.md#cursors) from `page.cursor`. Marker only (**TODO**); often the whole
loop becomes `query_iter()`.

### `format-soql`

`X.query(format_soql("... {} ...", v))` → `X.query("... {} ...", v)`
([values in the query](guides/query.md#values-in-the-query)). Only plain positional `{}`
placeholders convert. **Status:** rewritten; TODO for `{name}`, `{:like}` or a
`format_soql(...)` built elsewhere (use `esf.soql()` there).

### `restful`

`X.restful(path, method=..., params=..., json=...)` → `X.request(method, path, params=...,
json=...)`. **Status:** rewritten; TODO for other keyword arguments (e.g. `data=`).

### `apexecute`

`X.apexecute(path, method=..., data=...)` → `X.request(METHOD, "/services/apexrest/<path>",
json=data)`. (The dedicated `sf.apex.*` helpers arrive in 0.2.) **Status:** rewritten for a
literal path; TODO otherwise.

### `exceptions`

simple-salesforce exceptions in `except`, `isinstance`, `raise` and the imports are replaced by
the [error classes](errors.md) when the mapping is 1:1, and `import even_simpler_salesforce as esf`
is added once (an existing alias is reused):

| simple-salesforce | even-simpler-salesforce |
|---|---|
| `SalesforceError` | `esf.SalesforceError` |
| `SalesforceResourceNotFound` | `esf.NotFound` |
| `SalesforceMoreThanOneRecord` | `esf.MultipleMatches` |
| `SalesforceRefusedRequest` | `esf.Forbidden` |
| `SalesforceAuthenticationFailed` | `esf.AuthError` |
| `SalesforceExpiredSession` | TODO: refresh is automatic; `esf.SessionExpired` means a 401 survived a refresh and a dead refresh token raises `esf.ReauthorizationRequired`. Catch `esf.AuthError` or both; often the handler can go |
| `SalesforceMalformedRequest` | TODO: split into `RequestError` / `RecordError` and specific classes (`InvalidField`, `ValidationRuleFailed`, ...) |
| `SalesforceGeneralError` | TODO: `esf.ServerError` (5xx) or `esf.SalesforceError` |

### `client-internals`

- `X.sf_version` → `X.api_version` (rewritten).
- `X.sf_instance`, `X.base_url` → `X.instance_url` for links (the instance of the current
  token; `None` before the first call) and `X.request(...)` for calls. TODO.
- `X.session_id`, `X.headers`, `X.session` → `X.request(...)`; the access token isn't public by
  design. TODO. (`session_id`, `headers` and `session` are only matched on a confirmed client.)

### `login`

Constructing `Salesforce(...)`, `AsyncSalesforce(...)` or `SalesforceLogin(...)`: choosing a
[login flow](authentication/index.md) is a design decision, so this is a TODO.
`StaticToken(access_token, instance_url)` replaces `session_id=` / `instance=`;
username/password login isn't supported.

### `not-a-client`

A shaped site whose client couldn't be confirmed ([how it finds calls](#how-it-finds-salesforce-calls)):

- under the fallback (no `--client-names`, the module imports simple-salesforce or holds a
  confirmed client, upper-case object name): rewritten like its rule and marked for **review**;
- otherwise, when the object name looks like an sObject: not rewritten; a **TODO** with the
  suggested rewrite in `suggestion`.

Passing `--client-names` turns matching sites into plain rewrites.

### `unsupported`

APIs without a 0.1 replacement: Bulk API v1 (`X.bulk.Obj.insert(...)`), `bulk2`,
`toolingexecute`, `mdapi`, `describe_layout()`, the `headers=` / `raw_response=` keywords. TODO
with a pointer to the [migration guide](migrating-from-simple-salesforce.md).

A simple-salesforce import that is still needed after the run (some name from it is still used)
gets an `unsupported` TODO on the import line; imports that are no longer used are removed.

## Limits

- Detection is per module: types are not followed across imports, so a client passed in from
  another module is confirmed only by `--client-names` (or treated as
  [`not-a-client`](#not-a-client)).
- It doesn't evaluate code: values computed at runtime (a path in a variable, a query built
  elsewhere) get TODOs.
- A blank line followed by a whitespace-only line at the very end of a file can collapse into
  one (a libcst round-trip limitation).
