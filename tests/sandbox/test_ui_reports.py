"""User Interface API (``sf.ui``) and Reports and Dashboards (``sf.reports``) against a real org.

Covers big-bang.md §21 V60-V69 and V87, on both clients. The Leads these tests create carry the
run tag and are deleted at the end of the module (and by the session cleanup). Reports run with
a ``LAST_NAME startsWith <prefix>`` filter, so other runs' Leads in the org don't change the rows.

Org fixtures: ``tests/sandbox/org/extras`` (``unfiled$public/Esf_Sandbox_Leads_Tabular`` and
``..._Summary``) and ``tests/sandbox/org/reports`` (the matrix report
``Esf_Sandbox_Reports/Esf_Sandbox_Leads_Matrix`` and the dashboard
``Esf_Sandbox_Dashboards/Esf_Sandbox_Leads_Dashboard``). ``scripts/sandbox_setup_org.py``
deploys all three folders.

Set ``ESF_SANDBOX_RECORD_DIR=<dir>`` to record each exchange of the sync run into
``<dir>/<name>.json`` with ``esf.testing.record``. ``scripts/sandbox_ui_reports_fixtures.py``
turns those files into ``tests/fixtures/recorded/ui`` and ``.../reports``.

Docs (developer.salesforce.com refuses non-browser clients; read from search excerpts on
2026-11-12):

- UI API: *Get List Views for an Object*
  https://developer.salesforce.com/docs/atlas.en-us.uiapi.meta/uiapi/ui_api_resources_lists_object_get.htm,
  *Get a Record* https://developer.salesforce.com/docs/atlas.en-us.uiapi.meta/uiapi/ui_api_resources_record_get.htm,
  *Status Codes and Error Responses* https://developer.salesforce.com/docs/atlas.en-us.uiapi.meta/uiapi/ui_api_errors.htm.
- Reports: *Execute Sync* https://developer.salesforce.com/docs/atlas.en-us.api_analytics.meta/api_analytics/sforce_analytics_rest_api_getreportrundata.htm,
  *Execute Async* https://developer.salesforce.com/docs/atlas.en-us.api_analytics.meta/api_analytics/sforce_analytics_rest_api_instances_summaryasync.htm,
  *Decode the Fact Map* https://developer.salesforce.com/docs/atlas.en-us.api_analytics.meta/api_analytics/sforce_analytics_rest_api_factmap_example.htm,
  *Refresh a Dashboard* https://developer.salesforce.com/docs/atlas.en-us.api_analytics.meta/api_analytics/analytics_api_dashboard_refresh.htm,
  *Report List* https://developer.salesforce.com/docs/atlas.en-us.api_analytics.meta/api_analytics/sforce_analytics_rest_api_recentreportslist.htm.
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

import even_simpler_salesforce as esf
from even_simpler_salesforce.core.http import RequestSpec
from even_simpler_salesforce.testing.recorder import record

from .conftest import Cleanup, Driver, new_row
from .settings import Settings

pytestmark = pytest.mark.sandbox

MASTER = "012000000000000AAA"
# Valid ids (right case checksum) of records that don't exist.
MISSING_LEAD = "00Q000000000000EAA"
MISSING_REPORT = "00O000000000000EAA"
MISSING_INSTANCE = "0LG000000000000EAA"
MISSING_DASHBOARD = "01Z000000000000EAA"
OPEN = "Open - Not Contacted"
WORKING = "Working - Contacted"
# (LeadSource, Status) of the Leads the report tests read. Matrix cells Phone Inquiry x Working
# and Partner Referral x Open stay empty on purpose (V67: empty cells).
LEADS = [
    ("Web", OPEN),
    ("Web", OPEN),
    ("Web", WORKING),
    ("Phone Inquiry", OPEN),
    ("Partner Referral", WORKING),
]
REVENUE = 1500000
BIG = 2001  # one more than a synchronous run returns (V65)


def _client(sb: Driver, name: str | None = None, **kw: Any) -> Any:
    """A client; with ``ESF_SANDBOX_RECORD_DIR`` the sync run records into ``<dir>/<name>.json``."""
    out = os.environ.get("ESF_SANDBOX_RECORD_DIR")
    if out and name and not sb.is_async:
        Path(out).mkdir(parents=True, exist_ok=True)
        target = Path(out) / f"{name}.json"
        target.unlink(missing_ok=True)
        kw["http_client"] = httpx.Client(transport=record(target))
    return sb.client(**kw)


def _tag_filter(prefix: str) -> list[dict[str, str]]:
    return [{"column": "LAST_NAME", "operator": "startsWith", "value": prefix}]


@pytest.fixture(scope="module")
def org_ids(settings: Settings) -> dict[str, str]:
    """Ids of the report and dashboard fixtures (skips when they aren't deployed)."""
    with settings.sync_client() as sf:
        reports = {
            r["DeveloperName"]: r["Id"]
            for r in sf.query_iter(
                "SELECT Id, DeveloperName FROM Report WHERE DeveloperName LIKE 'Esf_Sandbox_Leads_%'"
            )
        }
        dashboards = sf.query(
            "SELECT Id FROM Dashboard WHERE DeveloperName = 'Esf_Sandbox_Leads_Dashboard'"
        ).records
    needed = {"Esf_Sandbox_Leads_Tabular", "Esf_Sandbox_Leads_Summary", "Esf_Sandbox_Leads_Matrix"}
    if not needed <= set(reports) or not dashboards:
        pytest.skip("sandbox: report/dashboard fixtures missing; run scripts/sandbox_setup_org.py")
    return {
        "tabular": reports["Esf_Sandbox_Leads_Tabular"],
        "summary": reports["Esf_Sandbox_Leads_Summary"],
        "matrix": reports["Esf_Sandbox_Leads_Matrix"],
        "dashboard": dashboards[0]["Id"],
    }


def _wait_for_report_rows(sf: esf.Salesforce, report_id: str, prefix: str, count: int) -> None:
    """Wait until a report run sees ``count`` new Leads.

    Org-observed (API 68.0): a report run right after the insert can miss the new records for
    a moment, so the report tests would be flaky without this wait.
    """
    deadline = time.monotonic() + 60
    while len(sf.reports.run(report_id, _tag_filter(prefix)).rows()) < count:
        assert time.monotonic() < deadline, "the report never saw the new Leads"
        time.sleep(2)


@pytest.fixture(scope="module")
def leads(
    settings: Settings, tag: str, cleanup: Cleanup, org_ids: dict[str, str]
) -> Iterator[dict[str, Any]]:
    """Five tagged Leads (``LEADS``) for the UI API and report tests; deleted afterwards."""
    if settings.read_only:
        pytest.skip("sandbox: ESF_SANDBOX_READ_ONLY=1 (no writes)")
    prefix = f"{tag}-r"
    rows = [
        new_row("Lead", prefix, f"{i}", LeadSource=src, Status=status, AnnualRevenue=REVENUE)
        for i, (src, status) in enumerate(LEADS)
    ]
    with settings.sync_client() as sf:
        result = sf.collections.create(rows, sobject="Lead", all_or_none=True)
        ids = [r.id for r in result]
        cleanup.add(*ids)
        try:
            _wait_for_report_rows(sf, org_ids["tabular"], prefix, len(LEADS))
            yield {"prefix": prefix, "ids": ids}
        finally:
            sf.collections.delete(ids)
            cleanup.discard(*ids)


@pytest.fixture(scope="module")
def big_leads(settings: Settings, tag: str, cleanup: Cleanup, org_ids: dict[str, str]) -> Iterator[str]:
    """2,001 tagged Leads (heavy writes) for the 2,000-row limit of a synchronous run."""
    if settings.read_only or not settings.allow_writes:
        pytest.skip("sandbox: heavy writes (>200 rows) need ESF_SANDBOX_ALLOW_WRITES=1")
    prefix = f"{tag}-big"
    rows = [new_row("Lead", prefix, f"{i:04d}") for i in range(BIG)]
    with settings.sync_client() as sf:
        result = sf.collections.create(rows, sobject="Lead")
        ids = [r.id for r in result.succeeded]
        cleanup.add(*ids)
        try:
            assert len(ids) == BIG, result.failed[:3]
            _wait_for_report_rows(sf, org_ids["tabular"], prefix, 2000)
            yield prefix
        finally:
            sf.collections.delete(ids)
            cleanup.discard(*ids)


# --- UI API ------------------------------------------------------------------------------------


def test_ui_list_views(sb: Driver) -> None:
    """V60: ``GET ui-api/list-info/{object}`` lists views with apiName, id, label and url."""
    sf = _client(sb, "list_views_lead")
    views = sb.run(sf.ui.list_views("Lead"))
    assert {"count", "lists", "pageSize", "nextPageToken", "currentPageToken", "objectApiName"} <= set(views)
    assert views["pageSize"] == 20  # default page size of list-info
    names = {v["apiName"]: v for v in views["lists"]}
    assert "AllOpenLeads" in names
    for v in views["lists"]:
        assert set(v) == {"apiName", "id", "label", "url"}
        assert v["url"].endswith(f"/ui-api/list-info/Lead/{v['apiName']}")
    assert names["AllOpenLeads"]["id"].startswith("00B")
    # The recently viewed list is a real entry, but it has no list view id.
    assert names["__Recent"]["id"] is None


def test_ui_errors(sb: Driver, leads: dict[str, Any]) -> None:
    """V61: the status and errorCode of UI API errors, as the client classifies them."""
    sf = _client(sb, "errors")
    # An unknown object: 403 INSUFFICIENT_ACCESS (org-observed; the docs don't say).
    with pytest.raises(esf.Forbidden) as e1:
        sb.run(sf.ui.object_info("Esf_Nope__c", cached=False))
    assert (e1.value.status, e1.value.error_code) == (403, "INSUFFICIENT_ACCESS")
    with pytest.raises(esf.Forbidden) as e1b:
        sb.run(sf.ui.list_views("Esf_Nope__c"))
    assert (e1b.value.status, e1b.value.error_code) == (403, "INSUFFICIENT_ACCESS")
    # An object the UI API doesn't support: 400 INVALID_TYPE.
    with pytest.raises(esf.InvalidType) as e2:
        sb.run(sf.ui.object_info("PermissionSetAssignment", cached=False))
    assert (e2.value.status, e2.value.error_code) == (400, "INVALID_TYPE")
    # A record that doesn't exist (or the user can't see): 404 NOT_FOUND.
    with pytest.raises(esf.NotFound) as e3:
        sb.run(sf.ui.record(MISSING_LEAD, fields=["Lead.Name"]))
    assert (e3.value.status, e3.value.error_code) == (404, "NOT_FOUND")
    lead_id = leads["ids"][0]
    # An unknown field in ``fields``: 400 INVALID_FIELD.
    with pytest.raises(esf.InvalidField) as e4:
        sb.run(sf.ui.record(lead_id, fields=["Lead.Esf_Nope__c"]))
    assert (e4.value.status, e4.value.error_code) == (400, "INVALID_FIELD")
    # A field of another object: 400 INVALID_INPUT (a plain RequestError).
    with pytest.raises(esf.RequestError) as e5:
        sb.run(sf.ui.record(lead_id, fields=["Account.Name"]))
    assert (e5.value.status, e5.value.error_code) == (400, "INVALID_INPUT")
    # An id with a wrong case checksum: 400.
    with pytest.raises(esf.RequestError) as e6:
        sb.run(sf.ui.record("00Q000000000000AAA", fields=["Lead.Name"]))
    assert e6.value.status == 400
    # An unknown list view: 404 NOT_FOUND; a bad page token: 400 ILLEGAL_QUERY_PARAMETER_VALUE.
    with pytest.raises(esf.NotFound):
        sb.run(sf.ui.list_records("Lead", "Esf_Nope"))
    with pytest.raises(esf.RequestError) as e7:
        sb.run(sf.ui.list_records("Lead", "AllOpenLeads", page_token="zz"))
    assert (e7.value.status, e7.value.error_code) == (400, "ILLEGAL_QUERY_PARAMETER_VALUE")


def _status(sb: Driver, sf: Any, path: str, headers: dict[str, str]) -> int:
    spec = RequestSpec("GET", path, headers=headers, operation="raw", allow_not_modified=True)
    return sb.run(sf._send(spec)).status  # pyright: ignore[reportPrivateUsage]


def test_ui_if_modified_since_is_ignored(sb: Driver) -> None:
    """V62: object-info and picklist-values ignore ``If-Modified-Since`` (always 200).

    They send no ``Last-Modified`` header, but an ``ETag``; ``If-None-Match`` with it gets 304,
    so the describe cache revalidates them with the ``ETag``.
    """
    sf = sb.client(describe_cache=esf.DescribeCacheConfig(ttl=0))
    first = sb.run(sf.ui.object_info("Lead"))
    assert sb.run(sf.ui.object_info("Lead"))["apiName"] == first["apiName"]
    sb.run(sf.ui.picklist_values("Lead", MASTER))
    sb.run(sf.ui.picklist_values("Lead", MASTER))
    info = [e["status"] for e in sb.recorder.requests("ui.object_info")]
    picks = [e["status"] for e in sb.recorder.requests("ui.picklist_values")]
    assert info == [200, 304], info
    assert picks == [200, 304], picks
    for path in ("ui-api/object-info/Lead", f"ui-api/object-info/Lead/picklist-values/{MASTER}"):
        raw = sb.run(sf._send(RequestSpec("GET", path, operation="raw")))  # pyright: ignore[reportPrivateUsage]
        assert raw.headers.get("last-modified") is None
        etag = raw.headers.get("etag")
        assert etag
        future = time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime(time.time() + 3600))
        assert _status(sb, sf, path, {"If-Modified-Since": future}) == 200
        assert _status(sb, sf, path, {"If-Modified-Since": "Thu, 01 Jan 2015 00:00:00 GMT"}) == 200
        assert _status(sb, sf, path, {"If-None-Match": etag}) == 304


