# Flask

The `Salesforce` extension keeps one sync `Salesforce` client per app and process. All threads
share it. Your views get it from `ext.client`.

```sh
pip install "even-simpler-salesforce[flask]"
```

```python
import os
from typing import Any

from flask import Flask

import even_simpler_salesforce as esf
from even_simpler_salesforce.contrib.flask import Salesforce

sf_ext = Salesforce(
    auth=esf.ClientCredentials(
        domain=os.environ["SF_DOMAIN"],
        client_id=os.environ["SF_CLIENT_ID"],
        client_secret=os.environ["SF_CLIENT_SECRET"],
    ),
    error_handlers=True,
)


def create_app() -> Flask:
    app = Flask("leads")
    sf_ext.init_app(app)

    @app.get("/leads/<lead_id>")
    def get_lead(lead_id: str) -> dict[str, Any]:
        return sf_ext.client.sobject("Lead").get(lead_id, fields=["Id", "LastName", "Company"])

    return app


app = create_app()
```

`init_app()` creates the client of that app. The extension closes the client when the process
exits. `sf_ext.client` needs an app context, so use it in views, CLI commands and tests.

You can also pass the app directly: `Salesforce(app, auth=...)`.

## Log Salesforce calls per request

Pass `on_summary` to get the Salesforce calls of each request. The extension starts an
[`esf.capture()`](../logging.md#per-request-summary-capture) before each request. When Flask tears
the request down, the extension calls `on_summary` with the request and the summary fields:
`sf_calls`, `sf_ms`, `sf_errors`, `sf_api_used` and more.

```python
import logging

log = logging.getLogger("app")


def log_summary(request, fields: dict[str, Any]) -> None:
    # %r escapes control characters, so a crafted path can't forge log lines.
    log.info("request.completed %s %r", request.method, request.path, extra=fields)


sf_ext = Salesforce(
    auth=esf.ClientCredentials(
        domain=os.environ["SF_DOMAIN"],
        client_id=os.environ["SF_CLIENT_ID"],
        client_secret=os.environ["SF_CLIENT_SECRET"],
    ),
    on_summary=log_summary,
)
```

The extension calls `on_summary` also when the view raised. Call `init_app()` before you
register `before_request` hooks that call Salesforce. Otherwise the summary misses their calls.

## Map errors to HTTP responses

With `error_handlers=True`, the extension turns typed errors into JSON responses. Each body has a
stable `code` that your own clients can check.

```python
response = create_app().test_client().get("/leads/00Q000000000000AAA")  # no such lead

print(response.status_code, response.json)
# 404 {'code': 'salesforce_not_found', 'error_code': 'NOT_FOUND'}
```

| Error | Status | `code` |
|---|---|---|
| `ReauthorizationRequired` | 401 | `salesforce_reauth_required` |
| `RecordError` | 422, with `error_code`, `message` and `fields` | `salesforce_record_error` |
| `NotFound` | 404 | `salesforce_not_found` |
| `MalformedQuery` for an invalid [cursor](../guides/query.md#cursors) | 400 | `salesforce_invalid_cursor` |
| `LimitError` | 503, with `Retry-After` | `salesforce_limit` |
| `TransportError` | 502 | `salesforce_unavailable` |
| `ServerError`, `EventPublishUnavailable` | 503 | `salesforce_unavailable` |

Other errors stay unhandled, so Flask logs them and answers 500. To change one mapping, register
your own handler. A handler for a subclass wins over the extension's handler for its base class.

```python
@app.errorhandler(esf.RowLocked)
def row_locked(exc: esf.RowLocked):
    return {"code": "try_again"}, 409
```

!!! warning
    The `message` of a `RecordError` can quote record values. Return it only to callers that
    can see the record.

## Use your own client

Pass `client` instead of a login flow to set more client options, such as `cursor_key` or
`observers`. The extension closes the client when the process exits.

```python
sf_ext = Salesforce(
    client=esf.Salesforce(
        esf.ClientCredentials(
            domain=os.environ["SF_DOMAIN"],
            client_id=os.environ["SF_CLIENT_ID"],
            client_secret=os.environ["SF_CLIENT_SECRET"],
        ),
        cursor_key=b"a-long-random-secret",  # signs the cursors you send to browsers
    ),
)
```

## Test your routes

Give your app factory a `client` argument. Your tests then pass the
[`esf_fake`](../testing.md#pytest-plugin) fixture, and your views run against an in-memory org.

```python
from even_simpler_salesforce.testing import FakeSalesforce


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
    ext = Salesforce(app, client=client, error_handlers=True)

    @app.get("/leads/<lead_id>")
    def get_lead(lead_id: str) -> dict[str, Any]:
        return ext.client.sobject("Lead").get(lead_id, fields=["Id", "LastName", "Company"])

    return app


def test_get_lead(esf_fake: FakeSalesforce) -> None:
    lead_id = esf_fake.add("Lead", {"LastName": "Doe", "Company": "Acme"})

    response = create_app(esf_fake).test_client().get(f"/leads/{lead_id}")

    assert response.json["Company"] == "Acme"
```

## The example app

[`examples/flask_app`](https://github.com/Uttsavv/even-simpler-salesforce/tree/main/examples/flask_app)
is a complete app that uses the extension. Its tests run in CI. It also shows:

- queries built with `esf.soql()` and paginated with [cursors](../guides/query.md#cursors) that are
  safe to hand to a browser;
- single-record reads and writes;
- a bulk update through [sObject Collections](../guides/collections.md) that reports partial
  failures per id;
- tests with the fakes;
- one test of the real client against mocked HTTP.
