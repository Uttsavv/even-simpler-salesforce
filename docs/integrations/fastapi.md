# FastAPI

Create one `AsyncSalesforce` in the app's lifespan and hand it to your routes with a dependency.
The packaged integration `even_simpler_salesforce.contrib.fastapi` is planned for 0.2. Until then,
copy the wiring below or from the [example app](#the-example-app).

```python
import os
from contextlib import asynccontextmanager
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Request

import even_simpler_salesforce as esf


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.sf = esf.AsyncSalesforce(
        esf.ClientCredentials(
            domain=os.environ["SF_DOMAIN"],
            client_id=os.environ["SF_CLIENT_ID"],
            client_secret=os.environ["SF_CLIENT_SECRET"],
        )
    )
    try:
        yield
    finally:
        await app.state.sf.aclose()


def get_sf(request: Request) -> esf.AsyncSalesforce:
    return request.app.state.sf


SF = Annotated[esf.AsyncSalesforce, Depends(get_sf)]
app = FastAPI(lifespan=lifespan)


@app.get("/leads/{lead_id}")
async def get_lead(lead_id: str, sf: SF) -> dict[str, Any]:
    return await sf.sobject("Lead").get(lead_id, fields=["Id", "LastName", "Company"])
```

Every request shares the client, so all routes use one connection pool and one token.

## Map errors to HTTP responses

Each Salesforce error has its own class. Turn a class into a status code with an exception
handler.

```python
from fastapi.responses import JSONResponse


@app.exception_handler(esf.NotFound)
async def not_found(request: Request, exc: esf.NotFound) -> JSONResponse:
    return JSONResponse({"error_code": exc.error_code, "message": "not found"}, status_code=404)
```

The example app maps these classes:

| Error | Status |
|---|---|
| `NotFound` | 404 |
| `RecordError` | 422 |
| `MalformedQuery` (a bad cursor) | 400 |
| `LimitError` | 503, with `Retry-After` |

## Log Salesforce calls per request

Wrap each HTTP request in [`esf.capture()`](../logging.md#per-request-summary-capture). Then add
the summary fields to your app's request log line: `sf_calls`, `sf_ms`, `sf_errors`, `sf_api_used`
and more.

```python
import logging

log = logging.getLogger("app")


class SalesforceSummaryMiddleware:
    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        with esf.capture() as summary:
            try:
                await self.app(scope, receive, send)
            finally:
                log.info("request.completed", extra=summary.as_fields())


app.add_middleware(SalesforceSummaryMiddleware)
```

## Test your routes

Override the dependency with the [`esf_async_fake`](../testing.md#pytest-plugin) fixture. Your
routes then run against an in-memory org.

```python
from fastapi.testclient import TestClient

from even_simpler_salesforce.testing import AsyncFakeSalesforce


def test_get_lead(esf_async_fake: AsyncFakeSalesforce) -> None:
    lead_id = esf_async_fake.add("Lead", {"LastName": "Doe", "Company": "Acme"})
    app.dependency_overrides[get_sf] = lambda: esf_async_fake

    response = TestClient(app).get(f"/leads/{lead_id}")

    assert response.json()["Company"] == "Acme"
    app.dependency_overrides.clear()
```

## The example app

[`examples/fastapi_app`](https://github.com/Uttsavv/even-simpler-salesforce/tree/main/examples/fastapi_app)
is a complete app that uses only the public 0.1 API. Its tests run in CI. It also shows:

- queries built with `esf.soql()` and paginated with [cursors](../guides/query.md#cursors) that are
  safe to hand to a browser;
- single-record reads and writes;
- a bulk update through [sObject Collections](../guides/collections.md) that reports partial
  failures per id;
- tests with the fakes;
- one test of the real client against mocked HTTP.

The 0.2 package will give you the same pieces ready-made: the client lifecycle, the dependency and
the summary middleware.
