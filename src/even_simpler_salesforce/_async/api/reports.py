"""Reports and dashboards: ``sf.reports`` and ``sf.reports.dashboards`` (§9.14)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any, cast

from ...config import Backoff
from ...core.http import RequestSpec
from ...core.poll import DEFAULT_TIMEOUT, PollClock
from ...core.reports import (
    dashboard_path,
    instance_path,
    instance_status,
    instances_path,
    parse_instance,
    recent_reports_path,
    report_describe_path,
    report_path,
    run_body,
    run_params,
)
from ...errors import ReportRunFailed, ReportTimeout, UnexpectedResponse
from ...models import ReportInstance, ReportResult

if TYPE_CHECKING:
    from ..client import AsyncSalesforce

__all__ = ["AsyncDashboards", "AsyncReports"]

Filters = Sequence[Mapping[str, Any]]


def _object(raw: Any, what: str, operation: str) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise UnexpectedResponse(f"unexpected {what} response", operation=operation)
    return cast("dict[str, Any]", raw)


class AsyncReports:
    """Run reports and read their results through the Reports and Dashboards REST API.

    Running a report reads data, so the client resends a run after a timeout or a 5xx.
    Starting an asynchronous run creates a report instance in Salesforce, so the client never
    resends it after it may have reached Salesforce.

    Attributes:
        dashboards: Dashboard results and refresh (``sf.reports.dashboards``).
    """

    def __init__(self, client: AsyncSalesforce) -> None:
        self._sf = client
        self.dashboards = AsyncDashboards(client)

    async def recent(self) -> list[dict[str, Any]]:
        """``GET analytics/reports``: up to 200 reports the user viewed most recently.

        This is not a list of every report. To find all reports, query the ``Report`` object:
        ``sf.query("SELECT Id, Name, DeveloperName, FolderName FROM Report")``.

        Returns:
            One dict per report, as Salesforce sends it: ``id``, ``name``, ``url``,
            ``describeUrl``, ``instancesUrl`` and ``fieldsUrl``.

        Example:
            for report in await sf.reports.recent():
                print(report["id"], report["name"])
        """
        raw = await self._sf._send(  # pyright: ignore[reportPrivateUsage]
            RequestSpec("GET", recent_reports_path(), idempotent=True, operation="reports.recent")
        )
        body = raw.json()
        if not isinstance(body, list) or not all(isinstance(r, dict) for r in cast("list[Any]", body)):
            raise UnexpectedResponse("unexpected recent reports response", operation="reports.recent")
        return cast("list[dict[str, Any]]", body)

    async def describe(self, report_id: str) -> dict[str, Any]:
        """``GET analytics/reports/{id}/describe``: the report metadata.

        Args:
            report_id: Id of the report (``00O...``).

        Returns:
            The body: ``reportMetadata``, ``reportExtendedMetadata`` and ``reportTypeMetadata``.
        """
        raw = await self._sf._send(  # pyright: ignore[reportPrivateUsage]
            RequestSpec("GET", report_describe_path(report_id), idempotent=True, operation="reports.describe")
        )
        return _object(raw.json(), "report describe", "reports.describe")

    async def run(
        self, report_id: str, filters: Filters | None = None, *, include_details: bool = True
    ) -> ReportResult:
        """Run a report and wait for the results in the same call.

        Without ``filters`` this is ``GET analytics/reports/{id}``. With ``filters`` it is a
        ``POST`` whose body sets ``reportMetadata.reportFilters``. Salesforce returns at most
        2,000 detail rows; check ``result.all_data``.

        Args:
            report_id: Id of the report (``00O...``).
            filters: Filters for this run, such as ``[{"column": "STAGE_NAME", "operator":
                "equals", "value": "Prospecting"}]``. They replace the saved filters.
            include_details: Return detail rows as well as the summaries.

        Returns:
            The results. Read them with ``rows()`` and ``summary_rows()``.
        """
        body = run_body(filters)
        params = run_params(include_details)
        path = report_path(report_id)
        if body is None:
            spec = RequestSpec("GET", path, params=params, idempotent=True, operation="reports.run")
        else:
            content, ctype = self._sf._transport.encode(body)  # pyright: ignore[reportPrivateUsage]
            spec = RequestSpec(
                "POST",
                path,
                params=params,
                content=content,
                content_type=ctype,
                idempotent=True,  # a run reads data; it changes nothing in the org
                operation="reports.run",
            )
        raw = await self._sf._send(spec)  # pyright: ignore[reportPrivateUsage]
        return ReportResult(_object(raw.json(), "report run", "reports.run"))

    async def run_async(
        self, report_id: str, filters: Filters | None = None, *, include_details: bool = True
    ) -> ReportInstance:
        """Start an asynchronous run: ``POST analytics/reports/{id}/instances``.

        Use it for reports that take longer than the synchronous API timeout. Pass the
        result to :meth:`instance` to wait for the results.

        Args:
            report_id: Id of the report (``00O...``).
            filters: Filters for this run (see :meth:`run`).
            include_details: Return detail rows as well as the summaries.

        Returns:
            The new instance, usually with status ``"New"``.
        """
        body = run_body(filters)
        content: bytes | None = None
        ctype: str | None = None
        if body is not None:
            content, ctype = self._sf._transport.encode(body)  # pyright: ignore[reportPrivateUsage]
        raw = await self._sf._send(  # pyright: ignore[reportPrivateUsage]
            RequestSpec(
                "POST",
                instances_path(report_id),
                params=run_params(include_details),
                content=content,
                content_type=ctype,
                idempotent=False,  # each POST creates a report instance
                operation="reports.run_async",
            )
        )
        return parse_instance(raw.json(), report_id)

    async def instance(
        self,
        report_id: str,
        instance_id: str,
        *,
        poll: Backoff | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> ReportResult:
        """Wait for an asynchronous run and return its results.

        Polls ``GET analytics/reports/{id}/instances/{instance}`` until the status is
        ``"Success"``.

        Args:
            report_id: Id of the report.
            instance_id: Id of the instance, from :meth:`run_async`.
            poll: How long to wait between status calls. Defaults to ``esf.Backoff()``
                (2 s, doubling up to 30 s).
            timeout: Seconds to wait before raising :class:`ReportTimeout` (an hour by
                default).

        Raises:
            ReportRunFailed: the run ended with status ``"Error"``.
            ReportTimeout: the run didn't finish within ``timeout``. It keeps running in
                Salesforce.
            UnexpectedResponse: Salesforce sent a status this library doesn't know.

        Example:
            result = await sf.reports.instance(run.report_id, run.id, poll=esf.Backoff(start=5))
        """
        clock = PollClock(timeout, poll)
        spec = RequestSpec(
            "GET", instance_path(report_id, instance_id), idempotent=True, operation="reports.instance"
        )
        while True:
            raw = await self._sf._send(spec)  # pyright: ignore[reportPrivateUsage]
            body = _object(raw.json(), "report instance", "reports.instance")
            status = instance_status(body)
            if status == "Success":
                return ReportResult(body)
            if status == "Error":
                raise ReportRunFailed(
                    f"report run {instance_id} of report {report_id} ended with status Error",
                    report_id=report_id,
                    instance_id=instance_id,
                    operation="reports.instance",
                    status=raw.status,
                    request_id=raw.request_id,
                )
            delay = clock.next_delay()
            if delay is None:
                raise ReportTimeout(
                    f"report run {instance_id} is still {status} after {timeout:g} s; "
                    "it keeps running in Salesforce",
                    report_id=report_id,
                    instance_id=instance_id,
                    run_status=status,
                    operation="reports.instance",
                )
            await self._sf._transport.sleep(delay)  # pyright: ignore[reportPrivateUsage]


class AsyncDashboards:
    """Read and refresh dashboards (``sf.reports.dashboards``).

    Reading results is a read and may be resent. A refresh starts work in Salesforce, so the
    client never resends it after it may have reached Salesforce.
    """

    def __init__(self, client: AsyncSalesforce) -> None:
        self._sf = client

    async def get(self, dashboard_id: str) -> dict[str, Any]:
        """``GET analytics/dashboards/{id}``: dashboard metadata, component data and status.

        Each entry of ``componentData`` has a ``reportResult`` in the report run format; wrap
        it in :class:`ReportResult` to read its rows. ``reportResult`` is ``None`` (and
        ``status.componentDataStatus`` is ``"NO_DATA"``) until the dashboard was refreshed
        once.

        Args:
            dashboard_id: Id of the dashboard (``01Z...``).

        Raises:
            InvalidReference: no dashboard with this id (Salesforce answers 400
                ``INVALID_CROSS_REFERENCE_KEY``, not 404).
        """
        raw = await self._sf._send(  # pyright: ignore[reportPrivateUsage]
            RequestSpec("GET", dashboard_path(dashboard_id), idempotent=True, operation="dashboards.get")
        )
        return _object(raw.json(), "dashboard", "dashboards.get")

    async def refresh(self, dashboard_id: str) -> dict[str, Any]:
        """``PUT analytics/dashboards/{id}``: start a refresh of every component.

        The refresh runs in Salesforce. Call :meth:`get` later: each component's
        ``status.refreshStatus`` is ``"IDLE"`` once it is done.

        Args:
            dashboard_id: Id of the dashboard (``01Z...``).

        Returns:
            The body: ``{"statusUrl": ...}``, the status resource of the dashboard.

        Raises:
            Conflict: a refresh of this dashboard is still running (HTTP 409).
            Forbidden: the dashboard was refreshed less than a minute ago (HTTP 403).
        """
        raw = await self._sf._send(  # pyright: ignore[reportPrivateUsage]
            RequestSpec(
                "PUT",
                dashboard_path(dashboard_id),
                idempotent=False,  # starts a refresh in Salesforce
                operation="dashboards.refresh",
            )
        )
        return _object(raw.json(), "dashboard refresh", "dashboards.refresh")
