"""Reports and dashboards: ``sf.reports`` and ``sf.reports.dashboards`` (§9.14), on both clients.

The responses are **recorded** from a Developer Edition org on API 68.0
(``tests/fixtures/recorded/reports/*.json``, made by ``tests/sandbox/test_ui_reports.py`` and
``scripts/sandbox_ui_reports_fixtures.py``) against three Lead reports (tabular, summary by
lead source and status, matrix by lead source and status) and a dashboard on the summary
report. Only ``instance_running.json`` and ``instance_error.json`` are synthetic: a small run
finished before the first poll and never failed. Docs, read through search excerpts because
developer.salesforce.com refuses automated fetches (2026-11-12):

- Execute Sync (``GET`` or ``POST analytics/reports/{id}``; ``includeDetails``; filters in
  ``reportMetadata`` of the ``POST`` body):
  https://developer.salesforce.com/docs/atlas.en-us.api_analytics.meta/api_analytics/sforce_analytics_rest_api_getreportrundata.htm
- Run Reports Synchronously or Asynchronously (the 2,000 detail-row limit):
  https://developer.salesforce.com/docs/atlas.en-us.api_analytics.meta/api_analytics/sforce_analytics_rest_api_get_reportdata.htm
- Execute Async and Instances List:
  https://developer.salesforce.com/docs/atlas.en-us.api_analytics.meta/api_analytics/sforce_analytics_rest_api_instances_summaryasync.htm
  https://developer.salesforce.com/docs/atlas.en-us.api_analytics.meta/api_analytics/sforce_analytics_rest_api_instances_resource.htm
- Decode the Fact Map (``T!T``, ``0!T``, ``0_0!0_0`` keys):
  https://developer.salesforce.com/docs/atlas.en-us.api_analytics.meta/api_analytics/sforce_analytics_rest_api_factmap_example.htm
- Get Dashboard Results and Refresh a Dashboard:
  https://developer.salesforce.com/docs/atlas.en-us.api_analytics.meta/api_analytics/analytics_api_dashboard_results_resource.htm
  https://developer.salesforce.com/docs/atlas.en-us.api_analytics.meta/api_analytics/analytics_api_dashboard_refresh.htm

Org-observed (big-bang.md §21 V65-V69): empty matrix cells are sent with a zero count and no
rows; a currency detail cell is ``{"amount", "currency"}`` but a currency aggregate is a plain
number; a refresh answers 201 with only ``statusUrl``; an unknown dashboard is 400
``INVALID_CROSS_REFERENCE_KEY``.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
import respx

import even_simpler_salesforce as esf

from ..conftest import DATA, Harness, Recorder
from ..fixtures import fixture_status, load_fixture

# The masked ids in the recordings.
REPORT = "00O000000000001EAA"
INSTANCE = "0LG000000000002GAA"
DASHBOARD = "01Z000000000001EAA"
RUN_URL = f"{DATA}/analytics/reports/{REPORT}"
INSTANCES_URL = f"{RUN_URL}/instances"
INSTANCE_URL = f"{INSTANCES_URL}/{INSTANCE}"
DASHBOARD_URL = f"{DATA}/analytics/dashboards/{DASHBOARD}"
TAG = [{"column": "LAST_NAME", "operator": "startsWith", "value": "esf-sandbox-"}]
OPEN, WORKING = "Open - Not Contacted", "Working - Contacted"
SOURCES = {
    ("Web", OPEN): 2,
    ("Web", WORKING): 1,
    ("Phone Inquiry", OPEN): 1,
    ("Partner Referral", WORKING): 1,
}


def test_describe(h: Harness, mock: respx.MockRouter, rec: Recorder) -> None:
    mock.get(f"{RUN_URL}/describe").respond(json=load_fixture("reports/describe.json"))
    sf = h.client(recorder=rec)
    body = h.run(sf.reports.describe(REPORT))
    assert set(body) == {"attributes", "reportExtendedMetadata", "reportMetadata", "reportTypeMetadata"}
    assert body["reportMetadata"]["reportFormat"] == "TABULAR"
    assert body["reportMetadata"]["detailColumns"] == ["LAST_NAME", "COMPANY", "LEAD_SOURCE"]
    (event,) = rec.requests
    assert event["operation"] == "reports.describe"
    assert event["path_template"] == "/analytics/reports/:id/describe"


def test_run_without_filters_is_a_get(h: Harness, mock: respx.MockRouter, rec: Recorder) -> None:
    """Execute Sync, ``GET analytics/reports/{id}?includeDetails=true``.

    In a tabular report every row sits in ``T!T``. Columns that link to the record
    (``LAST_NAME``, ``COMPANY``) carry the record id as ``value`` and the text as ``label``.
    """
    route = mock.get(RUN_URL).respond(json=load_fixture("reports/tabular_run.json"))
    sf = h.client(recorder=rec)
    result = h.run(sf.reports.run(REPORT))
    assert isinstance(result, esf.ReportResult)
    assert route.calls[0].request.url.params["includeDetails"] == "true"
    assert result.report_id == REPORT
    assert result.name == "ESF Sandbox Leads Tabular"
    assert result.format == "TABULAR"
    assert result.all_data is True
    assert result.has_detail_rows is True
    assert set(result.raw["factMap"]) == {"T!T"}
    rows = result.rows()
    assert len(rows) == 5
    assert list(rows[0]) == ["LAST_NAME", "COMPANY", "LEAD_SOURCE"]
    assert all(r["LAST_NAME"].startswith("00Q") and r["COMPANY"] == r["LAST_NAME"] for r in rows)
    assert sorted(r["LEAD_SOURCE"] for r in rows) == [
        "Partner Referral",
        "Phone Inquiry",
        "Web",
        "Web",
        "Web",
    ]
    first = result.raw["factMap"]["T!T"]["rows"][0]["dataCells"][0]
    assert first["label"].startswith("esf-sandbox-")
    assert result.summary_rows() == [{"RowCount": 5}]
    (event,) = rec.requests
    assert event["operation"] == "reports.run"
    assert event["method"] == "GET"
    assert event["path_template"] == "/analytics/reports/:id"


def test_run_without_details(h: Harness, mock: respx.MockRouter) -> None:
    route = mock.post(RUN_URL).respond(json=load_fixture("reports/tabular_run_no_details.json"))
    sf = h.client()
    result = h.run(sf.reports.run(REPORT, TAG, include_details=False))
    assert route.calls[0].request.url.params["includeDetails"] == "false"
    assert result.has_detail_rows is False
    assert result.rows() == []
    assert result.summary_rows() == [{"RowCount": 5}]


def test_rows_of_a_summary_report_with_two_groupings(h: Harness, mock: respx.MockRouter) -> None:
    """Grouped by lead source, then status: detail rows sit in the deepest groupings
    (``0_0!T``); ``0!T`` holds the source subtotal and ``T!T`` the grand total.
    """
    mock.get(RUN_URL).respond(json=load_fixture("reports/summary_two_level_run.json"))
    sf = h.client()
    result = h.run(sf.reports.run(REPORT))
    assert result.format == "SUMMARY"
    fact_map = result.raw["factMap"]
    assert {"T!T", "0!T", "0_0!T"} <= set(fact_map)
    assert not fact_map["0!T"]["rows"]
    assert not fact_map["T!T"]["rows"]
    rows = result.rows()
    assert list(rows[0]) == ["LEAD_SOURCE", "STATUS", "LAST_NAME", "COMPANY"]
    pairs = [(r["LEAD_SOURCE"], r["STATUS"]) for r in rows]
    assert {p: pairs.count(p) for p in pairs} == SOURCES
    assert result.summary_rows() == [
        {"LEAD_SOURCE": "Web", "RowCount": 3},
        {"LEAD_SOURCE": "Web", "STATUS": OPEN, "RowCount": 2},
        {"LEAD_SOURCE": "Web", "STATUS": WORKING, "RowCount": 1},
        {"LEAD_SOURCE": "Phone Inquiry", "RowCount": 1},
        {"LEAD_SOURCE": "Phone Inquiry", "STATUS": OPEN, "RowCount": 1},
        {"LEAD_SOURCE": "Partner Referral", "RowCount": 1},
        {"LEAD_SOURCE": "Partner Referral", "STATUS": WORKING, "RowCount": 1},
        {"RowCount": 5},
    ]


def test_currency_and_date_cells(h: Harness, mock: respx.MockRouter) -> None:
    """A run whose ``POST`` body also set the groupings, columns and aggregates.

    A currency detail cell's ``value`` is ``{"amount", "currency"}`` (``None`` in a
    single-currency org), a currency aggregate is a plain number and a date grouping's value
    an ISO date.
    """
    mock.post(RUN_URL).respond(json=load_fixture("reports/summary_run.json"))
    sf = h.client()
    result = h.run(sf.reports.run(REPORT, TAG))
    rows = result.rows()
    assert len(rows) == 5
    assert rows[0]["ANNUAL_REVENUE"] == {"amount": 1500000, "currency": None}
    (day,) = {r["CREATED_DATE"] for r in rows}
    assert len(day) == 10
    assert day[4] == day[7] == "-"
    assert result.summary_rows() == [
        {"CREATED_DATE": day, "s!ANNUAL_REVENUE": 7500000.0, "RowCount": 5},
        {"s!ANNUAL_REVENUE": 7500000.0, "RowCount": 5},
    ]


def test_rows_of_a_matrix_report(h: Harness, mock: respx.MockRouter) -> None:
    """Rows by lead source, columns by status. Empty cells (``Phone Inquiry`` x ``Working``)
    are in the fact map with a zero count and no rows.
    """
    mock.post(RUN_URL).respond(json=load_fixture("reports/matrix_run.json"))
    sf = h.client()
    result = h.run(sf.reports.run(REPORT, TAG))
    assert result.format == "MATRIX"
    assert len(result.raw["factMap"]) == (3 + 1) * (2 + 1)
    pairs = [(r["LEAD_SOURCE"], r["STATUS"]) for r in result.rows()]
    assert {p: pairs.count(p) for p in pairs} == SOURCES
    counts = {(s.get("LEAD_SOURCE"), s.get("STATUS")): s["RowCount"] for s in result.summary_rows()}
    assert counts[("Phone Inquiry", WORKING)] == 0
    assert counts[("Partner Referral", OPEN)] == 0
    assert counts[(None, OPEN)] == 3
    assert counts[("Web", None)] == 3
    assert counts[(None, None)] == 5
    # a subtotal or total has no keys for the groupings it sums over
    assert result.summary_rows()[-1] == {"RowCount": 5}


def test_rows_of_a_matrix_report_with_two_row_groupings(h: Harness, mock: respx.MockRouter) -> None:
    """Rows by lead source and created day, columns by status (keys like ``0_0!1``)."""
    mock.post(RUN_URL).respond(json=load_fixture("reports/matrix_two_level_run.json"))
    sf = h.client()
    result = h.run(sf.reports.run(REPORT, TAG))
    assert "0_0!0" in result.raw["factMap"]
    rows = result.rows()
    assert list(rows[0]) == ["LEAD_SOURCE", "CREATED_DATE", "STATUS", "LAST_NAME", "COMPANY"]
    pairs = [(r["LEAD_SOURCE"], r["STATUS"]) for r in rows]
    assert {p: pairs.count(p) for p in pairs} == SOURCES
    levels = {len(s) for s in result.summary_rows()}
    assert levels == {1, 2, 3, 4}  # total, source or status subtotal, source x day, cell


def test_recent_lists_recently_viewed_reports(h: Harness, mock: respx.MockRouter, rec: Recorder) -> None:
    """*Report List*, ``GET analytics/reports`` (up to 200 recently viewed reports):
    https://developer.salesforce.com/docs/atlas.en-us.api_analytics.meta/api_analytics/sforce_analytics_rest_api_recentreportslist.htm
    """
    route = mock.get(f"{DATA}/analytics/reports").respond(json=load_fixture("reports/recent.json"))
    sf = h.client(recorder=rec)
    reports = h.run(sf.reports.recent())
    assert {r["name"] for r in reports} >= {"ESF Sandbox Leads Tabular", "ESF Sandbox Leads Matrix"}
    for r in reports:
        assert set(r) == {"id", "name", "url", "describeUrl", "instancesUrl", "fieldsUrl"}
        assert r["instancesUrl"].endswith(f"/analytics/reports/{r['id']}/instances")
    assert route.calls[0].request.url.params == httpx.QueryParams()
    (event,) = rec.requests
    assert event["operation"] == "reports.recent"
    assert event["path_template"] == "/analytics/reports"


@pytest.mark.parametrize("body", [{"reports": []}, ["not a report"]])
def test_recent_rejects_unexpected_bodies(h: Harness, mock: respx.MockRouter, body: Any) -> None:
    mock.get(f"{DATA}/analytics/reports").respond(json=body)
    sf = h.client()
    with pytest.raises(esf.UnexpectedResponse, match="recent reports"):
        h.run(sf.reports.recent())


def test_run_with_filters_posts_report_metadata(h: Harness, mock: respx.MockRouter) -> None:
    """Execute Sync with ``POST`` and ``reportMetadata.reportFilters`` in the body."""
    route = mock.post(RUN_URL).respond(json=load_fixture("reports/tabular_run_no_details.json"))
    sf = h.client()
    result = h.run(sf.reports.run(REPORT, TAG, include_details=False))
    request = route.calls[0].request
    assert json.loads(request.content) == {"reportMetadata": {"reportFilters": TAG}}
    assert request.headers["content-type"] == "application/json"
    assert request.url.params["includeDetails"] == "false"
    assert result.raw["reportMetadata"]["reportFilters"][0]["value"].startswith("esf-sandbox-")


def test_run_is_resent_after_a_5xx(h: Harness, mock: respx.MockRouter) -> None:
    """A run reads data, so it is resent after a 5xx, even as a ``POST`` with filters."""
    route = mock.post(RUN_URL).mock(
        side_effect=[
            httpx.Response(503, json=[{"errorCode": "SERVER_UNAVAILABLE", "message": "busy"}]),
            httpx.Response(200, json=load_fixture("reports/summary_run.json")),
        ]
    )
    sf = h.client()
    h.run(sf.reports.run(REPORT, TAG))
    assert route.call_count == 2


def test_run_async_is_never_resent(h: Harness, mock: respx.MockRouter) -> None:
    """Starting a run creates an instance, so a 5xx after it reached Salesforce isn't resent."""
    route = mock.post(INSTANCES_URL).respond(
        503, json=[{"errorCode": "SERVER_UNAVAILABLE", "message": "busy"}]
    )
    sf = h.client()
    with pytest.raises(esf.ServerError):
        h.run(sf.reports.run_async(REPORT))
    assert route.call_count == 1


