---
name: esf-usage
description: >-
  Write application code that calls Salesforce with even-simpler-salesforce (import
  even_simpler_salesforce as esf): pick a login flow, create the async or sync client and close
  it, create, get, update, upsert and delete records, run SOQL with safe {} placeholders, and
  choose between sObject Collections, Composite and Bulk API 2.0. Use it when code imports
  even_simpler_salesforce, AsyncSalesforce or Salesforce, or when the user asks to "call
  Salesforce from Python", "query leads", "upsert accounts", "load records in bulk", "connect
  to Salesforce with client credentials or JWT", or "handle Salesforce errors".
---

# Use even-simpler-salesforce

Write code against the documented public API only: the names in `esf.__all__` and the documented
submodules (`esf.observability`, `esf.testing`, `esf.contrib.fastapi`, `esf.contrib.flask`).
**There is one documented way for each task.** Use it. Don't invent aliases, wrappers or
helpers that the library doesn't have. If you aren't sure that a name exists, check the docs
linked below or `dir(esf)`.

## Pick a login flow

| Login flow | Use it when |
|---|---|
| `esf.ClientCredentials(domain=, client_id=, client_secret=)` | a backend acts as its own integration user |
| `esf.JwtBearer(domain=, client_id=, username=, private_key=)` | a server acts as a named user, with a certificate; needs the `[jwt]` extra |
| `esf.WebServerFlow(domain=, client_id=, redirect_uri=, token_key=)` | people log in in the browser and the app acts as each of them |
| `esf.StaticToken(access_token, instance_url)` | scripts and tests with a token from elsewhere; it can't refresh |

There is no username and password login. Read secrets from the environment or a secret
manager. Never hard-code them, and never log `sf.auth` or a token.

## Create the client

`AsyncSalesforce` and `Salesforce` have the same methods. Use the async client in asyncio apps
and the sync client in scripts, Flask, Django and Celery. Create **one client per process** and
share it, because each client owns a connection pool. Close it when the process stops.

```python
import os

import even_simpler_salesforce as esf

auth = esf.ClientCredentials(
    domain=os.environ["SF_DOMAIN"],  # https://<my-domain>.my.salesforce.com
    client_id=os.environ["SF_CLIENT_ID"],
    client_secret=os.environ["SF_CLIENT_SECRET"],
)


async def lead_names() -> list[str]:
    async with esf.AsyncSalesforce(auth, client_name="acme-sync") as sf:
        return [r["Name"] async for r in sf.query_iter("SELECT Name FROM Lead LIMIT 5")]


with esf.Salesforce(auth) as sync_sf:
    print(sync_sf.count("SELECT COUNT() FROM Lead"))
```

The client gets and refreshes tokens for you. For a long-lived app, create the client at startup
and call `await sf.aclose()` or `sf.close()` at shutdown. For FastAPI and Flask, use
`even_simpler_salesforce.contrib.fastapi` and `even_simpler_salesforce.contrib.flask`. `sf.tagged(caller=..., priority=...)` and `sf.with_auth(auth)`
return child clients that share the pool. Close the root client, not the child.

## Records

```python
sf = esf.AsyncSalesforce(auth)
leads = sf.sobject("Lead")

lead_id = await leads.create({"LastName": "Lovelace", "Company": "Engines"})  # the id, a str
lead = await leads.get(lead_id, fields=["Id", "Name", "Status"])
await leads.update(lead_id, {"Status": "Working"})  # returns None
result = await sf.sobject("Account").upsert("External_Id__c", "A-42", {"Name": "Acme"})
print(result.id, result.created)
await leads.delete(lead_id)
```

## Query with placeholders

Pass values after the SOQL. The client quotes each `{}` value. **Never build SOQL with an
f-string, `%` or `.format()`**: they escape nothing, so a quote in the value breaks the query or
changes it.

