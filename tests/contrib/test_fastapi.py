"""``even_simpler_salesforce.contrib.fastapi`` driven through FastAPI's own ``TestClient``.

Framework behaviour these tests rely on:

- Lifespan: ``TestClient`` used as a context manager runs startup and shutdown
  (https://fastapi.tiangolo.com/advanced/events/, https://fastapi.tiangolo.com/advanced/testing-events/).
- ``app.dependency_overrides`` is keyed by the original dependency callable
  (https://fastapi.tiangolo.com/advanced/testing-dependencies/).
- Exception handlers are looked up along the exception's MRO, so an app's handler for a subclass
  wins over the integration's handler for the base class
  (https://fastapi.tiangolo.com/tutorial/handling-errors/#install-custom-exception-handlers).
"""

# No ``from __future__ import annotations``: FastAPI must resolve the route annotations, which
# use aliases local to ``build()``.
import asyncio
from collections.abc import Iterator
from typing import Annotated, Any

import httpx
import pytest
import respx
from fastapi import Depends, FastAPI, Request, WebSocket
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

import even_simpler_salesforce as esf
from even_simpler_salesforce.contrib.fastapi import SalesforceIntegration
from even_simpler_salesforce.testing import AsyncFakeSalesforce

INSTANCE = "https://acme.my.salesforce.com"


def build(integration: SalesforceIntegration) -> FastAPI:
    app = FastAPI(lifespan=integration.lifespan)
    integration.init_app(app)
    admin = Annotated[esf.AsyncSalesforce, Depends(integration.admin)]
    user = Annotated[esf.AsyncSalesforce, Depends(integration.user)]

    @app.get("/leads/{lead_id}")
    async def get_lead(lead_id: str, sf: admin) -> dict[str, Any]:
        return await sf.sobject("Lead").get(lead_id, fields=["Id", "LastName"])

    @app.get("/me/leads/{lead_id}")
    async def my_lead(lead_id: str, sf: user) -> dict[str, Any]:
        return await sf.sobject("Lead").get(lead_id, fields=["Id", "LastName"])

    @app.get("/count")
    async def count(sf: admin) -> dict[str, int]:
        return {"n": await sf.count("SELECT COUNT() FROM Lead")}

    @app.websocket("/ws")
    async def ws(websocket: WebSocket) -> None:
        await websocket.accept()
        await websocket.send_text("hi")
        await websocket.close()

    return app


@pytest.fixture
def summaries() -> list[tuple[str, dict[str, Any]]]:
    return []


@pytest.fixture
def user_auths() -> list[esf.Auth]:
    return []


@pytest.fixture
def client(
    esf_async_fake: AsyncFakeSalesforce,
    summaries: list[tuple[str, dict[str, Any]]],
    user_auths: list[esf.Auth],
) -> Iterator[TestClient]:
    def auth_for(request: Request) -> esf.Auth:
        user_auths.append(esf.StaticToken(f"user-{request.headers['x-user']}", INSTANCE))
        return user_auths[-1]

    integration = SalesforceIntegration(
        client=esf_async_fake,
        auth_for=auth_for,
        on_summary=lambda request, fields: summaries.append((request.url.path, fields)),
        error_handlers=True,
    )
    with TestClient(build(integration), raise_server_exceptions=False) as tc:
        yield tc


def test_needs_exactly_one_of_auth_or_client(esf_async_fake: AsyncFakeSalesforce) -> None:
    with pytest.raises(TypeError, match="exactly one"):
        SalesforceIntegration()
    with pytest.raises(TypeError, match="exactly one"):
        SalesforceIntegration(esf.StaticToken("t", INSTANCE), client=esf_async_fake)


def test_admin_dependency_and_summary(
    client: TestClient, esf_async_fake: AsyncFakeSalesforce, summaries: list[tuple[str, dict[str, Any]]]
) -> None:
    lead_id = esf_async_fake.add("Lead", {"LastName": "Doe", "Company": "Acme"})
    resp = client.get(f"/leads/{lead_id}")
    assert resp.status_code == 200
    assert resp.json()["LastName"] == "Doe"
    path, fields = summaries[-1]
    assert path == f"/leads/{lead_id}"
    assert fields["sf_calls"] == 1
    assert fields["sf_ops"] == {"sobject.get": 1}