def test_run_async_returns_an_instance(h: Harness, mock: respx.MockRouter, rec: Recorder) -> None:
    route = mock.post(INSTANCES_URL).respond(json=load_fixture("reports/instance_new.json"))
    sf = h.client(recorder=rec)
    inst = h.run(sf.reports.run_async(REPORT, TAG))
    assert inst == esf.ReportInstance(
        id=INSTANCE,
        report_id=REPORT,
        status="New",
        request_date=load_fixture("reports/instance_new.json")["requestDate"],
        completion_date=None,
        url=f"/services/data/v68.0/analytics/reports/{REPORT}/instances/{INSTANCE}",
    )
    assert json.loads(route.calls[0].request.content) == {"reportMetadata": {"reportFilters": TAG}}
    assert route.calls[0].request.url.params["includeDetails"] == "true"
    (event,) = rec.requests
    assert event["operation"] == "reports.run_async"


def test_run_async_without_filters_sends_no_body(h: Harness, mock: respx.MockRouter) -> None:
    route = mock.post(INSTANCES_URL).respond(json=load_fixture("reports/instance_new.json"))
    sf = h.client()
    h.run(sf.reports.run_async(REPORT, include_details=False))
    assert route.calls[0].request.content == b""
    assert route.calls[0].request.url.params["includeDetails"] == "false"


