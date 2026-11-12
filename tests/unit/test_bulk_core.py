"""``core/bulk.py``: Bulk API 2.0 job bodies, CSV encoding, upload splitting and parsing. No I/O.

Salesforce behaviour relied on, from the *Bulk API 2.0 and Bulk API Developer Guide*:

- *Prepare CSV Files*
  (https://developer.salesforce.com/docs/atlas.en-us.api_asynch.meta/api_asynch/datafiles_prepare_csv.htm):
  UTF-8 only; the first row names the fields; "Empty field values are ignored when you update
  records. To set a field value to null, use a field value of #N/A"; values with the delimiter,
  a line break or a double quote are quoted. Delimiters ``BACKQUOTE``, ``CARET``, ``COMMA``,
  ``PIPE``, ``SEMICOLON``, ``TAB``; line endings ``LF`` and ``CRLF``.
- *Upload Job Data*
  (https://developer.salesforce.com/docs/atlas.en-us.api_asynch.meta/api_asynch/upload_job_data.htm):
  at most 150 MB of base64 content per request; "upload data that does not exceed 100 MB".
- *Job States* and *Get Job Info*
  (https://developer.salesforce.com/docs/atlas.en-us.api_asynch.meta/api_asynch/get_job_info.htm).
- *Get Results for a Query Job*
  (https://developer.salesforce.com/docs/atlas.en-us.api_asynch.meta/api_asynch/query_get_job_results.htm):
  ``Sforce-Locator`` is the string ``null`` on the last page.
- *Get Parallel Results for a Query Job*
  (https://developer.salesforce.com/docs/atlas.en-us.api_asynch.meta/api_bulk_v2/query_get_parallel_job_results.htm):
  API 58.0 and later; ``resultPages[].resultUrl``, ``nextRecordsUrl`` (``nextRecordUrl`` in the
  field table) and ``done``.
- *Bulk API 2.0 Query* considerations: no compound address or geolocation fields.

The ``sf__Error`` cell format (``CODE:message:Fields --``) is from the failed-results example;
it is not specified field by field (backlog V41). Job bodies in this file are **synthetic**.
"""

from __future__ import annotations

import csv
import io
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

import even_simpler_salesforce as esf
from even_simpler_salesforce.core import bulk as B

JOB = "750xx0000000001AAA"

# --- job requests ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (("merge", None, "LF", "COMMA"), "operation must be one of"),
        (("upsert", None, "LF", "COMMA"), "upsert needs external_id_field"),
        (("insert", "Ext__c", "LF", "COMMA"), "only used by upsert"),
        (("insert", None, "CR", "COMMA"), "line_ending"),
        (("insert", None, "LF", "COLON"), "column_delimiter"),
    ],
)
def test_check_ingest_rejects(args: tuple[str, Any, str, str], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        B.check_ingest(*args)


def test_check_ingest_accepts() -> None:
    B.check_ingest("upsert", "Ext__c", "CRLF", "TAB")
    B.check_ingest("hardDelete", None, "LF", "COMMA")


def test_job_bodies() -> None:
    assert B.ingest_job_body("Lead", "insert", None, "LF", "COMMA") == {
        "object": "Lead",
        "operation": "insert",
        "contentType": "CSV",
        "lineEnding": "LF",
        "columnDelimiter": "COMMA",
    }
    assert B.ingest_job_body("Lead", "upsert", "Ext__c", "CRLF", "PIPE")["externalIdFieldName"] == "Ext__c"
    assert B.query_job_body("SELECT Id FROM Lead", False)["operation"] == "query"
    body = B.query_job_body("SELECT Id FROM Lead", True)
    assert body == {
        "operation": "queryAll",
        "query": "SELECT Id FROM Lead",
        "contentType": "CSV",
        "columnDelimiter": "COMMA",
        "lineEnding": "LF",
    }


# --- CSV encoding ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "cell"),
    [
        (None, "#N/A"),
        ("", "#N/A"),
        ("Doe", "Doe"),
        (" padded ", " padded "),
        (True, "true"),
        (False, "false"),
        (42, "42"),
        (1.5, "1.5"),
        (Decimal("12.3400"), "12.3400"),
        (Decimal("1E+3"), "1000"),
        (date(2026, 1, 2), "2026-01-02"),
        (datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc), "2026-01-02T03:04:05+00:00"),
    ],
)
def test_csv_cell(value: Any, cell: str) -> None:
    assert B.csv_cell(value) == cell


