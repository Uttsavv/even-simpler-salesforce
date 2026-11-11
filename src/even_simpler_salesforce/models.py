"""Result models. Plain frozen dataclasses; no I/O."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Generic, TypeVar

from .core.cursor import Cursor
from .core.http import check_record_id
from .errors import ErrorDetail, PartialFailure

__all__ = [
    "ActionResult",
    "BatchResult",
    "LeadConvert",
    "LeadConvertResult",
    "FileLink",
    "Limit",
    "MergeResult",
    "PublishResult",
    "QueryPlan",
    "QueryPlanNote",
    "QueryResult",
    "RecordResult",
    "ReportInstance",
    "ReportResult",
    "UpsertResult",
    "UsageSnapshot",
]

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class QueryResult(Generic[T]):
    """One page of a SOQL query.

    Attributes:
        records: Rows of this page (plain dicts unless ``model=`` was given).
        total_size: ``totalSize`` reported by Salesforce for the whole query.
        done: ``True`` when this is the last page.
        cursor: Opaque cursor for :meth:`query_more`, or ``None`` when ``done``.
    """

    records: list[T]
    total_size: int
    done: bool
    cursor: Cursor | None = None

    def __iter__(self) -> Iterator[T]:
        return iter(self.records)

    def __len__(self) -> int:
        return len(self.records)


@dataclass(frozen=True, slots=True)
class RecordResult:
    """Outcome for one record of a multi-record operation."""

    ok: bool
    id: str | None = None
    created: bool | None = None
    errors: tuple[ErrorDetail, ...] = ()


@dataclass(frozen=True, slots=True)
class ActionResult:
    """Outcome of one invocation of an invocable action (one entry per input).

    Attributes:
        ok: ``isSuccess`` from Salesforce.
        outputs: ``outputValues`` (empty when the invocation failed).
        errors: The error entries when it failed.
        action_name: ``actionName`` as Salesforce reports it.
    """

    ok: bool
    outputs: Mapping[str, Any] = field(default_factory=lambda: {})
    errors: tuple[ErrorDetail, ...] = ()
    action_name: str | None = None


@dataclass(frozen=True, slots=True)
class BatchResult(Generic[T]):
    """Ordered results of a multi-record operation (same order as the input).

    ``T`` is the per-item result type (:class:`RecordResult` for writes).
    """

    results: list[T]
    operation: str | None = None
    sobject: str | None = None

    def __iter__(self) -> Iterator[T]:
        return iter(self.results)

    def __len__(self) -> int:
        return len(self.results)

    def __getitem__(self, index: int) -> T:
        return self.results[index]

    @property
    def succeeded(self) -> list[T]:
        """Results that succeeded (check ``not batch.failed`` for "every record succeeded")."""
        return [r for r in self.results if getattr(r, "ok", True)]

    @property
    def ok(self) -> list[T]:
        """Deprecated alias of :attr:`succeeded`; removed in 0.2.

        It reads like a bool (``if batch.ok:``) but is a list, true when *any* record succeeded.
        """
        import warnings

        from . import EsfDeprecationWarning

        warnings.warn(
            "BatchResult.ok is deprecated: use .succeeded (a list), or `not batch.failed` to "
            "check that every record succeeded",
            EsfDeprecationWarning,
            stacklevel=2,
        )
        return self.succeeded

    @property
    def failed(self) -> list[T]:
        """Results that failed."""
        return [r for r in self.results if not getattr(r, "ok", True)]

    def raise_for_errors(self) -> None:
        """Raise :class:`PartialFailure` if any record failed."""
        failed = self.failed
        if not failed:
            return
        details: list[ErrorDetail] = []
        for r in failed:
            details.extend(getattr(r, "errors", ()))
        first = details[0] if details else None
        raise PartialFailure(
            f"{len(failed)} of {len(self.results)} records failed"
            + (f"; first: {first.error_code}: {first.message}" if first else ""),
            error_code=first.error_code if first else None,
            fields=first.fields if first else (),
            operation=self.operation,
            sobject=self.sobject,
            errors=tuple(details),
            results=self,
        )


@dataclass(frozen=True, slots=True)
class QueryPlanNote:
    """A note on why a query plan can't use an index (``notes[]`` of ``query?explain=``)."""

    description: str
    fields: tuple[str, ...] = ()
    table_enum_or_id: str | None = None


@dataclass(frozen=True, slots=True)
class QueryPlan:
    """One plan from ``GET query?explain=`` (Salesforce Beta feature). Plans come sorted from
    most to least optimal; the first is the one Salesforce would use.

    Attributes:
        leading_operation: ``"Index"``, ``"Other"`` or ``"TableScan"`` (``leadingOperationType``).
        relative_cost: Cost against the selectivity threshold; above 1.0 is not selective.
        cardinality: Estimated rows the plan returns.
        sobject_cardinality: Approximate number of records of the queried object in the org.
        sobject_type: The queried object.
        fields: Indexed fields the plan uses.
        notes: Why other fields couldn't use an index.
    """

    leading_operation: str
    relative_cost: float
    cardinality: int
    sobject_cardinality: int
    sobject_type: str
    fields: tuple[str, ...] = ()
    notes: tuple[QueryPlanNote, ...] = ()


