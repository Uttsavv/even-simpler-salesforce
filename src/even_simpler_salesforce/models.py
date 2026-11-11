"""Result models. Plain frozen dataclasses; no I/O."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Generic, TypeVar

from .core.cursor import Cursor
from .core.http import check_record_id
from .errors import ErrorDetail, PartialFailure

__all__ = [
    "ActionResult",
    "BatchResult",
    "ChangeHeader",
    "ChangeType",
    "LeadConvert",
    "LeadConvertResult",
    "FileLink",
    "Limit",
    "MergeResult",
    "PubSubEvent",
    "PublishResult",
    "QueryPlan",
    "QueryPlanNote",
    "QueryResult",
    "RecordResult",
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


class ChangeType(str, Enum):
    """``ChangeEventHeader.changeType`` of a Change Data Capture event.

    ``GAP_*`` events say that Salesforce could not produce a normal change event, so they carry
    no field values: re-read the records. ``GAP_OVERFLOW`` says that one transaction changed
    too many records for Salesforce to send a change event for each one: re-sync the object.
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
        event_id: ``EventUuid`` of a platform event, the key to de-duplicate on. ``None`` for
            change events: de-duplicate those on ``(change.transaction_key,
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