@pytest.mark.parametrize("value", ["#N/A", float("nan"), float("inf"), Decimal("NaN"), Decimal("-Infinity")])
def test_csv_cell_rejects_ambiguous_and_non_finite(value: Any) -> None:
    with pytest.raises(ValueError):
        B.csv_cell(value)


@pytest.mark.parametrize("value", [{"a": 1}, [1], b"x", object()])
def test_csv_cell_rejects_values_without_csv_form(value: Any) -> None:
    with pytest.raises(TypeError, match="no form"):
        B.csv_cell(value)


def test_row_encoder_nulls_missing_keys_and_quoting() -> None:
    """None sets null (#N/A); a missing key leaves the field unchanged (empty cell)."""
    enc = B.RowEncoder()
    assert enc.header({"Id": 1, "Description": 2, "Phone": 3}) == b"Id,Description,Phone\n"
    assert enc.columns == ("Id", "Description", "Phone")
    assert enc.encode({"Id": "00Q1", "Description": None}) == b"00Q1,#N/A,\n"
    line = enc.encode({"Id": "00Q2", "Description": 'a,"b"\nc', "Phone": "1"})
    assert line == b'00Q2,"a,""b""\nc",1\n'
    assert next(csv.reader(io.StringIO(line.decode(), newline=""))) == ["00Q2", 'a,"b"\nc', "1"]
    assert enc.encode({"Id": "00Q3", "Description": "Zoë 日本"}) == "00Q3,Zoë 日本,\n".encode()


def test_row_encoder_delimiter_and_line_ending() -> None:
    enc = B.RowEncoder("TAB", "CRLF")
    assert enc.header({"A": 1, "B": 2}) == b"A\tB\r\n"
    assert enc.encode({"A": "x\ty", "B": "z"}) == b'"x\ty"\tz\r\n'


def test_row_encoder_rejects_bad_rows() -> None:
    enc = B.RowEncoder()
    with pytest.raises(TypeError, match="must be dicts"):
        enc.header(["A"])  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="no fields"):
        enc.header({})
    with pytest.raises(ValueError, match="non-empty strings"):
        enc.header({" ": 1})
    with pytest.raises(ValueError, match="non-empty strings"):
        enc.header({1: 1})  # type: ignore[dict-item]
    enc.header({"A": 1})
    with pytest.raises(TypeError, match="must be dicts"):
        enc.encode(("A",))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="'B' is not in the first row"):
        enc.encode({"A": 1, "B": 2})


def test_check_line_ending() -> None:
    B.check_line_ending(b"A,B\n", "LF")
    B.check_line_ending(b"A,B\r\n", "CRLF")
    B.check_line_ending(b"A,B", "CRLF")  # one line without a line ending: nothing to compare
    with pytest.raises(ValueError, match="line_ending='CRLF'"):
        B.check_line_ending(b"A,B\r\n", "LF")
    with pytest.raises(ValueError, match="line_ending='LF'"):
        B.check_line_ending(b"A,B\n", "CRLF")


# --- splitting uploads ---------------------------------------------------------------------------


def _cut(data: bytes, limit: int, chunk: int) -> tuple[bytes, list[bytes]]:
    cutter = B.CsvCutter(limit)
    for i in range(0, len(data), chunk):
        cutter.feed(data[i : i + chunk])
    cutter.feed(b"")
    header, ranges = cutter.finish()
    body = data.removeprefix(b"\xef\xbb\xbf")
    offset = len(data) - len(body)
    return header, [header + body[s - offset : e - offset] for s, e in ranges]


def _records(part: bytes) -> list[list[str]]:
    return list(csv.reader(io.StringIO(part.decode(), newline="")))


cell = st.text(alphabet=st.sampled_from(list('ab,"\n é')), max_size=12)


