"""Reports and dashboards: ``sf.reports`` and ``sf.reports.dashboards`` (§9.14), on both clients.

Every response here is **synthetic** (``tests/fixtures/recorded/reports/*.json``), hand-written
from the Salesforce Reports and Dashboards REST API Developer Guide because no org recording
exists yet:

- Execute Sync (``GET`` or ``POST analytics/reports/{id}``; ``includeDetails``; filters in
  ``reportMetadata`` of the ``POST`` body):
  https://developer.salesforce.com/docs/atlas.en-us.api_analytics.meta/api_analytics/sforce_analytics_rest_api_getreportrundata.htm
- Run Reports Synchronously or Asynchronously (the 2,000 detail-row limit of a synchronous run
  and why long reports run asynchronously):
  https://developer.salesforce.com/docs/atlas.en-us.api_analytics.meta/api_analytics/sforce_analytics_rest_api_get_reportdata.htm
- Execute Async and Instances List (``POST .../instances`` returns an instance ``id``; status
  ``New``, ``Running``, ``Success`` or ``Error``; ``url`` is null when the run failed):
  https://developer.salesforce.com/docs/atlas.en-us.api_analytics.meta/api_analytics/sforce_analytics_rest_api_instances_summaryasync.htm
  https://developer.salesforce.com/docs/atlas.en-us.api_analytics.meta/api_analytics/sforce_analytics_rest_api_instances_resource.htm
- Decode the Fact Map (``T!T``, ``0!T``, ``0_0!0_0`` keys):
  https://developer.salesforce.com/docs/atlas.en-us.api_analytics.meta/api_analytics/sforce_analytics_rest_api_factmap_example.htm
- Get Dashboard Results and Refresh a Dashboard (``GET`` and ``PUT analytics/dashboards/{id}``;
  ``statusUrl``; ``refreshStatus`` ``IDLE`` or ``RUNNING``):
  https://developer.salesforce.com/docs/atlas.en-us.api_analytics.meta/api_analytics/analytics_api_dashboard_results_resource.htm
  https://developer.salesforce.com/docs/atlas.en-us.api_analytics.meta/api_analytics/analytics_api_dashboard_refresh.htm

developer.salesforce.com refuses automated fetches, so these pages were read through search
excerpts on 2026-11-11. The exact bodies are open in big-bang.md §21 (V65-V68).
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
import respx

import even_simpler_salesforce as esf

from ..conftest import DATA, Harness, Recorder
from ..fixtures import load_fixture

REPORT = "00Oxx0000000001AAA"
INSTANCE = "0LGxx0000000001AAA"
DASHBOARD = "01Zxx0000000001AAA"
RUN_URL = f"{DATA}/analytics/reports/{REPORT}"
INSTANCES_URL = f"{RUN_URL}/instances"
INSTANCE_URL = f"{INSTANCES_URL}/{INSTANCE}"
DASHBOARD_URL = f"{DATA}/analytics/dashboards/{DASHBOARD}"
STAGE = [{"column": "STAGE_NAME", "operator": "equals", "value": "Prospecting"}]


def test_describe(h: Harness, mock: respx.MockRouter, rec: Recorder) -> None:
    mock.get(f"{RUN_URL}/describe").respond(json=load_fixture("reports/describe.json"))
    sf = h.client(recorder=rec)
    body = h.run(sf.reports.describe(REPORT))
    assert body["reportMetadata"]["reportFormat"] == "SUMMARY"
    (event,) = rec.requests
    assert event["operation"] == "reports.describe"
    assert event["path_template"] == "/analytics/reports/:id/describe"


def test_run_without_filters_is_a_get(h: Harness, mock: respx.MockRouter, rec: Recorder) -> None:
    """Synthetic: Execute Sync, ``GET analytics/reports/{id}?includeDetails=true``."""
    route = mock.get(RUN_URL).respond(json=load_fixture("reports/tabular_run.json"))
    sf = h.client(recorder=rec)
    result = h.run(sf.reports.run(REPORT))
    assert isinstance(result, esf.ReportResult)
    assert route.calls[0].request.url.params["includeDetails"] == "true"
    assert result.report_id == REPORT
    assert result.name == "Open Opportunities"
    assert result.format == "TABULAR"
    assert result.all_data is True
    assert result.has_detail_rows is True
    assert result.rows()[0] == {
        "ACCOUNT_NAME": "001xx0000000001AAA",
        "OPPORTUNITY_NAME": "Acme - 200 units",
        "AMOUNT": {"amount": 1000.0, "currency": None},
    }
    assert result.summary_rows() == [{"s!AMOUNT": {"amount": 4000.0, "currency": None}, "RowCount": 3}]
    (event,) = rec.requests
    assert event["operation"] == "reports.run"
    assert event["method"] == "GET"
    assert event["path_template"] == "/analytics/reports/:id"


def _money(amount: float) -> dict[str, Any]:
    return {"amount": amount, "currency": None}


def test_rows_of_a_summary_report_with_two_groupings(h: Harness, mock: respx.MockRouter) -> None:
    """Synthetic fixture ``reports/summary_two_level_run.json``: grouped by stage, then type.

    Detail rows sit in the deepest groupings (``0_0!T``); ``0!T`` holds the stage subtotal and
    ``T!T`` the grand total (*Decode the Fact Map*).
    """
    mock.get(RUN_URL).respond(json=load_fixture("reports/summary_two_level_run.json"))
    sf = h.client()
    result = h.run(sf.reports.run(REPORT))
    assert result.format == "SUMMARY"
    assert result.rows() == [
        {
            "STAGE_NAME": "Prospecting",
            "TYPE": "New Business",
            "OPPORTUNITY_NAME": "Acme - 200 units",
            "AMOUNT": _money(1000.0),
        },
        {
            "STAGE_NAME": "Prospecting",
            "TYPE": "New Business",
            "OPPORTUNITY_NAME": "Hooli expansion",
            "AMOUNT": _money(300.0),
        },
        {
            "STAGE_NAME": "Prospecting",
            "TYPE": "Existing Business",
            "OPPORTUNITY_NAME": "Initech pilot",
            "AMOUNT": _money(500.0),
        },
        {
            "STAGE_NAME": "Closed Won",
            "TYPE": "Existing Business",
            "OPPORTUNITY_NAME": "Globex renewal",
            "AMOUNT": _money(2500.0),
        },
    ]
    assert result.summary_rows() == [
        {"STAGE_NAME": "Prospecting", "s!AMOUNT": _money(1800.0), "RowCount": 3},
        {"STAGE_NAME": "Prospecting", "TYPE": "New Business", "s!AMOUNT": _money(1300.0), "RowCount": 2},
        {"STAGE_NAME": "Prospecting", "TYPE": "Existing Business", "s!AMOUNT": _money(500.0), "RowCount": 1},
        {"STAGE_NAME": "Closed Won", "s!AMOUNT": _money(2500.0), "RowCount": 1},
        {"STAGE_NAME": "Closed Won", "TYPE": "Existing Business", "s!AMOUNT": _money(2500.0), "RowCount": 1},
        {"s!AMOUNT": _money(4300.0), "RowCount": 4},
    ]


def test_rows_of_a_matrix_report_with_row_and_column_groupings(h: Harness, mock: respx.MockRouter) -> None:
    """Synthetic fixture ``reports/matrix_two_level_run.json``: rows grouped by stage and type,
    columns by close month. Empty cells (``0_1!0``, ``1_0!1``, ``1!1``) are left out of the
    fact map, so the helpers skip them.
    """
    mock.get(RUN_URL).respond(json=load_fixture("reports/matrix_two_level_run.json"))
    sf = h.client()
    result = h.run(sf.reports.run(REPORT))
    assert result.format == "MATRIX"
    assert [
        (r["STAGE_NAME"], r["TYPE"], r["CLOSE_DATE"], r["OPPORTUNITY_NAME"], r["AMOUNT"])
        for r in result.rows()
    ] == [
        ("Prospecting", "New Business", "2026-10-01", "Acme - 200 units", _money(1000.0)),
        ("Prospecting", "New Business", "2026-11-01", "Hooli expansion", _money(300.0)),
        ("Prospecting", "Existing Business", "2026-11-01", "Initech pilot", _money(500.0)),
        ("Closed Won", "Existing Business", "2026-10-01", "Globex renewal", _money(2500.0)),
    ]
    assert list(result.rows()[0]) == ["STAGE_NAME", "TYPE", "CLOSE_DATE", "OPPORTUNITY_NAME", "AMOUNT"]
    oct_, nov = "2026-10-01", "2026-11-01"
    summary = [
        (s.get("STAGE_NAME"), s.get("TYPE"), s.get("CLOSE_DATE"), s["s!AMOUNT"]["amount"], s["RowCount"])
        for s in result.summary_rows()
    ]
    assert summary == [
        ("Prospecting", None, oct_, 1000.0, 1),
        ("Prospecting", None, nov, 800.0, 2),
        ("Prospecting", None, None, 1800.0, 3),
        ("Prospecting", "New Business", oct_, 1000.0, 1),
        ("Prospecting", "New Business", nov, 300.0, 1),
        ("Prospecting", "New Business", None, 1300.0, 2),
        ("Prospecting", "Existing Business", nov, 500.0, 1),
        ("Prospecting", "Existing Business", None, 500.0, 1),
        ("Closed Won", None, oct_, 2500.0, 1),
        ("Closed Won", None, None, 2500.0, 1),
        ("Closed Won", "Existing Business", oct_, 2500.0, 1),
        ("Closed Won", "Existing Business", None, 2500.0, 1),
        (None, None, oct_, 3500.0, 2),
        (None, None, nov, 800.0, 2),
        (None, None, None, 4300.0, 4),
    ]
    # a subtotal or total has no keys for the groupings it sums over
    assert set(result.summary_rows()[-1]) == {"s!AMOUNT", "RowCount"}
    assert set(result.summary_rows()[2]) == {"STAGE_NAME", "s!AMOUNT", "RowCount"}


def test_recent_lists_recently_viewed_reports(h: Harness, mock: respx.MockRouter, rec: Recorder) -> None:
    """Synthetic fixture ``reports/recent.json``: *Report List*, ``GET analytics/reports``
    (up to 200 recently viewed reports):
    https://developer.salesforce.com/docs/atlas.en-us.api_analytics.meta/api_analytics/sforce_analytics_rest_api_recentreportslist.htm
    """
    route = mock.get(f"{DATA}/analytics/reports").respond(json=load_fixture("reports/recent.json"))
    sf = h.client(recorder=rec)
    reports = h.run(sf.reports.recent())
    assert [(r["id"], r["name"]) for r in reports] == [
        (REPORT, "Open Opportunities"),
        ("00Oxx0000000002AAA", "Pipeline by Stage and Type"),
    ]
    assert reports[0]["instancesUrl"].endswith(f"/analytics/reports/{REPORT}/instances")
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
    """Synthetic: Execute Sync with ``POST`` and ``reportMetadata.reportFilters`` in the body."""
    route = mock.post(RUN_URL).respond(json=load_fixture("reports/summary_run.json"))
    sf = h.client()
    result = h.run(sf.reports.run(REPORT, STAGE, include_details=False))
    request = route.calls[0].request
    assert json.loads(request.content) == {"reportMetadata": {"reportFilters": STAGE}}
    assert request.headers["content-type"] == "application/json"
    assert request.url.params["includeDetails"] == "false"
    assert [r["STAGE_NAME"] for r in result.summary_rows()[:2]] == ["Prospecting", "Closed Won"]


def test_run_is_resent_after_a_5xx(h: Harness, mock: respx.MockRouter) -> None:
    """A run reads data, so it is resent after a 5xx, even as a ``POST`` with filters."""
    route = mock.post(RUN_URL).mock(
        side_effect=[
            httpx.Response(503, json=[{"errorCode": "SERVER_UNAVAILABLE", "message": "busy"}]),
            httpx.Response(200, json=load_fixture("reports/summary_run.json")),
        ]
    )
    sf = h.client()
    h.run(sf.reports.run(REPORT, STAGE))
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
    inst = h.run(sf.reports.run_async(REPORT, STAGE))
    assert inst == esf.ReportInstance(
        id=INSTANCE,
        report_id=REPORT,
        status="New",
        request_date="2026-11-11T09:00:00Z",
        completion_date=None,
        url=f"/services/data/v68.0/analytics/reports/{REPORT}/instances/{INSTANCE}",
    )
    assert json.loads(route.calls[0].request.content) == {"reportMetadata": {"reportFilters": STAGE}}
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
    assert len(result.rows()) == 3
    assert result.report_id == REPORT
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
    mock.get(RUN_URL).respond(
        404, json=[{"errorCode": "NOT_FOUND", "message": "The requested resource does not exist"}]
    )
    sf = h.client()
    with pytest.raises(esf.NotFound):
        h.run(sf.reports.run(REPORT))


def test_dashboard_get(h: Harness, mock: respx.MockRouter, rec: Recorder) -> None:
    mock.get(DASHBOARD_URL).respond(json=load_fixture("reports/dashboard.json"))
    sf = h.client(recorder=rec)
    body = h.run(sf.reports.dashboards.get(DASHBOARD))
    (component,) = body["componentData"]
    assert component["status"]["refreshStatus"] == "IDLE"
    summary = esf.ReportResult(component["reportResult"]).summary_rows()
    assert summary[0] == {
        "STAGE_NAME": "Prospecting",
        "s!AMOUNT": {"amount": 1500.0, "currency": None},
        "RowCount": 2,
    }
    (event,) = rec.requests
    assert event["operation"] == "dashboards.get"
    assert event["path_template"] == "/analytics/dashboards/:id"


def test_dashboard_refresh_is_a_put_never_resent(h: Harness, mock: respx.MockRouter, rec: Recorder) -> None:
    route = mock.put(DASHBOARD_URL).respond(json=load_fixture("reports/dashboard_refresh.json"))
    sf = h.client(recorder=rec)
    body = h.run(sf.reports.dashboards.refresh(DASHBOARD))
    assert body["statusUrl"].endswith(f"/analytics/dashboards/{DASHBOARD}/status")
    assert route.calls[0].request.content == b""
    (event,) = rec.requests
    assert event["operation"] == "dashboards.refresh"

    route.respond(503, json=[{"errorCode": "SERVER_UNAVAILABLE", "message": "busy"}])
    with pytest.raises(esf.ServerError):
        h.run(sf.reports.dashboards.refresh(DASHBOARD))
    assert route.call_count == 2  # one call each; the failed refresh wasn't resent
