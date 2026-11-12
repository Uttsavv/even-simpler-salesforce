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
    combine_infos,
    expand_compound_fields,
)
from ..core.http import check_record_id, check_sobject
from ..core.records import flatten
from ..core.soql import soql_values
from ..errors import NotFound, RequestError, SalesforceError
from ..models import BulkFailure, BulkJobInfo, BulkJobState, ErrorDetail
from ._ids import make_id
from ._state import FakeState, Who

__all__ = [
    "AsyncFakeBulk",
    "AsyncFakeIngestJob",
    "AsyncFakeQueryJob",
    "AsyncFakeResultPage",
    "FakeBulk",
    "FakeIngestJob",
    "FakeQueryJob",
    "FakeResultPage",
]


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
        parts: Sequence[BulkJobInfo] | None = None,
    ) -> None:
        self._info = info
        self._successes = successes
        self._failures = failures
        self._parts = tuple(parts) if parts else (info,)

    def __repr__(self) -> str:
        return f"<FakeIngestJob {self._info.id} {self._info.operation} {self._info.sobject}>"

    @property
    def ids(self) -> tuple[str, ...]:
        """The job id (the fake never splits an upload), or the ids given to ``ingest_job()``."""
        return tuple(p.id for p in self._parts)

    @property
    def info(self) -> BulkJobInfo:
        """Final status: always ``JobComplete``."""
        return self._info

    @property
    def parts(self) -> tuple[BulkJobInfo, ...]:
        """One status per job."""
        return self._parts

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


class FakeResultPage:
    """One page of :meth:`FakeQueryJob.pages`: rows held in memory."""

    def __init__(
        self, locator: str | None, next_locator: str | None, rows: list[dict[str, str | None]]
    ) -> None:
        #: The locator that fetched this page. ``None`` for the first page.
        self.locator = locator
        #: The locator of the next page, ``None`` after the last page.
        self.next_locator = next_locator
        #: The number of rows in the page.
        self.size: int | None = len(rows)
        self._rows = rows
        self._closed = False

    def __repr__(self) -> str:
        return f"<{type(self).__name__} locator={self.locator!r} next_locator={self.next_locator!r}>"

    def rows(self) -> Iterator[dict[str, str | None]]:
        """The rows of the page. Raises ``ValueError`` once the iteration has moved on."""
        if self._closed:
            raise ValueError("this page is closed; read its rows before you ask for the next page")
        for row in self._rows:
            yield dict(row)

    def close(self) -> None:
        """Mark the page closed, as the real client does when ``pages()`` moves on."""
        self._closed = True


class FakeQueryJob:
    """What ``fake.bulk.start_query()`` returns: a finished job with the scripted rows.

    The rows are taken from the ``fake.on_query`` scripts when the job is created.
    """

    def __init__(self, info: BulkJobInfo, rows: list[dict[str, str | None]]) -> None:
        self._info = info
        self._rows = rows

    def __repr__(self) -> str:
        info = self._info
        return f"<FakeQueryJob {info.id} {info.operation} {info.sobject} {info.state.value}>"

    @property
    def id(self) -> str:
        """The job id. Pass it to ``fake.bulk.query_job()`` to get the job back."""
        return self._info.id

    @property
    def info(self) -> BulkJobInfo:
        """Final status: always ``JobComplete``."""
        return self._info

    def refresh(self) -> BulkJobInfo:
        """Return :attr:`info`; the fake job is already finished."""
        return self._info

    def wait(self, *, poll: Backoff | None = None, timeout: float = 3600.0) -> BulkJobInfo:
        """Return :attr:`info` at once: the fake job is already ``JobComplete``.

        Use ``fake.fail_next("bulk.query", esf.BulkJobFailed(...))`` to make ``start_query()``
        fail instead.
        """
        if not timeout > 0:
            raise ValueError("timeout must be > 0 seconds")
        return self._info

    def abort(self) -> BulkJobInfo:
        """No-op: the fake job finished before you could abort it."""
        return self._info

    def rows(self, *, parallel_downloads: int = 1) -> Iterator[dict[str, str | None]]:
        """Every row, as Bulk CSV strings (empty values ``None``)."""
        _check_parallel(parallel_downloads)
        for row in self._rows:
            yield dict(row)

    def pages(
        self, *, locator: str | None = None, max_records: int | None = None
    ) -> Iterator[FakeResultPage]:
        """The rows in pages of ``max_records`` (one page when ``None``).

        Locators are the row offset as a string; pass ``page.next_locator`` back to resume.
        """
        _check_pages(locator, max_records)
        start = 0
        if locator is not None:
            if not locator.isdigit() or int(locator) > len(self._rows):
                raise RequestError(
                    f"unknown locator {locator!r} for job {self._info.id}",
                    error_code="INVALIDLOCATOR",
                    status=400,
                    operation="bulk.query.results",
                )
            start = int(locator)
        size = max_records or max(len(self._rows), 1)
        while True:
            end = min(start + size, len(self._rows))
            nxt = str(end) if end < len(self._rows) else None
            page = FakeResultPage(locator, nxt, self._rows[start:end])
            try:
                yield page
            finally:
                page.close()
            if nxt is None:
                return
            locator, start = nxt, end