def test_ui_record_parameters(sb: Driver, leads: dict[str, Any]) -> None:
    """V63: one of fields/optionalFields/layoutTypes is required; fields + layoutTypes needs more."""
    sf = _client(sb, "record_parameters")
    lead_id = leads["ids"][0]
    path = f"ui-api/records/{lead_id}"
    with pytest.raises(esf.RequestError) as e1:
        sb.run(sf.request("GET", path))
    assert (e1.value.status, e1.value.error_code) == (400, "INVALID_INPUT_COMBINATION")
    assert "at least one" in str(e1.value)
    both = {"fields": "Lead.Name", "layoutTypes": "Full"}
    with pytest.raises(esf.RequestError) as e2:
        sb.run(sf.request("GET", path, params=both))
    assert (e2.value.status, e2.value.error_code) == (400, "INVALID_INPUT_COMBINATION")
    # ... unless childRelationships is given too (a parameter sf.ui.record doesn't send).
    with_children = sb.run(sf.request("GET", path, params={**both, "childRelationships": "Lead.Tasks"}))
    assert "LastName" in with_children["fields"]
    # optionalFields alone is enough from API 45.0.
    assert set(sb.run(sf.request("GET", path, params={"optionalFields": "Lead.Name"}))["fields"]) == {"Name"}
    # The client refuses fields with layout_types before sending.
    with pytest.raises(ValueError, match="not both"):
        sb.run(sf.ui.record(lead_id, fields=["Lead.Name"], layout_types=["Full"]))
    # layoutTypes alone works; modes defaults to View.
    full = sb.run(sf.ui.record(lead_id, layout_types=["Full"]))
    assert full["id"] == lead_id
    assert "LastName" in full["fields"]
    edit = sb.run(sf.ui.record(lead_id, layout_types=["Full"], modes=["Edit"]))
    assert "LastName" in edit["fields"]