def test_instance_polls_until_success(h: Harness, mock: respx.MockRouter, rec: Recorder) -> None:
    route = mock.get(INSTANCE_URL).mock(
        side_effect=[
            httpx.Response(200, json=load_fixture("reports/instance_running.json")),
            httpx.Response(200, json=load_fixture("reports/instance_running.json")),
            httpx.Response(200, json=load_fixture("reports/instance_success.json")),
        ]
    )
    sf = h.client(recorder=rec)
    result = h.run(sf.reports.instance(REPORT, INSTANCE, poll=esf.Backoff(start=3, max=3)))
    assert route.call_count == 3
    assert sf.sleeps == [3, 3]
    assert len(result.rows()) == 5
    assert result.report_id == REPORT
    assert result.raw["attributes"]["type"] == "ReportInstance"
    assert [e["operation"] for e in rec.requests] == ["reports.instance"] * 3
    assert rec.requests[0]["path_template"] == "/analytics/reports/:id/instances/:id"


def test_instance_error_raises_report_run_failed(h: Harness, mock: respx.MockRouter) -> None:
    mock.get(INSTANCE_URL).respond(json=load_fixture("reports/instance_error.json"))
    sf = h.client()
    with pytest.raises(esf.ReportRunFailed) as ei:
        h.run(sf.reports.instance(REPORT, INSTANCE))
    assert ei.value.report_id == REPORT
    assert ei.value.instance_id == INSTANCE
    assert ei.value.operation == "reports.instance"
    assert ei.value.docs_url is not None
    assert ei.value.docs_url.endswith("#reportrunfailed")


