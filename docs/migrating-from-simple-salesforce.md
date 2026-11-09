# Migrating from simple-salesforce

This page maps the simple-salesforce calls you use most to even-simpler-salesforce. Use it when you
move existing code over. It covers what exists in 0.1 and marks rows for later releases.

```diff
- from simple_salesforce import Salesforce
- sf = Salesforce(instance_url=instance_url, session_id=session_id)
- lead = sf.Lead.get(lead_id)
+ import even_simpler_salesforce as esf
+ sf = esf.Salesforce(esf.StaticToken(session_id, instance_url))
+ lead = sf.sobject("Lead").get(lead_id)
```

even-simpler-salesforce is a new library, not a fork. It has no compatibility layer, but most
changes are mechanical.

!!! tip "Let the codemod do the mechanical part"
    [`esf migrate src/`](migrate.md) rewrites the mechanical rows of these tables. It marks
    everything else with `# esf-migrate: TODO[...]` or `REVIEW[...]` comments and writes a JSON
    report.

## What changes

- There is no username and password login. Use an External Client App with
  [client credentials](authentication/client-credentials.md) or
  [JWT bearer](authentication/jwt-bearer.md). See [Salesforce setup](authentication/salesforce-setup.md).
  To use a token you already have, pass it as a [`StaticToken`](authentication/static-token.md).
- There is no attribute access for objects. `sf.Lead` becomes `sf.sobject("Lead")`.
- `esf.AsyncSalesforce` is the main client. `esf.Salesforce` is its sync twin with the same
  methods.
- The client handles expired sessions. It refreshes once and sends the call again, so you don't
  catch an expired-session error and log in again.
- Each Salesforce error has its own class, such as `InvalidField`, `ValidationRuleFailed` or
  `RowLocked`. Each one has `.error_code`, `.fields` and `.status`. See [Errors](errors.md).
- Results are plain `dict` objects, never `OrderedDict`.
- `esf.soql()` quotes values in SOQL.

## Log in

The client takes a login flow object instead of keyword arguments.

```python
import os

import even_simpler_salesforce as esf

sf = esf.Salesforce(
    esf.ClientCredentials(
        domain=os.environ["SF_DOMAIN"],
        client_id=os.environ["SF_CLIENT_ID"],
        client_secret=os.environ["SF_CLIENT_SECRET"],
    ),
    api_version="59.0",
)
```

| simple-salesforce | even-simpler-salesforce |
|---|---|
| `Salesforce(username=..., password=..., security_token=...)` | not supported; use `esf.Salesforce(esf.ClientCredentials(domain=..., client_id=..., client_secret=...))` or `esf.JwtBearer(...)` |
| `Salesforce(instance_url=..., session_id=...)` | `esf.Salesforce(esf.StaticToken(access_token, instance_url))` |
| `Salesforce(..., domain="test")` | `domain="test"` on the login flow (`JwtBearer`, `WebServerFlow`); `ClientCredentials` needs the My Domain URL |
| `Salesforce(..., version="59.0")` | `esf.Salesforce(auth, api_version="59.0")` (default 68.0, minimum 59.0) |
| `Salesforce(..., session=requests.Session())` | `esf.Salesforce(auth, http_client=httpx.Client(...))` |
| `sf.sf_version` | `sf.api_version` |
| `sf.sf_instance`, `sf.base_url` (to build links or URLs) | `sf.instance_url` for links; `sf.request(...)` for calls |
| `sf.session_id`, `sf.headers`, `sf.session` (to make your own HTTP calls) | `sf.request(...)`; the access token is not public by design |

`sf.instance_url` is read-only. It is the instance of the current token, and `None` before the
first call. `sf.request(...)` applies login, refresh, retries and logging for you.

## Records

Get an object with `sf.sobject("Lead")`. Its methods have the same names as before, except where
the table says otherwise.

```python
leads = sf.sobject("Lead")
lead_id = leads.create({"LastName": "Lovelace", "Company": "Engines"})  # '00Q...'
leads.update(lead_id, {"Status": "Working"})  # returns None
```

| simple-salesforce | even-simpler-salesforce |
|---|---|
| `sf.Lead.get(id)` | `sf.sobject("Lead").get(id)` (also `fields=[...]`, `model=`) |
| `sf.Lead.get_by_custom_id("Ext__c", v)` | `sf.sobject("Lead").get_by_external_id("Ext__c", v)` |
| `sf.Lead.create(data)` returns a dict | `sf.sobject("Lead").create(data)` returns the id (`str`) |
| `sf.Lead.update(id, data)` returns a status code | `sf.sobject("Lead").update(id, data)` returns `None`; failures raise |
| `sf.Lead.upsert("Ext__c/42", data)` | `sf.sobject("Lead").upsert("Ext__c", "42", data)` returns `UpsertResult(id, created)` |
| `sf.Lead.delete(id)` | `sf.sobject("Lead").delete(id)` |
| `sf.Lead.describe()` | `sf.sobject("Lead").describe()` (cached, revalidated) |
| `sf.Lead.metadata()` | `sf.sobject("Lead").metadata()` |
| `sf.describe()` | `sf.describe_global()` (cached) |
| `sf.Lead.updated(start, end)`, `deleted(...)` | `sf.sobject("Lead").updated(start, end)`, `deleted(...)` (timezone-aware datetimes) |
| `headers={"Sforce-Auto-Assign": "FALSE"}` | `assignment_rule_id=False` |

