"""User Interface API: ``sf.ui`` (§9.13), on both clients.

The responses are **recorded** from a Developer Edition org on API 68.0
(``tests/fixtures/recorded/ui/*.json``, made by ``tests/sandbox/test_ui_reports.py`` and
``scripts/sandbox_ui_reports_fixtures.py``). Docs, read through search excerpts because
developer.salesforce.com refuses automated fetches (2026-11-12):

- Get a Record (``GET ui-api/records/{recordId}``; ``fields``, ``layoutTypes`` ``Compact`` and
  ``Full``, ``modes`` ``Create``, ``Edit`` and ``View``; from API 45.0 one of ``fields``,
  ``optionalFields`` or ``layoutTypes`` is required):
  https://developer.salesforce.com/docs/atlas.en-us.uiapi.meta/uiapi/ui_api_resources_record_get.htm
- Get Values for All Picklist Fields of a Record Type:
  https://developer.salesforce.com/docs/atlas.en-us.uiapi.meta/uiapi/ui_api_resources_picklist_values_collection.htm
- Get Default Values to Create a Record / to Clone a Record:
  https://developer.salesforce.com/docs/atlas.en-us.uiapi.meta/uiapi/ui_api_resources_record_defaults_create.htm
  https://developer.salesforce.com/docs/atlas.en-us.uiapi.meta/uiapi/ui_api_resources_record_defaults_clone.htm
- Get List Views for an Object / Get List View Records:
  https://developer.salesforce.com/docs/atlas.en-us.uiapi.meta/uiapi/ui_api_resources_lists_object_get.htm
  https://developer.salesforce.com/docs/atlas.en-us.uiapi.meta/uiapi/ui_api_resources_list_views_records.htm
- Status Codes and Error Responses:
  https://developer.salesforce.com/docs/atlas.en-us.uiapi.meta/uiapi/ui_api_errors.htm

Org-observed where the docs are silent (big-bang.md §21 V61-V63): an unknown object is 403
``INSUFFICIENT_ACCESS``; ``fields`` with ``layoutTypes`` is 400 ``INVALID_INPUT_COMBINATION``;
``If-Modified-Since`` is ignored.
"""

from __future__ import annotations

import httpx
import pytest
import respx

import even_simpler_salesforce as esf

from ..conftest import DATA, Harness, Recorder
from ..fixtures import fixture_status, load_fixture

UI = f"{DATA}/ui-api"
LEAD_ID = "00Q000000000001EAA"  # the masked id in the recordings
MASTER = "012000000000000AAA"


def test_record_with_fields(h: Harness, mock: respx.MockRouter, rec: Recorder) -> None:
    """``fields`` is a comma-separated list of ``Object.Field`` names; values come formatted."""
    route = mock.get(f"{UI}/records/{LEAD_ID}").respond(json=load_fixture("ui/record_lead.json"))
    sf = h.client(recorder=rec)
    record = h.run(sf.ui.record(LEAD_ID, fields=["Lead.Name", "Lead.AnnualRevenue", "Lead.Status"]))
    assert record["id"] == LEAD_ID
    assert record["fields"]["AnnualRevenue"] == {"displayValue": "$1,500,000", "value": 1500000.0}
    assert record["fields"]["Name"]["displayValue"] is None
    assert record["fields"]["Status"]["displayValue"] == "Open - Not Contacted"
    assert dict(route.calls[0].request.url.params) == {"fields": "Lead.Name,Lead.AnnualRevenue,Lead.Status"}
    (event,) = rec.requests
    assert event["operation"] == "ui.record"
    assert event["path_template"] == "/ui-api/records/:id"
    assert event["outcome"] == "ok"


def test_record_with_layout_and_modes(h: Harness, mock: respx.MockRouter) -> None:
    route = mock.get(f"{UI}/records/{LEAD_ID}").respond(json=load_fixture("ui/record_lead_full.json"))
    sf = h.client()
    record = h.run(sf.ui.record(LEAD_ID, layout_types=["Full", "Compact"], modes=["View", "Edit"]))
    assert dict(route.calls[0].request.url.params) == {"layoutTypes": "Full,Compact", "modes": "View,Edit"}
    assert "LastName" in record["fields"]