@settings(max_examples=300, deadline=None)
@given(
    rows=st.lists(st.lists(cell, min_size=2, max_size=2), min_size=1, max_size=40),
    limit=st.integers(min_value=64, max_value=400),
    chunk=st.integers(min_value=1, max_value=97),
    final_newline=st.booleans(),
)
def test_cutter_never_loses_duplicates_or_cuts_records(
    rows: list[list[str]], limit: int, chunk: int, final_newline: bool
) -> None:
    """§15.1 chunker guard: parts are within the limit, start with the header, end on a record
    boundary, and together hold every row exactly once in order."""
    enc = B.RowEncoder()
    data = enc.header({"A": 1, "B": 2}) + b"".join(enc.encode({"A": a, "B": b}) for a, b in rows)
    if not final_newline:
        data = data.removesuffix(b"\n")
    longest = max(len(enc.encode({"A": a, "B": b})) for a, b in rows)
    if longest + len(b"A,B\n") > limit:
        with pytest.raises(ValueError, match="larger than the upload limit"):
            _cut(data, limit, chunk)
        return
    header, parts = _cut(data, limit, chunk)
    assert header == b"A,B\n"
    seen: list[list[str]] = []
    for part in parts:
        assert len(part) <= limit
        assert part.startswith(header)
        records = _records(part)
        assert records[0] == ["A", "B"]
        assert len(records) > 1
        seen += records[1:]
    expected = [[a if a != "" else "#N/A", b if b != "" else "#N/A"] for a, b in rows]
    assert seen == expected


def test_cutter_splits_at_the_limit_and_keeps_the_header() -> None:
    data = b"Name\n" + b"".join(b"row%03d\n" % i for i in range(30))  # 7 bytes per record
    header, parts = _cut(data, limit=5 + 7 * 10, chunk=13)
    assert header == b"Name\n"
    assert [len(p) for p in parts] == [75, 75, 75]
    assert parts[1] == b"Name\n" + b"".join(b"row%03d\n" % i for i in range(10, 20))


def test_cutter_strips_bom_and_reads_a_header_over_several_chunks() -> None:
    data = b"\xef\xbb\xbf" + b'"Na\nme",X\n' + b"a,b\n"
    header, parts = _cut(data, limit=1000, chunk=2)
    assert header == b'"Na\nme",X\n'
    assert parts == [b'"Na\nme",X\na,b\n']


def test_cutter_header_property_before_the_header_ends() -> None:
    cutter = B.CsvCutter()
    cutter.feed(b"\xef\xbb\xbfNa")
    assert cutter.header == b"Na"


def test_cutter_last_record_without_line_ending_is_cut_into_a_new_part() -> None:
    data = b"H\n" + b"aaaa\n" + b"bbbb"
    _, parts = _cut(data, limit=8, chunk=100)
    assert parts == [b"H\naaaa\n", b"H\nbbbb"]


