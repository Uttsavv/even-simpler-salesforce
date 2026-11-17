---
name: esf-debug
description: >-
  Diagnose failures in code that uses even-simpler-salesforce: read the typed error, its hint
  and docs link, turn on ESF_DEBUG, log the sf.request events, check API usage and org limits,
  and fix common login problems such as SessionExpired, ReauthorizationRequired, invalid_grant,
  refresh-token rotation and superseded tokens. Use it when the user reports "Salesforce call
  fails", "INVALID_FIELD", "SessionExpired", "invalid_grant", "REQUEST_LIMIT_EXCEEDED", "the
  token stopped working", "it worked yesterday", "why is this slow", or asks to "debug the
  Salesforce integration" or "log Salesforce calls".
---

# Debug a Salesforce integration

Start from the exception. Every failure is a typed subclass of `esf.SalesforceError` with the
same attributes, a hint for common causes and a link to its troubleshooting entry.

## Read the error

```python
import even_simpler_salesforce as esf

auth = esf.ClientCredentials(
    domain="https://acme.my.salesforce.com", client_id="3MVG9...", client_secret="..."
)
sf = esf.AsyncSalesforce(auth)
await sf.sobject("Lead").describe()  # a cached describe lets the hint suggest field names

try:
    await sf.query("SELECT Id, Scores__c FROM Lead")
except esf.SalesforceError as exc:
    print(type(exc).__name__, exc.error_code, exc.status)  # InvalidField INVALID_FIELD 400
    print(exc.operation, exc.sobject, exc.fields)
    print(exc.hint)  # did you mean 'Score__c'?
    print(exc.docs_url)  # the troubleshooting entry for this class
    print(exc.request_id)  # give this to Salesforce support
```

| Attribute | Use it for |
|---|---|
| `type(exc).__name__`, `error_code`, `message` | what went wrong |
| `status`, `operation`, `sobject`, `method`, `path` | which call failed; ids in `path` are replaced by `:id` |
| `fields` | the fields Salesforce named |
| `hint`, `docs_url` | the likely fix and the docs entry |
| `errors` | every error when Salesforce returned several |
| `raw` | the first 4 KiB of the response body; don't log it, it can hold record data |

The error groups tell you where to look:

| Group | Typical cause |
|---|---|
| `AuthError` | login, token refresh, connected app settings |
| `RequestError` | the request itself: SOQL, field names, ids, permissions, API version |
| `RecordError` | the org rejected a write: validation rule, required field, duplicate rule, trigger, flow |
| `LimitError` | daily API limit, rate limit, or the library's own budget guard |
| `TransportError`, `ServerError` | network, timeouts, Salesforce outages |

For a `RecordError` from a trigger or flow, use the esf-probe skill to find the automation.

## Turn on debug output

Set `ESF_DEBUG` before the client is created. Each call prints one line to stderr:

```sh
ESF_DEBUG=1 python my_job.py        # one line per call, with status, time, rows and API usage
ESF_DEBUG=2 python my_job.py        # also every HTTP attempt, retries and headers
ESF_DEBUG=1 esf query "SELECT Id FROM Lead LIMIT 1"
```

Error lines quote the first 200 characters of Salesforce's message, which can hold record
values. Don't leave `ESF_DEBUG` on in production. `Authorization` and cookies are always
redacted.

## Log the events

The client never configures logging. Add an observer to see the `sf.request` event of each
call. Events never contain tokens, secrets or record values.

```python
import logging

logging.basicConfig(level=logging.INFO)
logged = esf.AsyncSalesforce(auth, observers=[esf.observability.StdlibLogging()])
await logged.query("SELECT Id FROM Lead LIMIT 1")
# INFO:esf:sf.request operation=query sobject=Lead method=GET path_template=/query status=200 ...

with esf.capture() as summary:
    await logged.query("SELECT Id FROM Lead LIMIT 5")
print(summary.as_fields())  # sf_calls, sf_ms, sf_errors, sf_retries, sf_refreshed, sf_api_used, ...
await logged.aclose()
```

