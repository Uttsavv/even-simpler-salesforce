# Logging and observability

The library **never configures logging and never prints**, except when you set `ESF_DEBUG`.
Instead it emits structured **events** to the **observers** you attach. Each logical call
produces **one** `sf.request` event after all retries and refreshes, with everything you need to
debug it. Secrets are never included.

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

## Events

| Event | When | Fields |
|---|---|---|
| `sf.request` | each logical call finishes, successfully or not | [see below](#the-sfrequest-event) |
| `sf.retry` | before each automatic retry | `operation`, `attempt`, `reason`, `delay_ms`, `error_code`, `status` |
| `sf.auth.refreshed` | a token was acquired or refreshed | `token_key_hash`, `flow`, `duration_ms`, `rotated` (new refresh token), `proactive`, `initial` (first token, not a refresh) |
| `sf.auth.reauth_required` | a refresh token is dead (`invalid_grant`) | `token_key_hash` |
| `sf.usage.threshold` | a [budget](usage-and-budgets.md#budgets) threshold was crossed | `threshold`, `fraction`, `used`, `max` |
| `sf.debug.attempt` | each HTTP attempt, **only with `ESF_DEBUG=2`** | `method`, `url` (no query string), `status`, `request_headers` / `response_headers` (`Authorization` and cookies redacted), `elapsed_ms` |

`retry_reasons` / `reason` values: `connect_error`, `timeout`, `transport_error`, `row_locked`,
`rate_limited`, `http_500`, `http_502`, `http_503`, `http_504`.

## The `sf.request` event

| Field | Meaning |
|---|---|
| `operation` | `sobject.get`, `sobject.create`, `query`, `query_more`, `search`, `collections.update`, `limits`, `oauth.userinfo`, `raw` (or your `operation=`), ... |
| `sobject` | sObject type, when known (for queries, taken from the `FROM` clause) |
| `method` | HTTP method |
| `path_template` | path without the version prefix, with record ids replaced by `:id`, query locators by `:locator` and external-id values by `:value` |
| `status` | final HTTP status, or `None` if there was no response |
| `outcome` | `"ok"` or `"error"` (any exception, including a cancelled task or a rejected argument) |
| `error_code` | Salesforce error code of the failure |
| `error_class` | exception class name of the failure |
| `error_message` | first 200 characters of the error message (SOQL literals redacted). **Only with `ESF_DEBUG` set**, because DML messages can quote record values |
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

Token requests (`/services/oauth2/token`) don't produce `sf.request` events. They produce
`sf.auth.refreshed`.

### Never logged

The library never puts these in an event, a debug line or an exception message:

- access tokens, refresh tokens, JWT assertions, authorization codes and PKCE verifiers;
- `Authorization`, `Cookie` and `Set-Cookie` header values (shown as `<redacted>` at `ESF_DEBUG=2`);
- client secrets and private keys (`repr()` of the flow objects hides them; `Token`'s `repr()`
  shows only the last 4 characters of each token);
- token keys and client ids: only 12-character hashes (`token_key_hash`, `client_id_hash`);
- record bodies and field values, request bodies and query strings;
- SOQL literals (only `soql_hash`, or the `?`-redacted text you opted into);
- Salesforce error messages, unless `ESF_DEBUG` is set (the exception you catch always has the
  full message, and `exc.raw` has the body, but the library doesn't log them).

Paths you pass to `sf.request()` appear in `path_template` with only ids and query locators
replaced, so don't put personal data in custom paths.

## Observers

An observer is any object with `on_event(event)`. Events are `esf.Event(name, fields, timestamp)`,
and you can read fields with `event["status"]` or `event.get("caller")`.

- Pass observers with `observers=[...]`, or add one later with `sf.observers.add(obs)`. All
  child clients share them.
- `on_event` **must not raise**. Exceptions are swallowed and counted in `sf.observers.errors`,
  so a broken observer can't break your calls.
- `on_event` runs inline on the calling task or thread, so keep it fast and hand slow work to a
  queue.

The library ships three observers in `even_simpler_salesforce.observability`.

### Standard-library logging

```python
import logging

logging.basicConfig(level=logging.INFO)
logged = esf.AsyncSalesforce(auth, observers=[esf.observability.StdlibLogging()])
await logged.query("SELECT Id FROM Lead LIMIT 1")
# INFO:esf:sf.request operation=query sobject=Lead method=GET path_template=/query status=200 ...
await logged.aclose()
```

`StdlibLogging(logger="esf", level=logging.INFO)` logs each event at `level`, and at
`WARNING` for errors and `sf.auth.reauth_required`. The message is the event name followed by
`key=value` pairs. The fields are also attached as `extra={"esf": {...}, "esf_event": name}`
for JSON formatters. Attach your own handlers to the `esf` logger. The library itself also logs
two warnings there: `all_or_none=True` with more than 200 rows, and the one-time INFO message
when token introspection gives no expiry.

### structlog (`[structlog]` extra)

```python
import structlog

sl = esf.AsyncSalesforce(auth, observers=[esf.observability.Structlog(structlog.get_logger("esf"))])
await sl.query("SELECT Id FROM Lead LIMIT 1")
await sl.aclose()
```

Each event becomes `logger.info(event.name, **fields)`, or `logger.warning(...)` for errors.
Without an argument, it uses `structlog.get_logger("esf")`.

### OpenTelemetry (`[otel]` extra)

```python
otel = esf.observability.OpenTelemetry()  # or OpenTelemetry(tracer_provider=..., meter_provider=...)
traced = esf.AsyncSalesforce(auth, observers=[otel])
await traced.query("SELECT Id FROM Lead LIMIT 1")
await traced.aclose()
```

For each `sf.request` event it records:

- a span named `salesforce <operation>`, covering the call's duration, with attributes
  `salesforce.operation`, `salesforce.sobject`, `salesforce.status`, `salesforce.outcome`,
  `salesforce.error_code`, `salesforce.caller`, `salesforce.priority`, `salesforce.attempts`,
  `http.request.method` and `url.path` (the path template). The span status is `ERROR` on
  failure;
- counters `esf.requests` and `esf.errors`, a histogram `esf.duration` (ms), and a gauge
  `esf.api_usage` (fraction of the daily limit, when your OpenTelemetry API supports gauges).

The span is created after the call finishes, with its start time set back by the call's
duration. It is not the parent of your own spans for the call.

## Per-request summary: `capture()`

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

| Field | Meaning |
|---|---|
| `sf_calls`, `sf_ms` | number of calls and their total duration |
| `sf_errors` | error code (or class name) of each failed call |
| `sf_retries` | automatic retries (a resend after a token refresh doesn't count) |
| `sf_refreshed` | a token was refreshed (getting the first token doesn't count) |
| `sf_api_used`, `sf_api_max` | latest usage header seen in the block |
| `sf_ops` | calls per operation |
| `sf_records_failed` | Collections rows that failed |

`capture()` uses a context variable. It sees calls from any client, works across `await` and
in sync Collections threads, and nested blocks each see the calls made inside them. The
[example apps](integrations/fastapi.md) wrap each HTTP request in it.

## `ESF_DEBUG`

Set `ESF_DEBUG=1` before you create the client to get one readable line per call on stderr:

```text
esf POST  oauth.token    client_credentials  212ms
esf  GET    query           Lead       200    115ms  2000 rows  api 12.3k/100k
esf  PATCH  sobject.update  Lead       400     88ms  FIELD_CUSTOM_VALIDATION_EXCEPTION  "[ADDR_REQUIRED] Street is required when Country is set"  api 12.3k/100k
esf POST  oauth.refresh  web_server  240ms  rotated
```

- `ESF_DEBUG=1`: the lines above, plus one line for each other event except `sf.retry`. Error
  lines include the first 200 characters of the message.
- `ESF_DEBUG=2`: also `sf.retry` and an `sf.debug.attempt` line for every HTTP attempt, with
  headers (`Authorization` and cookies redacted) and timing.
- Any other non-numeric value means level 1. The variable is read when a client is created.

Error messages can include record values, so don't leave `ESF_DEBUG` on in production.

```python
await sf.aclose()
```
