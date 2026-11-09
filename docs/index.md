---
template: home.html
hide:
  - navigation
  - toc
---

# even-simpler-salesforce

## Beyond simple-salesforce

Coming from simple-salesforce? Here is what you get on top. Most of it needs no code at all.

<div class="esf-compare" markdown>

| | simple-salesforce | even-simpler-salesforce |
|---|---|---|
| Async | Sync only | [`AsyncSalesforce`](getting-started.md#async-or-sync), and a sync client with the same methods |
| Expired session | Raises `SalesforceExpiredSession` | [Refreshes the token](authentication/index.md#how-tokens-are-handled) and sends the call again |
| Browser login | Not available | [Web server flow with PKCE](authentication/web-server-flow.md) |
| Saved tokens | Not available | [Token stores](authentication/token-stores.md), safe under refresh-token rotation |
| Errors | One class per HTTP status | [One class per Salesforce error code](errors.md), such as `ValidationRuleFailed` and `RowLocked` |
| Retries | None built in | [Retries reads](errors.md#retries), and never sends a write twice by accident |
| API limits | Reads usage into `api_usage` | A [budget guard](usage-and-budgets.md#budgets) that stops calls before you hit the org limit |
| Logs | No structured events | [One event per call](logging.md), secrets redacted, OpenTelemetry and structlog |
| Tests | No test helpers | [In-memory fake, recorder and replayer](testing.md), and a pytest plugin |
| Moving over | By hand | [`esf migrate`](migrate.md) rewrites the mechanical part of your code |

</div>

## Start here

<div class="grid cards" markdown>

-   **[Getting started](getting-started.md)**

    Install the library and make your first call in five minutes.

-   **[Choose a login flow](authentication/index.md)**

    Client credentials, JWT bearer, web server flow or a static token.

-   **[Work with records](guides/records.md)**

    Create, read, update and delete records, then query them.

</div>

!!! note "Alpha (0.1)"
    Breaking changes are possible in 0.x releases. See the
    [project status](getting-started.md#project-status) for what is covered and where it has been tested.
