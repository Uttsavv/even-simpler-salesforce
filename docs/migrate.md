# The `esf migrate` codemod

`esf migrate` rewrites simple-salesforce code to even-simpler-salesforce. It changes only what it
can prove is safe. It marks everything else with a comment and lists it in a report, so you or a
coding agent can finish the job from a checklist.

```sh
pip install --pre "even-simpler-salesforce[migrate]"   # adds libcst

esf migrate src/                # dry run: prints a diff and a summary, changes nothing
esf migrate src/ --write        # applies the rewrites and inserts markers
```

The codemod handles the sync `simple_salesforce.Salesforce` and the async
`simple_salesforce.aio.AsyncSalesforce`. It edits the syntax tree with libcst, so it keeps your
comments and formatting. If you run it again on migrated code, it changes nothing.

## Tell it your client names

Pass the names your code uses for clients. This removes most review items.

```sh
esf migrate src/ --write --client-names sf,sf_client,admin_client
```

The codemod matches the last attribute of the client expression. So `sf_client` also matches
`self.sf_client`.

## Resolve markers

The codemod puts one comment on the line above each statement that needs a person:

```text
# esf-migrate: TODO[create-return] create() now returns the id (str), not a dict; check how the result is used
res = await sf_client.sobject("Task").create(data)
```

- `TODO[...]` means the codemod couldn't convert the code. The message says what to do.
- `REVIEW[...]` means the codemod converted the code, but you should check the result.

To resolve a finding, fix the code and delete the marker.

If a finding is a false positive, put `# esf-migrate: ignore[<rule>]` on the line above the
statement. This turns that rule off for that statement. Ignoring `sobject-attr` also turns off the
rules that build on it:

- `create-return`
- `upsert-path`
- `get-by-custom-id`
- `return-value`
- `not-a-client`

These rules also run as one group with `--rules`. For example, `--rules sobject-attr` also runs
`upsert-path`, and `--rules create-return` also runs `sobject-attr`.

Some sites sit inside a decorator, a default argument, a base class or a `match` subject. For
these, the marker goes above the decorator, `def`, `class` or `match` line. The codemod writes
identical markers on one statement once. A second run never adds a marker that is already there.

## Check in CI

Use `--check` in CI. It changes nothing and exits with code 1 while any marker or unconverted
pattern remains.

```sh
esf migrate src/ --check
```

## Get a report

`--report FILE` writes a JSON checklist of every finding. Use `-` to write it to stdout.

```sh
esf migrate src/ --report migrate.json
```