def test_instance_times_out(h: Harness, mock: respx.MockRouter) -> None:
    route = mock.get(INSTANCE_URL).respond(json=load_fixture("reports/instance_running.json"))
    sf = h.client()
    with pytest.raises(esf.ReportTimeout) as ei:
        h.run(sf.reports.instance(REPORT, INSTANCE, timeout=5, poll=esf.Backoff(start=2, max=2)))
    assert sf.sleeps == [2, 2, 1]
    assert route.call_count == 4
    assert ei.value.run_status == "Running"
    assert ei.value.instance_id == INSTANCE
    assert ei.value.docs_url is not None
    assert ei.value.docs_url.endswith("#reporttimeout")


def test_instance_unknown_status_is_not_polled(h: Harness, mock: respx.MockRouter) -> None:
    body: Any = load_fixture("reports/instance_running.json")
    body["attributes"]["status"] = "Queued"
    route = mock.get(INSTANCE_URL).respond(json=body)
    sf = h.client()
    with pytest.raises(esf.UnexpectedResponse, match="Queued"):
        h.run(sf.reports.instance(REPORT, INSTANCE))
    assert route.call_count == 1


def test_instance_default_poll_is_backoff(h: Harness, mock: respx.MockRouter) -> None:
    """Without ``poll`` the waits follow ``esf.Backoff()``: 2 s, doubling up to 30 s."""
    running = httpx.Response(200, json=load_fixture("reports/instance_running.json"))
    mock.get(INSTANCE_URL).mock(
        side_effect=[running] * 6 + [httpx.Response(200, json=load_fixture("reports/instance_success.json"))]
    )
    sf = h.client()
    h.run(sf.reports.instance(REPORT, INSTANCE))
    assert sf.sleeps == [2, 4, 8, 16, 30, 30]


