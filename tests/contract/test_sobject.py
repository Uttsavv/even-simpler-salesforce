from __future__ import annotations

import dataclasses
import json
from datetime import datetime, timezone

import httpx
import pytest
import respx

import even_simpler_salesforce as esf

from ..conftest import DATA, Harness, Recorder
from ..fixtures import load_fixture

LEAD_ID = "00Qxx0000000001AAA"


def test_get(h: Harness, mock: respx.MockRouter) -> None:
    route = mock.get(f"{DATA}/sobjects/Lead/{LEAD_ID}").respond(json=load_fixture("sobject/lead.json"))
    sf = h.client()
    lead = h.run(sf.sobject("Lead").get(LEAD_ID, fields=["Id", "Name", "Status"]))
    assert lead["Name"] == "Jane Doe"
    req = route.calls.last.request
    assert req.url.params["fields"] == "Id,Name,Status"
    assert req.headers["Authorization"] == "Bearer AT-initial-0001"
    assert req.headers["Accept"] == "application/json"
    assert "gzip" in req.headers["Accept-Encoding"]
    assert req.headers["User-Agent"].startswith("even-simpler-salesforce/")


@dataclasses.dataclass
class Lead:
    Id: str
    Name: str
    Status: str


def test_get_with_model_and_if_modified_since(h: Harness, mock: respx.MockRouter) -> None:
    route = mock.get(f"{DATA}/sobjects/Lead/{LEAD_ID}")
    route.side_effect = [
        httpx.Response(200, json=load_fixture("sobject/lead.json")),
        httpx.Response(304),
    ]
    sf = h.client()
    lead = h.run(sf.sobject("Lead").get(LEAD_ID, model=Lead))
    assert lead == Lead(LEAD_ID, "Jane Doe", "Open")
    since = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
    assert h.run(sf.sobject("Lead").get(LEAD_ID, if_modified_since=since)) is None
    assert route.calls.last.request.headers["If-Modified-Since"] == "Fri, 02 Jan 2026 03:04:05 GMT"
    with pytest.raises(ValueError, match="naive"):
        h.run(sf.sobject("Lead").get(LEAD_ID, if_modified_since=datetime(2026, 1, 1)))


def test_get_validates_inputs_before_sending(h: Harness, mock: respx.MockRouter) -> None:
    sf = h.client()
    with pytest.raises(ValueError):
        h.run(sf.sobject("Lead").get("../../oauth2/token"))
    with pytest.raises(ValueError):
        h.run(sf.sobject("Lead").get(LEAD_ID, fields=["Name; DROP"]))
    with pytest.raises(ValueError):
        sf.sobject("Lead/../x")
    assert not mock.calls
    assert repr(sf.sobject("Lead")).endswith("Lead>")


def test_get_not_found(h: Harness, mock: respx.MockRouter) -> None:
    mock.get(f"{DATA}/sobjects/Lead/{LEAD_ID}").respond(
        404, json=[{"errorCode": "NOT_FOUND", "message": "The requested resource does not exist"}]
    )
    sf = h.client()
    with pytest.raises(esf.NotFound) as ei:
        h.run(sf.sobject("Lead").get(LEAD_ID))
    e = ei.value
    assert (e.status, e.operation, e.sobject, e.method, e.path) == (
        404,
        "sobject.get",
        "Lead",
        "GET",
        "/sobjects/Lead/:id",
    )


def test_get_by_external_id(h: Harness, mock: respx.MockRouter) -> None:
    route = mock.get(f"{DATA}/sobjects/Account/Ext__c/A%2F42").respond(json={"Id": "001xx0000000001AAA"})
    sf = h.client()
    out = h.run(sf.sobject("Account").get_by_external_id("Ext__c", "A/42", fields=["Id"]))
    assert out == {"Id": "001xx0000000001AAA"}
    assert route.calls.last.request.url.params["fields"] == "Id"
    with pytest.raises(ValueError):
        h.run(sf.sobject("Account").get_by_external_id("Bad Field", "x"))