def test_ui_record_shapes(sb: Driver, leads: dict[str, Any]) -> None:
    """V64: record fields are ``{value, displayValue}``; defaults and picklist shapes."""
    lead_id = leads["ids"][0]
    sf = _client(sb, "shapes")
    rec = sb.run(sf.ui.record(lead_id, fields=["Lead.Name", "Lead.AnnualRevenue", "Lead.Status"]))
    assert set(rec) == {
        "apiName",
        "childRelationships",
        "eTag",
        "fields",
        "id",
        "lastModifiedById",
        "lastModifiedDate",
        "recordTypeId",
        "recordTypeInfo",
        "systemModstamp",
        "weakEtag",
    }
    assert rec["apiName"] == "Lead"
    assert set(rec["fields"]) == {"Name", "AnnualRevenue", "Status"}
    for f in rec["fields"].values():
        assert set(f) == {"value", "displayValue"}
    assert rec["fields"]["AnnualRevenue"]["value"] == REVENUE
    assert rec["fields"]["AnnualRevenue"]["displayValue"] == "$1,500,000"  # formatted currency
    assert rec["fields"]["Name"]["displayValue"] is None  # plain text has no display value
    assert rec["fields"]["Status"]["displayValue"] == OPEN  # picklist label
    create = sb.run(sf.ui.create_defaults("Lead"))
    assert set(create) >= {"record", "layout", "objectInfos"}
    assert create["record"]["id"] is None
    assert "Lead" in create["objectInfos"]
    clone = sb.run(sf.ui.clone_defaults(lead_id))
    assert set(clone) >= {"record", "layout", "objectInfos"}
    assert clone["record"]["id"] is None
    assert clone["record"]["fields"]["LastName"]["value"].startswith(leads["prefix"])
    picks = sb.run(sf.ui.picklist_values("Lead", MASTER, cached=False))
    source = picks["picklistFieldValues"]["LeadSource"]
    assert set(source) >= {"values", "defaultValue", "controllerValues"}
    assert set(source["values"][0]) == {"attributes", "label", "validFor", "value"}
    status = picks["picklistFieldValues"]["Status"]
    assert status["defaultValue"]["value"] == OPEN  # Lead.Status has a default
    info = sb.run(sf.ui.object_info("Lead", cached=False))
    assert info["apiName"] == "Lead"
    assert MASTER in info["recordTypeInfos"]


