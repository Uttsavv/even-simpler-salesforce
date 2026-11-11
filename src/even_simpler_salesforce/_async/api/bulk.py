"""Bulk API 2.0: ``sf.bulk`` (§9.11). Ingest jobs from rows or CSV, and streamed bulk queries."""

from __future__ import annotations

import functools
import os
import tempfile
import time
from collections.abc import (
    AsyncGenerator,
    AsyncIterator,
    Callable,
    Iterable,
    Mapping,
    Sequence,
)
from typing import IO, TYPE_CHECKING, Any

from ...config import Backoff
from ...core import bulk as _bulk
from ...core.bulk import (
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
    compound_hint,
    ingest_job_body,
    parse_bulk_error,
    parse_job_info,
    parse_locator,
    parse_result_pages,
    query_job_body,
)
from ...core.files import CHUNK_SIZE
from ...core.http import RequestSpec, check_record_id, check_sobject
from ...core.soql import soql_values
from ...errors import BulkJobFailed, BulkJobTimeout, SalesforceError
from ...models import BulkFailure, BulkJobInfo, BulkJobState
from .. import _compat

if TYPE_CHECKING:
    from ..client import AsyncSalesforce

__all__ = ["AsyncBulk", "AsyncIngestJob"]

#: Result downloads ask for CSV; the Accept header must match the job's content type.
_CSV = {"Accept": "text/csv"}
#: Bytes of a spooled file (generated CSV, downloaded result page) kept in memory before it
#: moves to disk.
SPOOL_MEMORY = 1024 * 1024
DEFAULT_TIMEOUT = 3600.0


def _pread(f: IO[bytes], offset: int, size: int) -> bytes:
    f.seek(offset)
    return f.read(size)


def _open_binary(path: str) -> IO[bytes]:
    return open(path, "rb")  # closed by _Source.close


def _spool() -> IO[bytes]:
    return tempfile.SpooledTemporaryFile(max_size=SPOOL_MEMORY)


class _Source:
    """Random access to the CSV being uploaded, so each part (and each resend) reads its range."""

    def __init__(
        self, data: bytes | bytearray | memoryview | None = None, f: IO[bytes] | None = None
    ) -> None:
        self._view = memoryview(data) if data is not None else None
        self._file = f

    async def read_at(self, offset: int, size: int) -> bytes:
        if self._view is not None:
            return bytes(self._view[offset : offset + size])
        assert self._file is not None
        return await _compat.to_thread(_pread, self._file, offset, size)

    def close(self) -> None:
        if self._file is not None:
            self._file.close()


def _job_path(version: str, kind: str, job_id: str, rest: str = "") -> str:
    """A job URL in the API version that created the job (another version returns 409)."""
    return f"/services/data/v{version}/jobs/{kind}/{check_record_id(job_id)}{rest}"


def _is_query(info: BulkJobInfo) -> bool:
    return info.operation in ("query", "queryAll")


def _event(info: BulkJobInfo) -> dict[str, Any]:
    return {
        "job_id": info.id,
        "operation": info.operation,
        "sobject": info.sobject,
        "state": info.state.value,
        "records_processed": info.records_processed,
        # Query job info has no numberRecordsFailed: report None, not 0.
        "records_failed": None if _is_query(info) else info.records_failed,
    }


def _failed(info: BulkJobInfo, hint: str | None = None) -> BulkJobFailed:
    detail = f": {info.error_message}" if info.error_message else ""
    return BulkJobFailed(
        f"Bulk job {info.id} ended in state {info.state.value}{detail}",
        job_id=info.id,
        state=info.state.value,
        error_message=info.error_message,
        operation=f"bulk.{'query' if _is_query(info) else 'ingest'}",
        sobject=info.sobject,
        hint=hint,
    )


async def _read_csv(chunks: AsyncIterator[bytes]) -> AsyncIterator[dict[str, str]]:
    reader = CsvReader()
    async for chunk in chunks:
        for row in reader.feed(chunk):
            yield row
    for row in reader.finish():
        yield row


async def _file_chunks(f: IO[bytes]) -> AsyncIterator[bytes]:
    while chunk := await _compat.to_thread(f.read, CHUNK_SIZE):
        yield chunk


