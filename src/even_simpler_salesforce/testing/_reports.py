"""Fake of ``sf.reports`` and ``sf.reports.dashboards`` (§9.14, §14).

Reports are scripted, not run: ``fake.on_report(report_id, result)`` gives the body a run
returns, and ``fake.on_dashboard(dashboard_id, body)`` the dashboard results. An asynchronous
run finishes at once, so ``instance()`` returns without waiting.
"""

from __future__ import annotations

import copy
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, cast

from ..core.reports import (
    dashboard_path,
    instance_path,
    instances_path,
    report_describe_path,
    report_path,
    run_body,
)
from ..errors import NotFound
from ..models import ReportInstance, ReportResult
from ._ids import make_id
from ._state import FakeState, UnscriptedCall, Who

__all__ = [
    "AsyncFakeDashboards",
    "AsyncFakeReports",
    "FakeDashboards",
    "FakeReports",
    "ReportScript",
]

_INSTANCE_PREFIX = "0LG"  # key prefix of report instance ids

Filters = Sequence[Mapping[str, Any]]
ResultSpec = Mapping[str, Any] | ReportResult | Callable[..., Any] | BaseException


@dataclass(slots=True)
class ReportScript:
    """A scripted report (returned by ``fake.on_report``).

    Attributes:
        report_id: Id of the report.
        result: The run body, a :class:`ReportResult`, a callable
            ``(filters, include_details) -> body``, or an exception instance to raise.
        describe: The body ``describe()`` returns. Defaults to the ``reportMetadata`` of the
            result.
        runs: How many times the report ran so far (both run kinds).
    """

    report_id: str
    result: ResultSpec
    describe: Mapping[str, Any] | None = None
    runs: int = 0

    def answer(self, filters: list[dict[str, Any]] | None, include_details: bool) -> dict[str, Any]:
        """The run body for one call."""
        self.runs += 1
        out: Any = self.result
        if isinstance(out, BaseException):
            raise copy.copy(out)
        if callable(out):
            out = out(copy.deepcopy(filters), include_details)
        if isinstance(out, ReportResult):
            out = out.raw
        body = copy.deepcopy(dict(cast("Mapping[str, Any]", out)))
        if not include_details and not callable(self.result):
            _drop_details(body)
        return body


def _drop_details(body: dict[str, Any]) -> None:
    fact_map: Any = body.get("factMap")
    if isinstance(fact_map, dict):
        for entry in cast("dict[str, Any]", fact_map).values():
            if isinstance(entry, dict):
                cast("dict[str, Any]", entry).pop("rows", None)
    body["hasDetailRows"] = False


def add_report(state: FakeState, script: ReportScript) -> ReportScript:
    report_path(script.report_id)  # same id check as the client
    with state.lock:
        state.reports[script.report_id] = script
    return script


def add_dashboard(state: FakeState, dashboard_id: str, body: Mapping[str, Any]) -> None:
    dashboard_path(dashboard_id)
    with state.lock:
        state.dashboards[dashboard_id] = copy.deepcopy(dict(body))


def _script(state: FakeState, report_id: str) -> ReportScript:
    script: ReportScript | None = state.reports.get(report_id)
    if script is None:
        raise UnscriptedCall(
            f"no report scripted for {report_id!r}. Register one with fake.on_report({report_id!r}, result)"
        )
    return script