## Query and search

`sf.query()` takes the values for `{}` placeholders and quotes them. It returns a `QueryResult`, not
a dict. To get every row, iterate over `sf.query_iter()`.

```python
page = sf.query("SELECT Id, Name FROM Lead WHERE Status = {}", "Working")
print(page.total_size, page.records)

rows = list(sf.query_iter("SELECT Id FROM Lead"))
```

| simple-salesforce | even-simpler-salesforce |
|---|---|
| `format_soql("... WHERE Id = {}", x)` | `esf.soql("... WHERE Id = {}", x)` |
| `format_soql("... LIKE '{:like}%'", x)` | `esf.soql("... LIKE {}", esf.soql_like(x, "{}%"))` |
| `sf.query(q)` returns a dict | `sf.query(q)` returns `QueryResult(records, total_size, done, cursor)` |
| `sf.query_more(next_url, identifier_is_url=True)` | `sf.query_more(page.cursor)` |
| `sf.query_all(q)` | `list(sf.query_iter(q))`, or `[r async for r in sf.query_iter(q)]` |
| `sf.query_all_iter(q)` | `sf.query_iter(q)` |
| `sf.query_all(q, include_deleted=True)` | `sf.query_iter(q, include_deleted=True)` |
| `sf.search(sosl)` | `sf.search(sosl)` returns the list of records |
| `sf.quick_search(term)` | `sf.parameterized_search(term)` |

## Many records

Use [sObject Collections](guides/collections.md) for writes of many records. Bulk API 2.0 is
planned for 0.3.

| simple-salesforce | even-simpler-salesforce |
|---|---|
| `sf.restful("composite/sobjects", method="POST", json=...)` | `sf.collections.create(rows, sobject="Lead")` (chunked, ordered `BatchResult`) |
| `sf.restful("composite/sobjects", method="PATCH", json=...)` | `sf.collections.update("Lead", rows)` |
| `sf.bulk.Lead.insert(rows)` (Bulk API 1.0) | Bulk API 2.0 is planned for 0.3; Collections handles thousands of rows today |
| `sf.bulk2.Lead.query(q)` | planned for 0.3 (`sf.bulk.query`); use `query_iter(q, page_size=2000)` |

## Other calls

For an endpoint without a helper, use [`sf.request()`](guides/requests.md).

| simple-salesforce | even-simpler-salesforce |
|---|---|
| `sf.restful(path, method=..., json=...)` | `sf.request(method, path, json=...)` |
| `sf.apexecute("Path", method="POST", data=...)` | `sf.request("POST", "/services/apexrest/Path", json=...)`; dedicated helpers planned for 0.2 |
| `sf.toolingexecute(...)` | `sf.request("GET", "tooling/query", params={"q": ...})`; Tooling helpers planned for 0.3 |
| `sf.limits()` | `sf.limits()` returns `dict[str, Limit]`; `sf.usage.snapshot()` gives usage at no cost |
| `sf.is_sandbox()` | not provided; query `SELECT IsSandbox FROM Organization` |
| `sf.mdapi` | Metadata API planned for 0.4 |

## Exceptions

Every class below derives from `esf.SalesforceError`.

| simple-salesforce | even-simpler-salesforce |
|---|---|
| `SalesforceMalformedRequest` (inspect `.content`) | a specific class: `InvalidField`, `MalformedQuery`, `ValidationRuleFailed`, `RequiredFieldMissing`, ... |
| `SalesforceResourceNotFound` | `NotFound` (or `InvalidType` for an unknown sObject type) |
| `SalesforceExpiredSession` | refreshed automatically; `SessionExpired` only for `StaticToken` or a second `401`; `ReauthorizationRequired` when a refresh token is dead |
| `SalesforceRefusedRequest` | `Forbidden` (or `RequestLimitExceeded` for `REQUEST_LIMIT_EXCEEDED`) |
| `SalesforceMoreThanOneRecord` | `MultipleMatches` (`.matches`) |
| `SalesforceGeneralError` | `ServerError`, `UnexpectedResponse`, or the class for the error code |
| `SalesforceAuthenticationFailed` | `AuthError` (what `esf migrate` writes); errors from the OAuth endpoint use its subclass `OAuthError` (`.error`, `.error_description`) |

## Behaviour differences

- The client doesn't send a write again after a timeout. It sends only reads again, so triggers
  never run twice. See [ADR 0001](adr/0001-retry-safety.md).
- Partial failures don't go unnoticed. Collections return per-row results and log
  `records_failed`. They raise `PartialFailure` if a chunk fails.
- The client rejects NaN and Infinity instead of blanking a field. `Decimal` values keep their
  precision.
- The client path-encodes external id values: `/` becomes `%2F` and a space becomes `%20`, never
  `+`.