@pytest.mark.parametrize(
    ("data", "limit", "message"),
    [
        (b"", 100, "no data rows"),
        (b"A,B", 100, "no data rows"),
        (b"A,B\n", 100, "no data rows"),
        (b'A,B\n"open,1\n', 100, "ends inside a quoted value"),
        (b"A\n\xff\n", 100, "not UTF-8"),
        (b"A\n\xc3", 100, "not UTF-8"),
        (b"A,B\n" + b"x" * 50 + b"\n", 20, "larger than the upload limit"),
        (b"A,B\n" + b"x" * 50, 20, "larger than the upload limit"),
        (b"A,B,C,D,E\n1\n", 8, "header row alone"),
    ],
)
def test_cutter_rejects(data: bytes, limit: int, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        _cut(data, limit, chunk=7)


def test_cutter_rejects_a_huge_header() -> None:
    cutter = B.CsvCutter()
    with pytest.raises(ValueError, match="header row is too long"):
        cutter.feed(b"A" * (B.MAX_HEADER_BYTES + 1))


def test_default_limit_is_100_mb() -> None:
    assert B.MAX_PART_BYTES == 100 * 1024 * 1024
    assert B.CsvCutter().limit == B.MAX_PART_BYTES


# --- CSV parsing ---------------------------------------------------------------------------------


def _read(data: bytes, chunk: int) -> list[dict[str, str]]:
    reader = B.CsvReader()
    out: list[dict[str, str]] = []
    for i in range(0, len(data), chunk):
        out += reader.feed(data[i : i + chunk])
    return out + reader.finish()


@settings(max_examples=200, deadline=None)
@given(
    rows=st.lists(st.lists(cell, min_size=2, max_size=2), max_size=20),
    chunk=st.integers(min_value=1, max_value=50),
)
def test_reader_gives_the_same_rows_for_any_chunking(rows: list[list[str]], chunk: int) -> None:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(["sf__Id", "Name"])
    w.writerows(rows)
    data = buf.getvalue().encode()
    # A row of two empty strings is written as ",", so no row looks blank and none is lost.
    assert _read(data, chunk) == [{"sf__Id": a, "Name": b} for a, b in rows]


def test_reader_is_always_utf8_and_strips_bom() -> None:
    data = "﻿Id,Name\n1,Zoë\n2,日本".encode()
    assert _read(data, 3) == [{"Id": "1", "Name": "Zoë"}, {"Id": "2", "Name": "日本"}]
    assert B.CsvReader().feed(b"") == []


def test_reader_other_delimiter_and_blank_lines() -> None:
    reader = B.CsvReader("|")
    assert reader.feed(b"A|B\n\n1|2\n") == [{"A": "1", "B": "2"}]
    assert reader.finish() == []


@pytest.mark.parametrize(
    ("data", "message"),
    [
        (b'A,B\n"x"y,1\n', "malformed CSV"),
        (b"A,B\n1,2,3\n", "3 values for 2 columns"),
        (b'A,B\n"open,1', "ends inside a quoted value"),
    ],
)
def test_reader_rejects_malformed(data: bytes, message: str) -> None:
    with pytest.raises(esf.UnexpectedResponse, match=message):
        _read(data, 100)


# --- responses -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("cell", "code", "message", "fields"),
    [
        (
            "REQUIRED_FIELD_MISSING:Required fields are missing: [LastName]:LastName --",
            "REQUIRED_FIELD_MISSING",
            "Required fields are missing: [LastName]",
            ("LastName",),
        ),
        ("INVALID_FIELD:bad:A__c, B__c", "INVALID_FIELD", "bad", ("A__c", "B__c")),
        (
            "DUPLICATE_VALUE:duplicate value found: Ext__c --",
            "DUPLICATE_VALUE",
            "duplicate value found: Ext__c",
            (),
        ),
        (
            "FIELD_CUSTOM_VALIDATION_EXCEPTION:Street is required:",
            "FIELD_CUSTOM_VALIDATION_EXCEPTION",
            "Street is required",
            (),
        ),
        # Cells recorded from a Developer Edition org, API 68.0 (V41): fields are separated by
        # spaces, and a cell without fields ends in ":--".
        (
            "REQUIRED_FIELD_MISSING:Required fields are missing: [LastName, Company]:LastName Company --",
            "REQUIRED_FIELD_MISSING",
            "Required fields are missing: [LastName, Company]",
            ("LastName", "Company"),
        ),
        (
            "INVALID_EMAIL_ADDRESS:Email: invalid email address: not-an-email:Email --",
            "INVALID_EMAIL_ADDRESS",
            "Email: invalid email address: not-an-email",
            ("Email",),
        ),
        (
            "FIELD_CUSTOM_VALIDATION_EXCEPTION:esf-sandbox: validation rule probe:--",
            "FIELD_CUSTOM_VALIDATION_EXCEPTION",
            "esf-sandbox: validation rule probe",
            (),
        ),
        ("Something odd happened", None, "Something odd happened", ()),
        ("", None, "", ()),
    ],
)
def test_parse_bulk_error(cell: str, code: str | None, message: str, fields: tuple[str, ...]) -> None:
    assert B.parse_bulk_error(cell) == esf.ErrorDetail(code, message, fields)


def test_parse_job_state() -> None:
    assert B.parse_job_state("JobComplete") is esf.BulkJobState.JOB_COMPLETE
    assert esf.BulkJobState.JOB_COMPLETE == "JobComplete"
    assert esf.BulkJobState.FAILED.is_final
    assert not esf.BulkJobState.IN_PROGRESS.is_final
    with pytest.raises(esf.UnexpectedResponse, match=f"Bulk job {JOB} has an unknown state 'Paused'"):
        B.parse_job_state("Paused", JOB)
    with pytest.raises(esf.UnexpectedResponse, match="^Bulk job has an unknown state None"):
        B.parse_job_state(None)


