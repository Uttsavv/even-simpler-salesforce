"""Result models. Plain frozen dataclasses; no I/O."""

from __future__ import annotations

import copy
import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Generic, Literal, TypeVar, cast

from .core.cursor import Cursor
from .core.http import check_record_id
from .errors import DeployFailed, ErrorDetail, PartialFailure, SalesforceError, UnexpectedResponse

__all__ = [
    "ActionResult",
    "AnonymousResult",
    "BatchResult",
    "CompositeResult",
    "GraphResult",
    "BulkFailure",
    "BulkJobInfo",
    "BulkJobState",
    "DeployMessage",
    "DeployOptions",
    "DeployResult",
    "DeployTestFailure",
    "FileProperties",
    "ChangeHeader",
    "ChangeType",
    "LeadConvert",
    "LeadConvertResult",
    "FileLink",
    "Limit",
    "MergeResult",
    "MetadataDescribe",
    "MetadataType",
    "PubSubEvent",
    "PublishResult",
    "QueryPlan",
    "QueryPlanNote",
    "QueryResult",
    "RecordResult",
    "SubrequestResult",
    "ReportInstance",
    "ReportResult",
    "RetrieveMessage",
    "RetrieveResult",
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
        outputs: ``outputValues``. Usually empty when the invocation failed; a failed Flow
            interview still has ``Flow__InterviewGuid`` and ``Flow__InterviewStatus``.
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
        """Deprecated alias of :attr:`succeeded`; to be removed in 0.7.

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


# --- Metadata API (0.4) ------------------------------------------------------------------------

TestLevel = Literal["NoTestRun", "RunSpecifiedTests", "RunLocalTests", "RunAllTestsInOrg", "RunRelevantTests"]
_TEST_LEVELS = frozenset(
    {"NoTestRun", "RunSpecifiedTests", "RunLocalTests", "RunAllTestsInOrg", "RunRelevantTests"}
)
#: Deploy states that mean the deployment did not (fully) succeed.
_DEPLOY_FAILED_STATES = frozenset({"Failed", "Canceled", "FinalizingDeployFailed", "SucceededPartial"})


@dataclass(frozen=True, slots=True)
class DeployOptions:
    """Options for ``sf.metadata.deploy()`` (the Metadata API ``deployOptions``).

    Attributes:
        check_only: Validate the deployment and run tests, but save nothing.
        rollback_on_error: Roll back everything if one component fails. ``None`` leaves it to
            Salesforce, which requires ``True`` in production orgs.
        ignore_warnings: Deploy even if there are warnings. If ``False``, a warning fails the
            component.
        test_level: Which Apex tests to run. ``None`` uses the Salesforce default.
        run_tests: Apex test classes to run. Needs ``test_level="RunSpecifiedTests"``.
        purge_on_delete: Delete components listed in ``destructiveChanges.xml`` at once,
            without the Recycle Bin. Not allowed in production orgs.
        allow_missing_files: Deploy even if files named in ``package.xml`` are missing.
        auto_update_package: Add files that are missing from ``package.xml``.
        single_package: ``package.xml`` is at the root of the zip file. Set ``False`` only for
            a zip with one folder per package.

    Raises:
        ValueError: an unknown ``test_level``, or ``run_tests`` without
            ``test_level="RunSpecifiedTests"`` (or that level without ``run_tests``).
        TypeError: ``run_tests`` is one string instead of a list of class names.
    """

    check_only: bool = False
    rollback_on_error: bool | None = None
    ignore_warnings: bool = False
    test_level: TestLevel | None = None
    run_tests: tuple[str, ...] = ()
    purge_on_delete: bool = False
    allow_missing_files: bool = False
    auto_update_package: bool = False
    single_package: bool = True

    def __post_init__(self) -> None:
        if isinstance(self.run_tests, str):  # pyright: ignore[reportUnnecessaryIsInstance]
            raise TypeError("run_tests must be a list of Apex class names, not one string")
        object.__setattr__(self, "run_tests", tuple(self.run_tests))
        if self.test_level is not None and self.test_level not in _TEST_LEVELS:
            raise ValueError(f"test_level must be one of {sorted(_TEST_LEVELS)}, got {self.test_level!r}")
        if bool(self.run_tests) != (self.test_level == "RunSpecifiedTests"):
            raise ValueError('run_tests and test_level="RunSpecifiedTests" go together')


@dataclass(frozen=True, slots=True)
class DeployMessage:
    """One component of a deployment (Metadata API ``DeployMessage``).

    Attributes:
        component_type: Metadata type, e.g. ``"ApexClass"``.
        full_name: Full name of the component, e.g. ``"Invoice"``.
        file_name: Path of the file in the zip, e.g. ``"classes/Invoice.cls"``.
        problem: Why the component failed, or a warning. ``None`` on success.
        problem_type: ``"Error"`` or ``"Warning"``.
        line: Line of the problem in the file, if Salesforce reports one.
        column: Column of the problem in the file, if Salesforce reports one.
    """

    component_type: str | None = None
    full_name: str | None = None
    file_name: str | None = None
    problem: str | None = None
    problem_type: str | None = None
    line: int | None = None
    column: int | None = None


@dataclass(frozen=True, slots=True)
class DeployTestFailure:
    """One failed Apex test method of a deployment.

    Attributes:
        class_name: The test class.
        method_name: The test method.
        message: The failure message.
        stack_trace: The Apex stack trace.
    """

    class_name: str | None = None
    method_name: str | None = None
    message: str | None = None
    stack_trace: str | None = None


@dataclass(frozen=True, slots=True)
class DeployResult:
    """State of a deployment (Metadata API ``DeployResult``).

    ``sf.metadata.deploy_status()`` returns it for every state, also when the deployment failed.
    Call :meth:`raise_for_errors` to turn a failure into :class:`DeployFailed`.

    Attributes:
        id: The deployment id (``0Af...``).
        status: ``Pending``, ``InProgress``, ``Finalizing``, ``FinalizingDeploy``,
            ``FinalizingDeployFailed``, ``Succeeded``, ``SucceededPartial``, ``Failed``,
            ``Canceling`` or ``Canceled``.
        done: Whether Salesforce has finished the deployment.
        success: Whether the deployment succeeded.
        check_only: Whether this was a validation that saves nothing.
        components_total: Number of components in the deployment.
        components_deployed: Number of components deployed so far.
        component_errors: Number of components that failed.
        tests_total: Number of Apex tests to run.
        tests_completed: Number of Apex tests that have run.
        test_errors: Number of Apex tests that failed.
        error_message: Why the whole deployment failed, if it did.
        error_status_code: Status code for ``error_message``.
        component_failures: The components that failed.
        component_successes: The components that deployed.
        test_failures: The Apex tests that failed.
        raw: The ``deployResult`` object as Salesforce sent it.
    """

    id: str
    status: str
    done: bool = False
    success: bool = False
    check_only: bool = False
    components_total: int = 0
    components_deployed: int = 0
    component_errors: int = 0
    tests_total: int = 0
    tests_completed: int = 0
    test_errors: int = 0
    error_message: str | None = None
    error_status_code: str | None = None
    component_failures: tuple[DeployMessage, ...] = ()
    component_successes: tuple[DeployMessage, ...] = ()
    test_failures: tuple[DeployTestFailure, ...] = ()
    raw: Mapping[str, Any] = field(default_factory=lambda: {}, repr=False, compare=False)

    @property
    def failed(self) -> bool:
        """Whether the deployment finished without fully succeeding.

        A finished deployment with failed Apex tests counts as failed too. With
        ``rollback_on_error`` off (the default in non-production orgs) Salesforce reports such a
        deployment as ``Succeeded``, with the failures under ``test_failures``.
        """
        if self.status in _DEPLOY_FAILED_STATES:
            return True
        return self.done and (not self.success or self.test_errors > 0 or bool(self.test_failures))

    def raise_for_errors(self) -> None:
        """Raise :class:`DeployFailed` if the deployment failed, deployed only some components,
        or ran Apex tests that failed.

        Does nothing while the deployment is still running.
        """
        if not self.failed:
            return
        details: list[ErrorDetail] = []
        if self.error_message:
            details.append(ErrorDetail(error_code=self.error_status_code, message=self.error_message))
        for m in self.component_failures:
            details.append(
                ErrorDetail(
                    error_code=m.problem_type,
                    message=f"{m.component_type or '?'} {m.full_name or '?'}: {m.problem or ''}".rstrip(": "),
                    fields=(m.full_name,) if m.full_name else (),
                    extended={"file_name": m.file_name, "line": m.line, "column": m.column},
                )
            )
        for t in self.test_failures:
            details.append(
                ErrorDetail(
                    error_code="TEST_FAILURE",
                    message=f"{t.class_name or '?'}.{t.method_name or '?'}: {t.message or ''}".rstrip(": "),
                    extended={"stack_trace": t.stack_trace},
                )
            )
        first = details[0].message if details else "no details"
        raise DeployFailed(
            f"deployment {self.id} ended {self.status}: {first}",
            error_code=self.error_status_code,
            operation="metadata.deploy",
            errors=tuple(details),
            job_id=self.id,
            state=self.status,
            result=self,
        )


@dataclass(frozen=True, slots=True)
class FileProperties:
    """One metadata component of the org (Metadata API ``FileProperties``).

    Attributes:
        type: Metadata type, e.g. ``"ApexClass"``.
        full_name: Full name, e.g. ``"Invoice"`` or ``"Account.Score__c"``.
        file_name: Path of the file in a retrieved zip, e.g. ``"classes/Invoice.cls"``.
        id: The component id, if it has one.
        namespace_prefix: Namespace of a managed package, or ``None``.
        manageable_state: ``unmanaged``, ``installed``, ``released`` and similar.
        created_by_name: Name of the user who created it.
        created_date: When it was created (ISO 8601 text, as sent).
        last_modified_by_name: Name of the user who changed it last.
        last_modified_date: When it was changed last (ISO 8601 text, as sent).
    """

    type: str
    full_name: str
    file_name: str | None = None
    id: str | None = None
    namespace_prefix: str | None = None
    manageable_state: str | None = None
    created_by_name: str | None = None
    created_date: str | None = None
    last_modified_by_name: str | None = None
    last_modified_date: str | None = None


@dataclass(frozen=True, slots=True)
class RetrieveMessage:
    """A problem with one file of a retrieve, such as a component that doesn't exist.

    Attributes:
        file_name: The file the message is about.
        problem: What went wrong.
    """

    file_name: str | None
    problem: str


@dataclass(frozen=True, slots=True)
class RetrieveResult:
    """A finished retrieve: the zip file and what is in it.

    Attributes:
        id: The retrieve job id.
        zip_file: The zip file as bytes, with ``package.xml`` at its root.
        file_properties: One entry per file in the zip.
        messages: Problems with single components. The retrieve still succeeded.
    """

    id: str
    zip_file: bytes = field(repr=False)
    file_properties: tuple[FileProperties, ...] = ()
    messages: tuple[RetrieveMessage, ...] = ()


@dataclass(frozen=True, slots=True)
class MetadataType:
    """One metadata type of the org (Metadata API ``DescribeMetadataObject``).

    Attributes:
        xml_name: Type name for ``package.xml``, e.g. ``"ApexClass"``.
        directory_name: Folder of the type in a zip, e.g. ``"classes"``.
        suffix: File suffix, e.g. ``"cls"``, or ``None``.
        in_folder: Components live in folders (reports, documents, email templates).
        meta_file: Each component has a ``-meta.xml`` file beside it.
        child_xml_names: Child types, e.g. ``CustomField`` for ``CustomObject``.
    """

    xml_name: str
    directory_name: str | None = None
    suffix: str | None = None
    in_folder: bool = False
    meta_file: bool = False
    child_xml_names: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class MetadataDescribe:
    """The metadata types of the org (Metadata API ``DescribeMetadataResult``).

    Attributes:
        types: The metadata types.
        organization_namespace: The namespace of the org, or ``None``.
        partial_save_allowed: Deployments may save some components when others fail.
            Always ``False`` in production orgs.
        test_required: Deployments must run Apex tests (the opposite of
            ``partial_save_allowed``).
    """

    types: tuple[MetadataType, ...] = ()
    organization_namespace: str | None = None
    partial_save_allowed: bool = False
    test_required: bool = False


class ChangeType(str, Enum):
    """``ChangeEventHeader.changeType`` of a Change Data Capture event.

    ``GAP_*`` events say that Salesforce could not produce a normal change event, so they carry
    no field values: re-read the records. ``GAP_OVERFLOW`` says that one transaction changed
    too many records for Salesforce to send a change event for each one: re-sync the object.

    ``SNAPSHOT`` is in the ``ChangeType`` enum of the change event schema that Salesforce sends
    (org-observed, API v68.0), but the Change Data Capture documentation doesn't say when an
    event has it. It is listed so that such an event doesn't end the subscription.
    """

    CREATE = "CREATE"
    UPDATE = "UPDATE"
    DELETE = "DELETE"
    UNDELETE = "UNDELETE"
    GAP_CREATE = "GAP_CREATE"
    GAP_UPDATE = "GAP_UPDATE"
    GAP_DELETE = "GAP_DELETE"
    GAP_UNDELETE = "GAP_UNDELETE"
    GAP_OVERFLOW = "GAP_OVERFLOW"
    SNAPSHOT = "SNAPSHOT"

    @property
    def is_gap(self) -> bool:
        """``True`` for every ``GAP_*`` value, including ``GAP_OVERFLOW``."""
        return self.value.startswith("GAP_")


@dataclass(frozen=True, slots=True)
class ChangeHeader:
    """The decoded ``ChangeEventHeader`` of a Change Data Capture event.

    The ``*_fields`` tuples hold field names, decoded from the bitmaps Salesforce sends. A
    component of a compound field is named ``Parent.Child``, for example
    ``BillingAddress.City``.

    Attributes:
        entity_name: The sObject, e.g. ``"Account"``.
        change_type: What happened to the records.
        record_ids: The changed records. Several ids mean Salesforce merged identical changes
            into one event. Empty for an overflow event.
        changed_fields: Fields set by an update (``UPDATE`` and ``UNDELETE`` only).
        nulled_fields: Fields set to ``null`` by an update.
        diff_fields: Long text fields sent as a diff instead of the full value.
        transaction_key: Groups the events of one transaction.
        sequence_number: Order of this event within its transaction (starts at 1).
        commit_timestamp: When the transaction was committed (UTC).
        commit_user: Id of the user who made the change.
        commit_number: The system change number of the commit.
        change_origin: The client that made the change, e.g. ``"com/salesforce/api/rest/68.0"``.
    """

    entity_name: str
    change_type: ChangeType
    record_ids: tuple[str, ...] = ()
    changed_fields: tuple[str, ...] = ()
    nulled_fields: tuple[str, ...] = ()
    diff_fields: tuple[str, ...] = ()
    transaction_key: str | None = None
    sequence_number: int | None = None
    commit_timestamp: datetime | None = None
    commit_user: str | None = None
    commit_number: int | None = None
    change_origin: str | None = None

    @property
    def is_gap(self) -> bool:
        """``True`` for ``GAP_*`` events: they carry no field values."""
        return self.change_type.is_gap

    @property
    def is_overflow(self) -> bool:
        """``True`` for ``GAP_OVERFLOW``: re-sync every record of :attr:`entity_name`."""
        return self.change_type is ChangeType.GAP_OVERFLOW


@dataclass(frozen=True, slots=True)
class PubSubEvent:
    """One event received from a Pub/Sub API subscription.

    Attributes:
        topic: The topic you subscribed to, e.g. ``"/data/AccountChangeEvent"``.
        replay_id: Opaque position of the event in the stream. Store it to resume after it.
            Replay ids are not unique across org migrations: never de-duplicate on them.
        event_id: ``ConsumerEvent.event.id`` as Salesforce sent it, or ``None`` when it was
            empty. On a platform event it is the ``EventUuid``, the key to de-duplicate on.
            Change events carry one too, but Salesforce documents it for platform events
            only: de-duplicate change events on ``(change.transaction_key,
            change.sequence_number)``.
        schema_id: Id of the Avro schema the payload was written with.
        payload: The decoded event. Change events keep their ``ChangeEventHeader`` here too.
        change: The decoded header of a change event, ``None`` for a platform event.
    """

    topic: str
    replay_id: bytes
    event_id: str | None
    schema_id: str
    payload: Mapping[str, Any]
    change: ChangeHeader | None = None
