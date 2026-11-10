"""Bulk API 2.0 (§9.11): job bodies, CSV encoding and parsing, upload splitting. No I/O.

Salesforce facts used here, from the *Bulk API 2.0 and Bulk API Developer Guide*, version 68.0
(https://resources.docs.salesforce.com/latest/latest/en-us/sfdc/pdf/api_asynch.pdf; HTML pages
under https://developer.salesforce.com/docs/atlas.en-us.api_asynch.meta/api_asynch/), read
2026-11-10:

- *Prepare CSV Files*: the first row lists the field names. Files must be UTF-8. "Empty field
  values are ignored when you update records. To set a field value to null, use a field value
  of #N/A." Values that hold the delimiter, a line ending or a double quote are wrapped in
  double quotes. Field values aren't trimmed. Delimiters: ``BACKQUOTE``, ``CARET``, ``COMMA``
  (default), ``PIPE``, ``SEMICOLON``, ``TAB``; line endings ``LF`` (default) or ``CRLF``. The
  header row can hold up to 32,000 characters.
- *Upload Job Data* (``PUT jobs/ingest/{id}/batches``): "A request can provide CSV data that
  does not in total exceed 150 MB of base64 encoded content ... upload data that does not
  exceed 100 MB." There is one upload per job, so larger data needs more than one job.
- *Create a Job*: ``object``, ``operation`` (``insert``, ``delete``, ``hardDelete``,
  ``update``, ``upsert``), ``externalIdFieldName`` (required for upsert), ``contentType``
  (``CSV``), ``lineEnding``, ``columnDelimiter``. The response has ``apiVersion`` (a JSON
  number in the examples, such as ``68.0``).
- *Job States*: ``Open``, ``UploadComplete``, ``InProgress``, ``JobComplete``, ``Failed``,
  ``Aborted``.
- *Get Job Successful/Failed/Unprocessed Record Results*: CSV with ``sf__Id`` and
  ``sf__Created`` (successful), ``sf__Id`` and ``sf__Error`` (failed), then the uploaded
  columns. The Bulk API 1.0 result example shows the error format
  ``REQUIRED_FIELD_MISSING:Required fields are missing: [LastName]:LastName --``.
- *Get Results for a Query Job*: ``Sforce-Locator`` names the next set of results; on the last
  set its value is the string ``null``. "Use the same API version to get query results that
  you used to create the query. Otherwise, the call returns a 409 error."
- *Get Parallel Results for a Query Job* (``GET jobs/query/{id}/resultPages``, API 58.0+):
  ``resultPages[].resultUrl``, ``nextRecordsUrl`` and ``done``; up to five URLs per response.
- *Understanding Bulk API 2.0 Query*: queries can't include compound address or geolocation
  fields. Query the components instead.
"""

from __future__ import annotations

import codecs
import csv
import decimal
import io
import math
import re
from collections.abc import Iterable, Mapping, Sequence
from datetime import date, datetime
from typing import Any, Literal, cast

from ..errors import ErrorDetail, UnexpectedResponse
from ..models import BulkJobInfo, BulkJobState

__all__ = [
    "COLUMN_DELIMITERS",
    "INGEST_OPERATIONS",
    "LINE_ENDINGS",
    "MAX_PART_BYTES",
    "NULL",
    "ColumnDelimiter",
    "CsvCutter",
    "CsvReader",
    "IngestOperation",
    "LineEnding",
    "RowEncoder",
    "check_ingest",
    "check_line_ending",
    "combine_infos",
    "compound_hint",
    "csv_cell",
    "expand_compound_fields",
    "ingest_job_body",
    "parse_bulk_error",
    "parse_job_info",
    "parse_job_state",
    "parse_locator",
    "parse_result_pages",
    "has_result_pages",
    "query_job_body",
    "select_fields",
]