def test_user_dependency_uses_auth_for(
    client: TestClient, esf_async_fake: AsyncFakeSalesforce, user_auths: list[esf.Auth]
) -> None:
    lead_id = esf_async_fake.add("Lead", {"LastName": "Doe", "Company": "Acme"})
    assert client.get(f"/me/leads/{lead_id}", headers={"x-user": "42"}).status_code == 200
    assert [a.access_token for a in user_auths] == ["user-42"]  # type: ignore[attr-defined]


def test_async_auth_for_with_real_client() -> None:
    async def auth_for(request: Request) -> esf.Auth:
        await asyncio.sleep(0)
        return esf.StaticToken(f"user-{request.headers['x-user']}", INSTANCE)

    integration = SalesforceIntegration(esf.StaticToken("app-token", INSTANCE), auth_for=auth_for)
    with respx.mock:
        route = respx.get(url__regex=rf"^{INSTANCE}/services/data/v[\d.]+/sobjects/Lead/00Q").respond(
            json={"attributes": {"type": "Lead"}, "Id": "00Q000000000001AAA", "LastName": "Doe"}
        )
        with TestClient(build(integration)) as tc:
            assert tc.get("/leads/00Q000000000001AAA").status_code == 200
            assert tc.get("/me/leads/00Q000000000001AAA", headers={"x-user": "7"}).status_code == 200
        auths = [c.request.headers["Authorization"] for c in route.calls]
    assert auths == ["Bearer app-token", "Bearer user-7"]


def test_user_dependency_needs_auth_for(esf_async_fake: AsyncFakeSalesforce) -> None:
    integration = SalesforceIntegration(client=esf_async_fake)
    with TestClient(build(integration)) as tc, pytest.raises(RuntimeError, match="auth_for"):
        tc.get("/me/leads/00Q000000000001AAA")


def test_store_is_used_for_the_app_client() -> None:
    store = esf.AsyncInMemoryTokenStore()
    integration = SalesforceIntegration(esf.StaticToken("t", INSTANCE), store=store)
    with TestClient(FastAPI(lifespan=integration.lifespan)):
        assert integration.client._store is store


def test_lifespan_creates_and_closes_the_client(esf_async_fake: AsyncFakeSalesforce) -> None:
    integration = SalesforceIntegration(client=esf_async_fake)
    with pytest.raises(RuntimeError, match="lifespan"):
        _ = integration.client
    with TestClient(build(integration)):
        assert integration.client is esf_async_fake  # type: ignore[comparison-overlap]
        assert not esf_async_fake.closed
    assert esf_async_fake.closed
    with pytest.raises(RuntimeError, match="lifespan"):
        integration.admin()


def test_built_client_is_closed_at_shutdown() -> None:
    integration = SalesforceIntegration(esf.StaticToken("t", INSTANCE))
    with TestClient(build(integration)):
        sf = integration.client
        assert isinstance(sf, esf.AsyncSalesforce)
    assert sf._transport.http.is_closed  # type: ignore[attr-defined]


def test_lifespan_refuses_to_start_without_init_app(esf_async_fake: AsyncFakeSalesforce) -> None:
    integration = SalesforceIntegration(client=esf_async_fake, error_handlers=True)
    app = FastAPI(lifespan=integration.lifespan)
    with pytest.raises(RuntimeError, match="init_app"), TestClient(app):
        pass


def test_composes_with_the_apps_own_lifespan(esf_async_fake: AsyncFakeSalesforce) -> None:
    from contextlib import asynccontextmanager

    integration = SalesforceIntegration(client=esf_async_fake)
    events: list[str] = []

    @asynccontextmanager
    async def lifespan(app: FastAPI):  # type: ignore[no-untyped-def]
        async with integration.lifespan(app):
            events.append("started")
            yield
        events.append("stopped")

    with TestClient(FastAPI(lifespan=lifespan)):
        assert integration.client is esf_async_fake  # type: ignore[comparison-overlap]
    assert events == ["started", "stopped"]
    assert esf_async_fake.closed


def test_dependency_overrides_swap_the_client(esf_async_fake: AsyncFakeSalesforce) -> None:
    integration = SalesforceIntegration(esf.StaticToken("t", INSTANCE))
    app = build(integration)
    app.dependency_overrides[integration.admin] = lambda: esf_async_fake
    lead_id = esf_async_fake.add("Lead", {"LastName": "Doe", "Company": "Acme"})
    with TestClient(app) as tc:
        assert tc.get(f"/leads/{lead_id}").json()["LastName"] == "Doe"