def test_record_arguments_are_checked_before_sending(h: Harness, mock: respx.MockRouter) -> None:
    sf = h.client()
    with pytest.raises(ValueError, match="fields or layout_types"):
        h.run(sf.ui.record(LEAD_ID))
    with pytest.raises(ValueError, match="Account.Name"):
        h.run(sf.ui.record(LEAD_ID, fields=["Name"]))
    with pytest.raises(ValueError, match="layout_types"):
        h.run(sf.ui.record(LEAD_ID, layout_types=["Tall"]))  # type: ignore[list-item]
    # Salesforce answers 400 INVALID_INPUT_COMBINATION to both (ui/error_fields_and_layout.json).
    with pytest.raises(ValueError, match="not both"):
        h.run(sf.ui.record(LEAD_ID, fields=["Lead.Name"], layout_types=["Full"]))
    with pytest.raises(ValueError, match="invalid Salesforce id"):
        h.run(sf.ui.record("../limits", fields=["Lead.Name"]))
    assert not mock.calls


def test_record_error_is_typed(h: Harness, mock: respx.MockRouter, rec: Recorder) -> None:
    """UI API errors use the REST shape, so classify maps them unchanged."""
    name = "ui/error_invalid_id.json"  # an id with a wrong case checksum
    mock.get(f"{UI}/records/{LEAD_ID}").respond(fixture_status(name), json=load_fixture(name))
    sf = h.client(recorder=rec)
    with pytest.raises(esf.RequestError) as ei:
        h.run(sf.ui.record(LEAD_ID, fields=["Lead.Name"]))
    assert (ei.value.status, ei.value.error_code) == (400, "UNKNOWN_EXCEPTION")
    assert "malformed" in str(ei.value)
    assert ei.value.operation == "ui.record"
    assert rec.requests[0]["error_class"] == "RequestError"


@pytest.mark.parametrize(
    ("name", "fields", "cls", "code"),
    [
        ("ui/error_invalid_field.json", ["Lead.Esf_Nope__c"], esf.InvalidField, "INVALID_FIELD"),
        ("ui/error_other_object_field.json", ["Account.Name"], esf.RequestError, "INVALID_INPUT"),
    ],
)
def test_record_field_errors(
    h: Harness,
    mock: respx.MockRouter,
    name: str,
    fields: list[str],
    cls: type[esf.SalesforceError],
    code: str,
) -> None:
    mock.get(f"{UI}/records/{LEAD_ID}").respond(fixture_status(name), json=load_fixture(name))
    sf = h.client()
    with pytest.raises(cls) as ei:
        h.run(sf.ui.record(LEAD_ID, fields=fields))
    assert (ei.value.status, ei.value.error_code) == (400, code)


def test_record_not_found(h: Harness, mock: respx.MockRouter) -> None:
    name = "ui/error_not_found.json"
    mock.get(f"{UI}/records/{LEAD_ID}").respond(fixture_status(name), json=load_fixture(name))
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
    names = [v["apiName"] for v in views["lists"]]
    assert names[:2] == ["__Recent", "AllOpenLeads"]
    assert views["lists"][0]["id"] is None  # the recently viewed list has no list view id
    assert views["pageSize"] == 20
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
    """Revalidation sends ``If-Modified-Since``; a 304 keeps the cached copy.

    A real org ignores ``If-Modified-Since`` on this resource and answers 200 (V62), which works
    too: the client stores the new copy.
    """
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


def test_object_info_revalidates_with_the_etag(h: Harness, mock: respx.MockRouter) -> None:
    """V62: the org sends an ``ETag`` and no ``Last-Modified``, and ignores ``If-Modified-Since``.

    So the cache revalidates with ``If-None-Match``; a 304 keeps the copy and the ``ETag``.
    """
    body = load_fixture("ui/object_info_lead.json")
    etag = '"9630fcc30722d1a03c35c3490acbdd8d--gzip"'
    route = mock.get(f"{UI}/object-info/Lead")
    route.side_effect = [
        httpx.Response(200, json=body, headers={"ETag": etag}),
        httpx.Response(304),
        httpx.Response(304),
    ]
    sf = h.client(describe_cache=esf.DescribeCacheConfig(ttl=0))
    h.run(sf.ui.object_info("Lead"))
    assert h.run(sf.ui.object_info("Lead"))["apiName"] == "Lead"
    assert h.run(sf.ui.object_info("Lead"))["apiName"] == "Lead"
    assert route.calls[1].request.headers["if-none-match"] == etag
    assert route.calls[2].request.headers["if-none-match"] == etag  # kept across the 304


