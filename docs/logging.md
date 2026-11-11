# Logging and observability

The client sends a structured event to your observers for each thing it does. It never
configures logging. It prints nothing unless you set [`ESF_DEBUG`](#esf_debug).

```python
import even_simpler_salesforce as esf

seen: list[esf.Event] = []


class Collect:
    def on_event(self, event: esf.Event) -> None:
        seen.append(event)


auth = esf.ClientCredentials(
    domain="https://acme.my.salesforce.com", client_id="3MVG9...", client_secret="..."
)
sf = esf.AsyncSalesforce(auth, observers=[Collect()])
await sf.query("SELECT Id, Name FROM Lead WHERE Status = 'Open' LIMIT 5")

request = next(e for e in seen if e.name == "sf.request")
print(request["operation"], request["status"], request["duration_ms"], request["soql_hash"])
print([e.name for e in seen])  # ['sf.auth.refreshed', 'sf.request']
```

Each call emits one `sf.request` event when it finishes, after all retries and token
refreshes. Events never contain tokens, secrets or record values. See
[Never logged](#never-logged) for the full list.

## Log with the standard library

`esf.observability` ships three observers. `StdlibLogging` writes each event to the `esf`
logger:

```python
import logging

logging.basicConfig(level=logging.INFO)
logged = esf.AsyncSalesforce(auth, observers=[esf.observability.StdlibLogging()])
await logged.query("SELECT Id FROM Lead LIMIT 1")
# INFO:esf:sf.request operation=query sobject=Lead method=GET path_template=/query status=200 ...
await logged.aclose()
```

`StdlibLogging(logger="esf", level=logging.INFO)` logs each event at `level`. It logs errors and
`sf.auth.reauth_required` at `WARNING`. The message is the event name, then `key=value` pairs.

The observer also puts the fields in `extra={"esf": {...}, "esf_event": name}` for JSON
formatters. Attach your own handlers to the `esf` logger.

## Log with structlog

Install the `[structlog]` extra, then pass a structlog logger:

```python
import structlog

sl = esf.AsyncSalesforce(auth, observers=[esf.observability.Structlog(structlog.get_logger("esf"))])
await sl.query("SELECT Id FROM Lead LIMIT 1")
await sl.aclose()
```

Each event becomes `logger.info(event.name, **fields)`, or `logger.warning(...)` for errors.
Without an argument, `Structlog()` uses `structlog.get_logger("esf")`.

## Trace with OpenTelemetry

Install the `[otel]` extra, then add the observer:

```python
otel = esf.observability.OpenTelemetry()  # or OpenTelemetry(tracer_provider=..., meter_provider=...)
traced = esf.AsyncSalesforce(auth, observers=[otel])
await traced.query("SELECT Id FROM Lead LIMIT 1")
await traced.aclose()
```

For each `sf.request` event it records a span and some metrics:

- A span named `salesforce <operation>` that covers the call. Its attributes are
  `salesforce.operation`, `salesforce.sobject`, `salesforce.method`, `salesforce.status`,
  `salesforce.outcome`, `salesforce.error_code`, `salesforce.caller`, `salesforce.priority`,
  `salesforce.attempts`, `http.request.method` and `url.path` (the path template). The span
  status is `ERROR` on failure.
- Counters `esf.requests` and `esf.errors`, and a histogram `esf.duration` in milliseconds.
- A gauge `esf.api_usage` with the used fraction of the daily limit. You get it only if your
  OpenTelemetry API supports gauges.

The metrics have only the labels `operation`, `method`, `status`, `outcome`, `error_code` and
`priority`. `caller` and `sobject` can have many different values, and each value makes a new
time series, so only the spans have them. To add them to the metrics, pass
`OpenTelemetry(metric_attributes=(*esf.observability.otel.DEFAULT_METRIC_ATTRIBUTES, "sobject"))`.

The span is not the parent of your own spans for the call. [How it works](#how-it-works) explains
why.

## Write your own observer

An observer is any object with an `on_event(event)` method, like `Collect` above. An event is an
`esf.Event(name, fields, timestamp)`. Read fields with `event["status"]` or
`event.get("caller")`.

Pass observers with `observers=[...]`, or add one later. All child clients share the observers:

```python
sf.observers.add(Collect())
```

Keep `on_event` fast. It runs inline on the calling task or thread, so send slow work to a
queue. Don't let `on_event` raise. If it raises, the client swallows the exception and counts it
in `sf.observers.errors`. So a broken observer can't break your calls.

## Summarise each request { #per-request-summary-capture }

Most apps already write one log line per HTTP request or job. `esf.capture()` collects every
Salesforce call made inside a block, so you can add a summary to that line:

```python
with esf.capture() as summary:
    await sf.query("SELECT Id FROM Lead LIMIT 5")
    lead_id = await sf.sobject("Lead").create({"LastName": "Germain", "Company": "Paris"})
    await sf.collections.update("Lead", [{"Id": lead_id, "Rating": "Warm"}])

print(summary.as_fields())
# {'sf_calls': 3, 'sf_ms': 4, 'sf_errors': [], 'sf_retries': 0, 'sf_refreshed': False,
#  'sf_api_used': 12006, 'sf_api_max': 100000,
#  'sf_ops': {'query': 1, 'sobject.create': 1, 'collections.update': 1}, 'sf_records_failed': 0}
```

`capture()` uses a context variable. So it sees calls from any client, across `await`, and in
the Collections threads of the sync client. Each nested block sees the calls made inside it.
The [example apps](integrations/fastapi.md) wrap each HTTP request in `capture()`.

| Field | Description |
|---|---|
| `sf_calls`, `sf_ms` | number of calls and their total duration |
| `sf_errors` | error code (or class name) of each failed call |
| `sf_retries` | automatic retries (a resend after a token refresh doesn't count) |
| `sf_refreshed` | a token was refreshed (getting the first token doesn't count) |
| `sf_api_used`, `sf_api_max` | latest usage header seen in the block |
| `sf_ops` | calls per operation |
| `sf_records_failed` | Collections rows that failed |

## Debug with `ESF_DEBUG` { #esf_debug }

!!! warning "Don't leave `ESF_DEBUG` on in production"
    Error messages can quote record values, and `ESF_DEBUG` writes them to stderr.

To get one readable line per call on stderr, set `ESF_DEBUG=1` before you create the client:

```text
esf POST  oauth.token    client_credentials  212ms
esf  GET    query           Lead       200    115ms  2000 rows  api 12.3k/100k
esf  PATCH  sobject.update  Lead       400     88ms  FIELD_CUSTOM_VALIDATION_EXCEPTION  "[ADDR_REQUIRED] Street is required when Country is set"  api 12.3k/100k
esf POST  oauth.refresh  web_server  240ms  rotated
```

| Value | Output |
|---|---|
| `1` | the lines above, plus one line for each other event except `sf.retry`; error lines include the first 200 characters of the message |
| `2` | also `sf.retry`, and an `sf.debug.attempt` line for every HTTP attempt with headers (`Authorization` and cookies redacted) and timing |
| any other non-numeric value | same as `1` |

The client reads the variable when you create it.

## Never logged

The library never puts these in an event, a debug line or an exception message:

- access tokens, refresh tokens, JWT assertions, authorization codes and PKCE verifiers;
- `Authorization`, `Cookie` and `Set-Cookie` header values (shown as `<redacted>` at `ESF_DEBUG=2`);
- client secrets and private keys: `repr()` of a login flow hides them, and `repr()` of a
  `Token` shows only the last 4 characters of each token;
- token keys and client ids: only 12-character hashes (`token_key_hash`, `client_id_hash`);
- record bodies and field values, request bodies and query strings;
- SOQL literals: you get only `soql_hash`, or the text with `?` for each literal if you set
  `log_soql="redacted"`;
- Salesforce error messages, unless you set `ESF_DEBUG`.

The exception you catch always has the full message, and `exc.raw` has the body. The library
doesn't log them.

Don't put personal data in custom paths. Paths you pass to `sf.request()` appear in
`path_template`, and the client replaces only ids and query locators.

## Events

| Event | When | Fields |
|---|---|---|
| `sf.request` | each logical call finishes, successfully or not | [see below](#the-sfrequest-event) |
| `sf.retry` | before each automatic retry | `operation`, `attempt`, `reason`, `delay_ms`, `error_code`, `status` |
| `sf.auth.refreshed` | a token was acquired or refreshed | `token_key_hash`, `flow`, `duration_ms`, `rotated` (new refresh token), `proactive`, `initial` (first token, not a refresh) |
| `sf.auth.reauth_required` | a refresh token is dead (`invalid_grant`) | `token_key_hash` |
| `sf.usage.threshold` | a [budget](usage-and-budgets.md#budgets) threshold was crossed | `threshold`, `fraction`, `used`, `max` |
| `sf.bulk.job` | a [Bulk API 2.0](guides/bulk.md) job changed state or progress | `job_id`, `operation`, `sobject`, `state`, `records_processed`, `records_failed` |
| `sf.debug.attempt` | each HTTP attempt, only with `ESF_DEBUG=2` | `method`, `path_template` (not the URL: paths can carry external id values), `status`, `request_headers` / `response_headers` (`Authorization` and cookies redacted), `elapsed_ms` |

The `reason` of `sf.retry` and each entry of `retry_reasons` take one of these values:
`connect_error`, `timeout`, `transport_error`, `row_locked`, `rate_limited`, `http_500`,
`http_502`, `http_503`, `http_504`.

## The `sf.request` event

| Field | Description |
|---|---|
| `operation` | `sobject.get`, `sobject.create`, `query`, `query_more`, `search`, `collections.update`, `limits`, `oauth.userinfo`, `raw` (or your `operation=`), ... |
| `sobject` | sObject type, when known (for queries, taken from the `FROM` clause) |
| `method` | HTTP method |
| `path_template` | path without the version prefix, with record ids replaced by `:id`, query locators by `:locator` and external-id values by `:value` |
| `status` | final HTTP status, or `None` if there was no response |
| `outcome` | `"ok"` or `"error"` (any exception, including a cancelled task or a rejected argument) |
| `error_code` | Salesforce error code of the failure |
| `error_class` | exception class name of the failure |
| `error_message` | first 200 characters of the error message (SOQL literals redacted); only with `ESF_DEBUG` set, because DML messages can quote record values |
| `duration_ms` | wall time of the whole call, including retries and the refresh |
| `attempts` | HTTP attempts made |
| `retry_reasons` | one entry per retry |
| `refreshed` | the token was refreshed during this call |
| `records` | rows returned on this page (queries), or rows sent (writes) |
| `records_failed` | Collections only: rows with `success: false` in the response |
| `bytes_in`, `bytes_out` | bytes received (as sent over the wire, before decompression) and request body bytes sent (after gzip) |
| `api_used`, `api_max` | latest `Sforce-Limit-Info` values |
| `caller`, `priority` | [caller tags](usage-and-budgets.md#caller-tags-and-priority) (`priority` defaults to `"normal"`) |
| `soql_hash` | queries and searches: 12-character SHA-256 prefix of the SOQL/SOSL with every literal replaced by `?` |
| `soql` | only with `log_soql="redacted"`: that normalised text, e.g. `SELECT Id FROM Lead WHERE Status = ? LIMIT ?` |
| `request_id` | Salesforce's request id header (`Sforce-Request-Id`, or `X-Request-Id` / `X-B3-TraceId`) |
| `api_version` | API version used |
| `client_id_hash` | 12-character SHA-256 prefix of the OAuth client id (`None` for `StaticToken`) |

## How it works

Token requests (`/services/oauth2/token`) don't produce `sf.request` events. They produce
`sf.auth.refreshed`.

The OpenTelemetry observer creates the span after the call finishes. It sets the start time
back by the call's duration. So the span is not the parent of your own spans for the call.

The library also logs two messages straight to the `esf` logger:

- a warning if you use `all_or_none=True` with more than 200 rows;
- a one-time INFO message if token introspection gives no expiry.

```python
await sf.aclose()
```