class FakeReports:
    """Fake of ``sf.reports``: runs answer from ``fake.on_report`` scripts.

    Every call is recorded in ``fake.calls`` with the operation name of the real client
    (``reports.run``, ``reports.run_async``, ``reports.instance``, ...), so
    ``fake.fail_next("reports.run", error)`` works too.
    """

    def __init__(self, state: FakeState, who: Who) -> None:
        self._state = state
        self._who = who
        self.dashboards = FakeDashboards(state, who)

    def describe(self, report_id: str) -> dict[str, Any]:
        """The scripted ``describe`` body, else the ``reportMetadata`` of the scripted result."""
        path = report_describe_path(report_id)

        def fn() -> dict[str, Any]:
            script = _script(self._state, report_id)
            if script.describe is not None:
                return copy.deepcopy(dict(script.describe))
            body = script.answer(None, False)
            script.runs -= 1  # a describe is not a run
            return {k: body[k] for k in ("reportMetadata", "reportExtendedMetadata") if k in body}

        return self._state.run(
            self._who, "reports.describe", None, {"report_id": report_id}, fn, method="GET", path=path
        )

    def run(
        self, report_id: str, filters: Filters | None = None, *, include_details: bool = True
    ) -> ReportResult:
        """The scripted result. ``include_details=False`` drops the detail rows."""
        body = run_body(filters)
        flt = body["reportMetadata"]["reportFilters"] if body else None
        return self._state.run(
            self._who,
            "reports.run",
            None,
            {"report_id": report_id, "filters": flt, "include_details": include_details},
            lambda: ReportResult(_script(self._state, report_id).answer(flt, include_details)),
            method="GET" if body is None else "POST",
            path=report_path(report_id),
        )

    def run_async(
        self, report_id: str, filters: Filters | None = None, *, include_details: bool = True
    ) -> ReportInstance:
        """Run the scripted report at once and return a ``"New"`` instance for ``instance()``."""
        body = run_body(filters)
        flt = body["reportMetadata"]["reportFilters"] if body else None
        path = instances_path(report_id)

        def fn() -> ReportInstance:
            result = _script(self._state, report_id).answer(flt, include_details)
            self._state.counter += 1  # run() holds the state lock
            iid = make_id(_INSTANCE_PREFIX, self._state.counter)
            self._state.report_instances[iid] = (report_id, result)
            now = self._state.clock().isoformat()
            return ReportInstance(
                id=iid,
                report_id=report_id,
                status="New",
                request_date=now,
                url=f"/services/data/v{self._state.api_version}/{path}/{iid}",
            )

        return self._state.run(
            self._who,
            "reports.run_async",
            None,
            {"report_id": report_id, "filters": flt, "include_details": include_details},
            fn,
            method="POST",
            path=path,
        )

    def instance(
        self,
        report_id: str,
        instance_id: str,
        *,
        timeout: float = 600.0,
        poll_interval: float = 2.0,
    ) -> ReportResult:
        """The results of a run started with :meth:`run_async` (it has already finished).

        Raises:
            NotFound: no run with these ids, as Salesforce answers ``404``.
        """
        if not timeout > 0:
            raise ValueError("timeout must be > 0 seconds")
        if not poll_interval > 0:
            raise ValueError("poll_interval must be > 0 seconds")
        path = instance_path(report_id, instance_id)

        def fn() -> ReportResult:
            found: Any = self._state.report_instances.get(instance_id)
            if found is None or found[0] != report_id:
                raise NotFound(
                    f"report instance {instance_id} of report {report_id} doesn't exist",
                    error_code="NOT_FOUND",
                    status=404,
                )
            body = copy.deepcopy(cast("dict[str, Any]", found[1]))
            attrs: Any = body.get("attributes")
            body["attributes"] = {
                **(cast("dict[str, Any]", attrs) if isinstance(attrs, dict) else {}),
                "id": instance_id,
                "reportId": report_id,
                "status": "Success",
            }
            return ReportResult(body)

        return self._state.run(
            self._who,
            "reports.instance",
            None,
            {"report_id": report_id, "instance_id": instance_id},
            fn,
            method="GET",
            path=path,
        )


class FakeDashboards:
    """Fake of ``sf.reports.dashboards``: answers from ``fake.on_dashboard`` bodies."""

    def __init__(self, state: FakeState, who: Who) -> None:
        self._state = state
        self._who = who

    def _body(self, dashboard_id: str) -> dict[str, Any]:
        body: dict[str, Any] | None = self._state.dashboards.get(dashboard_id)
        if body is None:
            raise UnscriptedCall(
                f"no dashboard scripted for {dashboard_id!r}. "
                f"Register one with fake.on_dashboard({dashboard_id!r}, body)"
            )
        return body

    def get(self, dashboard_id: str) -> dict[str, Any]:
        """A copy of the body given to ``fake.on_dashboard``."""
        path = dashboard_path(dashboard_id)
        return self._state.run(
            self._who,
            "dashboards.get",
            None,
            {"dashboard_id": dashboard_id},
            lambda: copy.deepcopy(self._body(dashboard_id)),
            method="GET",
            path=path,
        )

    def refresh(self, dashboard_id: str) -> dict[str, Any]:
        """Record the refresh and return ``id`` and ``statusUrl``, like Salesforce."""
        path = dashboard_path(dashboard_id)

        def fn() -> dict[str, Any]:
            self._body(dashboard_id)
            status = f"/services/data/v{self._state.api_version}/{path}/status"
            return {"id": dashboard_id, "statusUrl": status}

        return self._state.run(
            self._who,
            "dashboards.refresh",
            None,
            {"dashboard_id": dashboard_id},
            fn,
            method="PUT",
            path=path,
        )


class AsyncFakeReports:
    """Async twin of :class:`FakeReports`."""

    def __init__(self, inner: FakeReports) -> None:
        self._inner = inner
        self.dashboards = AsyncFakeDashboards(inner.dashboards)

    async def describe(self, report_id: str) -> dict[str, Any]:
        """See :meth:`FakeReports.describe`."""
        return self._inner.describe(report_id)

    async def run(
        self, report_id: str, filters: Filters | None = None, *, include_details: bool = True
    ) -> ReportResult:
        """See :meth:`FakeReports.run`."""
        return self._inner.run(report_id, filters, include_details=include_details)

    async def run_async(
        self, report_id: str, filters: Filters | None = None, *, include_details: bool = True
    ) -> ReportInstance:
        """See :meth:`FakeReports.run_async`."""
        return self._inner.run_async(report_id, filters, include_details=include_details)

    async def instance(
        self, report_id: str, instance_id: str, *, timeout: float = 600.0, poll_interval: float = 2.0
    ) -> ReportResult:
        """See :meth:`FakeReports.instance`."""
        return self._inner.instance(report_id, instance_id, timeout=timeout, poll_interval=poll_interval)


class AsyncFakeDashboards:
    """Async twin of :class:`FakeDashboards`."""

    def __init__(self, inner: FakeDashboards) -> None:
        self._inner = inner

    async def get(self, dashboard_id: str) -> dict[str, Any]:
        """See :meth:`FakeDashboards.get`."""
        return self._inner.get(dashboard_id)

    async def refresh(self, dashboard_id: str) -> dict[str, Any]:
        """See :meth:`FakeDashboards.refresh`."""
        return self._inner.refresh(dashboard_id)