def test_object_info_without_revalidation_sends_no_etag(h: Harness, mock: respx.MockRouter) -> None:
    route = mock.get(f"{UI}/object-info/Lead").respond(
        json=load_fixture("ui/object_info_lead.json"), headers={"ETag": '"abc--gzip"'}
    )
    sf = h.client(describe_cache=esf.DescribeCacheConfig(ttl=0, revalidate=False))
    h.run(sf.ui.object_info("Lead"))
    h.run(sf.ui.object_info("Lead"))
    assert "if-none-match" not in route.calls[1].request.headers
    assert route.call_count == 2


def test_unknown_object_is_forbidden(h: Harness, mock: respx.MockRouter) -> None:
    """V61: Salesforce answers 403 ``INSUFFICIENT_ACCESS`` for an object that doesn't exist."""
    name = "ui/error_unknown_object.json"
    mock.get(f"{UI}/object-info/Nope__c").respond(fixture_status(name), json=load_fixture(name))
    sf = h.client()
    with pytest.raises(esf.Forbidden) as ei:
        h.run(sf.ui.object_info("Nope__c"))
    assert (ei.value.status, ei.value.error_code) == (403, "INSUFFICIENT_ACCESS")
    assert ei.value.operation == "ui.object_info"


def test_unsupported_object_is_invalid_type(h: Harness, mock: respx.MockRouter) -> None:
    name = "ui/error_unsupported_object.json"
    mock.get(f"{UI}/object-info/PermissionSetAssignment").respond(
        fixture_status(name), json=load_fixture(name)
    )
    sf = h.client()
    with pytest.raises(esf.InvalidType) as ei:
        h.run(sf.ui.object_info("PermissionSetAssignment"))
    assert (ei.value.status, ei.value.error_code) == (400, "INVALID_TYPE")


def test_object_type_not_found_is_invalid_type(h: Harness, mock: respx.MockRouter) -> None:
    """Synthetic body: ``NOT_FOUND`` on an object path still maps to ``InvalidType`` (§8.2)."""
    mock.get(f"{UI}/object-info/Nope__c").respond(
        404, json=[{"errorCode": "NOT_FOUND", "message": "The requested resource does not exist"}]
    )
    sf = h.client()
    with pytest.raises(esf.InvalidType):
        h.run(sf.ui.object_info("Nope__c"))


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
    assert [v["value"] for v in status["values"]] == [
        "Open - Not Contacted",
        "Working - Contacted",
        "Closed - Converted",
        "Closed - Not Converted",
    ]
    assert status["defaultValue"]["value"] == "Open - Not Contacted"
    state = values["picklistFieldValues"]["StateCode"]  # a dependent picklist
    assert state["controllerValues"]["AF"] == 0
    assert all(isinstance(i, int) for v in state["values"] for i in v["validFor"])
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
    assert defaults["record"]["id"] is None
    assert defaults["record"]["fields"]["Status"]["value"] == "Open - Not Contacted"
    assert defaults["layout"]["mode"] == "Create"
    assert "Lead" in defaults["objectInfos"]
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
    assert defaults["record"]["fields"]["Company"]["value"] == "esf-sandbox"
    assert defaults["record"]["fields"]["LastName"]["value"].startswith("esf-sandbox-")
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
    assert (page["count"], page["pageSize"], page["nextPageToken"]) == (2, 2, "2")
    assert page["previousPageToken"] is None
    page = h.run(sf.ui.list_records("Lead", "AllOpenLeads", page_size=2, page_token=page["nextPageToken"]))
    assert dict(route.calls[1].request.url.params) == {"pageSize": "2", "pageToken": "2"}
    assert (page["currentPageToken"], page["nextPageToken"], page["previousPageToken"]) == ("2", "4", "0")
    for r in page["records"]:
        assert r["apiName"] == "Lead"
        assert all(set(f) == {"value", "displayValue"} for f in r["fields"].values())
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


def test_list_records_bad_page_token(h: Harness, mock: respx.MockRouter) -> None:
    name = "ui/error_page_token.json"
    mock.get(f"{UI}/list-records/Lead/AllOpenLeads").respond(fixture_status(name), json=load_fixture(name))
    sf = h.client()
    with pytest.raises(esf.RequestError) as ei:
        h.run(sf.ui.list_records("Lead", "AllOpenLeads", page_token="zz"))
    assert (ei.value.status, ei.value.error_code) == (400, "ILLEGAL_QUERY_PARAMETER_VALUE")
