# FastAPI

!!! note "Planned for 0.2"
    The packaged integration `even_simpler_salesforce.contrib.fastapi` is planned for **0.2**.

**Works today:** the example app
[`examples/fastapi_app`](https://github.com/Uttsavv/even-simpler-salesforce/tree/main/examples/fastapi_app)
uses only the public 0.1 API, and its tests run in CI. It shows:

- one `AsyncSalesforce` created and closed in the app's lifespan;
- each HTTP request wrapped in [`esf.capture()`](../logging.md#per-request-summary-capture), with
  the summary (`sf_calls`, `sf_ms`, `sf_errors`, `sf_api_used`, ...) logged on the app's
  `request.completed` line;
- queries built with `esf.soql()` and paginated with [cursors](../guides/query.md#cursors) that
  are safe to hand to a browser;
- single-record reads and writes, and a bulk update through
  [sObject Collections](../guides/collections.md) that reports partial failures per id;
- typed errors mapped to HTTP responses (`NotFound` → 404, `RecordError` → 422, `MalformedQuery` → 400, `LimitError` → 503);
- tests that inject the [`esf_fake` / `esf_async_fake`](../testing.md#pytest-plugin) fixtures,
  plus one test of the real client against mocked HTTP.

Copy the wiring from the example into your app. The 0.2 package will provide the same pieces
(client lifecycle, dependency or extension, summary middleware) ready-made.