def _info_body(**kw: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "id": JOB,
        "operation": "insert",
        "object": "Lead",
        "createdById": "005xx0000000001AAA",
        "createdDate": "2026-11-10T10:00:00.000+0000",
        "systemModstamp": "2026-11-10T10:00:05.000+0000",
        "state": "JobComplete",
        "concurrencyMode": "Parallel",
        "contentType": "CSV",
        "apiVersion": 68.0,
        "jobType": "V2Ingest",
        "lineEnding": "LF",
        "columnDelimiter": "COMMA",
        "numberRecordsProcessed": 3,
        "numberRecordsFailed": 1,
        "retries": 0,
        "totalProcessingTime": 120,
        "errorMessage": "",
    }
    body.update(kw)
    return body


def test_parse_job_info() -> None:
    info = B.parse_job_info(_info_body(), "60.0")
    assert info == esf.BulkJobInfo(
        id=JOB,
        operation="insert",
        sobject="Lead",
        state=esf.BulkJobState.JOB_COMPLETE,
        api_version="68.0",
        records_processed=3,
        records_failed=1,
        created_date=datetime(2026, 11, 10, 10, 0, tzinfo=timezone.utc),
        system_modstamp=datetime(2026, 11, 10, 10, 0, 5, tzinfo=timezone.utc),
        total_processing_ms=120,
        error_message=None,
    )
    assert info.raw["jobType"] == "V2Ingest"


@pytest.mark.parametrize(
    ("version", "expected"),
    [("67.0", "67.0"), (66, "66.0"), (True, "60.0"), ("v67", "60.0"), (None, "60.0")],
)
def test_parse_job_info_version(version: Any, expected: str) -> None:
    assert B.parse_job_info(_info_body(apiVersion=version), "60.0").api_version == expected


def test_parse_job_info_tolerates_odd_values() -> None:
    info = B.parse_job_info(
        _info_body(
            numberRecordsProcessed=True,
            numberRecordsFailed=2.0,
            totalProcessingTime=float("nan"),
            createdDate="2026-11-10T10:00:00+0000",
            systemModstamp="yesterday",
            errorMessage="InvalidBatch : Field name not found : Foo",
            object=None,
        ),
        "68.0",
    )
    assert info.records_processed == 0
    assert info.records_failed == 2
    assert info.total_processing_ms is None
    assert info.created_date == datetime(2026, 11, 10, 10, 0, tzinfo=timezone.utc)
    assert info.system_modstamp is None
    assert info.sobject is None
    assert info.error_message == "InvalidBatch : Field name not found : Foo"
    assert B.parse_job_info(_info_body(createdDate=5, numberRecordsFailed="2"), "68.0").created_date is None


@pytest.mark.parametrize("body", [[], {"state": "Open"}, {"id": ""}, None])
def test_parse_job_info_without_id(body: Any) -> None:
    with pytest.raises(esf.UnexpectedResponse, match="no id"):
        B.parse_job_info(body, "68.0")


def _info(state: str, n: int = 1, **kw: Any) -> esf.BulkJobInfo:
    return B.parse_job_info(_info_body(id=f"750xx000000000{n}AAA", state=state, **kw), "68.0")


def test_combine_infos() -> None:
    one = _info("Open")
    assert B.combine_infos([one]) is one
    done = B.combine_infos([_info("JobComplete", 1), _info("JobComplete", 2, totalProcessingTime=None)])
    assert done.state is esf.BulkJobState.JOB_COMPLETE
    assert done.id == "750xx0000000001AAA"
    assert (done.records_processed, done.records_failed, done.total_processing_ms) == (6, 2, 120)
    assert done.raw == {}
    behind = B.combine_infos([_info("JobComplete", 1), _info("UploadComplete", 2), _info("InProgress", 3)])
    assert behind.state is esf.BulkJobState.UPLOAD_COMPLETE
    failed = B.combine_infos(
        [_info("Aborted", 1), _info("Failed", 2, errorMessage="boom"), _info("InProgress", 3)]
    )
    assert failed.state is esf.BulkJobState.FAILED
    assert failed.error_message == "boom"
    bare = B.combine_infos(
        [_info("Open", 1, createdDate=None, systemModstamp=None, totalProcessingTime=None)] * 2
    )
    assert (bare.created_date, bare.system_modstamp, bare.total_processing_ms) == (None, None, None)


@pytest.mark.parametrize(
    ("headers", "locator"),
    [
        ({}, None),
        ({"sforce-locator": "null"}, None),
        ({"sforce-locator": " "}, None),
        ({"sforce-locator": "MTAwMDA"}, "MTAwMDA"),
    ],
)
def test_parse_locator(headers: dict[str, str], locator: str | None) -> None:
    assert B.parse_locator(headers) == locator


