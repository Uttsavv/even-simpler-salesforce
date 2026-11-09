# Errors

Every exception the library raises is an `esf.SalesforceError`, whichever Salesforce API
produced it (REST, OAuth or Collections). All of them have the same attributes, so one
`except` clause can log any of them.

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
    print(exc.message)     # [ADDR_REQUIRED] Street is required when Country is set
    print(exc.fields)      # ('Street',)
    print(exc.status, exc.operation, exc.sobject)  # 400 sobject.create Lead
except esf.SalesforceError as exc:
    print("other Salesforce error:", exc)
```

## Attributes

| Attribute | Meaning |
|---|---|
| `message` | human-readable message, from Salesforce when available |
| `error_code` | Salesforce `errorCode` / `statusCode`, or the OAuth `error` (e.g. `"INVALID_FIELD"`) |
| `fields` | fields Salesforce reported as affected (tuple) |
| `status` | HTTP status, if there was a response |
| `operation` | library operation, e.g. `"sobject.update"`, `"query"`, `"collections.create"`, `"oauth.refresh"` |
| `sobject` | sObject type, if any |
| `method`, `path` | HTTP method and path template (ids replaced by `:id`) |
| `request_id` | Salesforce's request id header, if present |
| `retryable` | whether the error is transient **in principle** (see [Retries](#retries)) |
| `errors` | every `esf.ErrorDetail(error_code, message, fields, extended)` when Salesforce returned several |
| `raw` | the first 4 KiB of the response body, for debugging. The library never logs it |
| `tag` | custom tag parsed from a `[TAG] ...` message, if any |

`str(exc)` is `"ERROR_CODE: message (operation, sobject, HTTP status)"`. Exceptions can be
pickled with all their attributes, so they survive Celery, multiprocessing and
`concurrent.futures`.

## Hierarchy

Catch the most specific class you can handle, and fall back to a group (`RecordError`,
`RequestError`, `AuthError`, ...) or to `SalesforceError`.

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
├── ServerError                    500 / 502 / 503 / 504, SERVER_UNAVAILABLE
├── PartialFailure                 some records or chunks failed; .results
├── BulkJobFailed                  Bulk 2.0 job failed; .job_id, .state, .error_message   (used from 0.3)
├── ApexError                      APEX_ERROR, or an error from an Apex REST class; .body
├── GraphQLError                   GraphQL errors array; .graphql_errors                 (used from 0.3)
├── SoapFault                      unmapped SOAP fault; .fault_code                      (used from 0.2)
└── UnexpectedResponse             HTML pages (proxies, maintenance), non-JSON bodies, unexpected shapes
```

`BulkJobFailed`, `GraphQLError` and `SoapFault` already exist so that the hierarchy is stable.
Nothing in 0.1 raises them.

## How responses are mapped

The library looks at the response in this order:

1. `2xx` (and `304` for conditional requests) is success.
2. A non-empty body that isn't JSON raises `UnexpectedResponse` (`retryable` for 500/502/503/504).
   This happens with proxy error pages and maintenance pages.
3. `300` raises `MultipleMatches` with the matching record URLs.
4. An OAuth-style body (`{"error": ..., "error_description": ...}`): `invalid_token` or status
   401 raises `SessionExpired`; `invalid_grant` raises `ReauthorizationRequired`; anything else
   raises `OAuthError`.
5. A REST error list (`[{"errorCode", "message", "fields"}]`): the **first** entry's
   `errorCode` picks the class (table below), and all entries are in `.errors`.
6. Otherwise, the HTTP status picks the class: 401 `SessionExpired`, 403 `Forbidden`,
   404 `NotFound`, 409/412/428 `Conflict`, 410 `Gone`, 414/431 `RequestTooLarge`,
   429 `RequestLimitExceeded`, 420 and 5xx `ServerError`. Any other 4xx raises `RecordError` for
   writes and `RequestError` for everything else.

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

An unknown `errorCode` falls back to the status rule in step 6, and `.error_code` still holds
the code.

