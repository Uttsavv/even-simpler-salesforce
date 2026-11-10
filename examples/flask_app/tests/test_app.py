"""Tests for the Flask example: an injected fake, plus one respx-mocked real client."""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx
import pytest
import respx
from flask.testing import FlaskClient

import even_simpler_salesforce as esf
from even_simpler_salesforce.testing import FakeSalesforce
from flask_app.app import create_app

LIST_SOQL = (
    "SELECT Id, FirstName, LastName, Company, Email, Status FROM Lead "
    "WHERE Status = 'Open' ORDER BY CreatedDate DESC"
)


@pytest.fixture
def summaries() -> list[dict[str, Any]]:
    return []


@pytest.fixture
def client(esf_fake: FakeSalesforce, summaries: list[dict[str, Any]]) -> FlaskClient:
    app = create_app(esf_fake, on_summary=lambda request, fields: summaries.append(fields))
    app.testing = True
    return app.test_client()


def test_list_leads_with_cursor_pagination(
    client: FlaskClient, esf_fake: FakeSalesforce, summaries: list[dict[str, Any]]
) -> None:
    ids = esf_fake.add_many(
        "Lead", [{"LastName": f"L{i}", "Company": "Acme", "Status": "Open"} for i in range(450)]
    )
    esf_fake.on_query(LIST_SOQL, rows=lambda q: esf_fake.records("Lead"))
    seen: list[str] = []
    resp = client.get("/leads?status=Open&page_size=200")
    while True:
        assert resp.status_code == 200
        body = resp.get_json()
        seen.extend(r["Id"] for r in body["records"])
        if not body["next_cursor"]:
            break
        resp = client.get("/leads", query_string={"cursor": body["next_cursor"]})
    assert seen == ids
    assert [s["sf_ops"] for s in summaries] == [{"query": 1}, {"query_more": 1}, {"query_more": 1}]


def test_bad_inputs(client: FlaskClient) -> None:
    resp = client.get("/leads?cursor=garbage")
    assert resp.status_code == 400
    assert resp.get_json() == {"code": "salesforce_invalid_cursor", "error_code": "INVALID_CURSOR"}
    assert client.get("/leads?page_size=5").status_code == 400
    assert client.get("/leads?page_size=x").status_code == 400
    assert client.get("/leads/not-an-id").status_code == 422
    assert client.post("/leads", json={"LastName": "x"}).status_code == 422
    assert client.post("/leads/bulk-status", json={"ids": [], "status": "x"}).status_code == 422


def test_get_create_update_lead(
    client: FlaskClient, esf_fake: FakeSalesforce, summaries: list[dict[str, Any]]
) -> None:
    resp = client.post("/leads", json={"LastName": "Doe", "Company": "Acme", "Email": "doe@example.com"})
    assert resp.status_code == 201
    lead_id = resp.get_json()["id"]
    assert client.patch(f"/leads/{lead_id}", json={"Status": "Working", "Junk": 1}).status_code == 204
    body = client.get(f"/leads/{lead_id}").get_json()
    assert body["Status"] == "Working"
    assert body["Email"] == "doe@example.com"
    assert "Junk" not in esf_fake.record("Lead", lead_id)  # type: ignore[operator]
    assert summaries[-1]["sf_ops"] == {"sobject.get": 1}


def test_errors_map_to_http(
    client: FlaskClient, esf_fake: FakeSalesforce, summaries: list[dict[str, Any]]
) -> None:
    assert client.get(f"/leads/{esf_fake.new_id('Lead')}").status_code == 404
    lead_id = esf_fake.add("Lead", {"LastName": "Doe", "Company": "Acme"})
    esf_fake.fail_next(
        "sobject.update",
        esf.ValidationRuleFailed(
            "Email is invalid", error_code="FIELD_CUSTOM_VALIDATION_EXCEPTION", fields=("Email",)
        ),
    )
    resp = client.patch(f"/leads/{lead_id}", json={"Email": "nope"})
    assert resp.status_code == 422
    assert resp.get_json()["fields"] == ["Email"]
    assert summaries[-1]["sf_errors"] == ["FIELD_CUSTOM_VALIDATION_EXCEPTION"]
    esf_fake.fail_next("query", esf.RequestLimitExceeded("too many", error_code="REQUEST_LIMIT_EXCEEDED"))
    resp = client.get("/leads")
    assert resp.status_code == 503
    assert resp.headers["Retry-After"] == "30"


def test_bulk_status_reports_partial_failures(client: FlaskClient, esf_fake: FakeSalesforce) -> None:
    ids = esf_fake.add_many("Lead", [{"LastName": f"L{i}", "Company": "Acme"} for i in range(250)])
    missing = esf_fake.new_id("Lead")
    resp = client.post("/leads/bulk-status", json={"ids": [*ids, missing], "status": "Working"})
    body = resp.get_json()
    assert len(body["updated"]) == 250
    assert body["failed"][0]["id"] == missing
    assert body["failed"][0]["errors"][0]["error_code"] == "NOT_FOUND"
    assert all(r["Status"] == "Working" for r in esf_fake.records("Lead"))


INSTANCE = "https://acme.my.salesforce.com"


@respx.mock
def test_with_real_client_and_respx(caplog: pytest.LogCaptureFixture) -> None:
    lead = {"attributes": {"type": "Lead"}, "Id": "00Q000000000001AAA", "LastName": "Doe"}
    respx.get(f"{INSTANCE}/services/data/v68.0/query").respond(
        json={"totalSize": 1, "done": True, "records": [lead]},
        headers={"Sforce-Limit-Info": "api-usage=99/15000"},
    )
    bulk = respx.patch(f"{INSTANCE}/services/data/v68.0/composite/sobjects").mock(
        side_effect=lambda req: httpx.Response(
            200, json=[{"id": "00Q000000000001AAA", "success": True, "errors": []}]
        )
    )
    caplog.set_level(logging.INFO, logger="examples.flask_app")
    with esf.Salesforce(esf.StaticToken("00Dx!token", INSTANCE)) as sf:
        tc = create_app(sf).test_client()
        assert tc.get("/leads").get_json()["records"] == [{"Id": "00Q000000000001AAA", "LastName": "Doe"}]
        resp = tc.post("/leads/bulk-status", json={"ids": ["00Q000000000001AAA"], "status": "Working"})
        assert resp.get_json() == {"updated": ["00Q000000000001AAA"], "failed": []}
        assert tc.get("/usage").get_json()["api_used"] == 99
    assert json.loads(bulk.calls[0].request.content)["records"][0]["Status"] == "Working"
    line = next(r for r in caplog.records if r.getMessage().startswith("request.completed GET /leads"))
    assert line.sf_calls == 1  # type: ignore[attr-defined]
    assert line.sf_api_used == 99  # type: ignore[attr-defined]