#: Raw CSV bytes per ingest job, header included. Salesforce takes 150 MB after base64, which
#: is about 100 MB before it.
MAX_PART_BYTES = 100 * 1024 * 1024
#: Longest header row the cutter keeps in memory (Salesforce allows 32,000 characters).
MAX_HEADER_BYTES = 256 * 1024
#: The cell value that sets a field to null.
NULL = "#N/A"

IngestOperation = Literal["insert", "update", "upsert", "delete", "hardDelete"]
LineEnding = Literal["LF", "CRLF"]
ColumnDelimiter = Literal["BACKQUOTE", "CARET", "COMMA", "PIPE", "SEMICOLON", "TAB"]

INGEST_OPERATIONS: tuple[str, ...] = ("insert", "update", "upsert", "delete", "hardDelete")
LINE_ENDINGS: Mapping[str, str] = {"LF": "\n", "CRLF": "\r\n"}
COLUMN_DELIMITERS: Mapping[str, str] = {
    "BACKQUOTE": "`",
    "CARET": "^",
    "COMMA": ",",
    "PIPE": "|",
    "SEMICOLON": ";",
    "TAB": "\t",
}
_BOM = codecs.BOM_UTF8
_ORDER = {
    BulkJobState.OPEN: 0,
    BulkJobState.UPLOAD_COMPLETE: 1,
    BulkJobState.IN_PROGRESS: 2,
    BulkJobState.JOB_COMPLETE: 3,
    BulkJobState.ABORTED: 4,
    BulkJobState.FAILED: 5,
}


# --- job requests --------------------------------------------------------------------------


def check_ingest(
    operation: str, external_id_field: str | None, line_ending: str, column_delimiter: str
) -> None:
    """Validate the arguments of an ingest job before anything is sent.

    Raises:
        ValueError: unknown operation, line ending or delimiter, or an external id field that
            is missing for ``upsert`` or given for another operation.
    """
    if operation not in INGEST_OPERATIONS:
        raise ValueError(f"operation must be one of {', '.join(INGEST_OPERATIONS)}; got {operation!r}")
    if operation == "upsert" and not external_id_field:
        raise ValueError("upsert needs external_id_field")
    if operation != "upsert" and external_id_field is not None:
        raise ValueError("external_id_field is only used by upsert")
    if line_ending not in LINE_ENDINGS:
        raise ValueError(f"line_ending must be 'LF' or 'CRLF', got {line_ending!r}")
    if column_delimiter not in COLUMN_DELIMITERS:
        raise ValueError(
            f"column_delimiter must be one of {', '.join(COLUMN_DELIMITERS)}; got {column_delimiter!r}"
        )


def ingest_job_body(
    sobject: str,
    operation: str,
    external_id_field: str | None,
    line_ending: str,
    column_delimiter: str,
) -> dict[str, Any]:
    """Request body of ``POST jobs/ingest``."""
    body: dict[str, Any] = {
        "object": sobject,
        "operation": operation,
        "contentType": "CSV",
        "lineEnding": line_ending,
        "columnDelimiter": column_delimiter,
    }
    if external_id_field is not None:
        body["externalIdFieldName"] = external_id_field
    return body


def query_job_body(soql: str, include_deleted: bool) -> dict[str, Any]:
    """Request body of ``POST jobs/query`` (``queryAll`` when ``include_deleted``)."""
    return {
        "operation": "queryAll" if include_deleted else "query",
        "query": soql,
        "contentType": "CSV",
        "columnDelimiter": "COMMA",
        "lineEnding": "LF",
    }


# --- CSV encoding ------------------------------------------------------------------------


def csv_cell(value: Any) -> str:
    """The CSV text for one value of a row dict.

    ``None`` and ``""`` become ``#N/A`` (set the field to null). Booleans become
    ``true``/``false``; dates and datetimes use ISO 8601, like JSON bodies.

    Raises:
        ValueError: the string ``"#N/A"`` (ambiguous: pass ``None`` to set null) or a NaN or
            infinite number.
        TypeError: a value that has no CSV form (a dict, a list, bytes, ...).
    """
    if value is None or value == "":
        return NULL
    if isinstance(value, str):
        if value == NULL:
            raise ValueError("the string '#N/A' is ambiguous in Bulk CSV; pass None to set a field to null")
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{value!r} can't be sent to Salesforce")
        return repr(value)
    if isinstance(value, decimal.Decimal):
        if not value.is_finite():
            raise ValueError(f"{value!r} can't be sent to Salesforce")
        return format(value, "f")
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    raise TypeError(f"Bulk CSV has no form for {type(value).__name__} values")