@pytest.mark.parametrize(
    ("error", "status", "code"),
    [
        (
            esf.ReauthorizationRequired("revoked", error_code="invalid_grant"),
            401,
            "salesforce_reauth_required",
        ),
        (
            esf.ValidationRuleFailed("bad", error_code="FIELD_CUSTOM_VALIDATION_EXCEPTION"),
            422,
            "salesforce_record_error",
        ),
        (esf.NotFound("gone", error_code="NOT_FOUND"), 404, "salesforce_not_found"),
        (esf.MalformedQuery("invalid cursor", error_code="INVALID_CURSOR"), 400, "salesforce_invalid_cursor"),
        (
            esf.RequestLimitExceeded("busy", error_code="REQUEST_LIMIT_EXCEEDED", retry_after=5),
            503,
            "salesforce_limit",
        ),
        (esf.RequestTimeout("timed out"), 502, "salesforce_unavailable"),
        (esf.ServerError("down", error_code="SERVER_UNAVAILABLE"), 503, "salesforce_unavailable"),
    ],
)
def test_error_handlers(
    client: TestClient, esf_async_fake: AsyncFakeSalesforce, error: Exception, status: int, code: str
) -> None:
    esf_async_fake.fail_next("sobject.get", error)
    resp = client.get("/leads/00Q000000000001AAA")
    assert resp.status_code == status
    assert resp.json()["code"] == code
    if status == 503 and code == "salesforce_limit":
        assert resp.headers["Retry-After"] == "5"


def test_unmapped_errors_stay_500(
    client: TestClient, esf_async_fake: AsyncFakeSalesforce, summaries: list[tuple[str, dict[str, Any]]]
) -> None:
    esf_async_fake.fail_next("query", esf.MalformedQuery("unexpected token", error_code="MALFORMED_QUERY"))
    assert client.get("/count").status_code == 500
    assert summaries[-1][1]["sf_errors"] == ["MALFORMED_QUERY"]  # reported even when the request failed


def test_handlers_off_by_default(esf_async_fake: AsyncFakeSalesforce) -> None:
    integration = SalesforceIntegration(client=esf_async_fake)
    esf_async_fake.fail_next("sobject.get", esf.NotFound("gone"))
    with TestClient(build(integration), raise_server_exceptions=False) as tc:
        assert tc.get("/leads/00Q000000000001AAA").status_code == 500


def test_app_handler_for_a_subclass_wins(esf_async_fake: AsyncFakeSalesforce) -> None:
    integration = SalesforceIntegration(client=esf_async_fake, error_handlers=True)
    app = build(integration)

    @app.exception_handler(esf.RowLocked)
    async def locked(request: Request, exc: esf.RowLocked) -> JSONResponse:
        return JSONResponse({"code": "busy"}, status_code=409)

    esf_async_fake.fail_next("sobject.get", esf.RowLocked("locked", error_code="UNABLE_TO_LOCK_ROW"))
    with TestClient(app) as tc:
        resp = tc.get("/leads/00Q000000000001AAA")
    assert (resp.status_code, resp.json()) == (409, {"code": "busy"})


def test_websockets_pass_through(client: TestClient, summaries: list[tuple[str, dict[str, Any]]]) -> None:
    with client.websocket_connect("/ws") as ws:
        assert ws.receive_text() == "hi"
    assert summaries == []


@respx.mock
def test_real_client_summary_fields(summaries: list[tuple[str, dict[str, Any]]]) -> None:
    respx.get(url__regex=rf"^{INSTANCE}/services/data/v[\d.]+/query").mock(
        return_value=httpx.Response(
            200,
            json={"totalSize": 3, "done": True, "records": []},
            headers={"Sforce-Limit-Info": "api-usage=1200/15000"},
        )
    )
    integration = SalesforceIntegration(
        esf.StaticToken("t", INSTANCE),
        on_summary=lambda request, fields: summaries.append((request.method, fields)),
    )
    with TestClient(build(integration)) as tc:
        assert tc.get("/count").json() == {"n": 3}
    method, fields = summaries[-1]
    assert method == "GET"
    assert fields["sf_calls"] == 1
    assert fields["sf_api_used"] == 1200
    assert fields["sf_api_max"] == 15000