Errors in individual rows of a Collections call are not raised: they are `ErrorDetail`s in the
`BatchResult` (see [Collections](guides/collections.md#results-batchresult-and-recordresult)).
With `all_or_none=True`, the first failing chunk raises the class of its real error, as
described above.

## Custom errors with tags

Salesforce has no custom error codes. Validation rules, Flow "Custom Error" elements and Apex
`addError()` all produce `FIELD_CUSTOM_VALIDATION_EXCEPTION` with your message text. To give
those errors stable identities, start the message in the org with a tag, such as
`[ADDR_REQUIRED] Street is required when Country is set`, and map the tag to your own class:

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

- A tag is `[` + 2–64 characters from `A-Z`, `0-9` and `_` + `]` at the **start** of the message,
  followed by optional whitespace.
- A registered class **must subclass the class the error would otherwise have**. For a
  validation rule, that is `ValidationRuleFailed` or one of its subclasses. If it doesn't,
  the mapping is ignored for that error and you get the default class. `error_tags` values that
  aren't `SalesforceError` subclasses raise `TypeError` when the client is created.
- When a registered class matches, the tag is removed from `.message`. Otherwise the default
  class is raised with `.tag` set and the message unchanged. Code that doesn't use tags sees no
  difference.

## Retries

`retry=esf.RetryPolicy(...)` controls automatic retries:

| Field | Default | Meaning |
|---|---|---|
| `max_attempts` | `3` | total attempts per call, including the first (minimum 1) |
| `base_delay` | `0.25` | first backoff delay in seconds, doubled for each further retry |
| `max_delay` | `8.0` | cap on one backoff delay |
| `jitter` | `True` | full jitter: wait a random time between 0 and the delay |
| `respect_retry_after` | `True` | wait for `Retry-After` when Salesforce sends it |
| `max_retry_after` | `30.0` | if `Retry-After` is longer than this, raise instead of waiting (`exc.retry_after` says how long) |

```python
patient = esf.AsyncSalesforce(auth, retry=esf.RetryPolicy(max_attempts=5, max_delay=20))
no_retries = esf.AsyncSalesforce(auth, retry=esf.RetryPolicy(max_attempts=1))
await patient.aclose()
await no_retries.aclose()
```

What is retried ([ADR 0001](adr/0001-retry-safety.md)):

| Failure | Reads (GET/HEAD, POST reads such as `retrieve`, `parameterized_search`) | Writes, upserts, code-running GETs (Apex REST, `executeAnonymous`) |
|---|---|---|
| connect error: never sent (`ConnectError`) | retried | retried |
| timeout or reset after sending | retried | **not retried** (unless `idempotent=True`) |
| 500 / 502 / 503 / 504, `SERVER_UNAVAILABLE` | retried | **not retried** (unless `idempotent=True`) |
| `UNABLE_TO_LOCK_ROW` (`RowLocked`) | retried | retried (the write was rejected) |
| 429 / `REQUEST_LIMIT_EXCEEDED` **with** `Retry-After` | retried | retried |
| `REQUEST_LIMIT_EXCEEDED` without `Retry-After` | not retried | not retried |
| any other 4xx | not retried | not retried |
| `401` | one token refresh and resend, for every method (not counted as a retry) | same |

A write that timed out may already have committed, and sending it again could run triggers,
flows and e-mails a second time. It could also turn the success into a false `Conflict` or
`NotFound`. So the client raises `RequestTimeout` / `ServerError` and lets you decide. Use
`sf.request(..., idempotent=True)` for calls you know are safe to repeat.

`exc.retryable` says whether an error is transient **in principle**: `True` for
`TransportError`, `RowLocked` and 5xx `ServerError`s. It doesn't mean the library retried that
error. A write that timed out has `retryable=True`, but it is not resent. After the last
attempt, the last error is raised. Each retry emits an `sf.retry` event, and the final
`sf.request` event lists `attempts` and `retry_reasons`.

The token refresh request has its own rules (see
[Authentication](authentication/index.md#how-tokens-are-handled)).

## Things the library deliberately doesn't do

- It doesn't evaluate validation rules or trigger logic on the client. Any copy of that logic
  drifts from the org. Let Salesforce reject the record and handle the typed error.
- It doesn't hide partial failures. Rows that failed are always in the results and in the logs.
- It doesn't resend writes that may have run.

## Deprecations

Public API is removed only after it has emitted `esf.EsfDeprecationWarning` (a
`DeprecationWarning` subclass) for at least one minor release. To see these warnings early,
turn them into errors in your test suite:

```python
import warnings

warnings.filterwarnings("error", category=esf.EsfDeprecationWarning)
await sf.aclose()
```
