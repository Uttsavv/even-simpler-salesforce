# Errors

Every error from Salesforce or the network is a subclass of `esf.SalesforceError`. This is true
for REST, OAuth and Collections. Catch the most specific class you can handle, then catch
`SalesforceError` for the rest.

```python
import even_simpler_salesforce as esf

auth = esf.ClientCredentials(
    domain="https://acme.my.salesforce.com", client_id="3MVG9...", client_secret="..."
)
sf = esf.AsyncSalesforce(auth)

try:
    await sf.sobject("Lead").create({"LastName": "Franklin", "Company": "KCL", "Country": "UK"})
except esf.ValidationRuleFailed as exc:
    print(exc.error_code)  # FIELD_CUSTOM_VALIDATION_EXCEPTION
    print(exc.message)  # [ADDR_REQUIRED] Street is required when Country is set
    print(exc.fields)  # ('Street',)
    print(exc.status, exc.operation, exc.sobject)  # 400 sobject.create Lead
except esf.SalesforceError as exc:
    print("other Salesforce error:", exc)
```

Every error has the same [attributes](#attributes), so one `except` clause can log any of them.
`str(exc)` gives `"ERROR_CODE: message (operation, sobject, HTTP status)"`, then a
[hint](#hints-and-docs-links) if there is one, then a link to the error's entry on this page.

Invalid arguments are different. They raise `ValueError` or `TypeError` when you create the
object, before the client sends a request.

## Hints and docs links

Some errors have a common cause with a known fix. For these, `exc.hint` says what to check.
Every error also has `exc.docs_url`, a link to its entry under [Troubleshooting](#troubleshooting).

```python
await sf.sobject("Lead").describe()  # the hint reads this cached describe

try:
    await sf.query("SELECT Id, Scores__c FROM Lead")
except esf.InvalidField as exc:
    print(exc.hint)  # did you mean 'Score__c'?
    print(exc.docs_url)  # https://uttsavv.github.io/even-simpler-salesforce/latest/errors/#invalidfield
```

The client builds a hint only from data it already has. It never sends a request to build one.
A hint never holds record values or secrets. These errors get a hint:

| Error | Hint |
|---|---|
| `InvalidField` for a column that doesn't exist | the closest field names, if the describe for that sObject is in the cache |
| `SessionExpired` after the client refreshed the token | check the `api` OAuth scope and the session policies of the connected app |
| `Forbidden` with `API_DISABLED_FOR_ORG` | the org edition and the "API Enabled" user permission |
| `RequestLimitExceeded` | check `sf.usage.snapshot()` and set a [budget](usage-and-budgets.md) |
| `MalformedQuery` for a query with an unescaped single quote | pass values as arguments, such as `sf.query("... WHERE Name = {}", name)` |

`str(exc)` shows both on their own lines:

```console
INVALID_FIELD: No such column 'Scores__c' on entity 'Lead'. (query, Lead, HTTP 400)
  hint: did you mean 'Score__c'?
  docs: https://uttsavv.github.io/even-simpler-salesforce/latest/errors/#invalidfield
```

## Catch a group of errors

The classes form a tree. To handle related errors the same way, catch their group, such as
`RecordError`, `RequestError` or `AuthError`:

```python
try:
    await sf.sobject("Lead").create({"LastName": "Curie"})
except esf.RecordError as exc:  # RequiredFieldMissing, ValidationRuleFailed, RowLocked, ...
    print(type(exc).__name__, exc.fields)  # RequiredFieldMissing ('Company',)
```

The [hierarchy](#hierarchy) lists every class and the Salesforce error codes behind it.

## Retries

The client retries transient failures for you. To change how often it tries and how long it
waits, pass `retry=esf.RetryPolicy(...)`:

```python
patient = esf.AsyncSalesforce(auth, retry=esf.RetryPolicy(max_attempts=5, max_delay=20))
no_retries = esf.AsyncSalesforce(auth, retry=esf.RetryPolicy(max_attempts=1))
await patient.aclose()
await no_retries.aclose()
```

| Field | Default | Description |
|---|---|---|
| `max_attempts` | `3` | total attempts per call, including the first (minimum 1) |
| `base_delay` | `0.25` | first backoff delay in seconds, doubled for each further retry |
| `max_delay` | `8.0` | cap on one backoff delay |
| `jitter` | `True` | full jitter: wait a random time between 0 and the delay |
| `respect_retry_after` | `True` | wait for `Retry-After` when Salesforce sends it |
| `max_retry_after` | `30.0` | if `Retry-After` is longer than this, raise instead of waiting (`exc.retry_after` says how long) |

If the last attempt fails, the client raises its error. Each retry emits an `sf.retry` event.
The final `sf.request` event lists `attempts` and `retry_reasons`. [Logging](logging.md)
describes both events.

### Which failures are retried

!!! warning "If a write times out, the client doesn't send it again"
    The write may already have committed. A second send can run triggers, flows and e-mails
    twice, or turn the success into a false `Conflict` or `NotFound`. So the client raises
    `RequestTimeout` or `ServerError`, and you decide. If you know a call is safe to repeat,
    pass `sf.request(..., idempotent=True)`.

The client always resends reads. It resends writes only if Salesforce did not run them.
[ADR 0001](adr/0001-retry-safety.md) explains the rules.

| Failure | Reads (GET/HEAD, POST reads such as `retrieve`, `parameterized_search`) | Writes, upserts, code-running GETs (Apex REST, `executeAnonymous`) |
|---|---|---|
| connect error: never sent (`ConnectError`) | retried | retried |
| timeout or reset after sending | retried | not retried (unless `idempotent=True`) |
| 500 / 502 / 503 / 504, `SERVER_UNAVAILABLE` | retried | not retried (unless `idempotent=True`) |
| `UNABLE_TO_LOCK_ROW` (`RowLocked`) | retried | retried (the write was rejected) |
| 429 / `REQUEST_LIMIT_EXCEEDED` with `Retry-After` | retried | retried |
| `REQUEST_LIMIT_EXCEEDED` without `Retry-After` | not retried | not retried |
| any other 4xx | not retried | not retried |
| `401` with `INVALID_SESSION_ID` or `invalid_token` | one token refresh and resend (not counted as a retry) | same |
| any other `401` | one token refresh and resend | not resent: raises `SessionExpired` |

### `retryable`

`exc.retryable` tells you if an error is transient in principle. It is `True` for these errors:

- `TransportError`;
- `RowLocked`;
- `ServerError` and `UnexpectedResponse` with status 500, 502, 503 or 504, or with
  `SERVER_UNAVAILABLE`.

It doesn't tell you if the client retried. A write that timed out has `retryable=True`, but the
client did not send it again.

The token refresh request has its own rules. See
[Authentication](authentication/index.md#how-tokens-are-handled).

## Custom errors with tags

Salesforce has no custom error codes. Validation rules, Flow "Custom Error" elements and Apex
`addError()` all give `FIELD_CUSTOM_VALIDATION_EXCEPTION`. To tell them apart, start each
message in the org with a tag, such as `[ADDR_REQUIRED]`. Then map the tag to your own class:

```python
class AddressRequired(esf.ValidationRuleFailed):
    """The ADDR_REQUIRED validation rule on Lead."""


tagged_sf = esf.AsyncSalesforce(auth, error_tags={"ADDR_REQUIRED": AddressRequired})

try:
    await tagged_sf.sobject("Lead").create({"LastName": "Hodgkin", "Company": "Oxford", "Country": "UK"})
except AddressRequired as exc:
    print(exc.tag, "-", exc.message)  # ADDR_REQUIRED - Street is required when Country is set
await tagged_sf.aclose()
```

- A tag is `[`, then 2 to 64 characters from `A-Z`, `0-9` and `_`, then `]`. It must be at the
  start of the message. Whitespace after it is optional.
- Your class must subclass the default class for that error. For a validation rule, that is
  `ValidationRuleFailed` or one of its subclasses. If it doesn't, the client ignores the
  mapping for that error and raises the default class.
- If an `error_tags` value isn't a `SalesforceError` subclass, creating the client raises
  `TypeError`.
- If your class matches, the client removes the tag from `.message`. If no class matches, the
  client raises the default class with `.tag` set and the message unchanged.
- Code that doesn't use tags sees no difference.

## Errors in Collections

The client doesn't raise errors for single rows of a Collections call. It puts them in the
`BatchResult` as `ErrorDetail`s. See
[Collections](guides/collections.md#results-batchresult-and-recordresult). With
`all_or_none=True`, the first failed chunk raises the class of its real error.

## Deprecations

Before we remove public API, it emits `esf.EsfDeprecationWarning` for at least one minor
release. This class is a subclass of `DeprecationWarning`. To see these warnings early, turn
them into errors in your test suite:

```python
import warnings

warnings.filterwarnings("error", category=esf.EsfDeprecationWarning)
await sf.aclose()
```

## Attributes

| Attribute | Description |
|---|---|
| `message` | human-readable message, from Salesforce when available |
| `error_code` | Salesforce `errorCode` / `statusCode`, or the OAuth `error` (e.g. `"INVALID_FIELD"`) |
| `fields` | fields Salesforce reported as affected (tuple) |
| `status` | HTTP status, if there was a response |
| `operation` | library operation, e.g. `"sobject.update"`, `"query"`, `"collections.create"`, `"oauth.refresh"` |
| `sobject` | sObject type, if any |
| `method`, `path` | HTTP method and path template (ids replaced by `:id`) |
| `request_id` | Salesforce's request id header, if present |
| `retryable` | whether the error is transient in principle (see [`retryable`](#retryable)) |
| `errors` | every `esf.ErrorDetail(error_code, message, fields, extended)` when Salesforce returned several |
| `raw` | the first 4 KiB of the response body, for debugging; the library never logs it |
| `tag` | custom tag parsed from a `[TAG] ...` message, if any |
| `hint` | a likely fix, or `None` (see [Hints and docs links](#hints-and-docs-links)) |
| `docs_url` | link to the error's entry under [Troubleshooting](#troubleshooting) |

Exceptions pickle with all their attributes, so they survive Celery, multiprocessing and
`concurrent.futures`.

## Hierarchy

```text
SalesforceError
├── TransportError                 no usable HTTP response              (retryable)
│   ├── ConnectError               connection failed; request never sent
│   └── RequestTimeout             timed out
├── AuthError
│   ├── SessionExpired             401 that survived one refresh, or an expired StaticToken
│   ├── ReauthorizationRequired    refresh token expired, revoked or rotated out: log in again
│   ├── RefreshFailed              token refresh failed; .ambiguous=True when the outcome is unknown
│   ├── TokenConflict              token-store compare-and-swap lost (normally handled internally)
│   └── OAuthError                 OAuth endpoint error; .error, .error_description
├── RequestError                   4xx caused by the request
│   ├── MalformedQuery             MALFORMED_QUERY, INVALID_QUERY_LOCATOR, rejected cursor, ...
│   ├── InvalidField               INVALID_FIELD, INVALID_FIELD_FOR_INSERT_UPDATE, ...
│   ├── InvalidType                INVALID_TYPE (unknown sObject type)
│   ├── NotFound                   404, NOT_FOUND, ENTITY_IS_DELETED
│   ├── Forbidden                  INSUFFICIENT_ACCESS*, API_DISABLED_FOR_ORG, ...
│   ├── MultipleMatches            HTTP 300 on external-id upsert/get; .matches
│   ├── Conflict                   409 / 412 / 428
│   ├── Gone                       410
│   ├── RequestTooLarge            414 / 431
│   └── UnsupportedApiVersion      UNSUPPORTED_API_VERSION, or API_VERSION_TOO_OLD (raised locally)
├── RecordError                    DML rejected by the org
│   ├── ValidationRuleFailed       FIELD_CUSTOM_VALIDATION_EXCEPTION (validation rule, Flow, addError())
│   ├── RequiredFieldMissing       REQUIRED_FIELD_MISSING
│   ├── DuplicateDetected          DUPLICATES_DETECTED; .duplicates
│   ├── InvalidReference           INVALID_CROSS_REFERENCE_KEY, FIELD_INTEGRITY_EXCEPTION
│   ├── StringTooLong              STRING_TOO_LONG
│   ├── InvalidPicklistValue       INVALID_OR_NULL_FOR_RESTRICTED_PICKLIST
│   ├── RowLocked                  UNABLE_TO_LOCK_ROW                   (retryable)
│   ├── RecordSaveBlocked          CANNOT_INSERT_UPDATE_ACTIVATE_ENTITY (often an unhandled trigger exception)
│   ├── FlowFailed                 CANNOT_EXECUTE_FLOW_TRIGGER
│   ├── StorageLimitExceeded       STORAGE_LIMIT_EXCEEDED
│   └── EventPublishFailed         platform event publish refused, e.g. LIMIT_EXCEEDED
│       └── EventPublishUnavailable  PLATFORM_EVENT_PUBLISHING_UNAVAILABLE  (retryable)
├── LimitError
│   ├── RequestLimitExceeded       REQUEST_LIMIT_EXCEEDED (403) or 429; .retry_after
│   └── BudgetExceeded             the library's own budget guard; .usage
├── ServerError                    420 and 5xx, SERVER_UNAVAILABLE
├── PartialFailure                 some records or chunks failed; .results
├── BulkJobFailed                  Bulk 2.0 job failed; .job_id, .state, .error_message   (used from 0.3)
├── ApexError                      APEX_ERROR, or an error from an Apex REST class; .body
├── GraphQLError                   GraphQL errors array; .graphql_errors, .data
├── SoapFault                      unmapped SOAP fault (lead convert, merge); .fault_code
└── UnexpectedResponse             HTML pages (proxies, maintenance), non-JSON bodies, unexpected shapes
```

`BulkJobFailed` exists now to keep the hierarchy stable. Nothing raises it yet.

## Troubleshooting

Each error class has an entry here. `exc.docs_url` links to it.

### SalesforceError

The base class of every error. Read `exc.error_code` and `exc.message` first. If you report a
problem to Salesforce support, include `exc.request_id`.

### TransportError

The client got no usable HTTP response. Check the network, proxies and DNS. The client
retries reads for you. It doesn't resend writes that may have reached Salesforce.

### ConnectError

The client couldn't connect, so Salesforce never got the request. Check the instance URL, the
proxy settings and the firewall. It is always safe to send the call again.

### RequestTimeout

The request took longer than the timeout. For a write, check the record before you send it
again, because the write may have committed. To wait longer, raise `Timeouts(read=...)`.

### AuthError

A login or token problem. Catch this group to send the user back to log in.

### SessionExpired

Salesforce rejected the access token, and one refresh didn't fix it. If the hint names the
connected app, check its OAuth scopes and session policies. A `StaticToken` can't refresh, so
get a new token.

### ReauthorizationRequired

The refresh token is expired, revoked or replaced by a newer one. The user must log in again.
See [Authentication](authentication/index.md#how-tokens-are-handled).

### RefreshFailed

The token refresh failed. If `exc.ambiguous` is `True`, Salesforce may have issued a new
token that the client didn't receive. Try the call again later.

### TokenConflict

Two processes saved a token at the same time. The client handles this itself. If you see it,
your token store may not implement compare-and-swap correctly.

### OAuthError

An OAuth endpoint returned an error. Read `exc.error` and `exc.error_description`. Common
causes are a wrong client id, a wrong secret or a disabled flow in the connected app.

### RequestError

Salesforce rejected the request itself. Read `exc.error_code`. Sending the same request again
gives the same error.

### MalformedQuery

The SOQL or SOSL has a syntax error, or a cursor is invalid. Pass values as arguments,
such as `sf.query("... WHERE Name = {}", name)`. The client then quotes them for you.

### InvalidField

A field name doesn't exist on the sObject, or the user can't see or write it. Check the
spelling and the `__c` suffix of custom fields. Check field-level security for the user.

### InvalidType

The sObject type doesn't exist, or the user can't see it. Check the spelling and the `__c`
suffix. Check the object permissions of the user.

### NotFound

The record or resource doesn't exist, or the record is deleted. Check the id and the path.

### Forbidden

The user doesn't have access. Check the profile and permission sets of the user. For
`API_DISABLED_FOR_ORG`, the user needs the "API Enabled" permission, and the org edition must
include API access.

### MultipleMatches

An external id value matched more than one record. `exc.matches` lists their URLs. Make the
external id field unique.

### Conflict

The record changed since you read it, or a precondition failed. Read the record again, then
retry your change.

### Gone

The resource was removed, often because the API version is retired. Use a newer
`api_version`.

### RequestTooLarge

The URL or the headers are over 16,384 bytes. To read many records by id, use
`sf.collections.retrieve`. Split a very long query into smaller ones.

### UnsupportedApiVersion

The org doesn't offer the configured `api_version`, or a feature needs a newer one. Set
`api_version=` on the client.

### RecordError

The org rejected a record change. Catch this group to show the message to the user.

### ValidationRuleFailed

A validation rule, a Flow custom error or Apex `addError()` rejected the record. Show
`exc.message` to the user. To catch one rule by class, use [tags](#custom-errors-with-tags).

### RequiredFieldMissing

A required field has no value. `exc.fields` names the fields.

### DuplicateDetected

A duplicate rule blocked the record. `exc.duplicates` holds the matching records when
Salesforce returns them.

### InvalidReference

An id in a lookup field points to a missing record, or to the wrong sObject type. Check the
ids in `exc.fields`.

### StringTooLong

A text value is longer than the field allows. Run `esf describe <sObject>` to see the
length of each field.

### InvalidPicklistValue

The value isn't in the restricted picklist. Run `esf describe <sObject>` to see the active
values.

### RowLocked

Another transaction holds a lock on the record. The client retries this error. If it still
fails, send fewer updates to the same parent record at once.

### RecordSaveBlocked

The save was blocked. In practice this is often an unhandled exception in a trigger. Read
`exc.message`, then check the debug logs in the org.

### FlowFailed

A record-triggered Flow failed. Read `exc.message`, then check the Flow error e-mail or the
Flow's debug run in the org.

### EventPublishFailed

Salesforce didn't accept a [platform event](guides/platform-events.md). Read `exc.error_code`.
`LIMIT_EXCEEDED` means the org's hourly publishing allocation is used up: publish less often or
in fewer calls, or buy more allocation.

### EventPublishUnavailable

Event publishing is down for a moment, so `exc.retryable` is true. Publish the event again after
a short wait.

### StorageLimitExceeded

The org is out of data or file storage. Delete records or buy more storage.

### LimitError

A Salesforce limit or the library's own budget stopped the call.

### RequestLimitExceeded

The org is over its API request limit, or runs too many long requests at once. Check
`sf.usage.snapshot()` and set a budget. See [Usage and budgets](usage-and-budgets.md).

### BudgetExceeded

The client's own budget guard stopped the call before it was sent. `exc.usage` shows the
usage. See [Usage and budgets](usage-and-budgets.md).

### ServerError

Salesforce had an internal error or is not available. The client retries reads. Check
[Salesforce Trust](https://status.salesforce.com) if the error continues.

### PartialFailure

Some records of a batch failed. `exc.results` holds every row, with an error on each failed
row.

### BulkJobFailed

A Bulk API 2.0 job ended in the `Failed` state. Read `exc.error_message`.

### ApexError

Apex code threw an exception, or an Apex REST class returned an error. `exc.body` holds the
response body.

### GraphQLError

The GraphQL response has an `errors` array. Salesforce can send it with `200 OK`. Read
`exc.graphql_errors`: each entry has a `message`, and often `locations` in the query and
`extensions.ErrorType`. `exc.error_code` holds the `ErrorType` of the first entry, for example
`ValidationError` for an unknown field. `exc.data` holds the partial data, if any. See
[GraphQL](guides/graphql.md#handle-errors).

### SoapFault

A SOAP call returned a fault that has no more specific class. Read `exc.fault_code`.

### UnexpectedResponse

The response wasn't what Salesforce normally sends, such as a proxy page or a maintenance
page. Check proxies between you and Salesforce. `exc.raw` holds the start of the body.

## How it works

### How responses are mapped

The client checks each response in this order:

1. `2xx` (and `304` for conditional requests) is success.
2. A non-empty body that isn't JSON raises `UnexpectedResponse`. Proxy error pages and
   maintenance pages end up here. The error is `retryable` for status 500, 502, 503 and 504.
3. `300` raises `MultipleMatches` with the matching record URLs.
4. An OAuth body such as `{"error": ..., "error_description": ...}` maps like this:
   `invalid_token` or status 401 raises `SessionExpired`. `invalid_grant` raises
   `ReauthorizationRequired`. Any other value raises `OAuthError`.
5. A REST error list such as `[{"errorCode", "message", "fields"}]` maps by the `errorCode` of
   its first entry. The table below gives the class. All entries are in `.errors`.
6. In all other cases, the HTTP status picks the class:
    - 401: `SessionExpired`
    - 403: `Forbidden`
    - 404: `NotFound`
    - 409, 412 and 428: `Conflict`
    - 410: `Gone`
    - 414 and 431: `RequestTooLarge`
    - 429: `RequestLimitExceeded`
    - 420 and 5xx: `ServerError`
    - any other 4xx: `RecordError` for writes, `RequestError` for everything else

| `errorCode` | Class |
|---|---|
| `MALFORMED_QUERY`, `INVALID_QUERY_LOCATOR`, `INVALID_QUERY_FILTER_OPERATOR`, `MALFORMED_SEARCH`, `INVALID_SEARCH` | `MalformedQuery` |
| `INVALID_FIELD`, `INVALID_FIELD_FOR_INSERT_UPDATE`, `INVALID_FIELD_WHEN_USING_TEMPLATE` | `InvalidField` |
| `INVALID_TYPE` | `InvalidType` |
| `NOT_FOUND`, `ENTITY_IS_DELETED` | `NotFound` (`InvalidType` for `NOT_FOUND` on an sObject type: describe, metadata, updated, deleted) |
| `INSUFFICIENT_ACCESS`, `INSUFFICIENT_ACCESS_OR_READONLY`, `INSUFFICIENT_ACCESS_ON_CROSS_REFERENCE_ENTITY`, `API_DISABLED_FOR_ORG`, `API_CURRENTLY_DISABLED`, `FUNCTIONALITY_NOT_ENABLED` | `Forbidden` |
| `FIELD_CUSTOM_VALIDATION_EXCEPTION` | `ValidationRuleFailed` |
| `REQUIRED_FIELD_MISSING` | `RequiredFieldMissing` |
| `DUPLICATES_DETECTED` | `DuplicateDetected` |
| `INVALID_CROSS_REFERENCE_KEY`, `FIELD_INTEGRITY_EXCEPTION` | `InvalidReference` |
| `STRING_TOO_LONG` | `StringTooLong` |
| `INVALID_OR_NULL_FOR_RESTRICTED_PICKLIST` | `InvalidPicklistValue` |
| `UNABLE_TO_LOCK_ROW` | `RowLocked` |
| `CANNOT_INSERT_UPDATE_ACTIVATE_ENTITY` | `RecordSaveBlocked` |
| `CANNOT_EXECUTE_FLOW_TRIGGER` | `FlowFailed` |
| `STORAGE_LIMIT_EXCEEDED` | `StorageLimitExceeded` |
| `REQUEST_LIMIT_EXCEEDED` | `RequestLimitExceeded` (`.retry_after` from `Retry-After`) |
| `INVALID_SESSION_ID` | `SessionExpired` |
| `SERVER_UNAVAILABLE` | `ServerError` |
| `APEX_ERROR` | `ApexError` |
| `UNSUPPORTED_API_VERSION` | `UnsupportedApiVersion` |

An unknown `errorCode` falls back to the status rule in step 6. `.error_code` still holds the
code.

### SOAP faults

[Lead convert and merge](guides/leads.md) use the SOAP API. A SOAP fault maps by its
`exceptionCode` with the table above, because a fault is always HTTP 500. An unknown code
raises `SoapFault`, with the raw `faultcode` in `.fault_code`. A rejected session
(`INVALID_SESSION_ID`) refreshes the token once, like a REST `401`.

### Platform event errors

When you publish a [platform event](guides/platform-events.md), `PLATFORM_EVENT_PUBLISHING_UNAVAILABLE`
raises `EventPublishUnavailable`. `LIMIT_EXCEEDED`, `PLATFORM_EVENT_PUBLISH_FAILED` and any other
unknown code raise `EventPublishFailed`. Known record errors keep their class, such as
`RequiredFieldMissing`.

### What the client doesn't do

- It doesn't run validation rules or trigger logic itself. Any copy of that logic drifts from
  the org. Let Salesforce reject the record, then handle the typed error.
- It doesn't hide partial failures. Rows that failed are always in the results and in the logs.
- It doesn't resend writes that may have run.