In the `sf.request` event, read `attempts` and `retry_reasons` to see retries, and
`records_failed` for Collections rows that failed. `esf.observability.Structlog` and
`esf.observability.OpenTelemetry` work the same way.

## Check usage and limits

The client reads the org's API usage from every response, at no cost. `sf.limits()` and
`esf limits` cost one API call each.

```python
usage = sf.usage.snapshot()
print(usage.api_used, usage.api_max, usage.calls_by_operation, usage.errors_by_code)

limits = await sf.limits()
print(limits["DailyApiRequests"].remaining)
await sf.aclose()
```

`api_used` and `api_max` count the whole org, all integrations. The `calls_by_*` counters count
only this process. If you get `RequestLimitExceeded`, find the caller with
`sf.tagged(caller=...)` and `calls_by_caller`, then set an `esf.Budget`. `BudgetExceeded` is the
library's own guard, not Salesforce.

## Fix login problems

| Symptom | Cause | Fix |
|---|---|---|
| `SessionExpired` with `StaticToken` | the token expired or was revoked; a static token can't refresh | get a new token, or use a login flow that refreshes |
| `SessionExpired` after a refresh, with a hint about the connected app | the app lacks the `api` scope, or a session policy blocks it | check the app's OAuth scopes and session policies |
| `ReauthorizationRequired`, event `sf.auth.reauth_required` | the refresh token is expired, revoked or rotated out (`invalid_grant`) | the user must log in again |
| `ReauthorizationRequired` with `error_code="no_refresh_token"` | the token store has nothing under that `token_key` | finish the login callback, or check the `token_key` |
| `RefreshFailed` with `ambiguous=True` | a refresh request timed out; Salesforce may have issued a token | try again later; the client never resends a refresh |
| `TokenIdentityChanged` | the new or stored token belongs to another org or user, for example after a sandbox refresh | delete the stored token and log in again |
| `OAuthError` `invalid_grant` with `JwtBearer` | wrong user, certificate or audience, or the user isn't pre-authorized | read `exc.error_description`, then the JWT bearer page |

**Refresh-token rotation.** With rotation on, each refresh returns a new refresh token and makes
the old one unusable. A refresh also revokes the earlier access tokens of the grant. So:

- Don't give two processes copies of one refresh token. Share one durable token store with
  compare-and-swap instead. If two copies exist, the first refresh breaks the other, and
  reusing a rotated-out refresh token can make Salesforce revoke the whole chain.
- Don't force refreshes in tests or scripts against a shared org. Each forced refresh revokes
  the access tokens that other processes use.
- If another process refreshes, the client re-reads the store and resends the call with the
  newer token, at most twice. The event then shows `token_superseded` in `retry_reasons`. If the
  call still fails, the `SessionExpired` message says that another refresh superseded the token.
- A token from another tool, such as `sf org display`, stops working when that tool refreshes
  its grant.

## Things the client doesn't do

- It doesn't resend a write that timed out or got a 5xx, because the write may have committed.
  You get `RequestTimeout` or `ServerError`. Check the record before you send the write again.
- It doesn't log request bodies, field values or Salesforce error messages, unless you set
  `ESF_DEBUG`.

## More

- Errors and troubleshooting: https://raw.githubusercontent.com/Uttsavv/even-simpler-salesforce/main/docs/errors.md
- Logging: https://raw.githubusercontent.com/Uttsavv/even-simpler-salesforce/main/docs/logging.md
- Usage and budgets: https://raw.githubusercontent.com/Uttsavv/even-simpler-salesforce/main/docs/usage-and-budgets.md
- Login flows and refresh: https://raw.githubusercontent.com/Uttsavv/even-simpler-salesforce/main/docs/authentication/index.md
- Index of all docs pages: https://raw.githubusercontent.com/Uttsavv/even-simpler-salesforce/main/docs/llms.txt