def test_create(h: Harness, mock: respx.MockRouter) -> None:
    route = mock.post(f"{DATA}/sobjects/Lead/").respond(201, json=load_fixture("sobject/create.json"))
    sf = h.client()
    new_id = h.run(
        sf.sobject("Lead").create(
            {"LastName": "Doe", "Company": "Acme"},
            assignment_rule_id=True,
            duplicate_rules=esf.DuplicateRules(allow_save=True),
        )
    )
    assert new_id == "00Qxx0000000003AAA"
    req = route.calls.last.request
    assert json.loads(req.content) == {"LastName": "Doe", "Company": "Acme"}
    assert req.headers["Content-Type"] == "application/json"
    assert req.headers["Sforce-Auto-Assign"] == "TRUE"
    assert req.headers["Sforce-Duplicate-Rule-Header"].startswith("allowSave=true")


def test_create_headers_variants(h: Harness, mock: respx.MockRouter) -> None:
    route = mock.post(f"{DATA}/sobjects/Lead/").respond(201, json={"id": "00Qxx0000000003AAA"})
    sf = h.client()
    h.run(
        sf.sobject("Lead").create(
            {"a": 1},
            assignment_rule_id="01Qxx0000000001AAA",
            duplicate_rules=esf.DuplicateRules(allow_save=False),
        )
    )
    req = route.calls.last.request
    assert req.headers["Sforce-Auto-Assign"] == "01Qxx0000000001AAA"
    assert req.headers["Sforce-Duplicate-Rule-Header"].startswith("allowSave=false")
    h.run(sf.sobject("Lead").create({"a": 1}, assignment_rule_id=False))
    assert route.calls.last.request.headers["Sforce-Auto-Assign"] == "FALSE"
    with pytest.raises(ValueError):
        h.run(sf.sobject("Lead").create({"a": 1}, assignment_rule_id="not-an-id"))


def test_create_without_id_is_unexpected(h: Harness, mock: respx.MockRouter) -> None:
    mock.post(f"{DATA}/sobjects/Lead/").respond(201, json={"success": True})
    sf = h.client()
    with pytest.raises(esf.UnexpectedResponse):
        h.run(sf.sobject("Lead").create({"a": 1}))


def test_create_validation_error_with_tag(h: Harness, mock: respx.MockRouter) -> None:
    class AddressRequired(esf.ValidationRuleFailed):
        pass

    mock.post(f"{DATA}/sobjects/Lead/").respond(400, json=load_fixture("errors/validation_tagged.json"))
    sf = h.client(error_tags={"ADDR_REQUIRED": AddressRequired})
    with pytest.raises(AddressRequired) as ei:
        h.run(sf.sobject("Lead").create({"LastName": "x"}))
    assert ei.value.message == "Street is required"
    assert ei.value.tag == "ADDR_REQUIRED"
    assert ei.value.fields == ("Street",)


def test_unknown_dml_code_maps_to_record_error(h: Harness, mock: respx.MockRouter) -> None:
    mock.post(f"{DATA}/sobjects/Lead/").respond(400, json=[{"errorCode": "NEW_CODE_2027", "message": "m"}])
    sf = h.client()
    with pytest.raises(esf.RecordError) as ei:
        h.run(sf.sobject("Lead").create({"a": 1}))
    assert type(ei.value) is esf.RecordError
    assert ei.value.error_code == "NEW_CODE_2027"


def test_update_and_conflict(h: Harness, mock: respx.MockRouter) -> None:
    route = mock.patch(f"{DATA}/sobjects/Lead/{LEAD_ID}")
    route.side_effect = [httpx.Response(204), httpx.Response(412)]
    sf = h.client()
    assert h.run(sf.sobject("Lead").update(LEAD_ID, {"Status": "Working"})) is None
    assert json.loads(route.calls.last.request.content) == {"Status": "Working"}
    since = datetime(2026, 1, 2, tzinfo=timezone.utc)
    with pytest.raises(esf.Conflict) as ei:
        h.run(sf.sobject("Lead").update(LEAD_ID, {"Status": "x"}, if_unmodified_since=since))
    assert ei.value.status == 412
    assert route.calls.last.request.headers["If-Unmodified-Since"] == "Fri, 02 Jan 2026 00:00:00 GMT"