class RowEncoder:
    """Turns row dicts into CSV lines.

    The keys of the first row are the columns. A later row may leave a column out (its cell is
    empty, so the field stays unchanged), but it can't add one.
    """

    def __init__(self, column_delimiter: str = "COMMA", line_ending: str = "LF") -> None:
        self._buf = io.StringIO()
        self._writer = csv.writer(
            self._buf,
            delimiter=COLUMN_DELIMITERS[column_delimiter],
            lineterminator=LINE_ENDINGS[line_ending],
            quoting=csv.QUOTE_MINIMAL,
        )
        self.columns: tuple[str, ...] | None = None
        self._known: frozenset[str] = frozenset()

    def _line(self, cells: Iterable[str]) -> bytes:
        self._buf.seek(0)
        self._buf.truncate()
        self._writer.writerow(cells)
        return self._buf.getvalue().encode("utf-8")

    def header(self, row: Mapping[str, Any]) -> bytes:
        """Set the columns from ``row`` and return the header line."""
        if not isinstance(row, Mapping):  # pyright: ignore[reportUnnecessaryIsInstance]
            raise TypeError(f"rows must be dicts, got {type(row).__name__}")
        columns = tuple(row)
        if not columns:
            raise ValueError("the first row has no fields")
        for name in columns:
            if not isinstance(name, str) or not name.strip():  # pyright: ignore[reportUnnecessaryIsInstance]
                raise ValueError(f"field names must be non-empty strings, got {name!r}")
        self.columns = columns
        self._known = frozenset(columns)
        return self._line(columns)

    def encode(self, row: Mapping[str, Any]) -> bytes:
        """One CSV line for ``row``. Call :meth:`header` first."""
        if not isinstance(row, Mapping):  # pyright: ignore[reportUnnecessaryIsInstance]
            raise TypeError(f"rows must be dicts, got {type(row).__name__}")
        assert self.columns is not None, "call header() first"
        extra = [k for k in row if k not in self._known]
        if extra:
            raise ValueError(
                f"field {extra[0]!r} is not in the first row; the first row sets the columns, "
                "so put every field in it (use None to set null)"
            )
        return self._line([csv_cell(row[c]) if c in row else "" for c in self.columns])


def check_line_ending(header: bytes, line_ending: str) -> None:
    """Check that the header line of a CSV upload ends the way ``line_ending`` says.

    Raises:
        ValueError: the file uses the other line ending (Salesforce would reject every row).
    """
    crlf = header.endswith(b"\r\n")
    if line_ending == "LF" and crlf:
        raise ValueError("the CSV uses CRLF line endings; pass line_ending='CRLF'")
    if line_ending == "CRLF" and not crlf and header.endswith(b"\n"):
        raise ValueError("the CSV uses LF line endings; pass line_ending='LF'")


# --- splitting uploads -------------------------------------------------------------------


def _last_boundary(data: bytes, lo: int, hi: int, quoted_at_0: int) -> int:
    """The last record end in ``data`` in ``(lo, hi]``, or ``-1``.

    A record ends after a newline that is not inside a quoted field. ``quoted_at_0`` is 1 when
    ``data[0]`` lies inside a quoted field.
    """
    parity = (quoted_at_0 + data.count(b'"', 0, hi)) & 1
    p = hi
    while True:
        q = data.rfind(b"\n", lo, p)
        if q < 0:
            return -1
        parity ^= data.count(b'"', q + 1, p) & 1
        if parity == 0:
            return q + 1
        p = q