@dataclass(frozen=True, slots=True)
class UpsertResult:
    """Result of an upsert by external id."""

    id: str | None
    created: bool


@dataclass(frozen=True, slots=True)
class FileLink:
    """A file linked to a record: one ``ContentDocumentLink`` row, from ``sf.files.list_for()``.

    Attributes:
        link_id: Id of the ``ContentDocumentLink``.
        content_document_id: Id of the ``ContentDocument`` (the file).
        latest_version_id: Id of the latest published ``ContentVersion``. Pass it to
            ``sf.files.download()``.
        title: Title of the file.
        file_extension: File extension without the dot, for example ``"pdf"``.
        size: Size of the latest version in bytes.
        share_type: ``"V"`` (viewer), ``"C"`` (collaborator) or ``"I"`` (inferred).
        visibility: ``"AllUsers"``, ``"InternalUsers"`` or ``"SharedUsers"``.
    """

    link_id: str
    content_document_id: str
    latest_version_id: str | None
    title: str | None
    file_extension: str | None
    size: int | None
    share_type: str | None
    visibility: str | None


@dataclass(frozen=True, slots=True)
class Limit:
    """One entry of ``GET /limits``."""

    max: int
    remaining: int
    details: Mapping[str, Any] = field(default_factory=lambda: {})

    @property
    def used(self) -> int:
        """``max - remaining``."""
        return self.max - self.remaining


@dataclass(frozen=True, slots=True)
class UsageSnapshot:
    """Point-in-time view of API usage (§10.3).

    ``api_*`` values come from the latest ``Sforce-Limit-Info`` header (org-wide). Counters are
    per process.
    """

    api_used: int | None
    api_max: int | None
    api_fraction: float | None
    as_of: datetime | None
    calls_by_operation: Mapping[str, int]
    calls_by_sobject: Mapping[str, int]
    calls_by_caller: Mapping[str, int]
    errors_by_code: Mapping[str, int]
    retries: int
    refreshes: int
    projected_exhaustion: datetime | None
    api_bursts_used: int | None = None
    api_bursts_max: int | None = None


# --- lead convert, merge, platform events (0.2) ------------------------------------------------


@dataclass(frozen=True, slots=True)
class LeadConvert:
    """One lead to convert with ``sf.leads.convert_many()`` (Partner ``LeadConvert``).

    Attributes:
        lead_id: The lead to convert.
        converted_status: A converted ``LeadStatus`` value of the org, e.g. ``"Closed - Converted"``.
        account_id: Merge into this existing account instead of creating one.
        contact_id: Merge into this existing contact (needs ``account_id``; not for person accounts).
        opportunity_name: Name of the new opportunity (default: the company name).
        do_not_create_opportunity: Don't create an opportunity.
        owner_id: Owner of the new records (default: the lead owner).
        send_email: Email the new owner (``sendNotificationEmail``).
        overwrite_lead_source: Overwrite ``LeadSource`` on an existing contact.

    Raises:
        ValueError: an id is not a 15- or 18-character Salesforce id, ``converted_status`` is
            empty, or ``contact_id`` is set without ``account_id``.
    """

    lead_id: str
    converted_status: str
    account_id: str | None = None
    contact_id: str | None = None
    opportunity_name: str | None = None
    do_not_create_opportunity: bool = False
    owner_id: str | None = None
    send_email: bool = False
    overwrite_lead_source: bool = False

    def __post_init__(self) -> None:
        check_record_id(self.lead_id)
        for value in (self.account_id, self.contact_id, self.owner_id):
            if value is not None:
                check_record_id(value)
        if not isinstance(self.converted_status, str) or not self.converted_status:  # pyright: ignore[reportUnnecessaryIsInstance]
            raise ValueError("converted_status must be a non-empty LeadStatus value")
        if self.contact_id is not None and self.account_id is None:
            raise ValueError("contact_id needs account_id (the contact's account)")


@dataclass(frozen=True, slots=True)
class LeadConvertResult:
    """Outcome of one lead conversion (Partner ``LeadConvertResult``).

    Attributes:
        ok: Whether the lead was converted.
        lead_id: The converted lead.
        account_id: The new account, or the one you passed.
        contact_id: The new contact, or the one you passed.
        opportunity_id: The new opportunity, or ``None`` when none was created.
        errors: Why the conversion failed.
    """

    ok: bool
    lead_id: str | None = None
    account_id: str | None = None
    contact_id: str | None = None
    opportunity_id: str | None = None
    errors: tuple[ErrorDetail, ...] = ()