def _check_parallel(parallel_downloads: int) -> None:
    if not isinstance(parallel_downloads, int) or parallel_downloads < 1:  # pyright: ignore[reportUnnecessaryIsInstance]
        raise ValueError("parallel_downloads must be an int >= 1")


def _check_pages(locator: str | None, max_records: int | None) -> None:
    if locator is not None and (not isinstance(locator, str) or not locator.strip()):  # pyright: ignore[reportUnnecessaryIsInstance]
        raise ValueError("locator must be a non-empty string from page.next_locator, or None")
    if max_records is not None and (
        not isinstance(max_records, int) or isinstance(max_records, bool) or max_records < 1  # pyright: ignore[reportUnnecessaryIsInstance]
    ):
        raise ValueError("max_records must be an int >= 1, or None")


def _job_not_found(job_id: str) -> NotFound:
    return NotFound(f"no Bulk job with id {job_id}", error_code="NOT_FOUND", status=404)


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
            job = FakeIngestJob(info, successes, failures)
            self._state.bulk_jobs[info.id] = job
            return job

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

    def start_query(self, soql: str, *values: Any, include_deleted: bool = False) -> FakeQueryJob:
        """Create a finished query job with the rows of ``fake.on_query`` (operation ``bulk.query``).

        Rows are flattened (``"Owner.Name"``), values become strings and empty values ``None``.
        """
        soql = soql_values(soql, values)
        check_bulk_query(soql)
        sobject = _sobject_of(soql)

        def fn() -> FakeQueryJob:
            rows, _ = self._state.query_rows(soql)
            data = [{k: _text(v) for k, v in flatten(r).items()} for r in rows]
            info = BulkJobInfo(
                id=self._job_id(),
                operation="queryAll" if include_deleted else "query",
                sobject=sobject,
                state=BulkJobState.JOB_COMPLETE,
                api_version=self._state.api_version,
                records_processed=len(data),
                records_failed=0,
                created_date=self._state.clock(),
                system_modstamp=self._state.clock(),
            )
            job = FakeQueryJob(info, data)
            self._state.bulk_jobs[info.id] = job
            return job

        return self._state.run(
            self._who,
            "bulk.query",
            sobject,
            {"soql": soql, "include_deleted": include_deleted},
            fn,
            method="POST",
            path="jobs/query",
            soql=soql,
        )

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
        _check_parallel(parallel_downloads)
        if not timeout > 0:
            raise ValueError("timeout must be > 0 seconds")
        job = self.start_query(soql, *values, include_deleted=include_deleted)
        yield from job.rows(parallel_downloads=parallel_downloads)

    def query_job(self, job_id: str) -> FakeQueryJob:
        """The query job with this id, made by ``start_query()`` or ``query()`` on this fake.

        Raises ``NotFound`` for an unknown id or an ingest job's id, as Salesforce does.
        """
        check_record_id(job_id)

        def fn() -> FakeQueryJob:
            job = self._state.bulk_jobs.get(job_id)
            if not isinstance(job, FakeQueryJob):
                raise _job_not_found(job_id)
            return job

        return self._state.run(
            self._who, "bulk.query.status", None, {"job_id": job_id}, fn, method="GET", path="jobs/query"
        )

    def ingest_job(self, job_ids: Sequence[str]) -> FakeIngestJob:
        """The ingest job with these ids, made by ``ingest()`` on this fake.

        Raises ``NotFound`` for an unknown id or a query job's id, as Salesforce does.
        """
        if isinstance(job_ids, str):
            raise TypeError("job_ids must be a sequence of job ids, such as job.ids, not a string")
        ids = list(job_ids)
        if not ids:
            raise ValueError("job_ids is empty")
        for job_id in ids:
            check_record_id(job_id)

        def fn() -> FakeIngestJob:
            jobs: list[FakeIngestJob] = []
            for job_id in ids:
                job = self._state.bulk_jobs.get(job_id)
                if not isinstance(job, FakeIngestJob):
                    raise _job_not_found(job_id)
                jobs.append(job)
            if len(jobs) == 1:
                return jobs[0]
            parts = [p for j in jobs for p in j.parts]
            return FakeIngestJob(
                combine_infos(parts),
                [r for j in jobs for r in j._successes],  # pyright: ignore[reportPrivateUsage]
                [f for j in jobs for f in j._failures],  # pyright: ignore[reportPrivateUsage]
                parts,
            )

        return self._state.run(
            self._who,
            "bulk.ingest.status",
            None,
            {"job_ids": tuple(ids)},
            fn,
            method="GET",
            path="jobs/ingest",
        )

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


