"""Result models. Plain frozen dataclasses; no I/O."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Generic, TypeVar

from .core.cursor import Cursor
from .errors import ErrorDetail, PartialFailure

__all__ = [
    "BatchResult",
    "Limit",
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
    def ok(self) -> list[T]:
        """Results that succeeded."""
        return [r for r in self.results if getattr(r, "ok", True)]

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
class UpsertResult:
    """Result of an upsert by external id."""

    id: str | None
    created: bool


@dataclass(frozen=True, slots=True)
class Limit:
    """One entry of ``GET /limits``."""

    max: int
    remaining: int
    details: Mapping[str, Any] = field(default_factory=lambda: {})

    @property
    def used(self) -> int:
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