class CsvCutter:
    """Finds where to split a CSV upload into parts of at most ``limit`` bytes.

    Feed it the file in chunks. It checks that the file is UTF-8 and keeps only the header row
    in memory. Every part repeats the header and ends at a record boundary, so a quoted value
    with a line break is never cut.
    """

    def __init__(self, limit: int = MAX_PART_BYTES) -> None:
        self.limit = limit
        self._decoder = codecs.getincrementaldecoder("utf-8")()
        self._pos = 0  # absolute offset of the next chunk
        self._quoted = 0  # 1 when the next chunk starts inside a quoted field
        self._head = bytearray()
        self._header_end: int | None = None
        self._part_start = 0
        self._last = 0
        self._parts: list[tuple[int, int]] = []

    @property
    def header(self) -> bytes:
        """The header row with its line ending (no byte order mark)."""
        start = len(_BOM) if self._head.startswith(_BOM) else 0
        return bytes(self._head[start : self._header_end])

    def _check_utf8(self, chunk: bytes, final: bool = False) -> None:
        try:
            self._decoder.decode(chunk, final)
        except UnicodeDecodeError as exc:
            raise ValueError(f"the CSV is not UTF-8 (near byte {self._pos + exc.start})") from None

    def feed(self, chunk: bytes) -> None:
        """Scan the next chunk of the file."""
        if not chunk:
            return
        self._check_utf8(chunk)
        base, i = self._pos, 0
        if self._header_end is None:
            j = i
            while True:
                j = chunk.find(b"\n", j)
                if j < 0:
                    break
                if (self._quoted + chunk.count(b'"', 0, j)) & 1 == 0:
                    break
                j += 1
            end = len(chunk) if j < 0 else j + 1
            self._head += chunk[:end]
            if len(self._head) > MAX_HEADER_BYTES:
                raise ValueError("the CSV header row is too long (Salesforce allows 32,000 characters)")
            if j < 0:
                self._finish_chunk(chunk)
                return
            self._header_end = self._part_start = self._last = base + end
            i = end
        room = self.limit - len(self.header)
        if room <= 0:
            raise ValueError("the CSV header row alone is larger than the upload limit")
        while True:
            cap = self._part_start + room - base  # the part may end at or before this index
            if cap >= len(chunk):
                b = _last_boundary(chunk, i, len(chunk), self._quoted)
                if b > 0:
                    self._last = base + b
                break
            b = _last_boundary(chunk, i, cap, self._quoted) if cap > i else -1
            if b > 0:
                self._cut(base + b)
                i = b
            elif self._last > self._part_start:
                self._cut(self._last)
            else:
                raise ValueError(f"a CSV record is larger than the upload limit of {self.limit} bytes")
        self._finish_chunk(chunk)

    def _cut(self, at: int) -> None:
        self._parts.append((self._part_start, at))
        self._part_start = self._last = at

    def _finish_chunk(self, chunk: bytes) -> None:
        self._quoted = (self._quoted + chunk.count(b'"')) & 1
        self._pos += len(chunk)

    def finish(self) -> tuple[bytes, list[tuple[int, int]]]:
        """The header row and the byte ranges of the data rows of each part.

        Part ``n`` is ``header + data[start:end]``.

        Raises:
            ValueError: not UTF-8, an unclosed quote, no data rows, or a record over the limit.
        """
        self._check_utf8(b"", final=True)
        if self._quoted:
            raise ValueError("the CSV ends inside a quoted value")
        if self._header_end is None:
            raise ValueError("the CSV has no data rows")
        # feed() keeps the open part within the limit, so a last record without a line ending
        # fits into it.
        self._last = self._pos
        if self._last > self._part_start:
            self._parts.append((self._part_start, self._last))
        if not self._parts:
            raise ValueError("the CSV has no data rows")
        return self.header, list(self._parts)


# --- CSV parsing -------------------------------------------------------------------------