@dataclass(frozen=True, slots=True)
class MergeResult:
    """Outcome of ``sf.merge()`` (Partner ``MergeResult``).

    Attributes:
        ok: Whether the records were merged.
        id: The master record.
        merged_ids: The records merged into the master and deleted.
        updated_related_ids: Related records that now point to the master.
        errors: Why the merge failed.
    """

    ok: bool
    id: str | None = None
    merged_ids: tuple[str, ...] = ()
    updated_related_ids: tuple[str, ...] = ()
    errors: tuple[ErrorDetail, ...] = ()


@dataclass(frozen=True, slots=True)
class PublishResult:
    """Outcome of publishing one platform event.

    ``ok`` means Salesforce queued the event; delivery happens later. Use ``uuid`` to match the
    event to its ``EventUuid`` on the subscriber side.

    Attributes:
        ok: Whether Salesforce queued the event.
        id: The id Salesforce returned for the publish call.
        uuid: The event's ``EventUuid`` (from the ``OPERATION_ENQUEUED`` entry).
        errors: Why the publish failed.
    """

    ok: bool
    id: str | None = None
    uuid: str | None = None
    errors: tuple[ErrorDetail, ...] = ()


@dataclass(frozen=True, slots=True)
class ReportInstance:
    """One asynchronous report run, from ``sf.reports.run_async()``.

    Pass ``report_id`` and ``id`` to ``sf.reports.instance()`` to wait for the results.

    Attributes:
        id: Id of the report instance (the run).
        report_id: Id of the report.
        status: ``"New"``, ``"Running"``, ``"Success"`` or ``"Error"``.
        request_date: When the run was requested (ISO 8601 text from Salesforce).
        completion_date: When the run finished, or ``None`` while it runs.
        url: Path of the instance results, or ``None`` if the run failed.
    """

    id: str
    report_id: str
    status: str
    request_date: str | None = None
    completion_date: str | None = None
    url: str | None = None


@dataclass(frozen=True, slots=True)
class ReportResult:
    """The results of one report run: the body Salesforce returned, with row helpers.

    ``sf.reports.run()`` and ``sf.reports.instance()`` return one. You can also wrap the
    ``reportResult`` of a dashboard component: ``esf.ReportResult(component["reportResult"])``.

    Attributes:
        raw: The response body (``attributes``, ``reportMetadata``, ``factMap``, ...).
    """

    raw: Mapping[str, Any]

    @property
    def report_id(self) -> str | None:
        """Id of the report (``attributes.reportId``, else ``reportMetadata.id``)."""
        return _str_or_none(
            _sub(self.raw, "attributes").get("reportId") or _sub(self.raw, "reportMetadata").get("id")
        )

    @property
    def name(self) -> str | None:
        """Name of the report (``attributes.reportName``, else ``reportMetadata.name``)."""
        return _str_or_none(
            _sub(self.raw, "attributes").get("reportName") or _sub(self.raw, "reportMetadata").get("name")
        )

    @property
    def format(self) -> str | None:
        """``"TABULAR"``, ``"SUMMARY"``, ``"MATRIX"`` or ``"MULTI_BLOCK"`` (``reportFormat``)."""
        return _str_or_none(_sub(self.raw, "reportMetadata").get("reportFormat"))

    @property
    def all_data(self) -> bool:
        """``False`` when Salesforce cut the detail rows short (``allData``)."""
        return self.raw.get("allData") is not False

    @property
    def has_detail_rows(self) -> bool:
        """Whether the run returned detail rows (``hasDetailRows``)."""
        return self.raw.get("hasDetailRows") is True

    def rows(self) -> list[dict[str, Any]]:
        """The detail rows, one dict per record: grouping values, then detail column values.

        Keys are column API names such as ``"ACCOUNT_NAME"``; values are the cell ``value``.
        Empty when the run had no details.

        Raises:
            UnexpectedResponse: the body isn't a report result Salesforce would send.
            ValueError: the report is a joined report (``MULTI_BLOCK``).
        """
        from .core.reports import detail_rows

        return detail_rows(self.raw)

    def summary_rows(self) -> list[dict[str, Any]]:
        """The aggregates of every grouping, subtotal and the grand total, one dict each.

        Keys are grouping column names and aggregate names such as ``"s!Amount"`` and
        ``"RowCount"``. A subtotal lacks the deeper grouping columns; the grand total has only
        aggregates.

        Raises:
            UnexpectedResponse: the body isn't a report result Salesforce would send.
            ValueError: the report is a joined report (``MULTI_BLOCK``).
        """
        from .core.reports import summary_rows

        return summary_rows(self.raw)


def _sub(raw: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value: Any = raw.get(key)
    return value if isinstance(value, Mapping) else {}  # pyright: ignore[reportUnknownVariableType]


def _str_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) else None
