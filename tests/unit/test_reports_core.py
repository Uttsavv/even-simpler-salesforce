"""Pure helpers of the Reports and Dashboards REST API (``core/reports.py``, §9.14).

Fact map keys follow *Decode the Fact Map* (Reports and Dashboards REST API Developer Guide):
``T!T`` is the grand total (and holds the rows of a tabular report), ``0!T`` the first row
grouping of a summary report, and ``0_0!0_0`` a matrix cell of two-level groupings.
https://developer.salesforce.com/docs/atlas.en-us.api_analytics.meta/api_analytics/sforce_analytics_rest_api_factmap_example.htm

Instance statuses (``New``, ``Running``, ``Success``, ``Error``) are from *Instances List*:
https://developer.salesforce.com/docs/atlas.en-us.api_analytics.meta/api_analytics/sforce_analytics_rest_api_instances_resource.htm

The report bodies below are **synthetic**.
"""

from __future__ import annotations

import pickle
from typing import Any

import pytest

import even_simpler_salesforce as esf
from even_simpler_salesforce.core import reports as r

from ..fixtures import load_fixture

REPORT = "00Oxx0000000001AAA"
INSTANCE = "0LGxx0000000001AAA"


def test_paths() -> None:
    assert r.recent_reports_path() == "analytics/reports"
    assert r.report_path(REPORT) == f"analytics/reports/{REPORT}"
    assert r.report_describe_path(REPORT) == f"analytics/reports/{REPORT}/describe"
    assert r.instances_path(REPORT) == f"analytics/reports/{REPORT}/instances"
    assert r.instance_path(REPORT, INSTANCE) == f"analytics/reports/{REPORT}/instances/{INSTANCE}"
    assert r.dashboard_path("01Zxx0000000001AAA") == "analytics/dashboards/01Zxx0000000001AAA"
    with pytest.raises(ValueError):
        r.instance_path(REPORT, "x/y")


def test_run_params_and_body() -> None:
    assert r.run_params(True) == {"includeDetails": "true"}
    assert r.run_params(False) == {"includeDetails": "false"}
    assert r.run_body(None) is None
    flt = {"column": "STAGE_NAME", "operator": "equals", "value": "Won"}
    assert r.run_body([flt]) == {"reportMetadata": {"reportFilters": [flt]}}
    assert r.run_body(()) == {"reportMetadata": {"reportFilters": []}}


@pytest.mark.parametrize("bad", ["STAGE_NAME", {"column": "X"}, 3, b"x"])
def test_run_body_rejects_non_lists(bad: Any) -> None:
    with pytest.raises(TypeError, match="list of filter dicts"):
        r.run_body(bad)


def test_run_body_rejects_non_dict_items() -> None:
    with pytest.raises(TypeError, match="each filter"):
        r.run_body(["STAGE_NAME"])


@pytest.mark.parametrize(
    ("body", "status"),
    [
        ({"status": "New"}, "New"),
        ({"attributes": {"status": "Running"}}, "Running"),
        ({"attributes": {"status": "Success"}}, "Success"),
        ({"status": "Error", "attributes": {"status": "Running"}}, "Error"),
    ],
)
def test_instance_status(body: Any, status: str) -> None:
    assert r.instance_status(body) == status


@pytest.mark.parametrize("body", [{"status": "Queued"}, {}, {"attributes": "x"}, [], None])
def test_instance_status_unknown(body: Any) -> None:
    with pytest.raises(esf.UnexpectedResponse, match="unknown report instance status"):
        r.instance_status(body)


def test_parse_instance() -> None:
    inst = r.parse_instance(load_fixture("reports/instance_new.json"), REPORT)
    assert inst.id == INSTANCE
    assert inst.report_id == REPORT
    assert inst.status == "New"
    assert inst.completion_date is None
    with pytest.raises(esf.UnexpectedResponse, match="no 'id'"):
        r.parse_instance({"status": "New"}, REPORT)


# --- fact maps ---------------------------------------------------------------------------


def money(x: float) -> dict[str, Any]:
    return {"amount": x, "currency": None}


