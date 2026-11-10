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
`str(exc)` gives `"ERROR_CODE: message (operation, sobject, HTTP status)"`.

Invalid arguments are different. They raise `ValueError` or `TypeError` when you create the
object, before the client sends a request.

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
| 500 / 502 / 503 / 504, `SERVER_UNAVAILABLE` (backoff; `Retry-After` is not used) | retried | not retried (unless `idempotent=True`) |
| `UNABLE_TO_LOCK_ROW` (`RowLocked`) | retried | retried (the write was rejected) |
| 429 / `REQUEST_LIMIT_EXCEEDED` with `Retry-After` | retried | retried |
| `REQUEST_LIMIT_EXCEEDED` without `Retry-After` | not retried | not retried |
| any other 4xx | not retried | not retried |
| `401` | one token refresh and resend (not counted as a retry) | same |

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
│   └── StorageLimitExceeded       STORAGE_LIMIT_EXCEEDED
├── LimitError
│   ├── RequestLimitExceeded       REQUEST_LIMIT_EXCEEDED (403) or 429; .retry_after
│   └── BudgetExceeded             the library's own budget guard; .usage
├── ServerError                    420 and 5xx, SERVER_UNAVAILABLE
├── PartialFailure                 some records or chunks failed; .results
├── BulkJobFailed                  Bulk 2.0 job failed; .job_id, .state, .error_message   (used from 0.3)
├── ApexError                      APEX_ERROR, or an error from an Apex REST class; .body
├── GraphQLError                   GraphQL errors array; .graphql_errors                 (used from 0.3)
├── SoapFault                      unmapped SOAP fault; .fault_code                      (used from 0.2)
└── UnexpectedResponse             HTML pages (proxies, maintenance), non-JSON bodies, unexpected shapes
```

`BulkJobFailed`, `GraphQLError` and `SoapFault` exist now to keep the hierarchy stable. Nothing
in 0.1 raises them.

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

### What the client doesn't do

- It doesn't run validation rules or trigger logic itself. Any copy of that logic drifts from
  the org. Let Salesforce reject the record, then handle the typed error.
- It doesn't hide partial failures. Rows that failed are always in the results and in the logs.
- It doesn't resend writes that may have run.
