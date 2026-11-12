"""``fake.ui``: the User Interface API of ``FakeSalesforce`` / ``AsyncFakeSalesforce``."""

from __future__ import annotations

import asyncio

import pytest

import even_simpler_salesforce as esf
from even_simpler_salesforce.testing import AsyncFakeSalesforce, FakeSalesforce

from ..conftest import Recorder

MASTER = "012000000000000AAA"
DESCRIBE = {
    "name": "Lead",
    "label": "Lead",
    "labelPlural": "Leads",
    "fields": [
        {"name": "Id", "type": "id", "nillable": False, "createable": False},
        {"name": "LastName", "type": "string", "label": "Last Name", "nillable": False},
        {"name": "Company", "type": "string", "nillable": False},
        {
            "name": "Status",
            "type": "picklist",
            "nillable": False,
            "defaultedOnCreate": True,
            "defaultValue": "Open",
            "picklistValues": [
                {"value": "Open", "label": "Open", "active": True, "defaultValue": True},
                {"value": "Old", "label": "Old", "active": False, "defaultValue": False},
                {"value": "Closed", "label": "Closed", "active": True, "defaultValue": False},
            ],
        },
        {"name": "Tags__c", "type": "multipicklist", "picklistValues": [{"value": "a"}]},
        {"name": "CreatedDate", "type": "datetime"},
        "not a field",
    ],
}


def _fake() -> FakeSalesforce:
    fake = FakeSalesforce()
    fake.set_describe("Lead", DESCRIBE)
    return fake


def test_record_in_ui_form() -> None:
    fake = _fake()
    lead = fake.add("Lead", {"LastName": "Lovelace", "Company": "Engines", "Status": "Open"})
    record = fake.ui.record(lead, fields=["Lead.LastName", "Lead.Status"])
    assert record["apiName"] == "Lead"
    assert record["id"] == lead
    assert record["recordTypeId"] == MASTER
    assert record["fields"] == {
        "LastName": {"value": "Lovelace", "displayValue": None},
        "Status": {"value": "Open", "displayValue": None},
    }
    full = fake.ui.record(lead, layout_types=["Full"])
    assert set(full["fields"]) == {"Id", "LastName", "Company", "Status"}
    (call, _) = fake.calls_for("ui.record")
    assert call.args["fields"] == ["Lead.LastName", "Lead.Status"]


def test_record_relationship_fields() -> None:
    fake = FakeSalesforce()
    owner = {"attributes": {"type": "User"}, "Id": "005xx0000000001AAA", "Name": "Ada"}
    lead = fake.add("Lead", {"LastName": "L", "Owner": owner})
    record = fake.ui.record(lead, fields=["Lead.Owner.Name"])
    assert record["fields"]["Owner"]["value"] == {
        "apiName": "User",
        "id": "005xx0000000001AAA",
        "fields": {
            "Id": {"value": "005xx0000000001AAA", "displayValue": None},
            "Name": {"value": "Ada", "displayValue": None},
        },
    }


def test_record_errors() -> None:
    fake = _fake()
    lead = fake.add("Lead", {"LastName": "L"})
    with pytest.raises(esf.InvalidField) as ei:
        fake.ui.record(lead, fields=["Lead.Nope"])
    assert ei.value.fields == ("Lead.Nope",)
    with pytest.raises(esf.RequestError) as other:  # a field of another object
        fake.ui.record(lead, fields=["Account.Name"])
    assert (other.value.status, other.value.error_code) == (400, "INVALID_INPUT")
    with pytest.raises(ValueError, match="not both"):
        fake.ui.record(lead, fields=["Lead.Name"], layout_types=["Full"])
    with pytest.raises(esf.NotFound):
        fake.ui.record("00Qxx0000000099AAA", fields=["Lead.Name"])
    with pytest.raises(ValueError, match="fields or layout_types"):
        fake.ui.record(lead)
    fake.sobject("Lead").delete(lead)
    with pytest.raises(esf.NotFound) as gone:
        fake.ui.record(lead, layout_types=["Compact"])
    assert gone.value.error_code == "ENTITY_IS_DELETED"


def test_object_info_from_the_describe() -> None:
    fake = _fake()
    info = fake.ui.object_info("Lead")
    assert info["labelPlural"] == "Leads"
    assert info["defaultRecordTypeId"] == MASTER
    assert info["fields"]["LastName"] == {
        "apiName": "LastName",
        "dataType": "String",
        "label": "Last Name",
        "required": True,
        "updateable": True,
    }
    assert info["fields"]["Status"]["required"] is False  # defaulted on create
    assert info["fields"]["Tags__c"]["dataType"] == "MultiPicklist"
    assert info["fields"]["CreatedDate"]["dataType"] == "DateTime"
    assert fake.calls_for("ui.object_info", sobject="Lead")[0].args == {"cached": True}
    with pytest.raises(esf.Forbidden) as ei:  # like a real org: 403, not 404
        fake.ui.object_info("Nope__c")
    assert (ei.value.status, ei.value.error_code) == (403, "INSUFFICIENT_ACCESS")


def test_object_info_synthesised_from_records() -> None:
    fake = FakeSalesforce()
    fake.add("Widget__c", {"Name": "w"})
    info = fake.ui.object_info("Widget__c")
    assert info["custom"] is True
    assert set(info["fields"]) == {"Id", "Name"}