def test_tabular_rows() -> None:
    body = load_fixture("reports/tabular_run.json")
    rows = r.detail_rows(body)
    assert [row["OPPORTUNITY_NAME"] for row in rows] == [
        "Acme - 200 units",
        "Globex renewal",
        "Initech pilot",
    ]
    assert set(rows[0]) == {"ACCOUNT_NAME", "OPPORTUNITY_NAME", "AMOUNT"}
    assert r.summary_rows(body) == [{"s!AMOUNT": money(4000.0), "RowCount": 3}]


def test_summary_rows() -> None:
    body = load_fixture("reports/summary_run.json")
    rows = r.detail_rows(body)
    assert [(row["STAGE_NAME"], row["OPPORTUNITY_NAME"]) for row in rows] == [
        ("Prospecting", "Acme - 200 units"),
        ("Prospecting", "Initech pilot"),
        ("Closed Won", "Globex renewal"),
    ]
    assert r.summary_rows(body) == [
        {"STAGE_NAME": "Prospecting", "s!AMOUNT": money(1500.0), "RowCount": 2},
        {"STAGE_NAME": "Closed Won", "s!AMOUNT": money(2500.0), "RowCount": 1},
        {"s!AMOUNT": money(4000.0), "RowCount": 3},
    ]


def test_matrix_rows_skip_missing_cells() -> None:
    body = load_fixture("reports/matrix_run.json")
    rows = r.detail_rows(body)
    assert [(row["STAGE_NAME"], row["CLOSE_DATE"], row["OPPORTUNITY_NAME"]) for row in rows] == [
        ("Prospecting", "2026-10-01", "Acme - 200 units"),
        ("Prospecting", "2026-11-01", "Initech pilot"),
        ("Closed Won", "2026-10-01", "Globex renewal"),
    ]
    summary = r.summary_rows(body)
    keys = [(s.get("STAGE_NAME"), s.get("CLOSE_DATE"), s["RowCount"]) for s in summary]
    assert keys == [
        ("Prospecting", "2026-10-01", 1),
        ("Prospecting", "2026-11-01", 1),
        ("Prospecting", None, 2),
        ("Closed Won", "2026-10-01", 1),  # 1!1 is absent from the fact map
        ("Closed Won", None, 1),
        (None, "2026-10-01", 2),
        (None, "2026-11-01", 1),
        (None, None, 3),
    ]


def _two_level() -> dict[str, Any]:
    def agg(n: int) -> list[dict[str, Any]]:
        return [{"label": str(n), "value": n}]

    def row(name: str) -> dict[str, Any]:
        return {"dataCells": [{"label": name, "value": name}]}

    return {
        "reportMetadata": {
            "reportFormat": "SUMMARY",
            "detailColumns": ["OPPORTUNITY_NAME"],
            "aggregates": ["RowCount"],
            "groupingsDown": [{"name": "STAGE_NAME"}, {"name": "TYPE"}],
            "groupingsAcross": [],
        },
        "groupingsDown": {
            "groupings": [
                {
                    "key": "0",
                    "value": "Prospecting",
                    "groupings": [
                        {"key": "0_0", "value": "New", "groupings": []},
                        {"key": "0_1", "value": "Renewal", "groupings": []},
                    ],
                }
            ]
        },
        "groupingsAcross": {"groupings": []},
        "factMap": {
            "0_0!T": {"aggregates": agg(1), "rows": [row("A")]},
            "0_1!T": {"aggregates": agg(1), "rows": [row("B")]},
            "0!T": {"aggregates": agg(2)},
            "T!T": {"aggregates": agg(2)},
        },
    }


def test_two_level_summary_has_subtotals() -> None:
    body = _two_level()
    assert r.detail_rows(body) == [
        {"STAGE_NAME": "Prospecting", "TYPE": "New", "OPPORTUNITY_NAME": "A"},
        {"STAGE_NAME": "Prospecting", "TYPE": "Renewal", "OPPORTUNITY_NAME": "B"},
    ]
    assert r.summary_rows(body) == [
        {"STAGE_NAME": "Prospecting", "RowCount": 2},
        {"STAGE_NAME": "Prospecting", "TYPE": "New", "RowCount": 1},
        {"STAGE_NAME": "Prospecting", "TYPE": "Renewal", "RowCount": 1},
        {"RowCount": 2},
    ]