def test_ui_list_records_pages(sb: Driver, leads: dict[str, Any]) -> None:
    """V64: list-records pages with ``nextPageToken``, ``null`` on the last page."""
    sf = _client(sb, "list_records")
    first = sb.run(sf.ui.list_records("Lead", "AllOpenLeads", page_size=2))
    assert set(first) >= {"records", "count", "nextPageToken", "pageSize", "currentPageToken"}
    assert first["pageSize"] == 2
    assert first["count"] == len(first["records"]) <= 2
    assert first["nextPageToken"] is not None  # the org has more than two open Leads
    for r in first["records"]:
        assert r["apiName"] == "Lead"
        assert all(set(f) == {"value", "displayValue"} for f in r["fields"].values())
    second = sb.run(
        sf.ui.list_records("Lead", "AllOpenLeads", page_size=2, page_token=first["nextPageToken"])
    )
    assert second["currentPageToken"] == first["nextPageToken"]
    default = sb.run(sf.ui.list_records("Lead", "AllOpenLeads"))
    assert default["pageSize"] == 50
    recent = sb.run(sf.ui.list_records("Lead", "__Recent", page_size=2000))
    assert recent["nextPageToken"] is None


# --- Reports and dashboards ---------------------------------------------------------------------


def test_reports_recent(sb: Driver, org_ids: dict[str, str]) -> None:
    """V87: ``GET analytics/reports`` lists recently viewed reports, as sent."""
    sf = _client(sb, "recent")
    sb.run(sf.reports.describe(org_ids["tabular"]))  # viewing makes it recent
    recent = sb.run(sf.reports.recent())
    assert isinstance(recent, list)
    assert 0 < len(recent) <= 200
    for r in recent:
        # ``fieldsUrl`` is not in the docs' list of keys (org-observed).
        assert set(r) == {"id", "name", "url", "describeUrl", "instancesUrl", "fieldsUrl"}
        assert r["url"].endswith(f"/analytics/reports/{r['id']}")