class AsyncFakeResultPage:
    """Async twin of :class:`FakeResultPage`."""

    def __init__(self, inner: FakeResultPage) -> None:
        self._inner = inner
        self.locator = inner.locator
        self.next_locator = inner.next_locator
        self.size = inner.size

    def __repr__(self) -> str:
        return repr(self._inner).replace("<Fake", "<AsyncFake", 1)

    async def rows(self) -> AsyncIterator[dict[str, str | None]]:
        """See :meth:`FakeResultPage.rows`."""
        for row in self._inner.rows():
            yield row

    def close(self) -> None:
        """See :meth:`FakeResultPage.close`."""
        self._inner.close()


class AsyncFakeQueryJob:
    """Async twin of :class:`FakeQueryJob`."""

    def __init__(self, inner: FakeQueryJob) -> None:
        self._inner = inner

    def __repr__(self) -> str:
        return repr(self._inner).replace("<Fake", "<AsyncFake", 1)

    @property
    def id(self) -> str:
        """See :attr:`FakeQueryJob.id`."""
        return self._inner.id

    @property
    def info(self) -> BulkJobInfo:
        """See :attr:`FakeQueryJob.info`."""
        return self._inner.info

    async def refresh(self) -> BulkJobInfo:
        """See :meth:`FakeQueryJob.refresh`."""
        return self._inner.refresh()

    async def wait(self, *, poll: Backoff | None = None, timeout: float = 3600.0) -> BulkJobInfo:
        """See :meth:`FakeQueryJob.wait`."""
        return self._inner.wait(poll=poll, timeout=timeout)

    async def abort(self) -> BulkJobInfo:
        """See :meth:`FakeQueryJob.abort`."""
        return self._inner.abort()

    async def rows(self, *, parallel_downloads: int = 1) -> AsyncIterator[dict[str, str | None]]:
        """See :meth:`FakeQueryJob.rows`."""
        for row in self._inner.rows(parallel_downloads=parallel_downloads):
            yield row

    async def pages(
        self, *, locator: str | None = None, max_records: int | None = None
    ) -> AsyncIterator[AsyncFakeResultPage]:
        """See :meth:`FakeQueryJob.pages`."""
        for page in self._inner.pages(locator=locator, max_records=max_records):
            yield AsyncFakeResultPage(page)


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

    async def start_query(self, soql: str, *values: Any, include_deleted: bool = False) -> AsyncFakeQueryJob:
        """See :meth:`FakeBulk.start_query`."""
        return AsyncFakeQueryJob(self._inner.start_query(soql, *values, include_deleted=include_deleted))

    async def query_job(self, job_id: str) -> AsyncFakeQueryJob:
        """See :meth:`FakeBulk.query_job`."""
        return AsyncFakeQueryJob(self._inner.query_job(job_id))

    async def ingest_job(self, job_ids: Sequence[str]) -> AsyncFakeIngestJob:
        """See :meth:`FakeBulk.ingest_job`."""
        return AsyncFakeIngestJob(self._inner.ingest_job(job_ids))

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
