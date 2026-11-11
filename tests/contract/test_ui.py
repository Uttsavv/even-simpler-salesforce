"""User Interface API: ``sf.ui`` (§9.13), on both clients.

Every response here is **synthetic** (``tests/fixtures/recorded/ui/*.json``), hand-written from
the *User Interface API Developer Guide* because no org recording exists yet.
developer.salesforce.com refuses automated fetches, so these pages were read through search
excerpts on 2026-11-11:

- Get a Record (``GET ui-api/records/{recordId}``; ``fields``, ``layoutTypes`` ``Compact`` and
  ``Full``, ``modes`` ``Create``, ``Edit`` and ``View``; from API 45.0 one of ``fields``,
  ``optionalFields`` or ``layoutTypes`` is required):
  https://developer.salesforce.com/docs/atlas.en-us.uiapi.meta/uiapi/ui_api_resources_record_get.htm
- Get Values for All Picklist Fields of a Record Type
  (``GET ui-api/object-info/{objectApiName}/picklist-values/{recordTypeId}``):
  https://developer.salesforce.com/docs/atlas.en-us.uiapi.meta/uiapi/ui_api_resources_picklist_values_collection.htm
- Get Default Values to Create a Record (``recordTypeId`` optional):
  https://developer.salesforce.com/docs/atlas.en-us.uiapi.meta/uiapi/ui_api_resources_record_defaults_create.htm
- Get Default Values to Clone a Record:
  https://developer.salesforce.com/docs/atlas.en-us.uiapi.meta/uiapi/ui_api_resources_record_defaults_clone.htm
- Get List Views for an Object (``GET ui-api/list-info/{objectApiName}``):
  https://developer.salesforce.com/docs/atlas.en-us.uiapi.meta/uiapi/ui_api_resources_lists_object_get.htm
- Get List View Records (``pageSize`` 1 to 2,000, ``pageToken``, ``nextPageToken``):
  https://developer.salesforce.com/docs/atlas.en-us.uiapi.meta/uiapi/ui_api_resources_list_views_records.htm
- Status Codes and Error Responses (``errorCode`` and ``message``, for example
  ``ILLEGAL_QUERY_PARAMETER_VALUE`` for a malformed record id):
  https://developer.salesforce.com/docs/atlas.en-us.uiapi.meta/uiapi/ui_api_errors.htm

The exact bodies, the 404 for an unknown object and ``If-Modified-Since`` support are open in
big-bang.md §21 (V60 to V64).
"""

from __future__ import annotations

import httpx
import pytest
import respx

import even_simpler_salesforce as esf

from ..conftest import DATA, Harness, Recorder
from ..fixtures import load_fixture

UI = f"{DATA}/ui-api"
LEAD_ID = "00Qxx0000000001AAA"
MASTER = "012000000000000AAA"


def test_record_with_fields(h: Harness, mock: respx.MockRouter, rec: Recorder) -> None:
    """Synthetic: ``fields`` is a comma-separated list of ``Object.Field`` names."""
    route = mock.get(f"{UI}/records/{LEAD_ID}").respond(json=load_fixture("ui/record_lead.json"))
    sf = h.client(recorder=rec)
    record = h.run(sf.ui.record(LEAD_ID, fields=["Lead.Name", "Lead.AnnualRevenue"]))
    assert record["fields"]["AnnualRevenue"] == {"displayValue": "$1,500,000.00", "value": 1500000}
    assert dict(route.calls[0].request.url.params) == {"fields": "Lead.Name,Lead.AnnualRevenue"}
    (event,) = rec.requests
    assert event["operation"] == "ui.record"
    assert event["path_template"] == "/ui-api/records/:id"
    assert event["outcome"] == "ok"


def test_record_with_layout_and_modes(h: Harness, mock: respx.MockRouter) -> None:
    route = mock.get(f"{UI}/records/{LEAD_ID}").respond(json=load_fixture("ui/record_lead.json"))
    sf = h.client()
    h.run(sf.ui.record(LEAD_ID, layout_types=["Full", "Compact"], modes=["View", "Edit"]))
    assert dict(route.calls[0].request.url.params) == {"layoutTypes": "Full,Compact", "modes": "View,Edit"}


def test_record_arguments_are_checked_before_sending(h: Harness, mock: respx.MockRouter) -> None:
    sf = h.client()
    with pytest.raises(ValueError, match="fields or layout_types"):
        h.run(sf.ui.record(LEAD_ID))
    with pytest.raises(ValueError, match="Account.Name"):
        h.run(sf.ui.record(LEAD_ID, fields=["Name"]))
    with pytest.raises(ValueError, match="layout_types"):
        h.run(sf.ui.record(LEAD_ID, layout_types=["Tall"]))  # type: ignore[list-item]
    with pytest.raises(ValueError, match="invalid Salesforce id"):
        h.run(sf.ui.record("../limits", fields=["Lead.Name"]))
    assert not mock.calls