def test_reports_run_tabular_with_filters(sb: Driver, leads: dict[str, Any], org_ids: dict[str, str]) -> None:
    """V65, V67: a POST run with filters returns only the tagged Leads in ``T!T``."""
    sf = _client(sb, "tabular_run")
    result = sb.run(sf.reports.run(org_ids["tabular"], _tag_filter(leads["prefix"])))
    assert result.format == "TABULAR"
    assert set(result.raw["factMap"]) == {"T!T"}
    rows = result.rows()
    assert len(rows) == len(LEADS)
    # Columns that link to the record (LAST_NAME, COMPANY) have the record id as ``value``;
    # the text is the cell ``label``.
    assert {r["LAST_NAME"] for r in rows} == set(leads["ids"])
    assert {r["COMPANY"] for r in rows} == set(leads["ids"])
    labels = {row["dataCells"][0]["label"] for row in result.raw["factMap"]["T!T"]["rows"]}
    assert labels == {f"{leads['prefix']}{i}" for i in range(len(LEADS))}
    assert sorted(r["LEAD_SOURCE"] for r in rows) == sorted(s for s, _ in LEADS)
    assert result.all_data
    # The saved report has no filter: the run didn't change it.
    meta = sb.run(sf.reports.describe(org_ids["tabular"]))["reportMetadata"]
    assert meta["reportFilters"] == []
    summary = sb.run(sf.reports.run(org_ids["tabular"], _tag_filter(leads["prefix"]), include_details=False))
    assert not summary.has_detail_rows
    assert summary.rows() == []
    assert summary.summary_rows() == [{"RowCount": len(LEADS)}]