def test_instance_default_timeout_is_an_hour(h: Harness, mock: respx.MockRouter) -> None:
    mock.get(INSTANCE_URL).respond(json=load_fixture("reports/instance_running.json"))
    sf = h.client()
    with pytest.raises(esf.ReportTimeout, match="after 3600 s"):
        h.run(sf.reports.instance(REPORT, INSTANCE, poll=esf.Backoff(start=600, max=600)))
    assert sum(sf.sleeps) == 3600


@pytest.mark.parametrize("timeout", [0, -1])
def test_instance_rejects_bad_timeout(h: Harness, timeout: float) -> None:
    sf = h.client()
    with pytest.raises(ValueError, match="timeout"):
        h.run(sf.reports.instance(REPORT, INSTANCE, timeout=timeout))


def test_bad_ids_are_rejected(h: Harness) -> None:
    sf = h.client()
    with pytest.raises(ValueError, match="invalid Salesforce id"):
        h.run(sf.reports.run("../limits"))
    with pytest.raises(ValueError, match="invalid Salesforce id"):
        h.run(sf.reports.dashboards.get("nope"))


def test_unexpected_body(h: Harness, mock: respx.MockRouter) -> None:
    mock.get(RUN_URL).respond(json=[1, 2])
    sf = h.client()
    with pytest.raises(esf.UnexpectedResponse, match="report run"):
        h.run(sf.reports.run(REPORT))


def test_not_found_report(h: Harness, mock: respx.MockRouter) -> None:
    name = "reports/error_not_found.json"
    mock.get(RUN_URL).respond(fixture_status(name), json=load_fixture(name))
    sf = h.client()
    with pytest.raises(esf.NotFound) as ei:
        h.run(sf.reports.run(REPORT))
    assert (ei.value.status, ei.value.error_code) == (404, "NOT_FOUND")


