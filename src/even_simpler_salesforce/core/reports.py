"""Reports and dashboards (§9.14): paths, request bodies, run status and fact maps. No I/O.

Salesforce serves the Reports and Dashboards REST API under
``/services/data/vXX.X/analytics/``. A report run returns a *fact map*: a dict keyed by
``"<row key>!<column key>"``, where each key is a grouping key (``"0"``, ``"0_1"``, ...) or
``"T"`` for a total. Each entry holds the ``aggregates`` of that cell and, when the run
included details, the detail ``rows`` of a leaf cell.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from typing import Any, cast

from ..errors import UnexpectedResponse
from ..models import ReportInstance
from .http import check_record_id

__all__ = [
    "INSTANCE_STATUSES",
    "dashboard_path",
    "detail_rows",
    "instance_path",
    "instance_status",
    "instances_path",
    "parse_instance",
    "recent_reports_path",
    "report_describe_path",
    "report_path",
    "run_body",
    "run_params",
    "summary_rows",
]

INSTANCE_STATUSES = frozenset({"New", "Running", "Success", "Error"})
"""Statuses of an asynchronous report run (``attributes.status`` of an instance)."""

_RAW_LIMIT = 4096
_TOTAL = "T"


def recent_reports_path() -> str:
    """``analytics/reports``: up to 200 reports the user viewed most recently.

    *Report List* (Reports and Dashboards REST API Developer Guide):
    https://developer.salesforce.com/docs/atlas.en-us.api_analytics.meta/api_analytics/sforce_analytics_rest_api_recentreportslist.htm
    """
    return "analytics/reports"


def report_path(report_id: str) -> str:
    """``analytics/reports/{id}``: run a report synchronously."""
    return f"analytics/reports/{check_record_id(report_id)}"


def report_describe_path(report_id: str) -> str:
    """``analytics/reports/{id}/describe``: report metadata."""
    return f"{report_path(report_id)}/describe"


def instances_path(report_id: str) -> str:
    """``analytics/reports/{id}/instances``: start an asynchronous run."""
    return f"{report_path(report_id)}/instances"


def instance_path(report_id: str, instance_id: str) -> str:
    """``analytics/reports/{id}/instances/{instance}``: status and results of one run."""
    return f"{instances_path(report_id)}/{check_record_id(instance_id)}"


def dashboard_path(dashboard_id: str) -> str:
    """``analytics/dashboards/{id}``: results (``GET``) and refresh (``PUT``)."""
    return f"analytics/dashboards/{check_record_id(dashboard_id)}"


def run_params(include_details: bool) -> dict[str, str]:
    """Query parameters of a run: ``includeDetails=true|false``."""
    return {"includeDetails": "true" if include_details else "false"}


def run_body(filters: Sequence[Mapping[str, Any]] | None) -> dict[str, Any] | None:
    """The JSON body of a run with filters, or ``None`` to run the saved report as is.

    Salesforce reads the filters from ``reportMetadata.reportFilters``. They replace the saved
    filters of the report for this run only.

    Args:
        filters: Filter dicts such as ``{"column": "STAGE_NAME", "operator": "equals",
            "value": "Prospecting"}``.

    Raises:
        TypeError: ``filters`` is not a list of mappings.
    """
    if filters is None:
        return None
    if isinstance(filters, (str, bytes, Mapping)) or not isinstance(filters, Sequence):  # pyright: ignore[reportUnnecessaryIsInstance]
        raise TypeError("filters must be a list of filter dicts")
    items: list[dict[str, Any]] = []
    for f in filters:
        if not isinstance(f, Mapping):  # pyright: ignore[reportUnnecessaryIsInstance]
            raise TypeError("each filter must be a dict with 'column', 'operator' and 'value'")
        items.append(dict(f))
    return {"reportMetadata": {"reportFilters": items}}


def instance_status(body: Any) -> str:
    """The status of an asynchronous run: ``New``, ``Running``, ``Success`` or ``Error``.

    Reads ``status`` at the top level (the response that starts a run) or in
    ``attributes`` (the instance results).

    Raises:
        UnexpectedResponse: no status, or a status this library doesn't know. An unknown
            status is never polled, so a new Salesforce status can't cause an endless loop.
    """
    status: Any = None
    if isinstance(body, Mapping):
        b = cast("Mapping[str, Any]", body)
        attrs: Any = b.get("attributes")
        status = b.get("status")
        if status is None and isinstance(attrs, Mapping):
            status = cast("Mapping[str, Any]", attrs).get("status")
    if status not in INSTANCE_STATUSES:
        raise UnexpectedResponse(
            f"unknown report instance status {status!r}", raw=repr(cast(object, body))[:_RAW_LIMIT]
        )
    return cast(str, status)


def parse_instance(body: Any, report_id: str) -> ReportInstance:
    """Parse the response that starts an asynchronous run (``POST .../instances``).

    Raises:
        UnexpectedResponse: no instance ``id``, or an unknown status.
    """
    status = instance_status(body)
    b = cast("Mapping[str, Any]", body)
    rid: Any = b.get("id")
    if not isinstance(rid, str) or not rid:
        raise UnexpectedResponse(
            "report instance response has no 'id'", raw=repr(cast(object, body))[:_RAW_LIMIT]
        )

    def text(key: str) -> str | None:
        value: Any = b.get(key)
        return value if isinstance(value, str) else None

    return ReportInstance(
        id=rid,
        report_id=report_id,
        status=status,
        request_date=text("requestDate"),
        completion_date=text("completionDate"),
        url=text("url"),
    )


# --- fact maps ---------------------------------------------------------------------------


def _mapping(value: Any, what: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise UnexpectedResponse(f"report result has no {what!r} object")
    return cast("Mapping[str, Any]", value)


def _list(value: Any, what: str) -> list[Any]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise UnexpectedResponse(f"report result field {what!r} is not a list")
    return cast("list[Any]", value)


def _names(meta: Mapping[str, Any], field: str) -> list[str]:
    out: list[str] = []
    for item in _list(meta.get(field), f"reportMetadata.{field}"):
        name: Any = cast("Mapping[str, Any]", item).get("name") if isinstance(item, Mapping) else item
        if not isinstance(name, str):
            raise UnexpectedResponse(f"report result has a malformed reportMetadata.{field} entry")
        out.append(name)
    return out


def _groups(
    body: Mapping[str, Any], side: str, names: Sequence[str], *, leaves_only: bool
) -> Iterator[tuple[str, dict[str, Any]]]:
    """``(key, {grouping column: value})`` of each grouping on one side, depth first.

    The grand total ``"T"`` comes last (unless ``leaves_only`` and the side has groupings).
    """
    if names:
        root: Any = body.get(side)
        top = _list(_mapping(root, side).get("groupings"), f"{side}.groupings")
        yield from _walk(top, names, 0, {}, leaves_only, side)
        if leaves_only:
            return
    yield _TOTAL, {}


def _walk(
    nodes: list[Any],
    names: Sequence[str],
    depth: int,
    above: dict[str, Any],
    leaves_only: bool,
    side: str,
) -> Iterator[tuple[str, dict[str, Any]]]:
    for node in nodes:
        g: Mapping[str, Any] = cast("Mapping[str, Any]", node) if isinstance(node, Mapping) else {}
        if not isinstance(g.get("key"), str):
            raise UnexpectedResponse(f"report result has a malformed {side} grouping")
        values: dict[str, Any] = {**above, names[depth]: g.get("value")}
        leaf = depth == len(names) - 1
        if leaf or not leaves_only:
            yield g["key"], values
        if not leaf:
            yield from _walk(
                _list(g.get("groupings"), f"{side}.groupings"), names, depth + 1, values, leaves_only, side
            )


def _layout(body: Any) -> tuple[Mapping[str, Any], Mapping[str, Any], list[str], list[str]]:
    """``(body, factMap, row grouping names, column grouping names)``."""
    b = _mapping(body, "report result")
    fact_map = _mapping(b.get("factMap"), "factMap")
    meta = _mapping(b.get("reportMetadata"), "reportMetadata")
    fmt: Any = meta.get("reportFormat")
    if fmt == "MULTI_BLOCK":
        raise ValueError("joined reports (MULTI_BLOCK) have no single fact map; read result.raw instead")
    return b, fact_map, _names(meta, "groupingsDown"), _names(meta, "groupingsAcross")


def _cell(item: Any, key: str) -> Mapping[str, Any]:
    if not isinstance(item, Mapping):
        raise UnexpectedResponse(f"report fact map entry {key!r} has a malformed cell")
    return cast("Mapping[str, Any]", item)


def detail_rows(body: Any) -> list[dict[str, Any]]:
    """The detail rows of a report run, one dict per record, in report order.

    Each dict maps the grouping columns of the row's cell (row groupings, then column
    groupings) and the detail columns (``reportMetadata.detailColumns``) to the ``value`` of
    that cell. Rows come from the ``"T!T"`` entry of a tabular report and from the leaf
    entries (deepest row grouping ``!`` deepest column grouping) of a summary or matrix report.

    Returns:
        An empty list when the run had no details (``include_details=False``).

    Raises:
        UnexpectedResponse: the body isn't a report result, or a row has a different number
            of cells than there are detail columns.
        ValueError: the report is a joined report.
    """
    b, fact_map, down, across = _layout(body)
    meta = _mapping(b.get("reportMetadata"), "reportMetadata")
    columns = _names(meta, "detailColumns")
    out: list[dict[str, Any]] = []
    for dkey, dvals in _groups(b, "groupingsDown", down, leaves_only=True):
        for akey, avals in _groups(b, "groupingsAcross", across, leaves_only=True):
            key = f"{dkey}!{akey}"
            entry: Any = fact_map.get(key)
            if entry is None:
                continue  # Salesforce leaves out empty cells of a matrix
            for row in _list(_mapping(entry, f"factMap[{key}]").get("rows"), f"factMap[{key}].rows"):
                cells = _list(_cell(row, key).get("dataCells"), f"factMap[{key}].rows.dataCells")
                if len(cells) != len(columns):
                    raise UnexpectedResponse(
                        f"report row in {key!r} has {len(cells)} cells for {len(columns)} detail columns"
                    )
                record: dict[str, Any] = {**dvals, **avals}
                for name, cell in zip(columns, cells, strict=True):
                    record[name] = _cell(cell, key).get("value")
                out.append(record)
    return out


def summary_rows(body: Any) -> list[dict[str, Any]]:
    """The aggregates of every grouping of a report run, one dict per fact map entry.

    Each dict maps the grouping columns of the entry to their values, and each aggregate
    (``reportMetadata.aggregates``, such as ``"s!Amount"`` or ``"RowCount"``) to its
    ``value``. A subtotal row lacks the deeper grouping columns and the grand total has none,
    so ``len(row)`` tells the level. Order: row groupings depth first with their totals last;
    within each, column groupings in the same order.

    Raises:
        UnexpectedResponse: the body isn't a report result, or an entry has a different number
            of aggregates than the report defines.
        ValueError: the report is a joined report.
    """
    b, fact_map, down, across = _layout(body)
    meta = _mapping(b.get("reportMetadata"), "reportMetadata")
    aggregates = _names(meta, "aggregates")
    out: list[dict[str, Any]] = []
    for dkey, dvals in _groups(b, "groupingsDown", down, leaves_only=False):
        for akey, avals in _groups(b, "groupingsAcross", across, leaves_only=False):
            key = f"{dkey}!{akey}"
            entry: Any = fact_map.get(key)
            if entry is None:
                continue
            values = _list(_mapping(entry, f"factMap[{key}]").get("aggregates"), f"factMap[{key}].aggregates")
            if len(values) != len(aggregates):
                raise UnexpectedResponse(
                    f"report fact map entry {key!r} has {len(values)} aggregates, not {len(aggregates)}"
                )
            row: dict[str, Any] = {**dvals, **avals}
            for name, cell in zip(aggregates, values, strict=True):
                row[name] = _cell(cell, key).get("value")
            out.append(row)
    return out
