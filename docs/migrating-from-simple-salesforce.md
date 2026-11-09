# Migrating from simple-salesforce

even-simpler-salesforce is a new library, not a fork, and there is no compatibility layer. Most
code moves over with mechanical changes. This page maps the calls people use most. It covers
what exists in **0.1**; rows for later releases are marked.

!!! tip "Let the codemod do the mechanical part"
    [`esf migrate src/`](migrate.md) rewrites the mechanical rows of these tables and marks
    everything else with `# esf-migrate: TODO[...]` / `REVIEW[...]` comments and a JSON report.

## The main differences

- **No username/password login.** Use an External Client App with
  [client credentials](authentication/client-credentials.md) or [JWT bearer](authentication/jwt-bearer.md)
  ([setup](authentication/salesforce-setup.md)), or pass a token you already have as a
  [`StaticToken`](authentication/static-token.md).
- **No attribute magic.** `sf.Lead` becomes `sf.sobject("Lead")`.
- **Async first.** `esf.AsyncSalesforce` is the main client. `esf.Salesforce` is its sync twin
  with the same methods.
- **Expired sessions are handled for you.** The client refreshes once and resends the call, so
  you don't need to catch an expired-session error and log in again.
- **Specific exceptions.** Instead of one "malformed request" error whose content you inspect,
  you get `InvalidField`, `ValidationRuleFailed`, `RowLocked`, ..., each with `.error_code`,
  `.fields` and `.status` ([Errors](errors.md)).
- **Plain `dict` results**, never `OrderedDict`.
- **SOQL values are quoted** with `esf.soql()`.

## Connecting

| simple-salesforce | even-simpler-salesforce |
|---|---|
| `Salesforce(username=..., password=..., security_token=...)` | not supported; use `esf.Salesforce(esf.ClientCredentials(domain=..., client_id=..., client_secret=...))` or `esf.JwtBearer(...)` |
| `Salesforce(instance_url=..., session_id=...)` | `esf.Salesforce(esf.StaticToken(access_token, instance_url))` |
| `Salesforce(..., domain="test")` | `domain="test"` on the flow (`JwtBearer`, `WebServerFlow`); `ClientCredentials` needs the My Domain URL |
| `Salesforce(..., version="59.0")` | `esf.Salesforce(auth, api_version="59.0")` (default 68.0, minimum 59.0) |
| `Salesforce(..., session=requests.Session())` | `esf.Salesforce(auth, http_client=httpx.Client(...))` |
| `sf.sf_version` | `sf.api_version` |
| `sf.sf_instance` / `sf.base_url` (to build links or URLs) | `sf.instance_url` (read-only: the instance of the current token, `None` before the first call); make calls with `sf.request(...)` |
| `sf.session_id`, `sf.headers`, `sf.session` (to make your own HTTP calls) | `sf.request(...)`: auth, refresh, retries and logging are applied for you; the access token is not public by design |

## Records

| simple-salesforce | even-simpler-salesforce |
|---|---|
| `sf.Lead.get(id)` | `sf.sobject("Lead").get(id)` (also `fields=[...]`, `model=`) |
| `sf.Lead.get_by_custom_id("Ext__c", v)` | `sf.sobject("Lead").get_by_external_id("Ext__c", v)` |
| `sf.Lead.create(data)` returns a dict | `sf.sobject("Lead").create(data)` returns the **id** (`str`) |
| `sf.Lead.update(id, data)` returns a status code | `sf.sobject("Lead").update(id, data)` returns `None`; failures raise |
| `sf.Lead.upsert("Ext__c/42", data)` | `sf.sobject("Lead").upsert("Ext__c", "42", data)` returns `UpsertResult(id, created)` |
| `sf.Lead.delete(id)` | `sf.sobject("Lead").delete(id)` |
| `sf.Lead.describe()` | `sf.sobject("Lead").describe()` (cached, revalidated) |
| `sf.Lead.metadata()` | `sf.sobject("Lead").metadata()` |
| `sf.describe()` | `sf.describe_global()` (cached) |
| `sf.Lead.updated(start, end)` / `deleted(...)` | `sf.sobject("Lead").updated(start, end)` / `deleted(...)` (timezone-aware datetimes) |
| `headers={"Sforce-Auto-Assign": "FALSE"}` | `assignment_rule_id=False` |

## Query and search

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

| simple-salesforce | even-simpler-salesforce |
|---|---|
| `sf.restful("composite/sobjects", method="POST", json=...)` | `sf.collections.create(rows, sobject="Lead")` (chunked, ordered `BatchResult`) |
| `sf.restful("composite/sobjects", method="PATCH", json=...)` | `sf.collections.update("Lead", rows)` |
| `sf.bulk.Lead.insert(rows)` (Bulk API 1.0) | *Bulk API 2.0 is planned for 0.3*; Collections handles thousands of rows today |
| `sf.bulk2.Lead.query(q)` | *planned for 0.3* (`sf.bulk.query`); use `query_iter(q, page_size=2000)` |

## Everything else

| simple-salesforce | even-simpler-salesforce |
|---|---|
| `sf.restful(path, method=..., json=...)` | `sf.request(method, path, json=...)` ([Raw requests](guides/requests.md)) |
| `sf.apexecute("Path", method="POST", data=...)` | `sf.request("POST", "/services/apexrest/Path", json=...)`; dedicated helpers planned for 0.2 |
| `sf.toolingexecute(...)` | `sf.request("GET", "tooling/query", params={"q": ...})`; Tooling helpers planned for 0.3 |
| `sf.limits()` | `sf.limits()` returns `dict[str, Limit]`; `sf.usage.snapshot()` gives usage at no cost |
| `sf.is_sandbox()` | not provided; query `SELECT IsSandbox FROM Organization` |
| `sf.mdapi` | *Metadata API planned for 0.4* |

## Exceptions

| simple-salesforce | even-simpler-salesforce |
|---|---|
| `SalesforceMalformedRequest` (inspect `.content`) | a specific class: `InvalidField`, `MalformedQuery`, `ValidationRuleFailed`, `RequiredFieldMissing`, ... |
| `SalesforceResourceNotFound` | `NotFound` (or `InvalidType` for an unknown sObject type) |
| `SalesforceExpiredSession` | refreshed automatically; `SessionExpired` only for `StaticToken` or a second `401`; `ReauthorizationRequired` when a refresh token is dead |
| `SalesforceRefusedRequest` | `Forbidden` (or `RequestLimitExceeded` for `REQUEST_LIMIT_EXCEEDED`) |
| `SalesforceMoreThanOneRecord` | `MultipleMatches` (`.matches`) |
| `SalesforceGeneralError` | `ServerError`, `UnexpectedResponse`, or the class for the error code |
| `SalesforceAuthenticationFailed` | `AuthError` (what `esf migrate` writes); errors from the OAuth endpoint are its subclass `OAuthError` (`.error`, `.error_description`) |

Every class above derives from `esf.SalesforceError`.

## Behaviour you may rely on today

- **Writes are not resent after a timeout.** Some libraries retry every request. This one
  resends only reads, so triggers never run twice ([ADR 0001](adr/0001-retry-safety.md)).
- **Partial failures can't go unnoticed.** Collections return per-row results, log
  `records_failed`, and raise `PartialFailure` when a chunk fails.
- **NaN and Infinity are rejected** instead of silently blanking a field, and `Decimal` values
  keep their precision.
- **External id values are path-encoded** (`/` → `%2F`, space → `%20`), never `+`.