```python
email, statuses = "o'brien@example.com", ["Open", "Working"]

page = await sf.query("SELECT Id, Name FROM Lead WHERE Email = {} AND Status IN {}", email, statuses)
print(page.total_size, page.done, [r["Name"] for r in page])

async for row in sf.query_iter("SELECT Id FROM Lead WHERE Status = {}", "Open"):
    pass  # every row; the client fetches the pages

first = await sf.query_first("SELECT Id FROM Lead WHERE Email = {} LIMIT 1", email)  # a row or None
n = await sf.count("SELECT COUNT() FROM Lead WHERE Status IN {}", statuses)
```

| Need | Method |
|---|---|
| one page and a cursor | `sf.query()`, then `sf.query_more(page.cursor)` |
| every row | `sf.query_iter()` |
| exactly one row, else `NotFound` or `MultipleMatches` | `sf.query_one()` |
| the first row or `None` | `sf.query_first()` |
| a number | `sf.count("SELECT COUNT() ...")` |
| a `LIKE` value | `esf.soql_like(value, "%{}%")` as the value |
| a field or object name from input | `esf.soql_identifier(name)` inside `esf.soql(...)` |
| rows for CSV or a DataFrame | `esf.flatten(row)` |

## Many records

| Rows | Use | Why |
|---|---|---|
| one | `sf.sobject(name)` | one request |
| up to some thousands, same operation | `sf.collections` | 200 rows per request, chunks run concurrently, one result per row |
| up to 25 dependent calls that must succeed or fail together | `sf.composite.builder(all_or_none=True)` | later calls use earlier results through `ref()` |
| tens of thousands or more | `sf.bulk.ingest()`, `sf.bulk.query()` | Bulk API 2.0 jobs run in Salesforce |

```python
rows = [{"LastName": f"Imported {i}", "Company": "Acme"} for i in range(450)]
created = await sf.collections.create(rows, sobject="Lead")  # 3 requests
print(len(created.succeeded), len(created.failed))
created.raise_for_errors()  # PartialFailure if any row failed

c = sf.composite.builder(all_or_none=True)
acc = c.create("Account", {"Name": "Acme"})
c.create("Contact", {"LastName": "Doe", "AccountId": acc.ref("id")})
(await c.send()).raise_for_errors()
```

A Collections call doesn't raise for failed rows. Check `.failed` or call `.raise_for_errors()`.

## Errors

Every failure is a subclass of `esf.SalesforceError`. Catch the most specific class you can
handle, then a group such as `esf.RecordError` or `esf.AuthError`.

```python
try:
    await sf.sobject("Lead").create({"LastName": "Curie"})
except esf.RequiredFieldMissing as exc:
    print(exc.fields)  # ('Company',)
except esf.RecordError as exc:  # validation rules, duplicates, row locks, ...
    print(exc.error_code, exc.message)
await sf.aclose()
```

Invalid arguments raise `ValueError` or `TypeError` before the client sends anything. The client
retries reads for you. It doesn't resend a write that timed out, because the write may have
committed. Check the record before you send it again.

## More

Read the page for the task before you write code. Raw Markdown on GitHub:

- Index of all pages: https://raw.githubusercontent.com/Uttsavv/even-simpler-salesforce/main/docs/llms.txt
- Login flows: https://raw.githubusercontent.com/Uttsavv/even-simpler-salesforce/main/docs/authentication/index.md
- Records: https://raw.githubusercontent.com/Uttsavv/even-simpler-salesforce/main/docs/guides/records.md
- Query: https://raw.githubusercontent.com/Uttsavv/even-simpler-salesforce/main/docs/guides/query.md
- Collections: https://raw.githubusercontent.com/Uttsavv/even-simpler-salesforce/main/docs/guides/collections.md
- Composite: https://raw.githubusercontent.com/Uttsavv/even-simpler-salesforce/main/docs/guides/composite.md
- Bulk API 2.0: https://raw.githubusercontent.com/Uttsavv/even-simpler-salesforce/main/docs/guides/bulk.md
- Errors: https://raw.githubusercontent.com/Uttsavv/even-simpler-salesforce/main/docs/errors.md

The docs on `main` can be newer than the installed library. If a name in the docs is missing,
check `esf.__version__` and the changelog.