@pytest.mark.parametrize(
    ("headers", "count"),
    [
        ({}, None),
        ({"sforce-numberofrecords": " 50000 "}, 50000),
        ({"sforce-numberofrecords": "0"}, 0),
        ({"sforce-numberofrecords": "-1"}, None),
        ({"sforce-numberofrecords": "many"}, None),
    ],
)
def test_parse_record_count(headers: dict[str, str], count: int | None) -> None:
    """*Get Results for a Query Job*: ``Sforce-NumberOfRecords`` is the number of records in the set."""
    assert B.parse_record_count(headers) == count


PREFIX = f"/services/data/v68.0/jobs/query/{JOB}/"


def test_parse_result_pages() -> None:
    body = {
        "resultPages": [
            {"resultUrl": f"{PREFIX}results?locator=A"},
            {"resultUrl": f"{PREFIX}results?locator=B"},
        ],
        "nextRecordsUrl": f"{PREFIX}resultPages?locator=C",
        "done": False,
    }
    assert B.parse_result_pages(body, PREFIX) == (
        [f"{PREFIX}results?locator=A", f"{PREFIX}results?locator=B"],
        f"{PREFIX}resultPages?locator=C",
    )
    alias = {"resultPages": [], "nextRecordUrl": f"{PREFIX}resultPages?locator=C", "done": False}
    assert B.parse_result_pages(alias, PREFIX) == ([], f"{PREFIX}resultPages?locator=C")
    assert B.parse_result_pages({**body, "done": True}, PREFIX)[1] is None
    assert B.parse_result_pages({"resultPages": [], "nextRecordsUrl": None}, PREFIX) == ([], None)


def test_parse_result_pages_before_api_63() -> None:
    """Recorded at API 59.0-62.0: ``resultChunks[].resultLink``, relative to the version root."""
    prefix = f"/services/data/v60.0/jobs/query/{JOB}/"
    body = {
        "done": False,
        "nextRecordsUrl": f"/jobs/query/{JOB}/resultPages?locator=C",
        "resultChunks": [{"resultLink": f"/jobs/query/{JOB}/results?locator=A"}],
    }
    assert B.parse_result_pages(body, prefix) == (
        [f"{prefix}results?locator=A"],
        f"{prefix}resultPages?locator=C",
    )
    other = {"resultChunks": [{"resultLink": "/jobs/query/750xx0000000009AAA/results"}]}
    with pytest.raises(esf.UnexpectedResponse, match="not a result of this job"):
        B.parse_result_pages(other, prefix)


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ([], "unexpected resultPages"),
        ({"resultPages": None}, "no resultPages list"),
        ({"resultPages": ["x"]}, "not a result of this job"),
        ({"resultPages": [{"resultUrl": "https://evil.example.com/x"}]}, "not a result of this job"),
        ({"resultPages": [{"resultUrl": f"{PREFIX}../../other"}]}, "not a result of this job"),
        (
            {"resultPages": [], "nextRecordsUrl": "/services/data/v68.0/sobjects", "done": False},
            "not a result",
        ),
    ],
)
def test_parse_result_pages_rejects(body: Any, message: str) -> None:
    with pytest.raises(esf.UnexpectedResponse, match=message):
        B.parse_result_pages(body, PREFIX)


# --- compound fields -----------------------------------------------------------------------------

DESCRIBE: dict[str, Any] = {
    "name": "Account",
    "fields": [
        {"name": "Id", "type": "id"},
        {"name": "BillingAddress", "type": "address"},
        {"name": "BillingStreet", "type": "textarea", "compoundFieldName": "BillingAddress"},
        {"name": "BillingCity", "type": "string", "compoundFieldName": "BillingAddress"},
        {"name": "Spot__c", "type": "location"},
        {"name": "Spot__Latitude__s", "type": "double", "compoundFieldName": "Spot__c"},
        {"name": "Spot__Longitude__s", "type": "double", "compoundFieldName": "Spot__c"},
        {"name": "Name", "type": "string"},
        "not a dict",
        {"type": "address"},
    ],
}