def test_rows_without_details() -> None:
    body = load_fixture("reports/summary_run.json")
    for entry in body["factMap"].values():
        entry.pop("rows", None)
    assert r.detail_rows(body) == []


def test_joined_reports_are_refused() -> None:
    body = load_fixture("reports/tabular_run.json")
    body["reportMetadata"]["reportFormat"] = "MULTI_BLOCK"
    with pytest.raises(ValueError, match="MULTI_BLOCK"):
        r.detail_rows(body)


def _broken(path: str, value: Any) -> dict[str, Any]:
    body: Any = _two_level()
    *parents, last = path.split(".")
    node = body
    for p in parents:
        node = node[int(p)] if isinstance(node, list) else node[p]
    if isinstance(node, list):
        node[int(last)] = value
    else:
        node[last] = value
    return body


@pytest.mark.parametrize(
    ("path", "value", "match"),
    [
        ("factMap", [], "factMap"),
        ("reportMetadata", None, "reportMetadata"),
        ("reportMetadata.detailColumns", "X", "not a list"),
        ("reportMetadata.groupingsDown.0", {"label": "x"}, "malformed reportMetadata.groupingsDown"),
        ("groupingsDown", None, "groupingsDown"),
        ("groupingsDown.groupings.0", "x", "malformed groupingsDown grouping"),
        ("groupingsDown.groupings.0.groupings.0", {"value": 1}, "malformed groupingsDown grouping"),
        ("factMap.0_0!T", "x", "factMap"),
        ("factMap.0_0!T.rows.0", "x", "malformed cell"),
        ("factMap.0_0!T.rows.0.dataCells.0", 5, "malformed cell"),
        ("factMap.0_0!T.rows.0.dataCells", [], "0 cells for 1 detail columns"),
    ],
)
def test_malformed_bodies_in_rows(path: str, value: Any, match: str) -> None:
    with pytest.raises(esf.UnexpectedResponse, match=match):
        r.detail_rows(_broken(path, value))


@pytest.mark.parametrize(
    ("path", "value", "match"),
    [
        ("factMap.0!T.aggregates", [], "0 aggregates, not 1"),
        ("factMap.0!T.aggregates", {}, "not a list"),
        ("factMap.0!T.aggregates.0", "x", "malformed cell"),
        ("reportMetadata.aggregates.0", 3, "malformed reportMetadata.aggregates"),
    ],
)
def test_malformed_bodies_in_summary(path: str, value: Any, match: str) -> None:
    with pytest.raises(esf.UnexpectedResponse, match=match):
        r.summary_rows(_broken(path, value))


def test_not_a_report() -> None:
    with pytest.raises(esf.UnexpectedResponse, match="report result"):
        r.detail_rows([])


# --- models and errors -------------------------------------------------------------------


def test_report_result_properties() -> None:
    result = esf.ReportResult(load_fixture("reports/summary_run.json"))
    assert result.report_id == "00Oxx0000000001AAA"
    assert result.name == "Open Opportunities"
    assert result.format == "SUMMARY"
    assert result.all_data is True
    assert result.has_detail_rows is True
    assert len(result.rows()) == 3
    assert len(result.summary_rows()) == 3


def test_report_result_falls_back_to_metadata() -> None:
    result = esf.ReportResult({"reportMetadata": {"id": REPORT, "name": "Pipeline"}, "allData": False})
    assert result.report_id == REPORT
    assert result.name == "Pipeline"
    assert result.format is None
    assert result.all_data is False
    assert result.has_detail_rows is False
    empty = esf.ReportResult({"attributes": [], "reportMetadata": {"id": 5}})
    assert empty.report_id is None


def test_report_errors_pickle() -> None:
    failed = esf.ReportRunFailed("boom", report_id=REPORT, instance_id=INSTANCE)
    timeout = esf.ReportTimeout("slow", report_id=REPORT, instance_id=INSTANCE, run_status="Running")
    again = pickle.loads(pickle.dumps(failed))
    assert (again.report_id, again.instance_id) == (REPORT, INSTANCE)
    later = pickle.loads(pickle.dumps(timeout))
    assert later.run_status == "Running"
    assert later.docs_url is not None
    assert later.docs_url.endswith("#reporttimeout")
