# Flask

Create one sync `Salesforce` client per process and keep it in `app.extensions`. All threads share
it. The packaged integration `even_simpler_salesforce.contrib.flask` is planned for 0.2. Until
then, copy the wiring below or from the [example app](#the-example-app).

```python
import atexit
import os
from typing import Any

from flask import Flask, current_app

import even_simpler_salesforce as esf


def create_app(client: Any = None) -> Flask:
    app = Flask("leads")
    if client is None:
        client = esf.Salesforce(
            esf.ClientCredentials(
                domain=os.environ["SF_DOMAIN"],
                client_id=os.environ["SF_CLIENT_ID"],
                client_secret=os.environ["SF_CLIENT_SECRET"],
            )
        )
        atexit.register(client.close)
    app.extensions["salesforce"] = client

    @app.get("/leads/<lead_id>")
    def get_lead(lead_id: str) -> dict[str, Any]:
        sf = current_app.extensions["salesforce"]
        return sf.sobject("Lead").get(lead_id, fields=["Id", "LastName", "Company"])

    return app


app = create_app()
```

The `client` argument lets your tests pass in a fake instead of the real client.

## Map errors to HTTP responses

Each Salesforce error has its own class. Turn a class into a status code with an error handler.
In your app, register handlers and middleware inside `create_app()`, so every app gets them.

```python
from flask import jsonify


@app.errorhandler(esf.NotFound)
def not_found(exc: esf.NotFound):
    return jsonify({"error_code": exc.error_code, "message": "not found"}), 404
```

The example app maps these classes:

| Error | Status |
|---|---|
| `NotFound` | 404 |
| `RecordError` | 422 |
| `MalformedQuery` (a bad cursor) | 400 |
| `LimitError` | 503, with `Retry-After` |

## Log Salesforce calls per request

Wrap each HTTP request in [`esf.capture()`](../logging.md#per-request-summary-capture) with a WSGI
middleware. Then add the summary fields to your app's request log line: `sf_calls`, `sf_ms`,
`sf_errors`, `sf_api_used` and more.

```python
import logging

log = logging.getLogger("app")


class SalesforceSummaryMiddleware:
    def __init__(self, wsgi_app) -> None:
        self.wsgi_app = wsgi_app

    def __call__(self, environ, start_response):
        with esf.capture() as summary:
            try:
                return self.wsgi_app(environ, start_response)
            finally:
                log.info("request.completed", extra=summary.as_fields())


app.wsgi_app = SalesforceSummaryMiddleware(app.wsgi_app)
```

## Test your routes

Pass the [`esf_fake`](../testing.md#pytest-plugin) fixture to `create_app()`. Your routes then
run against an in-memory org.

```python
from even_simpler_salesforce.testing import FakeSalesforce


def test_get_lead(esf_fake: FakeSalesforce) -> None:
    lead_id = esf_fake.add("Lead", {"LastName": "Doe", "Company": "Acme"})

    response = create_app(esf_fake).test_client().get(f"/leads/{lead_id}")

    assert response.json["Company"] == "Acme"
```

## The example app

[`examples/flask_app`](https://github.com/Uttsavv/even-simpler-salesforce/tree/main/examples/flask_app)
is a complete app that uses only the public 0.1 API. Its tests run in CI. It also shows:

- queries built with `esf.soql()` and paginated with [cursors](../guides/query.md#cursors) that are
  safe to hand to a browser;
- single-record reads and writes;
- a bulk update through [sObject Collections](../guides/collections.md) that reports partial
  failures per id;
- tests with the fakes;
- one test of the real client against mocked HTTP.

The 0.2 package will give you the same pieces ready-made: the client lifecycle, the extension and
the summary middleware.