def test_record_error_is_typed(h: Harness, mock: respx.MockRouter, rec: Recorder) -> None:
    """Synthetic: UI API errors use the REST shape, so classify maps them unchanged."""
    mock.get(f"{UI}/records/{LEAD_ID}").respond(400, json=load_fixture("ui/error_invalid_id.json"))
    sf = h.client(recorder=rec)
    with pytest.raises(esf.RequestError) as ei:
        h.run(sf.ui.record(LEAD_ID, fields=["Lead.Name"]))
    assert ei.value.error_code == "ILLEGAL_QUERY_PARAMETER_VALUE"
    assert ei.value.operation == "ui.record"
    assert rec.requests[0]["error_class"] == "RequestError"


def test_record_not_found(h: Harness, mock: respx.MockRouter) -> None:
    mock.get(f"{UI}/records/{LEAD_ID}").respond(404, json=load_fixture("ui/error_not_found.json"))
    sf = h.client()
    with pytest.raises(esf.NotFound):
        h.run(sf.ui.record(LEAD_ID, fields=["Lead.Name"]))


def test_reads_are_retried(h: Harness, mock: respx.MockRouter) -> None:
    route = mock.get(f"{UI}/list-info/Lead")
    route.side_effect = [
        httpx.Response(503, json=[{"errorCode": "SERVER_UNAVAILABLE", "message": "try later"}]),
        httpx.Response(200, json=load_fixture("ui/list_views_lead.json")),
    ]
    sf = h.client()
    views = h.run(sf.ui.list_views("Lead"))
    assert [v["apiName"] for v in views["lists"]] == ["AllOpenLeads", "MyUnreadLeads"]
    assert route.call_count == 2


def test_object_info_uses_the_describe_cache(h: Harness, mock: respx.MockRouter, rec: Recorder) -> None:
    route = mock.get(f"{UI}/object-info/Lead").respond(json=load_fixture("ui/object_info_lead.json"))
    cache = esf.MemoryCache()
    sf = h.client(recorder=rec, cache=cache)
    first = h.run(sf.ui.object_info("Lead"))
    first["label"] = "changed"  # callers get their own copy
    again = h.run(sf.ui.object_info("Lead"))
    assert again["label"] == "Lead"
    assert route.call_count == 1
    assert any(":ui:object-info:Lead:" in k for k in cache._data)  # pyright: ignore[reportPrivateUsage]
    assert rec.requests[0]["operation"] == "ui.object_info"
    assert rec.requests[0]["sobject"] == "Lead"
    h.run(sf.ui.object_info("Lead", cached=False))
    assert route.call_count == 2


def test_object_info_is_cached_apart_from_describe(h: Harness, mock: respx.MockRouter) -> None:
    ui = mock.get(f"{UI}/object-info/Lead").respond(json=load_fixture("ui/object_info_lead.json"))
    describe = mock.get(f"{DATA}/sobjects/Lead/describe").respond(json={"name": "Lead", "fields": []})
    sf = h.client()
    h.run(sf.sobject("Lead").describe())
    assert h.run(sf.ui.object_info("Lead"))["apiName"] == "Lead"
    assert (ui.call_count, describe.call_count) == (1, 1)


def test_object_info_stale_revalidates(h: Harness, mock: respx.MockRouter) -> None:
    """Revalidation sends ``If-Modified-Since``; a 304 keeps the cached copy (V62)."""
    lm = "Tue, 10 Nov 2026 09:30:00 GMT"
    route = mock.get(f"{UI}/object-info/Lead")
    route.side_effect = [
        httpx.Response(200, json=load_fixture("ui/object_info_lead.json"), headers={"Last-Modified": lm}),
        httpx.Response(304),
    ]
    sf = h.client(describe_cache=esf.DescribeCacheConfig(ttl=0))
    h.run(sf.ui.object_info("Lead"))
    again = h.run(sf.ui.object_info("Lead"))
    assert again["apiName"] == "Lead"
    assert route.calls[1].request.headers["if-modified-since"] == lm


def test_unknown_object_is_invalid_type(h: Harness, mock: respx.MockRouter) -> None:
    """Synthetic: ``NOT_FOUND`` on an object path means the type doesn't exist (V61)."""
    mock.get(f"{UI}/object-info/Nope__c").respond(404, json=load_fixture("ui/error_not_found.json"))
    sf = h.client()
    with pytest.raises(esf.InvalidType) as ei:
        h.run(sf.ui.object_info("Nope__c"))
    assert ei.value.operation == "ui.object_info"