def test_reports_summary_fact_map(sb: Driver, leads: dict[str, Any], org_ids: dict[str, str]) -> None:
    """V67: a two-level summary report keeps rows in ``0_0!T`` leaves and totals in ``0!T``/``T!T``."""
    sf = _client(sb, "summary_two_level_run")
    result = sb.run(sf.reports.run(org_ids["summary"], _tag_filter(leads["prefix"])))
    assert result.format == "SUMMARY"
    fm = result.raw["factMap"]
    assert "T!T" in fm
    assert "0!T" in fm
    assert "0_0!T" in fm
    for key, entry in fm.items():
        down = key.split("!")[0]
        if down == "T" or "_" not in down:
            assert not entry.get("rows"), key  # subtotals and the grand total carry no rows
    rows = result.rows()
    assert len(rows) == len(LEADS)
    assert {(r["LEAD_SOURCE"], r["STATUS"]) for r in rows} == set(LEADS)
    counts = {(r.get("LEAD_SOURCE"), r.get("STATUS")): r["RowCount"] for r in result.summary_rows()}
    assert counts[("Web", OPEN)] == 2
    assert counts[("Web", None)] == 3
    assert counts[(None, None)] == len(LEADS)


def test_reports_currency_and_date_cells(sb: Driver, leads: dict[str, Any], org_ids: dict[str, str]) -> None:
    """V65, V67: a POST body may also replace groupings, columns and aggregates for one run.

    A currency detail cell's ``value`` is ``{"amount", "currency"}`` (``currency`` is null in a
    single-currency org); a currency aggregate's ``value`` is a plain number; a date grouping's
    ``value`` is an ISO date.
    """
    sf = _client(sb, "summary_run")
    body = {
        "reportMetadata": {
            "groupingsDown": [{"name": "CREATED_DATE", "dateGranularity": "Day", "sortOrder": "Asc"}],
            "aggregates": ["s!ANNUAL_REVENUE", "RowCount"],
            "detailColumns": ["LAST_NAME", "ANNUAL_REVENUE", "LEAD_SOURCE"],
            "reportFilters": _tag_filter(leads["prefix"]),
        }
    }
    raw = sb.run(
        sf.request(
            "POST", f"analytics/reports/{org_ids['summary']}", params={"includeDetails": "true"}, json=body
        )
    )
    result = esf.ReportResult(raw)
    (group,) = raw["groupingsDown"]["groupings"]
    assert len(group["value"]) == 10
    assert group["value"][4] == "-"
    rows = result.rows()
    assert len(rows) == len(LEADS)
    assert all(r["ANNUAL_REVENUE"] == {"amount": REVENUE, "currency": None} for r in rows)
    assert all(r["CREATED_DATE"] == group["value"] for r in rows)
    totals = result.summary_rows()[-1]
    assert totals == {"s!ANNUAL_REVENUE": REVENUE * len(LEADS), "RowCount": len(LEADS)}
    # The saved report kept its groupings.
    meta = sb.run(sf.reports.describe(org_ids["summary"]))["reportMetadata"]
    assert [g["name"] for g in meta["groupingsDown"]] == ["LEAD_SOURCE", "STATUS"]