See [The report](#the-report) for the format.

## Options

| Flag | Effect |
|---|---|
| *(default)* | dry run: prints a unified diff and a summary; changes no files |
| `--write` | applies rewrites and inserts `esf-migrate` markers in place |
| `--report FILE` | writes the [JSON report](#the-report); `-` for stdout |
| `--check` | changes nothing; exit 1 if any marker or unconverted pattern remains, else 0 |
| `--client-names a,b` | names that hold a Salesforce client (the last attribute: `sf_client` matches `self.sf_client`) |
| `--rules r1,r2` | run only these [rules](#rules) |
| `--exclude GLOB,...` | skip paths that match a glob: a name such as `build`, or a path such as `src/gen/*`; `.venv`, `venv`, `site-packages`, `node_modules`, `.git`, `__pycache__`, `.tox` and `.nox` are always skipped |

## Exit codes

| Code | Meaning |
|---|---|
| `0` | nothing left to do |
| `1` | items need review or are TODO; in a dry run, rewrites are still pending |
| `2` | a usage error, or a file that doesn't parse |

If a file doesn't parse, the codemod reports it and still processes the other files.

## The report

The report is one JSON object. The keys come in this order. `esf_version` is the installed library
version.

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

| Key | Meaning |
|---|---|
| `mode` | `"write"` or `"dry-run"` |
| `status` | `rewritten`, `needs-review` or `todo` |
| `line`, `col`, `end_line`, `end_col` | with `--write`, positions in the file after the run, markers included; otherwise positions in the original file |
| `suggestion` | replacement code that the codemod has but can't prove safe |
| `before`, `after`, `suggestion` | string literals over 80 characters and values that look like secrets are replaced |

Values that look like secrets include session ids, bearer tokens and the values of names such as
`password`, `secret`, `token`, `session_id`, `private_key` and `key`. Multi-line strings count as
long string literals.

## Rules

Each finding has a rule id. Rule ids are stable: new rules can come, but ids never change.

| Rule | Status |
|---|---|
| [`sobject-attr`](#sobject-attr) | rewritten |
| [`sobject-getattr`](#sobject-getattr) | rewritten |
| [`upsert-path`](#upsert-path) | rewritten, or TODO |
| [`get-by-custom-id`](#get-by-custom-id) | rewritten |
| [`create-return`](#create-return) | rewritten, or TODO |
| [`return-value`](#return-value) | needs review |
| [`stored-ref`](#stored-ref) | rewritten, or TODO |
| [`mock-sobject`](#mock-sobject) | needs review |
| [`query-all`](#query-all) | rewritten, needs review, or TODO |
| [`query-result`](#query-result) | rewritten, or TODO |
| [`query-more`](#query-more) | TODO |
| [`format-soql`](#format-soql) | rewritten, or TODO |
| [`restful`](#restful) | rewritten, or TODO |
| [`apexecute`](#apexecute) | rewritten, or TODO |
| [`exceptions`](#exceptions) | rewritten, or TODO |
| [`client-internals`](#client-internals) | rewritten, or TODO |
| [`client-methods`](#client-methods) | rewritten, or TODO |
| [`login`](#login) | TODO |
| [`not-a-client`](#not-a-client) | needs review, or TODO |
| [`unsupported`](#unsupported) | TODO |

### `sobject-attr`

`X.Obj.method(...)` becomes `X.sobject("Obj").method(...)`.

```diff
- lead = await sf.Lead.get(lead_id)
+ lead = await sf.sobject("Lead").get(lead_id)
```

The codemod rewrites method references such as `fn = sf.Lead.create` in the same way. A
reference to `get_by_custom_id` becomes `get_by_external_id`. References to `create` and `upsert`
need review, because the codemod can't see their calls.

An assignment to a method, such as `sf.Lead.create = fake` for monkeypatching, gets a TODO.
`sobject()` returns a new object each time, so the patch would not stick.

`describe_layout()` and the `headers=` and `raw_response=` keywords have no equivalent. They get an
[`unsupported`](#unsupported) TODO.

### `sobject-getattr`

`getattr(X, name)` used as an object becomes `X.sobject(name)`. "Used as an object" means your code
calls a per-object method on it, directly or through a variable. This rule needs a confirmed
client.

```diff
- desc = await getattr(sf_client, object_name).describe()
+ desc = await sf_client.sobject(object_name).describe()
```

### `upsert-path`

simple-salesforce takes `"Field/value"` as one string. `upsert()` here takes the external id field
and the value as separate arguments. The codemod splits literals and f-strings on the first
literal `/`.

```diff
- sf.Account.upsert(f"Ext__c/{code}", data)
+ sf.sobject("Account").upsert("Ext__c", str(code), data)
```

If the path is a variable or comes as `record_id=`, the site gets a TODO.

### `get-by-custom-id`

`get_by_custom_id(field, value)` becomes `get_by_external_id(field, value)`.

### `create-return`

`create()` returns the new id as a `str`, not a dict. If the function uses the result only as
`r["id"]` or `r.get("id")`, the codemod changes those uses to `r`.

```diff
  res = await sf.sobject("Lead").create(data)
- return res["id"]
+ return res
```

Any other use of the result gets a TODO. Examples are passing it on, returning it, storing it or
reading `r["success"]`.

### `return-value`

In simple-salesforce, `update()`, `upsert()` and `delete()` return HTTP status codes. Here,
`update()` and `delete()` return `None`, and `upsert()` returns an `UpsertResult(id, created)`. If
your code uses the result, the codemod marks the call for review.

### `stored-ref`

`v = X.Obj` stores an object accessor in a name. If every use of `v` in that scope is a per-object
method call, the codemod rewrites it to `v = X.sobject("Obj")`. Other cases get a TODO, for example
storing it on an attribute or passing it on. This rule needs a confirmed client.

### `mock-sobject`

This rule applies in test files: `test_*.py`, `*_test.py`, `conftest.py` and anything under
`tests/`. It rewrites mock assertions and mock setup on `X.Obj.method` like
[`sobject-attr`](#sobject-attr). Examples are `assert_*`, `return_value`, `side_effect`,
`call_args*`, `call_count`, `await_*`, `mock_calls` and assignments.

These sites need review. `MagicMock().sobject("Lead")` and `MagicMock().sobject("Task")` return the
same child mock. So an assertion no longer proves which object your code touched. Use
[`esf.testing.FakeSalesforce`](testing.md) instead. It stores records per object.

### `query-all`

`X.query_all(q)` returns a dict such as `{"records": [...], "totalSize": ...}`. The replacement is
the list of rows.

| Before | After | Status |
|---|---|---|
| `X.query_all(q)["records"]`, or a variable used only as `res["records"]` or `res.get("records", ...)` | `list(X.query_iter(q))` (sync) or `[r async for r in X.query_iter(q)]` (async) | rewritten |
| `X.query_all_iter(q, ...)` | `X.query_iter(q, ...)`; a positional `include_deleted` becomes the keyword | rewritten |
| any other use of the result | the list | needs review |
| an async `query_all(q)` without `await` | unchanged | TODO |

If the result needs review, check how your code uses it. For example, change `res["totalSize"]` to
`len(res)`.

### `query-result`

`X.query(q)` returns a [`QueryResult`](guides/query.md), not a dict. The codemod rewrites these
keys:

| Before | After |
|---|---|
| `["records"]`, `.get("records", ...)` | `.records` |
| `["totalSize"]` | `.total_size` |
| `["done"]` | `.done` |

Other keys, such as `["nextRecordsUrl"]`, get a TODO. If the codemod can't follow every use of
the result, the `query()` call gets a TODO. Examples are a result that your code returns, passes
on or assigns twice. This rule needs a confirmed client, because `query` is a common method name.

simple-salesforce took `include_deleted` as the second positional argument. Here, positional values
after the query fill `{}` placeholders. So `X.query("SELECT ...", flag)` becomes
`X.query("SELECT ...", include_deleted=flag)`. If the query isn't a literal, the call gets a TODO,
or a review item for `True` and `False`.

### `query-more`

`X.query_more(url, True)` takes a URL. `query_more()` here takes the opaque
[`Cursor`](guides/query.md#cursors) from `page.cursor`. The codemod only adds a TODO marker. Often
the whole loop becomes `query_iter()`.

### `format-soql`

`X.query(format_soql("... {} ...", v))` becomes `X.query("... {} ...", v)`. See
[Values in the query](guides/query.md#values-in-the-query). Only plain positional `{}` placeholders
convert.

These cases get a TODO:

- `{name}` placeholders
- `{:like}` placeholders
- a `format_soql(...)` call built elsewhere; use `esf.soql()` there

### `restful`

`X.restful(path, method=..., params=..., json=...)` becomes
`X.request(method, path, params=..., json=...)`. Other keyword arguments, such as `data=`, get a
TODO.

### `apexecute`

`X.apexecute(path, method=..., data=...)` becomes
`X.request(METHOD, "/services/apexrest/<path>", json=data)`. The codemod rewrites only a literal
path. Other paths get a TODO. The dedicated `sf.apex.*` helpers arrive in 0.2.

### `exceptions`

The codemod replaces simple-salesforce exceptions with the [error classes](errors.md) if the
mapping is one to one. This applies in `except`, `isinstance`, `raise` and the imports. It adds
`import even_simpler_salesforce as esf` once, or reuses an existing alias.

| simple-salesforce | even-simpler-salesforce |
|---|---|
| `SalesforceError` | `esf.SalesforceError` |
| `SalesforceResourceNotFound` | `esf.NotFound` |
| `SalesforceMoreThanOneRecord` | `esf.MultipleMatches` |
| `SalesforceRefusedRequest` | `esf.Forbidden` |
| `SalesforceAuthenticationFailed` | `esf.AuthError` |
| `SalesforceExpiredSession` | TODO: catch `esf.AuthError`, or remove the handler |
| `SalesforceMalformedRequest` | TODO: use `RequestError`, `RecordError` or a specific class such as `InvalidField` |
| `SalesforceGeneralError` | TODO: use `esf.ServerError` for 5xx errors, or `esf.SalesforceError` |

The client refreshes expired sessions itself, so you can often remove a `SalesforceExpiredSession`
handler. `esf.SessionExpired` means a 401 survived a refresh. A dead refresh token raises
`esf.ReauthorizationRequired`. To handle both, catch `esf.AuthError` or catch both classes.

`SalesforceMalformedRequest` splits into `RequestError`, `RecordError` and specific classes such as
`InvalidField` and `ValidationRuleFailed`.

These cases get a TODO:

- A call with arguments, such as `raise SalesforceResourceNotFound(url, 404, name, content)`. The
  error classes here take a message and keyword arguments.
- An attribute of a caught exception that has a new name:

| simple-salesforce | even-simpler-salesforce |
|---|---|
| `e.content` | `e.raw` |
| `e.resource_name` | `e.sobject` |
| `e.url` | `e.path` |

### `client-internals`

| Before | After | Status |
|---|---|---|
| `X.sf_version` | `X.api_version` | rewritten |
| `X.sf_instance`, `X.base_url` | `X.instance_url` for links, `X.request(...)` for calls | TODO |
| `X.session_id`, `X.headers`, `X.session` | `X.request(...)` | TODO |

`X.instance_url` is the instance of the current token. It is `None` before the first call. The
access token isn't public by design. The codemod matches `session_id`, `headers` and `session` only
on a confirmed client.

### `client-methods`

Some client methods changed name or result.

| Before | After | Status |
|---|---|---|
| `X.describe()` | `X.describe_global()` | rewritten |
| `X.quick_search(term)` | `X.parameterized_search(term)`, which returns the list of records | TODO |
| `X.search(sosl)["searchRecords"]` | `X.search(sosl)`, which returns the list of records | TODO |
| `X.limits()["X"]["Remaining"]`, `["Max"]` | `X.limits()["X"].remaining`, `.max` | TODO |
| `X.is_sandbox()` | query `SELECT IsSandbox FROM Organization` | TODO |

`describe()` needs a confirmed client, because `describe` is a common method name.

### `login`

This rule marks each `Salesforce(...)`, `AsyncSalesforce(...)` or `SalesforceLogin(...)` call with
a TODO. Choosing a [login flow](authentication/index.md) is a design decision.

`StaticToken(access_token, instance_url)` replaces `session_id=` and `instance=`. Username and
password login isn't supported.

### `not-a-client`

This rule applies to a site with the right shape whose client the codemod couldn't confirm. See
[How it finds Salesforce calls](#how-it-finds-salesforce-calls).

- Under the fallback, the codemod rewrites the site like its rule and marks it for review.
- Otherwise, if the object name looks like an sObject, the codemod doesn't rewrite the site. It
  adds a TODO and puts the suggested rewrite in `suggestion`.

If you pass `--client-names`, matching sites become plain rewrites.

### `unsupported`

These APIs have no replacement in 0.1, so they get a TODO with a link to the
[migration guide](migrating-from-simple-salesforce.md):

- Bulk API v1, such as `X.bulk.Obj.insert(...)`
- `bulk2`
- `toolingexecute`
- `mdapi`
- `describe_layout()`
- the `headers=` and `raw_response=` keywords

The codemod removes simple-salesforce imports that are no longer used. If code still uses a name
from an import, the import line gets an `unsupported` TODO.

## How it finds Salesforce calls

simple-salesforce reaches objects by attribute, as in `sf.Lead.get(...)`. So the codemod looks for
the shape `<expr>.<Name>.<method>(...)`. `<method>` is one of simple-salesforce's per-object
methods:

`get`, `get_by_custom_id`, `create`, `update`, `upsert`, `delete`, `describe`, `describe_layout`,
`metadata`, `updated`, `deleted`.

Any object works: standard, custom (`__c`), events (`__e`) and custom metadata (`__mdt`). Any client
expression works: `sf`, `self.sf_client`, `deps.admin_client` and so on.

### Confirmed clients

The codemod rewrites a site without a marker if the client is confirmed. A client is confirmed in
either of these cases:

- The last part of the client expression is in `--client-names`.
- In the same module, the name is annotated with, assigned from or returned by a simple-salesforce
  client type or constructor.

Examples of the second case are `sf: AsyncSalesforce`, `sf = Salesforce(...)`, `self.sf = sf` where
`sf` is such a parameter, and a function annotated `-> AsyncSalesforce`.

### Sites without a confirmed client

The codemod handles these sites in one of three ways:

- Fallback: this applies without `--client-names`, in a module that imports simple-salesforce or
  holds a confirmed client. If the object name starts with an upper-case letter, the codemod still
  rewrites the site. It marks the site [`not-a-client`](#not-a-client) for review.
- Looks like an sObject: otherwise, the codemod checks the object name. If the name has a
  custom suffix or is a common standard object, the site gets a `not-a-client` TODO with the
  suggested rewrite. The suffixes are `__c`, `__e`, `__mdt`, `__x`, `__b` and `__kav`. Common
  standard objects include `Account`, `Contact`, `Lead`, `Opportunity`, `Case`, `Task`, `Event` and
  `User`.
- Anything else stays as it is. For example, `config.Settings.get(...)` is not a Salesforce
  call.

The second case also applies with `--client-names` if the name doesn't match. So the codemod either
rewrites or marks every site with a client shape, and `--check` counts it.

In test files, mock chains such as `X.Lead.create.assert_called_once()` on sObject-like names use the
fallback even without the import. Tests rarely import simple-salesforce.

## Limitations

- Detection is per module. The codemod doesn't follow types across imports. So it confirms a
  client passed in from another module only through `--client-names`. Otherwise it treats the
  site as [`not-a-client`](#not-a-client).
- The codemod doesn't evaluate code. Values computed at runtime get TODOs, for example a path in a
  variable or a query built elsewhere.
- At the very end of a file, a blank line followed by a whitespace-only line can collapse into one
  line. This is a limitation of the libcst round trip.