class _Jobs:
    """Status calls and polling shared by ingest jobs and query jobs."""

    def __init__(self, client: AsyncSalesforce, kind: str) -> None:
        self._sf = client
        self._kind = kind  # "ingest" or "query"

    async def status(self, info: BulkJobInfo) -> BulkJobInfo:
        raw = await self._sf._send(
            RequestSpec(
                "GET",
                _job_path(info.api_version, self._kind, info.id),
                operation=f"bulk.{self._kind}.status",
                sobject=info.sobject,
            )
        )
        new = parse_job_info(raw.json(), info.api_version)
        if (new.state, new.records_processed, new.records_failed) != (
            info.state,
            info.records_processed,
            info.records_failed,
        ):  # a state change or progress: long jobs report as they go
            self._sf._transport.emit("sf.bulk.job", _event(new))
        return new

    async def set_state(self, info: BulkJobInfo, state: str, operation: str) -> BulkJobInfo:
        content, ctype = self._sf._transport.encode({"state": state})
        raw = await self._sf._send(
            RequestSpec(
                "PATCH",
                _job_path(info.api_version, self._kind, info.id),
                content=content,
                content_type=ctype,
                operation=operation,
                sobject=info.sobject,
            )
        )
        new = parse_job_info(raw.json(), info.api_version)
        self._sf._transport.emit("sf.bulk.job", _event(new))
        return new

    async def wait(
        self,
        infos: list[BulkJobInfo],
        poll: Backoff | None,
        timeout: float,
        hint: Callable[[BulkJobInfo], str | None] = lambda info: None,
    ) -> None:
        """Poll every unfinished job in ``infos`` (updated in place) until all are final.

        Raises:
            BulkJobFailed: a job ended ``Failed`` or ``Aborted``.
            BulkJobTimeout: the jobs didn't finish within ``timeout`` seconds.
        """
        if not timeout > 0:
            raise ValueError("timeout must be > 0 seconds")
        poll = poll or Backoff()
        started, slept, attempt = time.monotonic(), 0.0, 0
        while True:
            for n, info in enumerate(infos):
                if not info.state.is_final:
                    infos[n] = await self.status(info)
            if all(i.state.is_final for i in infos):
                for info in infos:
                    if info.state in (BulkJobState.FAILED, BulkJobState.ABORTED):
                        raise _failed(info, hint(info))
                return
            left = timeout - max(time.monotonic() - started, slept)
            if left <= 0:
                waiting = next(i for i in infos if not i.state.is_final)
                raise BulkJobTimeout(
                    f"Bulk job {waiting.id} is still {waiting.state.value} after {timeout:g} s; "
                    "it keeps running in Salesforce",
                    job_id=waiting.id,
                    state=waiting.state.value,
                    operation=f"bulk.{self._kind}.wait",
                    sobject=waiting.sobject,
                )
            delay = min(poll.delay(attempt), left)
            attempt += 1
            await self._sf._transport.sleep(delay)
            slept += delay