def test_reports_matrix_fact_map(sb: Driver, leads: dict[str, Any], org_ids: dict[str, str]) -> None:
    """V67: a sparse matrix report; empty cells are present with a zero count and no rows."""
    sf = _client(sb, "matrix_run")
    result = sb.run(sf.reports.run(org_ids["matrix"], _tag_filter(leads["prefix"])))
    assert result.format == "MATRIX"
    fm = result.raw["factMap"]
    rows = result.rows()
    assert {(r["LEAD_SOURCE"], r["STATUS"]) for r in rows} == set(LEADS)
    counts = {(r.get("LEAD_SOURCE"), r.get("STATUS")): r["RowCount"] for r in result.summary_rows()}
    assert counts[("Web", OPEN)] == 2
    assert counts[(None, OPEN)] == 3
    assert counts[(None, None)] == len(LEADS)
    # 3 sources x 2 statuses: every cell has an entry, the empty ones with a zero count.
    assert len(fm) == (3 + 1) * (2 + 1)
    assert counts[("Phone Inquiry", WORKING)] == 0
    assert counts[("Partner Referral", OPEN)] == 0
    sources = [g["value"] for g in result.raw["groupingsDown"]["groupings"]]
    statuses = [g["value"] for g in result.raw["groupingsAcross"]["groupings"]]
    empty = f"{sources.index('Phone Inquiry')}!{statuses.index(WORKING)}"
    assert not fm[empty].get("rows")
    # Two row groupings and one column grouping, for this run only.
    body = {
        "reportMetadata": {
            "groupingsDown": [
                {"name": "LEAD_SOURCE", "sortOrder": "Asc", "dateGranularity": "None"},
                {"name": "CREATED_DATE", "sortOrder": "Asc", "dateGranularity": "Day"},
            ],
            "groupingsAcross": [{"name": "STATUS", "sortOrder": "Asc", "dateGranularity": "None"}],
            "reportFormat": "MATRIX",
            "reportFilters": _tag_filter(leads["prefix"]),
        }
    }
    two = esf.ReportResult(
        sb.run(
            sf.request(
                "POST", f"analytics/reports/{org_ids['matrix']}", params={"includeDetails": "true"}, json=body
            )
        )
    )
    assert "0_0!0" in two.raw["factMap"]
    assert {(r["LEAD_SOURCE"], r["STATUS"]) for r in two.rows()} == set(LEADS)


@pytest.mark.usefixtures("heavy_writes")
def test_reports_sync_run_stops_at_2000_rows(sb: Driver, big_leads: str, org_ids: dict[str, str]) -> None:
    """V65: a synchronous run returns at most 2,000 detail rows and says ``allData: false``."""
    sf = sb.client()
    result = sb.run(sf.reports.run(org_ids["tabular"], _tag_filter(big_leads)))
    assert len(result.rows()) == 2000
    assert result.all_data is False
    assert result.summary_rows() == [{"RowCount": BIG}]


def test_reports_async_instance(sb: Driver, leads: dict[str, Any], org_ids: dict[str, str]) -> None:
    """V66: ``POST .../instances`` starts a run; polling returns the results on Success."""
    sf = _client(sb, "instances")
    started = sb.run(sf.reports.run_async(org_ids["summary"], _tag_filter(leads["prefix"])))
    assert started.id.startswith("0LG")
    assert started.status in ("New", "Running", "Success")
    assert started.url
    assert started.url.endswith(f"/instances/{started.id}")
    result = sb.run(
        sf.reports.instance(org_ids["summary"], started.id, poll=esf.Backoff(start=1, max=5), timeout=300)
    )
    attrs = result.raw["attributes"]
    assert attrs["status"] == "Success"
    assert attrs["id"] == started.id
    assert attrs["type"] == "ReportInstance"
    assert len(result.rows()) == len(LEADS)
    listed = sb.run(sf.request("GET", f"analytics/reports/{org_ids['summary']}/instances"))
    assert any(i["id"] == started.id for i in listed)
    with pytest.raises(esf.NotFound) as ei:
        sb.run(sf.reports.instance(org_ids["summary"], MISSING_INSTANCE, timeout=10))
    assert (ei.value.status, ei.value.error_code) == (404, "NOT_FOUND")


