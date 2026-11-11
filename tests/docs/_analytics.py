"""Reports and dashboards of the synthetic docs org (``analytics/...``; all **synthetic**).

Every report answers with the summary report fixture (``tests/fixtures/recorded/reports``):
opportunities grouped by ``STAGE_NAME``. ``GET analytics/reports`` lists the recently viewed
reports of ``recent.json``. An asynchronous run finishes at once. Every
dashboard answers with the dashboard fixture.
"""

from __future__ import annotations

import itertools
from typing import Any

from ..fixtures import load_fixture

_instances: dict[str, str] = {}
_seq = itertools.count(1)


def _run(report_id: str, include_details: bool) -> dict[str, Any]:
    body = load_fixture("reports/summary_run.json")
    body["attributes"]["reportId"] = report_id
    body["reportMetadata"]["id"] = report_id
    if not include_details:
        for entry in body["factMap"].values():
            entry.pop("rows", None)
        body["hasDetailRows"] = False
    return body


def analytics(method: str, parts: list[str], params: dict[str, str], version: str) -> tuple[Any, int] | None:
    """``(body, status)`` for ``analytics/<parts>``, or ``None`` for an unknown resource."""
    details = params.get("includeDetails", "false") == "true"
    if parts == ["reports"] and method == "GET":
        return load_fixture("reports/recent.json"), 200
    if parts[:1] == ["reports"] and len(parts) >= 2:
        rid = parts[1]
        rest = parts[2:]
        if not rest and method in ("GET", "POST"):
            return _run(rid, details), 200
        if rest == ["describe"] and method == "GET":
            body = _run(rid, False)
            return {k: body[k] for k in ("reportMetadata", "reportExtendedMetadata")}, 200
        if rest == ["instances"] and method == "POST":
            iid = f"0LG5g{next(_seq):010d}AAA"
            _instances[iid] = "true" if details else "false"
            url = f"/services/data/v{version}/analytics/reports/{rid}/instances/{iid}"
            body = {"id": iid, "status": "New", "requestDate": "2026-11-11T09:00:00Z", "url": url}
            return {**body, "completionDate": None, "hasDetailRows": details}, 200
        if len(rest) == 2 and rest[0] == "instances" and rest[1] in _instances and method == "GET":
            body = _run(rid, _instances[rest[1]] == "true")
            body["attributes"] = {**body["attributes"], "id": rest[1], "status": "Success"}
            return body, 200
    if parts[:1] == ["dashboards"] and len(parts) == 2:
        if method == "GET":
            return load_fixture("reports/dashboard.json"), 200
        if method == "PUT":
            return {
                "id": parts[1],
                "statusUrl": f"/services/data/v{version}/analytics/dashboards/{parts[1]}/status",
            }, 201
    return None