class AsyncIngestJob:
    """A Bulk API 2.0 ingest job, returned by ``sf.bulk.ingest()``.

    The upload is complete when you get the job, and Salesforce processes it in the background.
    Call :meth:`wait`, then read the results. If the data was larger than 100 MB, the client
    split it over several Salesforce jobs. This object then stands for all of them: ``ids``
    lists them, and the results iterate over every job in order.
    """

    def __init__(self, client: AsyncSalesforce, infos: Sequence[BulkJobInfo]) -> None:
        self._sf = client
        self._jobs = _Jobs(client, "ingest")
        self._infos = list(infos)

    def __repr__(self) -> str:
        info = self.info
        ids = ", ".join(self.ids)
        return f"<{type(self).__name__} {ids} {info.operation} {info.sobject} {info.state.value}>"

    @property
    def ids(self) -> tuple[str, ...]:
        """The Salesforce job ids, one per part of at most 100 MB."""
        return tuple(i.id for i in self._infos)

    @property
    def info(self) -> BulkJobInfo:
        """Status from the latest call: state, record counts and timing.

        For several jobs, the counts are summed and the state is the least finished one.
        """
        return combine_infos(self._infos)

    @property
    def parts(self) -> tuple[BulkJobInfo, ...]:
        """The status of each Salesforce job, in upload order."""
        return tuple(self._infos)

    async def refresh(self) -> BulkJobInfo:
        """Get the current status of every unfinished job. Returns :attr:`info`."""
        for n, info in enumerate(self._infos):
            if not info.state.is_final:
                self._infos[n] = await self._jobs.status(info)
        return self.info

    async def wait(self, *, poll: Backoff | None = None, timeout: float = DEFAULT_TIMEOUT) -> BulkJobInfo:
        """Poll until every job is finished. Returns the final :attr:`info`.

        A job that ends ``JobComplete`` can still have failed records: read
        :meth:`failed_results` or ``info.records_failed``.

        Args:
            poll: How long to wait between status calls. Defaults to ``esf.Backoff()``
                (2 s, doubling up to 30 s).
            timeout: Seconds to wait in total.

        Raises:
            BulkJobFailed: a job ended ``Failed`` or ``Aborted``. ``unprocessed_records()``
                lists the rows that Salesforce didn't process.
            BulkJobTimeout: the jobs didn't finish in time. They keep running; call
                ``wait()`` again to keep waiting.

        Example:
            info = await job.wait(poll=esf.Backoff(start=2, max=30), timeout=3600)
        """
        await self._jobs.wait(self._infos, poll, timeout)
        return self.info

    async def abort(self) -> BulkJobInfo:
        """Abort every job that isn't finished. Salesforce doesn't process aborted jobs."""
        for n, info in enumerate(self._infos):
            if not info.state.is_final:
                self._infos[n] = await self._jobs.set_state(info, "Aborted", "bulk.ingest.abort")
        return self.info

    async def successful_results(self) -> AsyncIterator[dict[str, str]]:
        """Rows Salesforce saved: ``sf__Id``, ``sf__Created`` and the columns you uploaded.

        The rows are streamed and parsed as they arrive; their order is not the upload order.
        """
        async for row in self._results("successfulResults/"):
            yield row

    async def failed_results(self) -> AsyncIterator[BulkFailure]:
        """Rows Salesforce rejected, with the parsed ``sf__Error`` as an :class:`ErrorDetail`.

        Example:
            async for failure in job.failed_results():
                print(failure.error.error_code, failure.fields["Email"])
        """
        async for row in self._results("failedResults/"):
            error = parse_bulk_error(row.pop("sf__Error", ""))
            record_id = row.pop("sf__Id", "") or None
            yield BulkFailure(error=error, id=record_id, fields=row)

    async def unprocessed_records(self) -> AsyncIterator[dict[str, str]]:
        """Rows Salesforce didn't process because a job failed or was aborted."""
        async for row in self._results("unprocessedrecords/"):
            yield row

    async def _results(self, what: str) -> AsyncGenerator[dict[str, str], None]:
        for info in list(self._infos):
            raw = await self._sf._send(
                RequestSpec(
                    "GET",
                    _job_path(info.api_version, "ingest", info.id, f"/{what}"),
                    headers=_CSV,
                    operation="bulk.ingest.results",
                    sobject=info.sobject,
                    expect_json=False,
                    stream_response=True,
                )
            )
            stream = raw.stream
            try:
                async for row in _read_csv(stream.__aiter__()):
                    yield row
            finally:
                await stream.aclose()