@pytest.mark.parametrize(
    "name", ["reports/error_bad_filter_column.json", "reports/error_bad_filter_operator.json"]
)
def test_bad_filter(h: Harness, mock: respx.MockRouter, name: str) -> None:
    mock.post(RUN_URL).respond(fixture_status(name), json=load_fixture(name))
    sf = h.client()
    with pytest.raises(esf.RequestError) as ei:
        h.run(sf.reports.run(REPORT, TAG))
    assert (ei.value.status, ei.value.error_code) == (400, "BAD_REQUEST")


def test_unknown_instance(h: Harness, mock: respx.MockRouter) -> None:
    name = "reports/error_instance_not_found.json"
    route = mock.get(INSTANCE_URL).respond(fixture_status(name), json=load_fixture(name))
    sf = h.client()
    with pytest.raises(esf.NotFound):
        h.run(sf.reports.instance(REPORT, INSTANCE))
    assert route.call_count == 1


def test_unknown_dashboard(h: Harness, mock: respx.MockRouter) -> None:
    """An unknown dashboard id is 400 ``INVALID_CROSS_REFERENCE_KEY``, not 404."""
    name = "reports/error_dashboard_not_found.json"
    mock.get(DASHBOARD_URL).respond(fixture_status(name), json=load_fixture(name))
    sf = h.client()
    with pytest.raises(esf.InvalidReference) as ei:
        h.run(sf.reports.dashboards.get(DASHBOARD))
    assert (ei.value.status, ei.value.error_code) == (400, "INVALID_CROSS_REFERENCE_KEY")


def test_dashboard_get(h: Harness, mock: respx.MockRouter, rec: Recorder) -> None:
    mock.get(DASHBOARD_URL).respond(json=load_fixture("reports/dashboard.json"))
    sf = h.client(recorder=rec)
    body = h.run(sf.reports.dashboards.get(DASHBOARD))
    (component,) = body["componentData"]
    assert component["status"]["refreshStatus"] == "IDLE"
    assert component["status"]["componentDataStatus"] == "DATA"
    assert body["attributes"]["dashboardName"] == "ESF Sandbox Leads Dashboard"
    result = esf.ReportResult(component["reportResult"])
    assert result.format == "SUMMARY"
    summary = result.summary_rows()
    assert summary[-1].keys() == {"RowCount"}
    assert all(set(s) <= {"LEAD_SOURCE", "RowCount"} for s in summary)  # one grouping level
    (event,) = rec.requests
    assert event["operation"] == "dashboards.get"
    assert event["path_template"] == "/analytics/dashboards/:id"


def test_dashboard_refresh_is_a_put_never_resent(h: Harness, mock: respx.MockRouter, rec: Recorder) -> None:
    name = "reports/dashboard_refresh.json"
    route = mock.put(DASHBOARD_URL).respond(fixture_status(name), json=load_fixture(name))
    sf = h.client(recorder=rec)
    body = h.run(sf.reports.dashboards.refresh(DASHBOARD))
    assert fixture_status(name) == 201
    assert body == {"statusUrl": f"/services/data/v68.0/analytics/dashboards/{DASHBOARD}/status"}
    assert route.calls[0].request.content == b""
    (event,) = rec.requests
    assert event["operation"] == "dashboards.refresh"

    route.respond(503, json=[{"errorCode": "SERVER_UNAVAILABLE", "message": "busy"}])
    with pytest.raises(esf.ServerError):
        h.run(sf.reports.dashboards.refresh(DASHBOARD))
    assert route.call_count == 2  # one call each; the failed refresh wasn't resent


def test_dashboard_refresh_too_soon(h: Harness, mock: respx.MockRouter) -> None:
    name = "reports/error_dashboard_refresh_too_soon.json"
    route = mock.put(DASHBOARD_URL).respond(fixture_status(name), json=load_fixture(name))
    sf = h.client()
    with pytest.raises(esf.Forbidden) as ei:
        h.run(sf.reports.dashboards.refresh(DASHBOARD))
    assert (ei.value.status, ei.value.error_code) == (403, "FORBIDDEN")
    assert "once in a minute" in str(ei.value)
    assert route.call_count == 1
