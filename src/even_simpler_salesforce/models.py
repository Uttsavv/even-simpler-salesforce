"""Result models. Plain frozen dataclasses; no I/O."""

from __future__ import annotations

import copy
import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Generic, TypeVar, cast

from .core.cursor import Cursor
from .core.http import check_record_id
from .errors import ErrorDetail, PartialFailure, SalesforceError, UnexpectedResponse

__all__ = [
    "ActionResult",
    "AnonymousResult",
    "BatchResult",
    "CompositeResult",
    "GraphResult",
    "BulkFailure",
    "BulkJobInfo",
    "BulkJobState",
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
    "SubrequestResult",
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


#: Characters XML 1.0 can't carry (a SOAP envelope with one is not well-formed).
_XML_ILLEGAL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]")


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
            empty, ``contact_id`` is set without ``account_id``, or a text value contains
            control characters (XML can't carry them).
        TypeError: a text value is not a ``str``, or a flag is not a ``bool``.
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
        # These go into the SOAP envelope as text: XML 1.0 can't carry control characters.
        for name in ("converted_status", "opportunity_name"):
            text = getattr(self, name)
            if text is None:
                continue
            if not isinstance(text, str):
                raise TypeError(f"{name} must be a str, got {type(text).__name__}")
            if _XML_ILLEGAL.search(text):
                raise ValueError(f"{name} must not contain control characters")
        for name in ("do_not_create_opportunity", "send_email", "overwrite_lead_source"):
            if not isinstance(getattr(self, name), bool):
                raise TypeError(f"{name} must be a bool, got {type(getattr(self, name)).__name__}")
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
class AnonymousResult:
    """Outcome of ``sf.tooling.execute_anonymous(code)``. Anonymous Apex can't return values.

    Check ``compiled`` first: ``success`` is ``False`` for code that did not compile too.

    Attributes:
        compiled: Whether the code compiled.
        success: Whether the code compiled and ran without an unhandled exception.
        compile_problem: Why compilation failed, else ``None``.
        exception_message: The unhandled exception, else ``None``.
        exception_stack_trace: Its stack trace, else ``None``.
        line: Line of the compile problem or exception, else ``None``.
        column: Column of the compile problem or exception, else ``None``.
        log: The debug log of the run with ``capture_log=True`` (``System.debug`` output), else
            ``None``. Also ``None`` when the client could not find or read the log.
    """

    compiled: bool
    success: bool
    compile_problem: str | None = None
    exception_message: str | None = None
    exception_stack_trace: str | None = None
    line: int | None = None
    column: int | None = None
    log: str | None = None


# Error codes Salesforce gives subrequests that didn't fail themselves: they were rolled back or
# skipped because another subrequest failed.
_HALTED = frozenset({"PROCESSING_HALTED", "BATCH_PROCESSING_HALTED", "ALL_OR_NONE_OPERATION_ROLLED_BACK"})


@dataclass(frozen=True, slots=True)
class SubrequestResult:
    """Outcome of one subrequest of a Composite, Composite Graph or Batch request.

    Attributes:
        reference_id: The subrequest's reference id (the client's name for it in a Batch).
        status: HTTP status of the subrequest (``httpStatusCode`` or ``statusCode``).
        body: Decoded body of the subrequest: the record for a get, the query page for a
            query, ``{"id": ..., "success": true, ...}`` for a create, ``None`` for 204.
        headers: Response headers of the subrequest, such as ``Location``.
        error: The typed error for a failed subrequest (§8), else ``None``.
    """

    reference_id: str | None
    status: int
    body: Any = None
    headers: Mapping[str, str] = field(default_factory=lambda: {})
    error: SalesforceError | None = None

    @property
    def ok(self) -> bool:
        """Whether the subrequest succeeded."""
        return self.error is None

    @property
    def halted(self) -> bool:
        """Whether Salesforce skipped or rolled back this subrequest because another one failed."""
        return self.error is not None and self.error.error_code in _HALTED

    @property
    def errors(self) -> tuple[ErrorDetail, ...]:
        """The error entries of a failed subrequest (empty when it succeeded)."""
        return self.error.errors if self.error is not None else ()


@dataclass(frozen=True, slots=True)
class CompositeResult:
    """Results of a Composite or Batch request, one per subrequest, in the order you added them.

    Index it by position or by reference id: ``result[0]``, ``result["acc"]``. A failed
    subrequest doesn't raise; call :meth:`raise_for_errors`.

    Attributes:
        results: One :class:`SubrequestResult` per subrequest.
        operation: ``"composite.send"``, ``"composite.batch"`` or ``"composite.graph"``.
    """

    results: list[SubrequestResult]
    operation: str | None = None

    def __iter__(self) -> Iterator[SubrequestResult]:
        return iter(self.results)

    def __len__(self) -> int:
        return len(self.results)

    def __getitem__(self, key: int | str) -> SubrequestResult:
        if isinstance(key, str):
            for r in self.results:
                if r.reference_id == key:
                    return r
            raise KeyError(key)
        return self.results[key]

    @property
    def succeeded(self) -> list[SubrequestResult]:
        """Subrequests that succeeded."""
        return [r for r in self.results if r.ok]

    @property
    def failed(self) -> list[SubrequestResult]:
        """Subrequests that failed, including the ones Salesforce halted or rolled back."""
        return [r for r in self.results if not r.ok]

    def raise_for_errors(self) -> None:
        """Raise the typed error of the first subrequest that caused a failure.

        Subrequests that Salesforce only halted or rolled back (``PROCESSING_HALTED``) don't
        pick the class: the real cause does, for example :class:`ValidationRuleFailed`. The
        exception's ``.errors`` holds the error entries of every failed subrequest.
        """
        failed = self.failed
        if not failed:
            return
        primary = next((r for r in failed if not r.halted), failed[0])
        exc = copy.copy(cast(SalesforceError, primary.error))
        exc.errors = tuple(d for r in failed for d in r.errors)
        raise exc


@dataclass(frozen=True, slots=True)
class GraphResult:
    """Outcome of one graph of a Composite Graph request.

    A graph is atomic: if one subrequest fails, Salesforce rolls back the whole graph, and
    only that graph.

    Attributes:
        graph_id: The graph's id.
        ok: ``isSuccessful`` from Salesforce.
        results: The graph's subrequest results.
    """

    graph_id: str
    ok: bool
    results: CompositeResult

    def raise_for_errors(self) -> None:
        """Raise the typed error of the subrequest that made this graph fail."""
        self.results.raise_for_errors()
        if not self.ok:
            raise UnexpectedResponse(f"graph {self.graph_id!r} failed without error details")


class BulkJobState(str, Enum):
    """State of a Bulk API 2.0 job (*Job States*, Bulk API 2.0 Developer Guide).

    ``JobComplete``, ``Failed`` and ``Aborted`` are final: the job doesn't change after them.
    The values equal the strings Salesforce sends, so ``state == "JobComplete"`` also works.
    """

    OPEN = "Open"
    UPLOAD_COMPLETE = "UploadComplete"
    IN_PROGRESS = "InProgress"
    JOB_COMPLETE = "JobComplete"
    FAILED = "Failed"
    ABORTED = "Aborted"

    @property
    def is_final(self) -> bool:
        """Whether the job has finished: ``JobComplete``, ``Failed`` or ``Aborted``."""
        return self in (BulkJobState.JOB_COMPLETE, BulkJobState.FAILED, BulkJobState.ABORTED)


@dataclass(frozen=True, slots=True)
class BulkJobInfo:
    """Status of a Bulk API 2.0 job, from the latest status call.

    Attributes:
        id: Job id (``750...``). For an ingest split over several jobs, the id of the first one.
        operation: ``insert``, ``update``, ``upsert``, ``delete``, ``hardDelete``, ``query`` or
            ``queryAll``.
        sobject: The object of the job.
        state: The job state.
        api_version: The API version that created the job. Results are read with it.
        records_processed: ``numberRecordsProcessed``.
        records_failed: ``numberRecordsFailed`` (ingest jobs only).
        created_date: When the job was created.
        system_modstamp: When Salesforce last changed the job.
        total_processing_ms: ``totalProcessingTime`` in milliseconds, if Salesforce sent it.
        error_message: ``errorMessage`` of a failed job.
        raw: The job info as Salesforce sent it. Empty for an ingest split over several jobs.
    """

    id: str
    operation: str | None
    sobject: str | None
    state: BulkJobState
    api_version: str
    records_processed: int = 0
    records_failed: int = 0
    created_date: datetime | None = None
    system_modstamp: datetime | None = None
    total_processing_ms: int | None = None
    error_message: str | None = None
    raw: Mapping[str, Any] = field(default_factory=lambda: {}, repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class BulkFailure:
    """One row of the failed-results CSV of a Bulk ingest job.

    Attributes:
        error: The parsed ``sf__Error`` column: status code, message and fields.
        id: ``sf__Id``: the record id, if Salesforce reported one.
        fields: The other columns, as you uploaded them.
    """

    error: ErrorDetail
    id: str | None = None
    fields: Mapping[str, str] = field(default_factory=lambda: {})