class AsyncBulk:
    """Bulk API 2.0: load or read large numbers of records with asynchronous jobs.

    Uploads and query results are streamed, so memory use stays flat whatever the data size.
    """

    def __init__(self, client: AsyncSalesforce) -> None:
        self._sf = client

    # --- ingest ----------------------------------------------------------------------------

    async def ingest(
        self,
        sobject: str,
        operation: IngestOperation,
        *,
        rows: Iterable[Mapping[str, Any]] | str | os.PathLike[str] | bytes,
        external_id_field: str | None = None,
        line_ending: LineEnding = "LF",
        column_delimiter: ColumnDelimiter = "COMMA",
    ) -> AsyncIngestJob:
        """Create an ingest job, upload the data and mark the upload complete.

        Salesforce then processes the job in the background. The client splits data over
        100 MB into several jobs, as Salesforce advises. Each job gets the header row and only
        whole records.

        Args:
            sobject: The object to load, such as ``"Lead"``.
            operation: ``"insert"``, ``"update"``, ``"upsert"``, ``"delete"`` or
                ``"hardDelete"``.
            rows: The data: an iterable of dicts, the path of a CSV file, or CSV ``bytes``.
                For dicts, the keys of the first row are the columns. ``None`` (or ``""``) sets
                a field to null, and a missing key leaves the field unchanged. A CSV file must
                be UTF-8 and is streamed from disk.
            external_id_field: The external id field that matches records. Required for
                ``upsert``, not allowed otherwise.
            line_ending: ``"LF"`` or ``"CRLF"``: the line ending of your CSV, or the one the
                client writes for dict rows.
            column_delimiter: The delimiter of your CSV, or the one the client writes:
                ``"COMMA"``, ``"TAB"``, ``"PIPE"``, ``"SEMICOLON"``, ``"CARET"`` or
                ``"BACKQUOTE"``.

        Returns:
            The job. Call ``wait()`` on it, then read its results.

        Raises:
            ValueError: invalid arguments, the string ``"#N/A"`` in a row, a CSV that isn't
                UTF-8, has no data rows or uses the other line ending, or a record over 100 MB.
            SalesforceError: Salesforce rejected the job or the upload. The client aborts the
                job it was uploading. Jobs it finished before keep running; the message names
                them.

        Example:
            job = await sf.bulk.ingest("Lead", "insert", rows=[{"LastName": "Doe", "Company": "Acme"}])
        """
        check_sobject(sobject)
        check_ingest(operation, external_id_field, line_ending, column_delimiter)
        source, header, parts = await self._prepare(rows, line_ending, column_delimiter)
        done: list[BulkJobInfo] = []
        try:
            check_line_ending(header, line_ending)
            body = ingest_job_body(sobject, operation, external_id_field, line_ending, column_delimiter)
            for start, end in parts:
                info = await self._create("ingest", body, sobject, None)
                try:
                    await self._upload(info, source, header, start, end)
                    info = await _Jobs(self._sf, "ingest").set_state(
                        info, "UploadComplete", "bulk.ingest.close"
                    )
                except Exception as exc:
                    try:
                        await _Jobs(self._sf, "ingest").set_state(info, "Aborted", "bulk.ingest.abort")
                    except SalesforceError:
                        pass
                    if done and isinstance(exc, SalesforceError):
                        exc.message = (
                            f"{exc.message} (job {info.id} was aborted; jobs "
                            f"{', '.join(i.id for i in done)} were uploaded and keep running)"
                        )
                        exc.args = (exc.message,)
                    raise
                done.append(info)
        finally:
            source.close()
        return AsyncIngestJob(self._sf, done)

    async def _prepare(
        self, rows: Any, line_ending: str, column_delimiter: str
    ) -> tuple[_Source, bytes, list[tuple[int, int]]]:
        """Scan (or write) the CSV and find the parts, before anything is sent."""
        cutter = CsvCutter(_bulk.MAX_PART_BYTES)
        if isinstance(rows, (bytes, bytearray, memoryview)):
            source = _Source(data=rows)  # pyright: ignore[reportUnknownArgumentType]
            view = memoryview(rows)  # pyright: ignore[reportUnknownArgumentType]
            for i in range(0, len(view), CHUNK_SIZE):
                cutter.feed(bytes(view[i : i + CHUNK_SIZE]))
        elif isinstance(rows, (str, os.PathLike)):
            f = await _compat.to_thread(_open_binary, os.fspath(rows))  # pyright: ignore[reportUnknownArgumentType]
            source = _Source(f=f)
            try:
                async for chunk in _file_chunks(f):
                    cutter.feed(chunk)
            except BaseException:
                source.close()
                raise
        elif isinstance(rows, Iterable):
            source = await self._spool_rows(rows, cutter, line_ending, column_delimiter)  # pyright: ignore[reportUnknownArgumentType]
        else:
            raise TypeError(
                f"rows must be an iterable of dicts, a CSV path or CSV bytes, got {type(rows).__name__}"
            )
        try:
            header, parts = cutter.finish()
        except BaseException:
            source.close()
            raise
        return source, header, parts

    async def _spool_rows(
        self, rows: Iterable[Mapping[str, Any]], cutter: CsvCutter, line_ending: str, column_delimiter: str
    ) -> _Source:
        encoder = RowEncoder(column_delimiter, line_ending)
        f = _spool()
        try:
            buf = bytearray()
            for row in rows:
                if encoder.columns is None:
                    buf += encoder.header(row)
                buf += encoder.encode(row)
                if len(buf) >= CHUNK_SIZE:
                    data = bytes(buf)
                    buf.clear()
                    cutter.feed(data)
                    await _compat.to_thread(f.write, data)
            if encoder.columns is None:
                raise ValueError("rows is empty; there is nothing to upload")
            if buf:
                cutter.feed(bytes(buf))
                await _compat.to_thread(f.write, bytes(buf))
        except BaseException:
            f.close()
            raise
        return _Source(f=f)

    async def _create(
        self, kind: str, body: dict[str, Any], sobject: str | None, soql: str | None
    ) -> BulkJobInfo:
        content, ctype = self._sf._transport.encode(body)
        raw = await self._sf._send(
            RequestSpec(
                "POST",
                f"jobs/{kind}",
                content=content,
                content_type=ctype,
                operation=f"bulk.{kind}.create",
                sobject=sobject,
                soql=soql,
            )
        )
        info = parse_job_info(raw.json(), self._sf.api_version)
        self._sf._transport.emit("sf.bulk.job", _event(info))
        return info

    async def _upload(self, info: BulkJobInfo, source: _Source, header: bytes, start: int, end: int) -> None:
        async def body() -> AsyncIterator[bytes]:
            yield header
            offset = start
            while offset < end:
                data = await source.read_at(offset, min(CHUNK_SIZE, end - offset))
                if not data:
                    raise ValueError("the CSV file got shorter during the upload")
                offset += len(data)
                yield data

        await self._sf._send(
            RequestSpec(
                "PUT",
                _job_path(info.api_version, "ingest", info.id, "/batches"),
                content_type="text/csv; charset=UTF-8",
                body_stream=body,
                body_length=len(header) + end - start,
                operation="bulk.ingest.upload",
                sobject=info.sobject,
                expect_json=False,
            )
        )

    # --- query -----------------------------------------------------------------------------

    async def query(
        self,
        soql: str,
        *values: Any,
        include_deleted: bool = False,
        parallel_downloads: int = 1,
        poll: Backoff | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> AsyncIterator[dict[str, str | None]]:
        """Run a Bulk API 2.0 query job and yield every row.

        The client creates the job, polls it until it's complete, and then streams the result
        CSV. Rows are dicts with the selected fields as keys (``"Owner.Name"`` for parent
        fields). Values are strings, and empty values are ``None``.

        Args:
            soql: The query. ``{}`` placeholders are filled with ``values``, as in ``sf.query``.
            *values: Values for the ``{}`` placeholders.
            include_deleted: Use ``queryAll``: also deleted and archived records.
            parallel_downloads: With more than 1, the client downloads up to this many result
                pages at the same time and still yields rows in order. Salesforce hands out at
                most five page URLs at a time (``resultPages``, API 58.0 and later), so values
                above 5 don't download more. Pages that wait their turn are kept in temporary
                files, not in memory.
            poll: How long to wait between status calls. Defaults to ``esf.Backoff()``.
            timeout: Seconds to wait for the job to finish.

        Raises:
            ValueError: the query selects ``FIELDS(ALL)`` or ``FIELDS(CUSTOM)``, which query
                jobs don't support. The client raises it before it creates a job.
            BulkJobFailed: the job failed or was aborted. If the query selects compound
                address or geolocation fields, ``hint`` names their components.
            BulkJobTimeout: the job didn't finish in time.

        Example:
            async for row in sf.bulk.query("SELECT Id, Name FROM Lead", parallel_downloads=4):
                print(row["Name"])
        """
        if not isinstance(parallel_downloads, int) or parallel_downloads < 1:  # pyright: ignore[reportUnnecessaryIsInstance]
            raise ValueError("parallel_downloads must be an int >= 1")
        from ..client import _sobject_of  # pyright: ignore[reportPrivateUsage]

        soql = soql_values(soql, values)
        check_bulk_query(soql)
        sobject = _sobject_of(soql)

        def hint(message: str | None) -> str | None:
            describe = self._sf._transport.cached_describe(self._sf._runtime, sobject) if sobject else None
            return compound_hint(soql, describe, message)

        try:
            info = await self._create("query", query_job_body(soql, include_deleted), sobject, soql)
        except SalesforceError as exc:
            if exc.hint is None:
                exc.hint = hint(exc.message)
            raise
        infos = [info]
        await _Jobs(self._sf, "query").wait(infos, poll, timeout, lambda i: hint(i.error_message))
        info = infos[0]
        # resultPages needs API 58.0; the client supports 59.0 and later.
        rows = self._serial(info) if parallel_downloads == 1 else self._parallel(info, parallel_downloads)
        try:
            async for row in rows:
                yield {k: (v if v != "" else None) for k, v in row.items()}
        finally:
            await rows.aclose()  # closes the open result stream if you stop early

    async def _page(self, info: BulkJobInfo, path: str, params: Mapping[str, str] | None) -> Any:
        return await self._sf._send(
            RequestSpec(
                "GET",
                path,
                params=params,
                headers=_CSV,
                operation="bulk.query.results",
                sobject=info.sobject,
                expect_json=False,
                stream_response=True,
            )
        )

    async def _serial(self, info: BulkJobInfo) -> AsyncGenerator[dict[str, str], None]:
        """Follow ``Sforce-Locator`` one page at a time, parsing each page as it streams."""
        path = _job_path(info.api_version, "query", info.id, "/results")
        locator: str | None = None
        while True:
            raw = await self._page(info, path, {"locator": locator} if locator else None)
            stream = raw.stream
            try:
                async for row in _read_csv(stream.__aiter__()):
                    yield row
            finally:
                await stream.aclose()
            locator = parse_locator(raw.headers)
            if locator is None:
                return

    async def _download(self, info: BulkJobInfo, url: str) -> IO[bytes]:
        raw = await self._page(info, url, None)
        stream = raw.stream
        f = _spool()
        try:
            async for chunk in stream:
                await _compat.to_thread(f.write, chunk)
            f.seek(0)
        except BaseException:
            f.close()
            raise
        finally:
            await stream.aclose()
        return f

    async def _parallel(self, info: BulkJobInfo, n: int) -> AsyncGenerator[dict[str, str], None]:
        """Download the pages of each ``resultPages`` batch at once, then yield them in order."""
        prefix = _job_path(info.api_version, "query", info.id, "/")
        next_url: str | None = _job_path(info.api_version, "query", info.id, "/resultPages")
        while next_url is not None:
            raw = await self._sf._send(
                RequestSpec("GET", next_url, operation="bulk.query.pages", sobject=info.sobject)
            )
            urls, next_url = parse_result_pages(raw.json(), prefix)

            settled = await _compat.gather_settled(
                n, [functools.partial(self._download, info, u) for u in urls]
            )
            files = [f for f in settled if not isinstance(f, Exception)]
            try:
                for item in settled:
                    if isinstance(item, Exception):
                        raise item
                for f in files:
                    async for row in _read_csv(_file_chunks(f)):
                        yield row
            finally:
                for f in files:
                    f.close()

    # --- helpers -----------------------------------------------------------------------------

    async def expand_compound_fields(self, sobject: str, fields: Sequence[str]) -> list[str]:
        """Replace compound address and geolocation fields with their component fields.

        Bulk queries can't select compound fields. This reads the describe of ``sobject``
        (from the describe cache when fresh) and swaps each compound field for its parts.
        Other names stay as they are.

        Example:
            fields = await sf.bulk.expand_compound_fields("Account", ["Id", "BillingAddress"])
            # ['Id', 'BillingStreet', 'BillingCity', 'BillingState', ...]
        """
        if isinstance(fields, str):
            raise TypeError("fields must be a list of field names, not a string")
        describe = await self._sf.sobject(check_sobject(sobject)).describe()
        return _bulk.expand_compound_fields(describe, fields)
