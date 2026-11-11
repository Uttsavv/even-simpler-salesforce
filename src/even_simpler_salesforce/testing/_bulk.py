"""``fake.bulk``: Bulk API 2.0 for :class:`FakeSalesforce` (§9.11, §14).

Ingest jobs run at once: ``sf.bulk.ingest()`` applies every row to the fake's store and returns
a job that is already ``JobComplete``. Rows go through the same CSV rules as the real client
(``None`` sets null, a missing key leaves the field unchanged, ``"#N/A"`` is rejected), and the
stored values are the CSV strings, because Salesforce receives strings too.

``sf.bulk.query()`` returns the rows scripted with ``fake.on_query(...)``, flattened and turned
into strings like the CSV that Salesforce sends.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterable, Iterator, Mapping, Sequence
from datetime import datetime
from typing import Any

from ..config import Backoff
from ..core.bulk import (
    COLUMN_DELIMITERS,
    NULL,
    ColumnDelimiter,
    CsvCutter,
    CsvReader,
    IngestOperation,
    LineEnding,
    RowEncoder,
    check_bulk_query,
    check_ingest,
    check_line_ending,
    expand_compound_fields,
)
from ..core.http import check_sobject
from ..core.records import flatten
from ..core.soql import soql_values
from ..errors import SalesforceError
from ..models import BulkFailure, BulkJobInfo, BulkJobState, ErrorDetail
from ._ids import make_id
from ._state import FakeState, Who

__all__ = ["AsyncFakeBulk", "AsyncFakeIngestJob", "FakeBulk", "FakeIngestJob"]


def _sobject_of(soql: str) -> str | None:
    from .._sync.client import _sobject_of as real  # pyright: ignore[reportPrivateUsage]

    return real(soql)


def _csv_bytes(rows: Any, line_ending: str, column_delimiter: str) -> bytes:
    if isinstance(rows, (bytes, bytearray, memoryview)):
        return bytes(rows)  # pyright: ignore[reportUnknownArgumentType]
    if isinstance(rows, (str, os.PathLike)):
        with open(os.fspath(rows), "rb") as f:  # pyright: ignore[reportUnknownArgumentType]
            return f.read()
    if not isinstance(rows, Iterable):
        raise TypeError(
            f"rows must be an iterable of dicts, a CSV path or CSV bytes, got {type(rows).__name__}"
        )
    encoder = RowEncoder(column_delimiter, line_ending)
    out = bytearray()
    for row in rows:  # pyright: ignore[reportUnknownVariableType]
        if encoder.columns is None:
            out += encoder.header(row)  # pyright: ignore[reportUnknownArgumentType]
        out += encoder.encode(row)  # pyright: ignore[reportUnknownArgumentType]
    if encoder.columns is None:
        raise ValueError("rows is empty; there is nothing to upload")
    return bytes(out)


def _text(value: Any) -> str | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


class FakeIngestJob:
    """What ``fake.bulk.ingest()`` returns: a finished job with in-memory results."""

    def __init__(
        self,
        info: BulkJobInfo,
        successes: list[dict[str, str]],
        failures: list[BulkFailure],
    ) -> None:
        self._info = info
        self._successes = successes
        self._failures = failures

    def __repr__(self) -> str:
        return f"<FakeIngestJob {self._info.id} {self._info.operation} {self._info.sobject}>"

    @property
    def ids(self) -> tuple[str, ...]:
        """The job id (the fake never splits an upload)."""
        return (self._info.id,)

    @property
    def info(self) -> BulkJobInfo:
        """Final status: always ``JobComplete``."""
        return self._info

    @property
    def parts(self) -> tuple[BulkJobInfo, ...]:
        """One status per job."""
        return (self._info,)

    def refresh(self) -> BulkJobInfo:
        """Return :attr:`info`; the fake job is already finished."""
        return self._info

    def wait(self, *, poll: Backoff | None = None, timeout: float = 3600.0) -> BulkJobInfo:
        """Return the final :attr:`info` at once: the fake job is already ``JobComplete``.

        Use ``fake.fail_next("bulk.ingest", esf.BulkJobFailed(...))`` to test a failed job.
        """
        if not timeout > 0:
            raise ValueError("timeout must be > 0 seconds")
        return self._info

    def abort(self) -> BulkJobInfo:
        """No-op: the fake job finished before you could abort it."""
        return self._info

    def successful_results(self) -> Iterator[dict[str, str]]:
        """``sf__Id``, ``sf__Created`` and the uploaded columns of each saved row."""
        for row in self._successes:
            yield dict(row)

    def failed_results(self) -> Iterator[BulkFailure]:
        """One :class:`BulkFailure` per rejected row."""
        yield from self._failures

    def unprocessed_records(self) -> Iterator[dict[str, str]]:
        """Always empty: the fake processes every row."""
        yield from ()


class FakeBulk:
    """Fake of ``sf.bulk``. Same arguments and validation as the real one."""

    def __init__(self, state: FakeState, who: Who) -> None:
        self._state = state
        self._who = who

    def ingest(
        self,
        sobject: str,
        operation: IngestOperation,
        *,
        rows: Iterable[Mapping[str, Any]] | str | os.PathLike[str] | bytes,
        external_id_field: str | None = None,
        line_ending: LineEnding = "LF",
        column_delimiter: ColumnDelimiter = "COMMA",
    ) -> FakeIngestJob:
        """Apply the rows to the store and return a finished job (operation ``bulk.ingest``)."""
        check_sobject(sobject)
        check_ingest(operation, external_id_field, line_ending, column_delimiter)
        data = _csv_bytes(rows, line_ending, column_delimiter)
        cutter = CsvCutter(limit=max(len(data), 1))
        cutter.feed(data)
        header, _ = cutter.finish()
        check_line_ending(header, line_ending)
        reader = CsvReader(COLUMN_DELIMITERS[column_delimiter])
        parsed = reader.feed(data.removeprefix(b"\xef\xbb\xbf")) + reader.finish()

        def fn() -> FakeIngestJob:
            successes: list[dict[str, str]] = []
            failures: list[BulkFailure] = []
            for row in parsed:
                try:
                    rid, created = self._apply(sobject, operation, external_id_field, row)
                    successes.append({"sf__Id": rid, "sf__Created": "true" if created else "false", **row})
                except SalesforceError as exc:
                    failures.append(
                        BulkFailure(
                            error=ErrorDetail(exc.error_code, exc.message, exc.fields),
                            id=row.get("Id") or None,
                            fields=dict(row),
                        )
                    )
            info = BulkJobInfo(
                id=self._job_id(),
                operation=operation,
                sobject=sobject,
                state=BulkJobState.JOB_COMPLETE,
                api_version=self._state.api_version,
                records_processed=len(parsed),
                records_failed=len(failures),
                created_date=self._state.clock(),
                system_modstamp=self._state.clock(),
            )
            return FakeIngestJob(info, successes, failures)

        return self._state.run(
            self._who,
            "bulk.ingest",
            sobject,
            {
                "operation": operation,
                "external_id_field": external_id_field,
                "rows": len(parsed),
                "columns": tuple(reader.header or ()),
            },
            fn,
            method="PUT",
            path="jobs/ingest",
            records=len(parsed),
        )

    def _job_id(self) -> str:
        with self._state.lock:
            self._state.counter += 1
            return make_id("750", self._state.counter)  # 750: the AsyncApexJob prefix of Bulk jobs

    def _apply(
        self, sobject: str, operation: str, external_id_field: str | None, row: Mapping[str, str]
    ) -> tuple[str, bool]:
        st = self._state
        data: dict[str, Any] = {k: (None if v == NULL else v) for k, v in row.items() if v != ""}
        if operation == "insert":
            return st.create(sobject, data, "bulk.ingest"), True
        if operation == "upsert":
            assert external_id_field is not None
            value = data.get(external_id_field)
            result = st.upsert(sobject, external_id_field, value, data, "bulk.ingest")
            return str(result.id), result.created
        rid = str(data.pop("Id", "") or "")
        if operation == "update":
            return st.update(sobject, rid, data, operation="bulk.ingest"), False
        st.delete(sobject, rid, operation="bulk.ingest")
        return rid, False

    def query(
        self,
        soql: str,
        *values: Any,
        include_deleted: bool = False,
        parallel_downloads: int = 1,
        poll: Backoff | None = None,
        timeout: float = 3600.0,
    ) -> Iterator[dict[str, str | None]]:
        """Yield the rows scripted with ``fake.on_query`` as Bulk CSV rows (operation ``bulk.query``).

        Rows are flattened (``"Owner.Name"``), values become strings and empty values ``None``.
        """
        if not isinstance(parallel_downloads, int) or parallel_downloads < 1:  # pyright: ignore[reportUnnecessaryIsInstance]
            raise ValueError("parallel_downloads must be an int >= 1")
        if not timeout > 0:
            raise ValueError("timeout must be > 0 seconds")
        soql = soql_values(soql, values)
        check_bulk_query(soql)

        def fn() -> list[dict[str, str | None]]:
            rows, _ = self._state.query_rows(soql)
            return [{k: _text(v) for k, v in flatten(r).items()} for r in rows]

        rows = self._state.run(
            self._who,
            "bulk.query",
            _sobject_of(soql),
            {"soql": soql, "include_deleted": include_deleted, "parallel_downloads": parallel_downloads},
            fn,
            method="GET",
            path="jobs/query",
            soql=soql,
        )
        yield from rows

    def expand_compound_fields(self, sobject: str, fields: Sequence[str]) -> list[str]:
        """Expand compound fields with the describe set by ``fake.set_describe``."""
        if isinstance(fields, str):
            raise TypeError("fields must be a list of field names, not a string")
        check_sobject(sobject)
        return expand_compound_fields(self._state.describe(sobject), fields)


class AsyncFakeIngestJob:
    """Async twin of :class:`FakeIngestJob`."""

    def __init__(self, inner: FakeIngestJob) -> None:
        self._inner = inner

    def __repr__(self) -> str:
        return repr(self._inner).replace("<Fake", "<AsyncFake", 1)

    @property
    def ids(self) -> tuple[str, ...]:
        """See :attr:`FakeIngestJob.ids`."""
        return self._inner.ids

    @property
    def info(self) -> BulkJobInfo:
        """See :attr:`FakeIngestJob.info`."""
        return self._inner.info

    @property
    def parts(self) -> tuple[BulkJobInfo, ...]:
        """See :attr:`FakeIngestJob.parts`."""
        return self._inner.parts

    async def refresh(self) -> BulkJobInfo:
        """See :meth:`FakeIngestJob.refresh`."""
        return self._inner.refresh()

    async def wait(self, *, poll: Backoff | None = None, timeout: float = 3600.0) -> BulkJobInfo:
        """See :meth:`FakeIngestJob.wait`."""
        return self._inner.wait(poll=poll, timeout=timeout)

    async def abort(self) -> BulkJobInfo:
        """See :meth:`FakeIngestJob.abort`."""
        return self._inner.abort()

    async def successful_results(self) -> AsyncIterator[dict[str, str]]:
        """See :meth:`FakeIngestJob.successful_results`."""
        for row in self._inner.successful_results():
            yield row

    async def failed_results(self) -> AsyncIterator[BulkFailure]:
        """See :meth:`FakeIngestJob.failed_results`."""
        for row in self._inner.failed_results():
            yield row

    async def unprocessed_records(self) -> AsyncIterator[dict[str, str]]:
        """See :meth:`FakeIngestJob.unprocessed_records`."""
        for row in self._inner.unprocessed_records():
            yield row


class AsyncFakeBulk:
    """Async twin of :class:`FakeBulk`."""

    def __init__(self, inner: FakeBulk) -> None:
        self._inner = inner

    async def ingest(
        self,
        sobject: str,
        operation: IngestOperation,
        *,
        rows: Iterable[Mapping[str, Any]] | str | os.PathLike[str] | bytes,
        external_id_field: str | None = None,
        line_ending: LineEnding = "LF",
        column_delimiter: ColumnDelimiter = "COMMA",
    ) -> AsyncFakeIngestJob:
        """See :meth:`FakeBulk.ingest`."""
        job = self._inner.ingest(
            sobject,
            operation,
            rows=rows,
            external_id_field=external_id_field,
            line_ending=line_ending,
            column_delimiter=column_delimiter,
        )
        return AsyncFakeIngestJob(job)

    async def query(
        self,
        soql: str,
        *values: Any,
        include_deleted: bool = False,
        parallel_downloads: int = 1,
        poll: Backoff | None = None,
        timeout: float = 3600.0,
    ) -> AsyncIterator[dict[str, str | None]]:
        """See :meth:`FakeBulk.query`."""
        rows = self._inner.query(
            soql,
            *values,
            include_deleted=include_deleted,
            parallel_downloads=parallel_downloads,
            poll=poll,
            timeout=timeout,
        )
        for row in rows:
            yield row

    async def expand_compound_fields(self, sobject: str, fields: Sequence[str]) -> list[str]:
        """See :meth:`FakeBulk.expand_compound_fields`."""
        return self._inner.expand_compound_fields(sobject, fields)
