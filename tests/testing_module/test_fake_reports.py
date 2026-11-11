"""``FakeSalesforce`` support for ``sf.reports`` and ``sf.reports.dashboards`` (§9.14, §14).

The report bodies are **synthetic** (``tests/fixtures/recorded/reports``).
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

import even_simpler_salesforce as esf
from even_simpler_salesforce.testing import AsyncFakeSalesforce, FakeSalesforce, ReportScript, UnscriptedCall

from ..fixtures import load_fixture

REPORT = "00Oxx0000000001AAA"
DASHBOARD = "01Zxx0000000001AAA"
STAGE = [{"column": "STAGE_NAME", "operator": "equals", "value": "Prospecting"}]


def test_run_and_describe() -> None:
    fake = FakeSalesforce()
    script = fake.on_report(REPORT, load_fixture("reports/summary_run.json"))
    assert isinstance(script, ReportScript)
    result = fake.reports.run(REPORT, STAGE)
    assert len(result.rows()) == 3
    assert result.has_detail_rows is True
    summary = fake.reports.run(REPORT, include_details=False)
    assert summary.rows() == []
    assert summary.has_detail_rows is False
    assert len(summary.summary_rows()) == 3
    assert script.runs == 2
    described = fake.reports.describe(REPORT)
    assert described["reportMetadata"]["reportFormat"] == "SUMMARY"
    assert set(described) == {"reportMetadata", "reportExtendedMetadata"}
    assert script.runs == 2
    first, second = fake.calls_for("reports.run")
    assert first.args == {"report_id": REPORT, "filters": STAGE, "include_details": True}
    assert second.args["filters"] is None


def test_callable_result_and_explicit_describe() -> None:
    fake = FakeSalesforce()
    seen: list[Any] = []

    def answer(filters: Any, include_details: bool) -> esf.ReportResult:
        seen.append((filters, include_details))
        return esf.ReportResult(load_fixture("reports/tabular_run.json"))

    fake.on_report(REPORT, answer, describe={"reportMetadata": {"id": REPORT}})
    assert len(fake.reports.run(REPORT, STAGE, include_details=False).rows()) == 3  # callable decides
    assert seen == [(STAGE, False)]
    assert fake.reports.describe(REPORT) == {"reportMetadata": {"id": REPORT}}


def test_scripted_error_and_fail_next() -> None:
    fake = FakeSalesforce()
    fake.on_report(REPORT, esf.Forbidden("no access"))
    with pytest.raises(esf.Forbidden):
        fake.reports.run(REPORT)
    fake.on_report(REPORT, load_fixture("reports/tabular_run.json"))
    fake.fail_next("reports.run", esf.NotFound("gone"))
    with pytest.raises(esf.NotFound):
        fake.reports.run(REPORT)
    assert fake.reports.run(REPORT).format == "TABULAR"


def test_run_async_and_instance() -> None:
    fake = FakeSalesforce()
    fake.on_report(REPORT, load_fixture("reports/summary_run.json"))
    inst = fake.reports.run_async(REPORT, STAGE)
    assert inst.status == "New"
    assert inst.report_id == REPORT
    assert inst.id.startswith("0LG")
    assert inst.url is not None
    assert inst.url.endswith(f"/analytics/reports/{REPORT}/instances/{inst.id}")
    result = fake.reports.instance(REPORT, inst.id)
    assert result.raw["attributes"]["status"] == "Success"
    assert result.raw["attributes"]["id"] == inst.id
    assert len(result.rows()) == 3
    with pytest.raises(esf.NotFound):
        fake.reports.instance(REPORT, "0LGxx0000000009AAA")
    with pytest.raises(esf.NotFound):
        fake.reports.instance("00Oxx0000000002AAA", inst.id)
    with pytest.raises(ValueError, match="timeout"):
        fake.reports.instance(REPORT, inst.id, timeout=0)
    with pytest.raises(ValueError, match="poll_interval"):
        fake.reports.instance(REPORT, inst.id, poll_interval=0)


def test_instance_without_attributes() -> None:
    fake = FakeSalesforce()
    body = load_fixture("reports/tabular_run.json")
    del body["attributes"]
    fake.on_report(REPORT, body)
    inst = fake.reports.run_async(REPORT)
    assert fake.reports.instance(REPORT, inst.id).report_id == REPORT


def test_unscripted_report_and_dashboard() -> None:
    fake = FakeSalesforce()
    with pytest.raises(UnscriptedCall, match="on_report"):
        fake.reports.run(REPORT)
    with pytest.raises(UnscriptedCall, match="on_dashboard"):
        fake.reports.dashboards.get(DASHBOARD)
    with pytest.raises(ValueError, match="invalid Salesforce id"):
        fake.on_report("bad", {})
    with pytest.raises(ValueError, match="invalid Salesforce id"):
        fake.on_dashboard("bad", {})


def test_dashboards() -> None:
    fake = FakeSalesforce()
    fake.on_dashboard(DASHBOARD, load_fixture("reports/dashboard.json"))
    body = fake.reports.dashboards.get(DASHBOARD)
    body["componentData"].clear()  # a copy
    assert fake.reports.dashboards.get(DASHBOARD)["componentData"]
    refreshed = fake.reports.dashboards.refresh(DASHBOARD)
    assert refreshed == {
        "id": DASHBOARD,
        "statusUrl": f"/services/data/v68.0/analytics/dashboards/{DASHBOARD}/status",
    }
    assert [c.operation for c in fake.calls] == ["dashboards.get", "dashboards.get", "dashboards.refresh"]


def test_async_fake() -> None:
    fake = AsyncFakeSalesforce()
    fake.on_report(REPORT, load_fixture("reports/matrix_run.json"))
    fake.on_dashboard(DASHBOARD, load_fixture("reports/dashboard.json"))

    async def go() -> None:
        assert (await fake.reports.describe(REPORT))["reportMetadata"]["reportFormat"] == "MATRIX"
        assert len((await fake.reports.run(REPORT)).rows()) == 3
        inst = await fake.reports.run_async(REPORT, include_details=False)
        assert (await fake.reports.instance(REPORT, inst.id)).rows() == []
        assert (await fake.reports.dashboards.get(DASHBOARD))["statusUrl"]
        assert (await fake.reports.dashboards.refresh(DASHBOARD))["id"] == DASHBOARD

    asyncio.run(go())


def test_views_share_reports() -> None:
    fake = FakeSalesforce()
    fake.on_report(REPORT, load_fixture("reports/tabular_run.json"))
    child = fake.with_auth(esf.StaticToken("y", "https://acme.my.salesforce.com"))
    assert child.reports.run(REPORT).format == "TABULAR"