def test_object_info_rejects_a_non_object_body(h: Harness, mock: respx.MockRouter) -> None:
    mock.get(f"{UI}/object-info/Lead").respond(json=["not", "an", "object"])
    sf = h.client()
    with pytest.raises(esf.UnexpectedResponse):
        h.run(sf.ui.object_info("Lead"))


def test_picklist_values_are_cached_per_record_type(
    h: Harness, mock: respx.MockRouter, rec: Recorder
) -> None:
    route = mock.get(f"{UI}/object-info/Lead/picklist-values/{MASTER}").respond(
        json=load_fixture("ui/picklist_values_lead.json")
    )
    sf = h.client(recorder=rec)
    values = h.run(sf.ui.picklist_values("Lead", MASTER))
    status = values["picklistFieldValues"]["Status"]
    assert [v["value"] for v in status["values"]] == ["Open - Not Contacted", "Working - Contacted"]
    h.run(sf.ui.picklist_values("Lead", MASTER))
    assert route.call_count == 1
    assert rec.requests[0]["operation"] == "ui.picklist_values"
    assert rec.requests[0]["path_template"] == "/ui-api/object-info/Lead/picklist-values/:id"
    with pytest.raises(ValueError, match="invalid Salesforce id"):
        h.run(sf.ui.picklist_values("Lead", "Master"))


def test_create_defaults(h: Harness, mock: respx.MockRouter, rec: Recorder) -> None:
    route = mock.get(f"{UI}/record-defaults/create/Lead").respond(
        json=load_fixture("ui/create_defaults_lead.json")
    )
    sf = h.client(recorder=rec)
    defaults = h.run(sf.ui.create_defaults("Lead"))
    assert defaults["record"]["fields"]["Status"]["value"] == "Open - Not Contacted"
    assert not route.calls[0].request.url.params
    h.run(sf.ui.create_defaults("Lead", record_type_id=MASTER))
    assert dict(route.calls[1].request.url.params) == {"recordTypeId": MASTER}
    assert route.call_count == 2  # not cached: defaults depend on the user and the moment
    assert rec.requests[0]["operation"] == "ui.create_defaults"
    assert rec.requests[0]["sobject"] == "Lead"


def test_clone_defaults(h: Harness, mock: respx.MockRouter, rec: Recorder) -> None:
    mock.get(f"{UI}/record-defaults/clone/{LEAD_ID}").respond(
        json=load_fixture("ui/clone_defaults_lead.json")
    )
    sf = h.client(recorder=rec)
    defaults = h.run(sf.ui.clone_defaults(LEAD_ID))
    assert defaults["record"]["id"] is None
    assert defaults["record"]["fields"]["Company"]["value"] == "Analytical Engines"
    assert rec.requests[0]["path_template"] == "/ui-api/record-defaults/clone/:id"


def test_list_records_pages(h: Harness, mock: respx.MockRouter, rec: Recorder) -> None:
    route = mock.get(f"{UI}/list-records/Lead/AllOpenLeads")
    route.side_effect = [
        httpx.Response(200, json=load_fixture("ui/list_records_page1.json")),
        httpx.Response(200, json=load_fixture("ui/list_records_page2.json")),
    ]
    sf = h.client(recorder=rec)
    page = h.run(sf.ui.list_records("Lead", "AllOpenLeads", page_size=2))
    assert dict(route.calls[0].request.url.params) == {"pageSize": "2"}
    names = [r["fields"]["Name"]["value"] for r in page["records"]]
    page = h.run(sf.ui.list_records("Lead", "AllOpenLeads", page_size=2, page_token=page["nextPageToken"]))
    assert dict(route.calls[1].request.url.params) == {"pageSize": "2", "pageToken": "2"}
    names += [r["fields"]["Name"]["value"] for r in page["records"]]
    assert names == ["Ada Lovelace", "Alan Turing", "Grace Hopper"]
    assert page["nextPageToken"] is None
    assert rec.requests[0]["operation"] == "ui.list_records"


def test_list_records_recent_and_checks(h: Harness, mock: respx.MockRouter) -> None:
    route = mock.get(f"{UI}/list-records/Lead/__Recent").respond(json={"records": [], "nextPageToken": None})
    sf = h.client()
    assert h.run(sf.ui.list_records("Lead", "__Recent"))["records"] == []
    assert not route.calls[0].request.url.params
    with pytest.raises(ValueError, match="page_size"):
        h.run(sf.ui.list_records("Lead", "__Recent", page_size=2001))
    with pytest.raises(ValueError, match="list view"):
        h.run(sf.ui.list_records("Lead", "../sobjects"))
    assert route.call_count == 1