def test_expand_compound_fields() -> None:
    assert B.expand_compound_fields(DESCRIBE, ["Id", "billingaddress", "Spot__c", "BillingCity", "Name"]) == [
        "Id",
        "BillingStreet",
        "BillingCity",
        "Spot__Latitude__s",
        "Spot__Longitude__s",
        "Name",
    ]
    assert B.expand_compound_fields({"fields": None}, ["A", "a"]) == ["A"]


def test_select_fields() -> None:
    assert B.select_fields("select Id, BillingAddress ,Name from Account where X = 1") == [
        "Id",
        "BillingAddress",
        "Name",
    ]
    assert B.select_fields("not soql") == []


def test_compound_hint() -> None:
    soql = "SELECT Id, BillingAddress FROM Account"
    hint = B.compound_hint(soql, DESCRIBE, None)
    assert hint is not None
    assert "BillingAddress -> BillingStreet, BillingCity" in hint
    assert "expand_compound_fields" in hint
    generic = B.compound_hint(soql, None, "FUNCTIONALITY_NOT_ENABLED: compound fields are not supported")
    assert generic is not None
    assert "components" in generic
    assert B.compound_hint("SELECT Id FROM Account", DESCRIBE, "Compound field") is not None
    assert B.compound_hint("SELECT Id FROM Account", DESCRIBE, "other") is None
    assert B.compound_hint(soql, None, None) is None


def test_compound_hint_for_fields_standard() -> None:
    """V81: the org runs FIELDS(STANDARD) unless the object has compound fields; then job
    creation fails with 400 API_ERROR "Selecting compound data not supported in Bulk Query"."""
    message = "Selecting compound data not supported in Bulk Query"
    hint = B.compound_hint("SELECT FIELDS(STANDARD) FROM Lead", DESCRIBE, message)
    assert hint is not None
    assert hint.startswith("FIELDS(STANDARD) includes")
    quoted = B.compound_hint("SELECT Id FROM Lead WHERE Name = 'FIELDS(STANDARD)'", None, message)
    assert quoted is not None
    assert quoted.startswith("Bulk queries can't read")


# --- polling -------------------------------------------------------------------------------------


def test_backoff_delays() -> None:
    poll = esf.Backoff(start=2, max=30)
    assert [poll.delay(n) for n in range(6)] == [2, 4, 8, 16, 30, 30]
    assert esf.Backoff().delay(10_000) == 30.0  # no overflow on long waits
    assert esf.Backoff(start=5, max=5, factor=1).delay(3) == 5


@pytest.mark.parametrize(
    ("kw", "message"),
    [({"start": 0}, "start"), ({"start": 10, "max": 5}, "max"), ({"factor": 0.5}, "factor")],
)
def test_backoff_rejects(kw: dict[str, float], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        esf.Backoff(**kw)


@pytest.mark.parametrize(
    "soql",
    [
        "SELECT FIELDS(ALL) FROM Lead LIMIT 200",
        "select fields( custom ) from Lead",
        "SELECT Id, FIELDS(Custom) FROM Account",
    ],
)
def test_check_bulk_query_rejects_unbounded_fields(soql: str) -> None:
    """*Create a Query Job* and the SOQL *SELECT* page agree that Bulk API 2.0 query jobs don't
    run ``FIELDS(ALL)`` or ``FIELDS(CUSTOM)``:
    https://developer.salesforce.com/docs/atlas.en-us.api_asynch.meta/api_asynch/query_create_job.htm
    https://developer.salesforce.com/docs/atlas.en-us.soql_sosl.meta/soql_sosl/sforce_api_calls_soql_select_fields.htm
    """
    with pytest.raises(ValueError, match="expand_compound_fields") as ei:
        B.check_bulk_query(soql)
    assert "don't support FIELDS(" in str(ei.value)


@pytest.mark.parametrize(
    "soql",
    [
        "SELECT Id, Name FROM Lead",
        "SELECT FIELDS(STANDARD) FROM Lead",  # the two pages disagree (V81): Salesforce decides
        "SELECT Id FROM Lead WHERE Name = 'FIELDS(ALL)'",
        "SELECT Id FROM Lead WHERE Name = 'it\\'s FIELDS(ALL)'",
        "SELECT MyFIELDS(ALL)__c FROM Lead",
    ],
)
def test_check_bulk_query_accepts(soql: str) -> None:
    B.check_bulk_query(soql)