def test_picklist_values() -> None:
    fake = _fake()
    values = fake.ui.picklist_values("Lead", MASTER)["picklistFieldValues"]
    assert set(values) == {"Status", "Tags__c"}
    assert [v["value"] for v in values["Status"]["values"]] == ["Open", "Closed"]
    assert values["Status"]["defaultValue"]["value"] == "Open"
    assert values["Status"]["url"].endswith(f"/ui-api/object-info/Lead/picklist-values/{MASTER}/Status")
    assert values["Tags__c"]["values"][0]["label"] == "a"
    assert values["Tags__c"]["defaultValue"] is None
    with pytest.raises(ValueError):
        fake.ui.picklist_values("Lead", "master")


def test_create_and_clone_defaults() -> None:
    fake = _fake()
    created = fake.ui.create_defaults("Lead")
    assert created["record"]["fields"] == {"Status": {"value": "Open", "displayValue": None}}
    assert created["record"]["id"] is None
    assert created["objectInfos"]["Lead"]["apiName"] == "Lead"
    assert created["layout"]["mode"] == "Create"
    rt = "012xx0000000001AAA"
    assert fake.ui.create_defaults("Lead", record_type_id=rt)["record"]["recordTypeId"] == rt
    with pytest.raises(ValueError):
        fake.ui.create_defaults("Lead", record_type_id="x")

    lead = fake.add("Lead", {"LastName": "Lovelace", "RecordTypeId": rt, "CreatedDate": "2026-01-01"})
    clone = fake.ui.clone_defaults(lead)["record"]
    assert clone["id"] is None
    assert set(clone["fields"]) == {"LastName", "RecordTypeId"}
    assert clone["recordTypeId"] == rt
    with pytest.raises(esf.NotFound):
        fake.ui.clone_defaults("00Qxx0000000099AAA")


def test_list_views_and_records() -> None:
    fake = FakeSalesforce()
    ids = fake.add_many(
        "Lead", [{"LastName": f"L{i}", "Status": "Open" if i % 2 else "Closed"} for i in range(5)]
    )
    view = fake.set_list_view("Lead", "OpenLeads", label="Open leads", where=lambda r: r["Status"] == "Open")
    assert view.id.startswith("00B")
    again = fake.set_list_view(
        "Lead", "OpenLeads", fields=["LastName"], where=lambda r: r["Status"] == "Open"
    )
    assert again.id == view.id
    assert again.label == "OpenLeads"

    views = fake.ui.list_views("Lead")
    assert views["count"] == 2
    assert [v["apiName"] for v in views["lists"]] == ["__Recent", "OpenLeads"]
    assert views["lists"][0]["id"] is None
    assert views["lists"][1]["url"].endswith("/ui-api/list-info/Lead/OpenLeads")
    fake.add("Account", {"Name": "A"})
    assert [v["apiName"] for v in fake.ui.list_views("Account")["lists"]] == ["__Recent"]
    with pytest.raises(esf.Forbidden):
        fake.ui.list_views("Nope__c")

    page = fake.ui.list_records("Lead", "OpenLeads", page_size=1)
    assert page["records"][0]["fields"] == {"LastName": {"value": "L1", "displayValue": None}}
    assert (page["nextPageToken"], page["previousPageToken"]) == ("1", None)
    page = fake.ui.list_records("Lead", "OpenLeads", page_size=1, page_token=page["nextPageToken"])
    assert page["records"][0]["id"] == ids[3]
    assert (page["nextPageToken"], page["previousPageToken"]) == (None, "0")

    recent = fake.ui.list_records("Lead", "__Recent")
    assert recent["count"] == 5
    assert recent["pageSize"] == 50
    with pytest.raises(esf.NotFound):
        fake.ui.list_records("Lead", "Missing")
    for token in ("abc", "-1"):
        with pytest.raises(esf.RequestError) as ei:
            fake.ui.list_records("Lead", "__Recent", page_token=token)
        assert ei.value.error_code == "ILLEGAL_QUERY_PARAMETER_VALUE"
    with pytest.raises(ValueError):
        fake.ui.list_records("Lead", "__Recent", page_size=0)


def test_injected_failures_and_events() -> None:
    rec = Recorder()
    fake = FakeSalesforce(observers=[rec])
    fake.set_describe("Lead", DESCRIBE)
    fake.fail_next("ui.object_info", esf.Forbidden("no access"))
    with pytest.raises(esf.Forbidden):
        fake.ui.object_info("Lead")
    fake.ui.object_info("Lead")
    assert [e["outcome"] for e in rec.requests] == ["error", "ok"]
    assert rec.requests[1]["path_template"] == "/ui-api/object-info/Lead"
    assert rec.requests[1]["operation"] == "ui.object_info"


def test_async_fake_ui() -> None:
    async def go() -> None:
        async with AsyncFakeSalesforce() as fake:
            fake.set_describe("Lead", DESCRIBE)
            lead = fake.add("Lead", {"LastName": "L", "Company": "C"})
            fake.set_list_view("Lead", "All")
            assert (await fake.ui.record(lead, fields=["Lead.Company"]))["fields"]["Company"]["value"] == "C"
            assert (await fake.ui.object_info("Lead", cached=False))["apiName"] == "Lead"
            assert "Status" in (await fake.ui.picklist_values("Lead", MASTER))["picklistFieldValues"]
            assert (await fake.ui.create_defaults("Lead"))["record"]["apiName"] == "Lead"
            assert (await fake.ui.clone_defaults(lead))["record"]["fields"]["LastName"]["value"] == "L"
            assert (await fake.ui.list_views("Lead"))["count"] == 2
            assert (await fake.ui.list_records("Lead", "All", page_size=10))["count"] == 1

    asyncio.run(go())