def test_upsert(h: Harness, mock: respx.MockRouter) -> None:
    route = mock.patch(f"{DATA}/sobjects/Account/External_Id__c/A-42")
    route.side_effect = [
        httpx.Response(201, json={"id": "001xx0000000001AAA", "success": True, "created": True}),
        httpx.Response(200, json={"id": "001xx0000000001AAA", "success": True, "created": False}),
        httpx.Response(204),
        httpx.Response(300, json=load_fixture("errors/multiple_matches.json")),
    ]
    sf = h.client()
    acc = sf.sobject("Account")
    assert h.run(acc.upsert("External_Id__c", "A-42", {"Name": "Acme"})) == esf.UpsertResult(
        "001xx0000000001AAA", True
    )
    assert h.run(acc.upsert("External_Id__c", "A-42", {"Name": "Acme"})).created is False
    assert h.run(acc.upsert("External_Id__c", "A-42", {"Name": "Acme"})) == esf.UpsertResult(None, False)
    with pytest.raises(esf.MultipleMatches) as ei:
        h.run(acc.upsert("External_Id__c", "A-42", {"Name": "Acme"}))
    assert len(ei.value.matches) == 2
    assert ei.value.path == "/sobjects/Account/External_Id__c/:value"


def test_delete(h: Harness, mock: respx.MockRouter) -> None:
    route = mock.delete(f"{DATA}/sobjects/Lead/{LEAD_ID}").respond(204)
    sf = h.client()
    assert h.run(sf.sobject("Lead").delete(LEAD_ID)) is None
    assert "If-Unmodified-Since" not in route.calls.last.request.headers
    # Salesforce doesn't support conditional headers on DELETE (ADR 0012).
    with pytest.raises(TypeError):
        sf.sobject("Lead").delete(LEAD_ID, if_unmodified_since=datetime.now(timezone.utc))  # type: ignore[call-arg]
    assert route.call_count == 1


def test_metadata_updated_deleted(h: Harness, mock: respx.MockRouter) -> None:
    mock.get(f"{DATA}/sobjects/Lead").respond(json=load_fixture("sobject/metadata_lead.json"))
    up = mock.get(f"{DATA}/sobjects/Lead/updated/").respond(json=load_fixture("sobject/updated.json"))
    de = mock.get(f"{DATA}/sobjects/Lead/deleted/").respond(json=load_fixture("sobject/deleted.json"))
    sf = h.client()
    lead = sf.sobject("Lead")
    assert h.run(lead.metadata())["objectDescribe"]["name"] == "Lead"
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    end = datetime(2026, 1, 2, tzinfo=timezone.utc)
    assert h.run(lead.updated(start, end))["ids"] == ["00Qxx0000000001AAA"]
    assert up.calls.last.request.url.params["start"] == "2026-01-01T00:00:00Z"
    assert up.calls.last.request.url.params["end"] == "2026-01-02T00:00:00Z"
    assert h.run(lead.deleted(start, end))["deletedRecords"][0]["id"] == "00Qxx0000000009AAA"
    assert de.called
    with pytest.raises(ValueError):
        h.run(lead.updated(datetime(2026, 1, 1), end))


def test_sobject_operation_names_in_events(h: Harness, mock: respx.MockRouter, rec: Recorder) -> None:
    mock.get(f"{DATA}/sobjects/Lead/{LEAD_ID}").respond(json={"Id": LEAD_ID})
    mock.patch(f"{DATA}/sobjects/Lead/{LEAD_ID}").respond(204)
    mock.delete(f"{DATA}/sobjects/Lead/{LEAD_ID}").respond(204)
    mock.post(f"{DATA}/sobjects/Lead/").respond(201, json={"id": LEAD_ID})
    sf = h.client(recorder=rec)
    lead = sf.sobject("Lead")
    h.run(lead.get(LEAD_ID))
    h.run(lead.create({"a": 1}))
    h.run(lead.update(LEAD_ID, {"a": 1}))
    h.run(lead.delete(LEAD_ID))
    ops = [(e["operation"], e["method"], e["path_template"], e["records"]) for e in rec.requests]
    assert ops == [
        ("sobject.get", "GET", "/sobjects/Lead/:id", None),
        ("sobject.create", "POST", "/sobjects/Lead/", 1),
        ("sobject.update", "PATCH", "/sobjects/Lead/:id", 1),
        ("sobject.delete", "DELETE", "/sobjects/Lead/:id", 1),
    ]
