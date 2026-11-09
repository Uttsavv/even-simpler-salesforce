"""Tests for the FastAPI example: an injected fake, plus one respx-mocked real client."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

import even_simpler_salesforce as esf
from even_simpler_salesforce.testing import AsyncFakeSalesforce
from fastapi_app.app import create_app

LIST_SOQL = (
    "SELECT Id, FirstName, LastName, Company, Email, Status FROM Lead "
    "WHERE Status = 'Open' ORDER BY CreatedDate DESC"
)


@pytest.fixture
def summaries() -> list[dict[str, Any]]:
    return []


@pytest.fixture
def client(esf_async_fake: AsyncFakeSalesforce, summaries: list[dict[str, Any]]) -> Iterator[TestClient]:
    app = create_app(lambda: esf_async_fake, on_summary=lambda scope, fields: summaries.append(fields))
    with TestClient(app) as tc:
        yield tc


def test_list_leads_with_cursor_pagination(
    client: TestClient, esf_async_fake: AsyncFakeSalesforce, summaries: list[dict[str, Any]]
) -> None:
    ids = esf_async_fake.add_many(
        "Lead", [{"LastName": f"L{i}", "Company": "Acme", "Status": "Open"} for i in range(450)]
    )
    esf_async_fake.on_query(LIST_SOQL, rows=lambda q: esf_async_fake.records("Lead"))
    seen: list[str] = []
    resp = client.get("/leads", params={"status": "Open"})
    pages = 0
    while True:
        assert resp.status_code == 200
        body = resp.json()
        pages += 1
        assert body["total_size"] == 450
        assert all("attributes" not in r for r in body["records"])
        seen.extend(r["Id"] for r in body["records"])
        if not body["next_cursor"]:
            break
        resp = client.get("/leads", params={"cursor": body["next_cursor"]})
    assert pages == 3
    assert seen == ids
    assert [s["sf_ops"] for s in summaries] == [{"query": 1}, {"query_more": 1}, {"query_more": 1}]


def test_tampered_cursor_is_400(client: TestClient) -> None:
    resp = client.get("/leads", params={"cursor": "not-a-cursor"})
    assert resp.status_code == 400
    assert resp.json()["error_code"] == "INVALID_CURSOR"


def test_status_is_quoted_safely(client: TestClient, esf_async_fake: AsyncFakeSalesforce) -> None:
    esf_async_fake.on_query(lambda q: "WHERE Status = 'x\\' OR Name != \\''" in q, rows=[])
    assert client.get("/leads", params={"status": "x' OR Name != '"}).json()["records"] == []


def test_get_create_update_lead(
    client: TestClient, esf_async_fake: AsyncFakeSalesforce, summaries: list[dict[str, Any]]
) -> None:
    resp = client.post("/leads", json={"LastName": "Doe", "Company": "Acme", "Email": "doe@example.com"})
    assert resp.status_code == 201
    lead_id = resp.json()["id"]
    assert lead_id.startswith("00Q")

    resp = client.patch(f"/leads/{lead_id}", json={"Status": "Working"})
    assert resp.status_code == 204

    resp = client.get(f"/leads/{lead_id}")
    assert resp.status_code == 200
    assert resp.json() == {
        "Id": lead_id,
        "FirstName": None,
        "LastName": "Doe",
        "Company": "Acme",
        "Email": "doe@example.com",
        "Status": "Working",
    }
    update = esf_async_fake.calls_for("sobject.update")[0]
    assert update.args["data"] == {"Status": "Working"}
    assert summaries[-1]["sf_calls"] == 1
    assert summaries[-1]["sf_ops"] == {"sobject.get": 1}


def test_missing_lead_is_404(client: TestClient, esf_async_fake: AsyncFakeSalesforce) -> None:
    resp = client.get(f"/leads/{esf_async_fake.new_id('Lead')}")
    assert resp.status_code == 404
    assert client.get("/leads/not-an-id").status_code == 422  # validated before calling Salesforce


def test_validation_rule_is_422(
    client: TestClient, esf_async_fake: AsyncFakeSalesforce, summaries: list[dict[str, Any]]
) -> None:
    lead_id = esf_async_fake.add("Lead", {"LastName": "Doe", "Company": "Acme"})
    esf_async_fake.fail_next(
        "sobject.update",
        esf.ValidationRuleFailed(
            "Email is invalid", error_code="FIELD_CUSTOM_VALIDATION_EXCEPTION", fields=("Email",)
        ),
    )
    resp = client.patch(f"/leads/{lead_id}", json={"Email": "nope"})
    assert resp.status_code == 422
    assert resp.json() == {
        "error_code": "FIELD_CUSTOM_VALIDATION_EXCEPTION",
        "message": "Email is invalid",
        "fields": ["Email"],
    }
    assert summaries[-1]["sf_errors"] == ["FIELD_CUSTOM_VALIDATION_EXCEPTION"]


def test_rate_limited_is_503(client: TestClient, esf_async_fake: AsyncFakeSalesforce) -> None:
    esf_async_fake.fail_next(
        "query", esf.RequestLimitExceeded("too many", error_code="REQUEST_LIMIT_EXCEEDED")
    )
    resp = client.get("/leads")
    assert resp.status_code == 503
    assert resp.headers["Retry-After"] == "30"


def test_bulk_status_reports_partial_failures(
    client: TestClient, esf_async_fake: AsyncFakeSalesforce
) -> None:
    ids = esf_async_fake.add_many("Lead", [{"LastName": f"L{i}", "Company": "Acme"} for i in range(250)])
    locked = ids[7]
    esf_async_fake.reject(
        "Lead",
        esf.RowLocked("unable to obtain exclusive access", error_code="UNABLE_TO_LOCK_ROW"),
        when=lambda d: d.get("Id") == locked,
    )
    missing = esf_async_fake.new_id("Lead")
    resp = client.post("/leads/bulk-status", json={"ids": [*ids, missing], "status": "Working"})
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["updated"]) == 249
    assert [f["id"] for f in body["failed"]] == [locked, missing]
    assert body["failed"][0]["errors"][0]["error_code"] == "UNABLE_TO_LOCK_ROW"
    assert esf_async_fake.record("Lead", ids[0])["Status"] == "Working"  # type: ignore[index]
    assert esf_async_fake.record("Lead", locked).get("Status") is None  # type: ignore[union-attr]
    assert client.post("/leads/bulk-status", json={"ids": [], "status": "x"}).status_code == 422


def test_client_closed_on_shutdown(esf_async_fake: AsyncFakeSalesforce) -> None:
    with TestClient(create_app(lambda: esf_async_fake)):
        assert not esf_async_fake.closed
    assert esf_async_fake.closed


# --- the real client, mocked at the HTTP layer --------------------------------------------

INSTANCE = "https://acme.my.salesforce.com"


@respx.mock
def test_with_real_client_and_respx(caplog: pytest.LogCaptureFixture) -> None:
    lead = {"attributes": {"type": "Lead"}, "Id": "00Q000000000001AAA", "LastName": "Doe", "Status": "Open"}
    respx.get(f"{INSTANCE}/services/data/v68.0/query").respond(
        json={"totalSize": 1, "done": True, "records": [lead]},
        headers={"Sforce-Limit-Info": "api-usage=1200/15000"},
    )
    bulk = respx.patch(f"{INSTANCE}/services/data/v68.0/composite/sobjects").mock(
        side_effect=lambda req: httpx.Response(
            200, json=[{"id": "00Q000000000001AAA", "success": True, "errors": []}]
        )
    )

    def factory() -> esf.AsyncSalesforce:
        return esf.AsyncSalesforce(esf.StaticToken("00Dx!token", INSTANCE))

    caplog.set_level(logging.INFO, logger="examples.fastapi_app")
    with TestClient(create_app(factory)) as tc:
        page = tc.get("/leads").json()
        assert page["records"] == [{"Id": "00Q000000000001AAA", "LastName": "Doe", "Status": "Open"}]
        resp = tc.post("/leads/bulk-status", json={"ids": ["00Q000000000001AAA"], "status": "Working"})
        assert resp.json() == {"updated": ["00Q000000000001AAA"], "failed": []}
        usage = tc.get("/usage").json()
        assert usage["api_used"] == 1200
        assert usage["calls_by_operation"] == {"query": 1, "collections.update": 1}
    sent = json.loads(bulk.calls[0].request.content)
    assert sent["records"] == [
        {"Id": "00Q000000000001AAA", "Status": "Working", "attributes": {"type": "Lead"}}
    ]
    first = next(r for r in caplog.records if r.getMessage().startswith("request.completed GET /leads"))
    assert first.sf_calls == 1  # type: ignore[attr-defined]
    assert first.sf_api_used == 1200  # type: ignore[attr-defined]
    assert first.sf_ops == {"query": 1}  # type: ignore[attr-defined]