class CsvReader:
    """Parses a CSV response that arrives in chunks into row dicts.

    The body is always decoded as UTF-8, whatever charset the response names. Only the
    incomplete last record of a chunk is kept between chunks.
    """

    def __init__(self, delimiter: str = ",") -> None:
        self._delimiter = delimiter
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._buf = ""
        self._quoted = 0
        self.header: list[str] | None = None

    def _rows(self, text: str) -> list[dict[str, str]]:
        try:
            records = list(csv.reader(io.StringIO(text, newline=""), delimiter=self._delimiter, strict=True))
        except csv.Error as exc:
            raise UnexpectedResponse(f"malformed CSV response: {exc}", raw=text[:4096]) from None
        out: list[dict[str, str]] = []
        for record in records:
            if self.header is None:
                if record and record[0].startswith("﻿"):
                    record[0] = record[0][1:]
                self.header = record
                continue
            if not record:
                continue
            if len(record) != len(self.header):
                raise UnexpectedResponse(
                    f"CSV row has {len(record)} values for {len(self.header)} columns",
                    raw=text[:4096],
                )
            out.append(dict(zip(self.header, record, strict=True)))
        return out

    def feed(self, data: bytes) -> list[dict[str, str]]:
        """Rows completed by this chunk."""
        text = self._decoder.decode(data)
        if not text:
            return []
        end = -1
        parity = (self._quoted + text.count('"')) & 1
        p = len(text)
        while True:
            q = text.rfind("\n", 0, p)
            if q < 0:
                break
            parity ^= text.count('"', q + 1, p) & 1
            if parity == 0:
                end = q + 1
                break
            p = q
        if end < 0:
            self._buf += text
            self._quoted = (self._quoted + text.count('"')) & 1
            return []
        complete, self._buf = self._buf + text[:end], text[end:]
        self._quoted = self._buf.count('"') & 1
        return self._rows(complete)

    def finish(self) -> list[dict[str, str]]:
        """Rows of a last record without a line ending.

        Raises:
            UnexpectedResponse: the body ends inside a quoted value (it was cut off).
        """
        self._buf += self._decoder.decode(b"", True)
        if self._quoted:
            raise UnexpectedResponse("the CSV response ends inside a quoted value", raw=self._buf[:4096])
        rest, self._buf = self._buf, ""
        return self._rows(rest) if rest.strip() else []


# --- responses -----------------------------------------------------------------------------

_ERROR = re.compile(r"^(?P<code>[A-Z][A-Z0-9_]*):(?P<rest>.*)$", re.S)
_FIELD_LIST = re.compile(r"[A-Za-z][A-Za-z0-9_.]*(?:,\s*[A-Za-z][A-Za-z0-9_.]*)*")


def parse_bulk_error(text: str) -> ErrorDetail:
    """Parse an ``sf__Error`` cell (``CODE:message:Field1,Field2 --``) into an :class:`ErrorDetail`.

    A cell that doesn't start with a status code keeps its whole text as the message.
    """
    t = text.strip()
    if t.endswith("--"):
        t = t[:-2].rstrip()
    m = _ERROR.match(t)
    if not m:
        return ErrorDetail(None, t)
    rest = m.group("rest")
    fields: tuple[str, ...] = ()
    head, sep, tail = rest.rpartition(":")
    if sep and (tail == "" or _FIELD_LIST.fullmatch(tail)):
        rest = head
        fields = tuple(f.strip() for f in tail.split(",") if f.strip())
    return ErrorDetail(m.group("code"), rest.strip(), fields)


def parse_job_state(value: Any, job_id: str | None = None) -> BulkJobState:
    """The :class:`BulkJobState` for ``value``.

    Raises:
        UnexpectedResponse: a state this library doesn't know. Polling it could never end.
    """
    try:
        return BulkJobState(value)
    except ValueError:
        job = f"Bulk job {job_id}" if job_id else "Bulk job"
        raise UnexpectedResponse(f"{job} has an unknown state {value!r}", raw=repr(value)[:200]) from None


def _int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return int(value)
    return None