def test_reports_errors(sb: Driver, org_ids: dict[str, str]) -> None:
    """V69: unknown report 404 NOT_FOUND; bad filters 400 BAD_REQUEST; unknown dashboard 400."""
    sf = _client(sb, "errors_reports")
    with pytest.raises(esf.NotFound) as e1:
        sb.run(sf.reports.run(MISSING_REPORT))
    assert (e1.value.status, e1.value.error_code) == (404, "NOT_FOUND")
    with pytest.raises(esf.RequestError) as e2:
        sb.run(
            sf.reports.run(org_ids["tabular"], [{"column": "ESF_NOPE", "operator": "equals", "value": "x"}])
        )
    assert (e2.value.status, e2.value.error_code) == (400, "BAD_REQUEST")
    with pytest.raises(esf.RequestError) as e3:
        sb.run(
            sf.reports.run(
                org_ids["tabular"], [{"column": "LEAD_SOURCE", "operator": "sortOf", "value": "x"}]
            )
        )
    assert (e3.value.status, e3.value.error_code) == (400, "BAD_REQUEST")
    # The same check happens before an asynchronous run starts.
    with pytest.raises(esf.RequestError) as e4:
        sb.run(
            sf.reports.run_async(
                org_ids["tabular"], [{"column": "ESF_NOPE", "operator": "equals", "value": "x"}]
            )
        )
    assert (e4.value.status, e4.value.error_code) == (400, "BAD_REQUEST")
    # An unknown dashboard id: 400 INVALID_CROSS_REFERENCE_KEY (not 404).
    with pytest.raises(esf.InvalidReference) as e5:
        sb.run(sf.reports.dashboards.get(MISSING_DASHBOARD))
    assert (e5.value.status, e5.value.error_code) == (400, "INVALID_CROSS_REFERENCE_KEY")


def _refused(exc: esf.SalesforceError) -> None:
    """A refresh refused because one is running (409) or ran less than a minute ago (403)."""
    if isinstance(exc, esf.Conflict):
        assert (exc.status, exc.error_code) == (409, "CONFLICT")
        assert "already in progress" in str(exc)
    else:
        assert isinstance(exc, esf.Forbidden), exc
        assert (exc.status, exc.error_code) == (403, "FORBIDDEN")
        assert "once in a minute" in str(exc)


def test_dashboard_refresh_and_get(sb: Driver, leads: dict[str, Any], org_ids: dict[str, str]) -> None:
    """V68, V69: ``PUT`` starts a refresh (body: ``statusUrl`` only); ``GET`` returns components.

    A component has ``reportResult: null`` until the dashboard was refreshed once. A refresh
    while one is running is refused with 409 ``CONFLICT``, and one less than a minute after
    the last with 403 ``FORBIDDEN``.
    """
    sf = _client(sb, "dashboard")
    dash = org_ids["dashboard"]
    status_path = f"analytics/dashboards/{dash}/status"
    try:
        started = sb.run(sf.reports.dashboards.refresh(dash))
    except esf.SalesforceError as exc:  # the other client's refresh is running or too recent
        _refused(exc)
    else:
        assert set(started) == {"statusUrl"}
        assert started["statusUrl"].endswith(f"/{status_path}")
        with pytest.raises(esf.SalesforceError) as again:
            sb.run(sf.reports.dashboards.refresh(dash))
        _refused(again.value)
    deadline = time.monotonic() + 120
    while True:
        status = sb.run(sf.request("GET", status_path))
        states = {c["refreshStatus"] for c in status["componentStatus"]}
        if states == {"IDLE"} or time.monotonic() > deadline:
            break
        time.sleep(3)
    assert states == {"IDLE"}
    body = sb.run(sf.reports.dashboards.get(dash))
    assert set(body) >= {"attributes", "dashboardMetadata", "componentData"}
    comp = body["componentData"][0]
    assert comp["status"]["refreshStatus"] == "IDLE"
    assert comp["status"]["componentDataStatus"] == "DATA"
    result = esf.ReportResult(comp["reportResult"])
    assert result.format == "SUMMARY"
    assert result.summary_rows()[-1]["RowCount"] >= len(LEADS)
