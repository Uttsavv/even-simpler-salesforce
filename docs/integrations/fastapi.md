# FastAPI

`SalesforceIntegration` creates one `AsyncSalesforce` when your app starts and closes it when the
app stops. Your routes get the client through a dependency.

```sh
pip install "even-simpler-salesforce[fastapi]"
```

```python
import os
from typing import Annotated, Any

from fastapi import Depends, FastAPI

import even_simpler_salesforce as esf
from even_simpler_salesforce.contrib.fastapi import SalesforceIntegration

esf_app = SalesforceIntegration(
    esf.ClientCredentials(
        domain=os.environ["SF_DOMAIN"],
        client_id=os.environ["SF_CLIENT_ID"],
        client_secret=os.environ["SF_CLIENT_SECRET"],
    ),
    error_handlers=True,
)
app = FastAPI(lifespan=esf_app.lifespan)
esf_app.init_app(app)

SF = Annotated[esf.AsyncSalesforce, Depends(esf_app.admin)]


@app.get("/leads/{lead_id}")
async def get_lead(lead_id: str, sf: SF) -> dict[str, Any]:
    return await sf.sobject("Lead").get(lead_id, fields=["Id", "LastName", "Company"])
```

All requests share the client, so all routes use one connection pool and one token.

Call `init_app(app)` before the app starts. It adds the error handlers and the middleware for
`on_summary`. Starlette does not accept new middleware after the first request.

## Log Salesforce calls per request

Pass `on_summary` to get the Salesforce calls of each HTTP request. The integration wraps each
request in [`esf.capture()`](../logging.md#per-request-summary-capture). After the response, it
calls `on_summary` with the request and the summary fields: `sf_calls`, `sf_ms`, `sf_errors`,
`sf_api_used` and more.

```python
import logging

log = logging.getLogger("app")


def log_summary(request, fields: dict[str, Any]) -> None:
    log.info("request.completed %s %s", request.method, request.url.path, extra=fields)


esf_app = SalesforceIntegration(
    esf.ClientCredentials(
        domain=os.environ["SF_DOMAIN"],
        client_id=os.environ["SF_CLIENT_ID"],
        client_secret=os.environ["SF_CLIENT_SECRET"],
    ),
    on_summary=log_summary,
)
```

The integration calls `on_summary` also when the route raised. It does not call it for
WebSocket connections.

## Map errors to HTTP responses

With `error_handlers=True`, the integration turns typed errors into JSON responses. Each body has
a stable `code` that your own clients can check.

```python
from fastapi.testclient import TestClient

with TestClient(app) as client:
    response = client.get("/leads/00Q000000000000AAA")  # no such lead

print(response.status_code, response.json())
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

Other errors stay unhandled, so FastAPI logs them and answers 500. A `MalformedQuery` for bad
SOQL is a bug in your app, not in the request.

To change one mapping, register your own handler. A handler for a subclass wins over the
integration's handler for its base class.

```python
from fastapi import Request
from fastapi.responses import JSONResponse


@app.exception_handler(esf.RowLocked)
async def row_locked(request: Request, exc: esf.RowLocked) -> JSONResponse:
    return JSONResponse({"code": "try_again"}, status_code=409)
```

!!! warning
    The `message` of a `RecordError` can quote record values. Return it only to callers that
    can see the record.

## Act as the logged-in user

Pass `auth_for` to get a client per user. It gets the request and returns that user's login
flow. The `user` dependency calls it and returns `client.with_auth(...)`: a child client that
shares the connection pool and loads the user's token from the token store.

```python
from fastapi import Request


def auth_for(request: Request) -> esf.Auth:
    return esf.WebServerFlow(
        domain=os.environ["SF_DOMAIN"],
        client_id=os.environ["SF_CLIENT_ID"],
        redirect_uri="https://app.example.com/oauth/callback",
        token_key=f"user:{request.state.user_id}",  # set by your own login middleware
    )


esf_app = SalesforceIntegration(
    esf.ClientCredentials(
        domain=os.environ["SF_DOMAIN"],
        client_id=os.environ["SF_CLIENT_ID"],
        client_secret=os.environ["SF_CLIENT_SECRET"],
    ),
    store=esf.AsyncInMemoryTokenStore(),  # use a shared store in production
    auth_for=auth_for,
)
UserSF = Annotated[esf.AsyncSalesforce, Depends(esf_app.user)]
```

`auth_for` can also be an `async` function. The [web server flow](../authentication/web-server-flow.md)
page shows how users log in and how their tokens get into the store.

## Use your own client

Pass `client` instead of a login flow to set more client options, such as `cursor_key` or
`observers`. The integration closes the client when the app stops.

```python
esf_app = SalesforceIntegration(
    client=esf.AsyncSalesforce(
        esf.ClientCredentials(
            domain=os.environ["SF_DOMAIN"],
            client_id=os.environ["SF_CLIENT_ID"],
            client_secret=os.environ["SF_CLIENT_SECRET"],
        ),
        cursor_key=b"a-long-random-secret",  # signs the cursors you send to browsers
    ),
)
```

## Run your own startup code

If your app has its own lifespan, enter the integration's lifespan inside it.

```python
from contextlib import asynccontextmanager


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with esf_app.lifespan(app):
        # The client exists here, also for code outside routes.
        print(esf_app.client.api_version)
        yield


app = FastAPI(lifespan=lifespan)
esf_app.init_app(app)
```

## Test your routes

Override the dependency with the [`esf_async_fake`](../testing.md#pytest-plugin) fixture. Your
routes then run against an in-memory org.

```python
from even_simpler_salesforce.testing import AsyncFakeSalesforce

app = FastAPI(lifespan=esf_app.lifespan)
esf_app.init_app(app)
SF = Annotated[esf.AsyncSalesforce, Depends(esf_app.admin)]


@app.get("/leads/{lead_id}")
async def get_lead(lead_id: str, sf: SF) -> dict[str, Any]:
    return await sf.sobject("Lead").get(lead_id, fields=["Id", "LastName", "Company"])


def test_get_lead(esf_async_fake: AsyncFakeSalesforce) -> None:
    lead_id = esf_async_fake.add("Lead", {"LastName": "Doe", "Company": "Acme"})
    app.dependency_overrides[esf_app.admin] = lambda: esf_async_fake

    with TestClient(app) as client:
        response = client.get(f"/leads/{lead_id}")

    assert response.json()["Company"] == "Acme"
    app.dependency_overrides.clear()
```

To check the error handlers and `on_summary` against the fake too, pass it as
`SalesforceIntegration(client=esf_async_fake, ...)` in an app factory.

## The example app

[`examples/fastapi_app`](https://github.com/Uttsavv/even-simpler-salesforce/tree/main/examples/fastapi_app)
is a complete app that uses the integration. Its tests run in CI. It also shows:

- queries built with `esf.soql()` and paginated with [cursors](../guides/query.md#cursors) that are
  safe to hand to a browser;
- single-record reads and writes;
- a bulk update through [sObject Collections](../guides/collections.md) that reports partial
  failures per id;
- tests with the fakes;
- one test of the real client against mocked HTTP.