def _when(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None


def _version(value: Any, fallback: str) -> str:
    if isinstance(value, bool):
        return fallback
    if isinstance(value, (int, float)):
        return f"{float(value):.1f}"
    if isinstance(value, str) and re.fullmatch(r"\d{2,3}\.\d", value):
        return value
    return fallback


def parse_job_info(body: Any, fallback_version: str) -> BulkJobInfo:
    """Turn a job info response into a :class:`BulkJobInfo`.

    Args:
        body: The decoded JSON body.
        fallback_version: The API version of the request, used when the body has none.

    Raises:
        UnexpectedResponse: no job id, or an unknown state.
    """
    if not isinstance(body, dict) or not body.get("id"):  # pyright: ignore[reportUnknownMemberType]
        raise UnexpectedResponse("Bulk job info has no id", raw=repr(body)[:4096])  # pyright: ignore[reportUnknownArgumentType]
    data = cast("dict[str, Any]", body)
    job_id = str(data["id"])

    def text(key: str) -> str | None:
        v = data.get(key)
        return str(v) if v is not None and v != "" else None

    return BulkJobInfo(
        id=job_id,
        operation=text("operation"),
        sobject=text("object"),
        state=parse_job_state(data.get("state"), job_id),
        api_version=_version(data.get("apiVersion"), fallback_version),
        records_processed=_int(data.get("numberRecordsProcessed")) or 0,
        records_failed=_int(data.get("numberRecordsFailed")) or 0,
        created_date=_when(data.get("createdDate")),
        system_modstamp=_when(data.get("systemModstamp")),
        total_processing_ms=_int(data.get("totalProcessingTime")),
        error_message=text("errorMessage"),
        raw=data,
    )


def combine_infos(infos: Sequence[BulkJobInfo]) -> BulkJobInfo:
    """One :class:`BulkJobInfo` for an ingest split over several jobs.

    Counts and processing times are summed. The state is ``Failed`` or ``Aborted`` if any job
    has it, ``JobComplete`` when all are complete, and otherwise the state of the job that is
    furthest behind.
    """
    if len(infos) == 1:
        return infos[0]
    states = [i.state for i in infos]
    final = [s for s in states if s in (BulkJobState.FAILED, BulkJobState.ABORTED)]
    if final:
        state = max(final, key=_ORDER.__getitem__)
    elif all(s is BulkJobState.JOB_COMPLETE for s in states):
        state = BulkJobState.JOB_COMPLETE
    else:
        state = min((s for s in states if not s.is_final), key=_ORDER.__getitem__)
    times = [i.total_processing_ms for i in infos if i.total_processing_ms is not None]
    created = [i.created_date for i in infos if i.created_date is not None]
    changed = [i.system_modstamp for i in infos if i.system_modstamp is not None]
    first = infos[0]
    return BulkJobInfo(
        id=first.id,
        operation=first.operation,
        sobject=first.sobject,
        state=state,
        api_version=first.api_version,
        records_processed=sum(i.records_processed for i in infos),
        records_failed=sum(i.records_failed for i in infos),
        created_date=min(created) if created else None,
        system_modstamp=max(changed) if changed else None,
        total_processing_ms=sum(times) if times else None,
        error_message=next((i.error_message for i in infos if i.error_message), None),
    )


def parse_locator(headers: Mapping[str, str]) -> str | None:
    """The ``Sforce-Locator`` of a query results page, or ``None`` after the last page."""
    value = headers.get("sforce-locator")
    if value is None or value.strip() in ("", "null"):
        return None
    return value.strip()


#: First API version with ``GET jobs/query/{id}/resultPages``.
RESULT_PAGES_VERSION = 58.0


def has_result_pages(api_version: str) -> bool:
    """Whether ``api_version`` offers the ``resultPages`` resource for parallel downloads."""
    try:
        return float(api_version) >= RESULT_PAGES_VERSION
    except ValueError:
        return False


def parse_result_pages(body: Any, prefix: str) -> tuple[list[str], str | None]:
    """The result URLs and the next ``resultPages`` URL of a ``resultPages`` response.

    Args:
        body: The decoded JSON body.
        prefix: ``/services/data/vXX.X/jobs/query/{id}/`` of the job. Every URL must start with
            it, so a response can't send the client (and its token) somewhere else.

    Raises:
        UnexpectedResponse: a malformed body or a URL outside the job.
    """
    if not isinstance(body, dict):
        raise UnexpectedResponse("unexpected resultPages response", raw=repr(body)[:4096])
    data = cast("dict[str, Any]", body)
    pages: Any = data.get("resultPages")
    if not isinstance(pages, list):
        raise UnexpectedResponse("resultPages response has no resultPages list", raw=repr(data)[:4096])

    def checked(url: Any) -> str:
        if not isinstance(url, str) or not url.lower().startswith(prefix.lower()) or ".." in url:
            raise UnexpectedResponse("resultPages URL is not a result of this job", raw=repr(url)[:500])
        return url

    urls = [
        checked(cast("dict[str, Any]", p).get("resultUrl") if isinstance(p, dict) else None)
        for p in cast("list[Any]", pages)
    ]
    # The reference names the field ``nextRecordUrl``; its example shows ``nextRecordsUrl``.
    nxt: Any = data.get("nextRecordsUrl") or data.get("nextRecordUrl")
    done = data.get("done")
    if done is True or not nxt:
        return urls, None
    return urls, checked(nxt)


# --- compound fields -----------------------------------------------------------------------

_SELECT = re.compile(r"^\s*SELECT\s+(?P<fields>.+?)\s+FROM\s", re.I | re.S)
_COMPOUND_TYPES = frozenset({"address", "location"})


def select_fields(soql: str) -> list[str]:
    """The field names in the top-level ``SELECT`` list of ``soql`` (best effort)."""
    m = _SELECT.match(soql)
    if not m:
        return []
    return [f.strip() for f in m.group("fields").split(",") if f.strip()]


def _components(describe: Mapping[str, Any]) -> dict[str, list[str]]:
    fields: Any = describe.get("fields")
    if not isinstance(fields, list):
        return {}
    rows = [cast("dict[str, Any]", f) for f in cast("list[Any]", fields) if isinstance(f, dict)]
    compound = {str(f["name"]) for f in rows if f.get("type") in _COMPOUND_TYPES and f.get("name")}
    out: dict[str, list[str]] = {name: [] for name in compound}
    for f in rows:
        parent = f.get("compoundFieldName")
        if parent in out and f.get("name"):
            out[str(parent)].append(str(f["name"]))
    return out


def expand_compound_fields(describe: Mapping[str, Any], fields: Iterable[str]) -> list[str]:
    """Replace compound address and geolocation fields in ``fields`` with their components.

    Other names are kept as they are. The result has no duplicates and keeps the order.
    """
    parts = {k.lower(): v for k, v in _components(describe).items()}
    out: list[str] = []
    seen: set[str] = set()
    for name in fields:
        for item in parts.get(name.lower()) or [name]:
            if item.lower() not in seen:
                seen.add(item.lower())
                out.append(item)
    return out


def compound_hint(soql: str, describe: Mapping[str, Any] | None, error_message: str | None) -> str | None:
    """A hint for a Bulk query that failed because it selects compound fields.

    Uses only the cached describe of the object. Returns ``None`` when nothing points at
    compound fields.
    """
    if describe is not None:
        parts = {k.lower(): (k, v) for k, v in _components(describe).items()}
        found = [parts[f.lower()] for f in select_fields(soql) if f.lower() in parts]
        if found:
            named = "; ".join(f"{name} -> {', '.join(comps)}" for name, comps in found)
            return (
                f"Bulk queries can't read compound fields. Select the components instead: {named}. "
                "sf.bulk.expand_compound_fields() rewrites a field list for you"
            )
    if error_message and "compound" in error_message.lower():
        return (
            "Bulk queries can't read compound address or geolocation fields. Select their components, "
            "for example with sf.bulk.expand_compound_fields()"
        )
    return None
