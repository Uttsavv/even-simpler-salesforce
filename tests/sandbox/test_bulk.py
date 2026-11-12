"""Bulk API 2.0 (``sf.bulk``, §9.11) against a real org, on both clients (big-bang.md §21).

Covers V18 (CSV nulls, defaults, BOM, non-ASCII), V40 (gzipped uploads), V41 (``sf__Error`` and
``sf__Created`` cells), V42 (compound fields), V43 (``resultPages`` shapes), V44 (job info),
V81 (``FIELDS(STANDARD)``), V82 (query job progress and ``sf.bulk.job`` events) and V88 (job
handles).

Every Lead carries the run tag in ``LastName`` and is deleted by the test (soft delete: the
Developer Edition user has no "Bulk API Hard Delete" permission) and, as a safety net, by the
session cleanup. The tests that load more than 200 rows need ``ESF_SANDBOX_ALLOW_WRITES=1``.

With ``ESF_SANDBOX_RECORD_BULK=1`` the sync run of the recorded tests writes redacted fixtures
to ``tests/fixtures/recorded/bulk/`` (``esf.testing.record``); ``tests/contract/test_bulk_recorded.py``
replays them. Only ``sf.bulk`` calls go through the recording client; checks and cleanup use a
second client.

Documentation (Bulk API 2.0 and Bulk API Developer Guide; developer.salesforce.com blocks
non-browser clients, so these were read through search excerpts):

- *Prepare CSV Files* (https://developer.salesforce.com/docs/atlas.en-us.api_asynch.meta/api_asynch/datafiles_prepare_csv.htm):
  UTF-8; "Empty field values are ignored when you update records. To set a field value to null,
  use a field value of #N/A." Silent on inserts and on a byte order mark.
- *Upload Job Data* (https://developer.salesforce.com/docs/atlas.en-us.api_asynch.meta/api_asynch/upload_job_data.htm):
  150 MB of base64 content per request. *Compression Headers* in the REST API Developer Guide
  (https://developer.salesforce.com/docs/atlas.en-us.api_rest.meta/api_rest/intro_rest_compression.htm):
  the API decompresses requests sent with ``Content-Encoding: gzip``.
- *Get Parallel Results for a Query Job* (https://developer.salesforce.com/docs/atlas.en-us.api_asynch.meta/api_asynch/query_get_parallel_job_results.htm).
- *Create a Query Job* (https://developer.salesforce.com/docs/atlas.en-us.api_asynch.meta/api_asynch/query_create_job.htm)
  and SOQL *SELECT* ``FIELDS()`` (https://developer.salesforce.com/docs/atlas.en-us.soql_sosl.meta/soql_sosl/sforce_api_calls_soql_select_fields.htm).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import httpx
import pytest

import even_simpler_salesforce as esf
from even_simpler_salesforce.core.http import RequestSpec

from .conftest import Cleanup, Driver

pytestmark = pytest.mark.sandbox

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "recorded" / "bulk"
POLL = esf.Backoff(start=1, max=3)
TIMEOUT = httpx.Timeout(120.0, connect=10.0)  # httpx defaults to 5 s; uploads take longer
DEFAULT_STATUS = "Open - Not Contacted"  # the default of Lead.Status in a new org


def _recording(sb: Driver, name: str) -> Any:
    """An httpx client recording to ``bulk/<name>.json`` (sync run only), or ``None``."""
    if sb.is_async or os.environ.get("ESF_SANDBOX_RECORD_BULK", "") != "1":
        return None
    return httpx.Client(transport=esf.testing.record(FIXTURES / f"{name}.json"), timeout=TIMEOUT)


def _bulk_client(sb: Driver, name: str | None = None, **kw: Any) -> Any:
    http = _recording(sb, name) if name else None
    return sb.client(**kw) if http is None else sb.client(http_client=http, **kw)


def _leads(sb: Driver, sf: Any, ids: list[str], fields: str) -> dict[str, dict[str, Any]]:
    soql = esf.soql(f"SELECT Id, {fields} FROM Lead WHERE Id IN {{}}", ids)
    return {r["Id"]: r for r in sb.collect(sf.query_iter(soql))}


def _delete(sb: Driver, sf: Any, ids: list[str], cleanup: Cleanup) -> None:
    if len(ids) > 200:
        job = sb.run(sf.bulk.ingest("Lead", "delete", rows=[{"Id": i} for i in ids]))
        assert sb.run(job.wait(poll=POLL)).records_failed == 0
    elif ids:
        sb.run(sf.collections.delete(ids))
    cleanup.discard(*ids)


def _ingest(sb: Driver, sf: Any, *args: Any, **kw: Any) -> tuple[Any, list[dict[str, str]]]:
    job = sb.run(sf.bulk.ingest(*args, **kw))
    sb.run(job.wait(poll=POLL))
    return job, sb.collect(job.successful_results())


# --- V18 / V41 / V44: CSV semantics ----------------------------------------------------------------


@pytest.mark.usefixtures("writes")
def test_csv_nulls_defaults_and_unicode(sb: Driver, tag: str, cleanup: Cleanup) -> None:
    """V18: on insert an empty cell and ``#N/A`` both leave a field at its default (Status)
    or null; on update an empty cell leaves the field unchanged and ``#N/A`` sets null; an upsert
    that inserts behaves like an insert. Non-ASCII text round-trips. ``sf__Created`` is the
    quoted string ``true`` or ``false`` (V41)."""
    sf = _bulk_client(sb, "csv_semantics")
    check = sb.client()
    k = sb.kind
    text = '日本語 🙂 «q» "x", y\nz'
    rows = [
        {
            "LastName": f"{tag}-a-{k}",
            "Company": "esf-sandbox",
            "FirstName": "Zoë Ünïcødé",
            "Status": "Working - Contacted",
            "Description": text,
            "Title": "Dr. Ø",
        },
        {"LastName": f"{tag}-b-{k}", "Company": "esf-sandbox"},  # missing keys: empty cells
        {
            "LastName": f"{tag}-c-{k}",
            "Company": "esf-sandbox",
            "FirstName": None,
            "Status": None,
            "Description": None,
            "Title": None,
        },  # None: #N/A
    ]
    job, ok = _ingest(sb, sf, "Lead", "insert", rows=rows)
    ids = {r["LastName"]: r["sf__Id"] for r in ok}
    cleanup.add(*ids.values())
    created: list[str] = list(ids.values())
    try:
        info = job.info
        assert info.state is esf.BulkJobState.JOB_COMPLETE
        assert (info.records_processed, info.records_failed) == (3, 0)
        assert info.api_version == sf.api_version  # V44: apiVersion is a JSON number, 68.0
        assert info.created_date is not None
        assert info.created_date.utcoffset() is not None  # "2026-11-11T22:40:19.000+0000"
        assert {r["sf__Created"] for r in ok} == {"true"}
        assert next(r for r in ok if r["LastName"].endswith(f"-a-{k}"))["Description"] == text
        a, b, c = (ids[f"{tag}-{x}-{k}"] for x in "abc")
        got = _leads(sb, check, [a, b, c], "FirstName, Status, Description, Title")
        assert (got[a]["FirstName"], got[a]["Description"], got[a]["Title"]) == ("Zoë Ünïcødé", text, "Dr. Ø")
        for rid in (b, c):  # empty cell and #N/A on insert: default for Status, null otherwise
            assert got[rid]["Status"] == DEFAULT_STATUS
            assert (got[rid]["FirstName"], got[rid]["Description"], got[rid]["Title"]) == (None, None, None)

        # update: b sets the columns; a leaves FirstName out (empty cell) and nulls Title
        update = [{"Id": b, "FirstName": "Ann", "Title": "Prof"}, {"Id": a, "Title": None}]
        job, ok = _ingest(sb, sf, "Lead", "update", rows=update)
        assert job.info.records_failed == 0
        assert {r["sf__Created"] for r in ok} == {"false"}
        got = _leads(sb, check, [a, b], "FirstName, Title")
        assert (got[a]["FirstName"], got[a]["Title"]) == ("Zoë Ünïcødé", None)
        assert (got[b]["FirstName"], got[b]["Title"]) == ("Ann", "Prof")

        # upsert that inserts: #N/A and an empty cell leave Status at its default
        x1, x2 = f"{tag}-x1-{k}", f"{tag}-x2-{k}"
        upsert = [
            {"Esf_Ext__c": x1, "LastName": f"{tag}-u1-{k}", "Company": "esf-sandbox", "Status": None},
            {"Esf_Ext__c": x2, "LastName": f"{tag}-u2-{k}", "Company": "esf-sandbox"},
        ]
        job, ok = _ingest(sb, sf, "Lead", "upsert", rows=upsert, external_id_field="Esf_Ext__c")
        uids = [r["sf__Id"] for r in ok]
        cleanup.add(*uids)
        created += uids
        assert {r["sf__Created"] for r in ok} == {"true"}
        assert {r["Status"] for r in _leads(sb, check, uids, "Status").values()} == {DEFAULT_STATUS}
        # upsert that updates: sf__Created is "false"
        job, ok = _ingest(
            sb, sf, "Lead", "upsert", rows=[{"Esf_Ext__c": x1, "Title": "T"}], external_id_field="Esf_Ext__c"
        )
        assert [(r["sf__Created"], r["Esf_Ext__c"]) for r in ok] == [("false", x1)]

        # A byte order mark: the client strips it (the org rejects it, see the raw test below)
        data = b"\xef\xbb\xbf" + f"LastName,Company\n{tag}-bom-{k},esf-sandbox\n".encode()
        job, ok = _ingest(sb, sf, "Lead", "insert", rows=data)
        assert [r["LastName"] for r in ok] == [f"{tag}-bom-{k}"]
        cleanup.add(*(r["sf__Id"] for r in ok))
        created += [r["sf__Id"] for r in ok]
    finally:
        _delete(sb, check, created, cleanup)


@pytest.mark.usefixtures("writes")
def test_org_rejects_a_byte_order_mark(sb: Driver, tag: str) -> None:
    """V18, org-observed: a raw upload starting with a UTF-8 BOM fails the whole job with
    ``InvalidBatch : Field name not found : \\ufeffLastName``. The docs are silent; the client
    strips the BOM (``CsvCutter.header``)."""
    sf = sb.client()

    def send(method: str, path: str, **kw: Any) -> Any:
        return sb.run(sf._send(RequestSpec(method, path, operation="sandbox.raw", **kw)))  # pyright: ignore[reportPrivateUsage]

    body, ctype = sf._transport.encode({"object": "Lead", "operation": "insert", "contentType": "CSV"})  # pyright: ignore[reportPrivateUsage]
    job = send("POST", "jobs/ingest", content=body, content_type=ctype).json()
    data = b"\xef\xbb\xbf" + f"LastName,Company\n{tag}-rawbom-{sb.kind},esf-sandbox\n".encode()
    send("PUT", f"jobs/ingest/{job['id']}/batches", content=data, content_type="text/csv", expect_json=False)
    body, ctype = sf._transport.encode({"state": "UploadComplete"})  # pyright: ignore[reportPrivateUsage]
    send("PATCH", f"jobs/ingest/{job['id']}", content=body, content_type=ctype)
    status: dict[str, Any] = {}
    for _ in range(60):
        status = send("GET", f"jobs/ingest/{job['id']}").json()
        if status["state"] in ("JobComplete", "Failed", "Aborted"):
            break
        sb.run(sf._transport.sleep(1))  # pyright: ignore[reportPrivateUsage]
    assert status["state"] == "Failed"
    assert status["errorMessage"] == "InvalidBatch : Field name not found : ﻿LastName"


@pytest.mark.usefixtures("writes")
def test_failed_results_format(sb: Driver, tag: str, cleanup: Cleanup) -> None:
    """V41: ``sf__Error`` is ``CODE:message:Field1 Field2 --`` (fields separated by spaces) or
    ``CODE:message:--`` without fields; ``sf__Id`` is empty for failed inserts."""
    sf = _bulk_client(sb, "failed_results")
    k = sb.kind
    rows = [
        {"LastName": f"{tag}-vr-{k}", "Company": "ESF_VR", "Email": None},  # validation rule
        {"LastName": None, "Company": "esf-sandbox"},  # required field
        {"LastName": f"{tag}-em-{k}", "Company": "esf-sandbox", "Email": "not-an-email"},
        {"LastName": None, "Company": None},  # two required fields
        {"LastName": f"{tag}-flow-{k}", "Company": "ESF_FLOW"},  # flow custom error
        {"LastName": f"{tag}-trig-{k}", "Company": "ESF_ADDERROR"},  # Apex addError
        {"LastName": f"{tag}-ok-{k}", "Company": "esf-sandbox"},
    ]
    job, ok = _ingest(sb, sf, "Lead", "insert", rows=rows)
    cleanup.add(*(r["sf__Id"] for r in ok))
    try:
        assert (job.info.records_processed, job.info.records_failed) == (7, 6)
        assert [r["LastName"] for r in ok] == [f"{tag}-ok-{k}"]
        failures = sb.collect(job.failed_results())
        by_name = {(f.fields["LastName"], f.fields["Company"]): f for f in failures}
        assert all(f.id is None for f in failures)
        vr = by_name[(f"{tag}-vr-{k}", "ESF_VR")].error
        assert (vr.error_code, vr.message, vr.fields) == (
            "FIELD_CUSTOM_VALIDATION_EXCEPTION",
            "esf-sandbox: validation rule probe",
            (),
        )
        missing = by_name[("", "esf-sandbox")].error
        assert (missing.error_code, missing.fields) == ("REQUIRED_FIELD_MISSING", ("LastName",))
        both = by_name[("", "")].error
        assert both.message == "Required fields are missing: [LastName, Company]"
        assert both.fields == ("LastName", "Company")
        email = by_name[(f"{tag}-em-{k}", "esf-sandbox")].error
        assert (email.error_code, email.fields) == ("INVALID_EMAIL_ADDRESS", ("Email",))
        for key in ((f"{tag}-flow-{k}", "ESF_FLOW"), (f"{tag}-trig-{k}", "ESF_ADDERROR")):
            assert by_name[key].error.error_code == "FIELD_CUSTOM_VALIDATION_EXCEPTION"
            assert by_name[key].error.fields == ()
    finally:
        _delete(sb, sb.client(), [r["sf__Id"] for r in ok], cleanup)


# --- V40: gzip ------------------------------------------------------------------------------------


@pytest.mark.usefixtures("heavy_writes")
def test_gzipped_upload(sb: Driver, tag: str, cleanup: Cleanup) -> None:
    """V40: the client gzips a part over 64 KiB and sends it chunked; the org accepts it."""
    seen: list[httpx.Headers] = []

    def hook(request: httpx.Request) -> None:
        if request.method == "PUT":
            seen.append(request.headers)

    async def ahook(request: httpx.Request) -> None:
        hook(request)

    http: Any = (
        httpx.AsyncClient(event_hooks={"request": [ahook]}, timeout=TIMEOUT)
        if sb.is_async
        else httpx.Client(event_hooks={"request": [hook]}, timeout=TIMEOUT)
    )
    sf = sb.client(http_client=http)
    n = 1000
    rows = [
        {"LastName": f"{tag}-gz{i}-{sb.kind}", "Company": "esf-sandbox", "Description": "x" * 100}
        for i in range(n)
    ]
    job, ok = _ingest(sb, sf, "Lead", "insert", rows=rows)
    ids = [r["sf__Id"] for r in ok]
    cleanup.add(*ids)
    try:
        assert [h.get("content-encoding") for h in seen] == ["gzip"]
        assert seen[0].get("transfer-encoding") == "chunked"
        assert (job.info.records_processed, job.info.records_failed) == (n, 0)
        assert len(ids) == n
    finally:
        _delete(sb, sf, ids, cleanup)


# --- V42 / V81: query creation errors -----------------------------------------------------------


def test_query_compound_fields_and_fields_standard(sb: Driver) -> None:
    """V42: a compound field fails at job creation with 400 ``API_ERROR`` "Selecting compound
    data not supported in Bulk Query" (no job is created). V81: ``FIELDS(STANDARD)`` runs,
    unless the object has compound fields (same 400); ``FIELDS(ALL)``/``FIELDS(CUSTOM)`` get a
    400 "The SOQL FIELDS function is not supported with an unbounded set of fields in this
    API." from the org, so the client rejects them before sending."""
    sf = _bulk_client(sb, "query_errors")
    for soql in ("SELECT Id, BillingAddress FROM Account", "SELECT FIELDS(STANDARD) FROM Lead"):
        with pytest.raises(esf.RequestError) as ei:
            sb.collect(sf.bulk.query(soql))
        assert (ei.value.status, ei.value.error_code) == (400, "API_ERROR")
        assert ei.value.message == "Selecting compound data not supported in Bulk Query"
        assert ei.value.hint is not None
        assert "expand_compound_fields" in ei.value.hint
    assert ei.value.hint.startswith("FIELDS(STANDARD) includes")
    rows = sb.collect(sf.bulk.query("SELECT FIELDS(STANDARD) FROM Campaign"))
    assert isinstance(rows, list)
    with pytest.raises(ValueError, match="FIELDS"):
        sb.collect(sf.bulk.query("SELECT FIELDS(ALL) FROM Lead LIMIT 200"))


# --- V43 / V44: resultPages -----------------------------------------------------------------------


@pytest.mark.usefixtures("writes")
@pytest.mark.parametrize("version", ["68.0", "60.0"])
def test_parallel_result_pages(sb: Driver, tag: str, cleanup: Cleanup, version: str) -> None:
    """V43: API 63.0+ sends ``resultPages[].resultUrl`` (full path in the job's version) and
    ``nextRecordsUrl``; 58.0-62.0 send ``resultChunks[].resultLink`` relative to
    ``/services/data/vXX.X``. Both download in parallel and in order."""
    check = sb.client()
    rows = [{"LastName": f"{tag}-rp{i}-{version}-{sb.kind}", "Company": "esf-sandbox"} for i in range(3)]
    ids = [r.id for r in sb.run(check.collections.create(rows, sobject="Lead")).succeeded]
    cleanup.add(*ids)
    try:
        assert len(ids) == 3
        name = f"result_pages_v{version.split('.')[0]}"
        sf = _bulk_client(sb, name, api_version=version)
        soql = "SELECT Id, LastName FROM Lead WHERE LastName LIKE {} ORDER BY LastName"
        like = f"{tag}-rp%-{version}-{sb.kind}"
        got = sb.collect(sf.bulk.query(soql, like, parallel_downloads=3))
        assert [r["Id"][:15] for r in got] == [i[:15] for i in ids]
        pages = [e for e in sb.recorder.requests("bulk.query.pages") if e["outcome"] == "ok"]
        assert len(pages) == 1
    finally:
        _delete(sb, check, ids, cleanup)


# --- V82: query job progress ---------------------------------------------------------------------


@pytest.mark.usefixtures("heavy_writes")
def test_query_job_progress_events(sb: Driver, tag: str, cleanup: Cleanup) -> None:
    """V82: query job info has ``numberRecordsProcessed`` (absent at ``UploadComplete``, 0 then
    the total while ``InProgress``) and never ``numberRecordsFailed``; ``sf.bulk.job`` events
    report ``records_failed=None`` for query jobs."""
    sf = sb.client()
    n = 4000
    rows = [{"LastName": f"{tag}-q{i}-{sb.kind}", "Company": "esf-sandbox"} for i in range(n)]
    _, ok = _ingest(sb, sf, "Lead", "insert", rows=rows)
    ids = [r["sf__Id"] for r in ok]
    cleanup.add(*ids)
    try:
        assert len(ids) == n
        sb.recorder.events.clear()
        soql = "SELECT Id FROM Lead WHERE LastName LIKE {}"
        got = sb.collect(sf.bulk.query(soql, f"{tag}-q%-{sb.kind}", poll=esf.Backoff(start=0.3, max=0.5)))
        assert len(got) == n
        events = [e for e in sb.recorder.events if e.name == "sf.bulk.job"]
        assert events
        assert all(e["records_failed"] is None for e in events)
        assert events[-1]["state"] == "JobComplete"
        assert events[-1]["records_processed"] == n
    finally:
        _delete(sb, sf, ids, cleanup)


# --- V88: job handles -----------------------------------------------------------------------------


@pytest.mark.usefixtures("writes")
def test_job_handles_reopen_and_resume(sb: Driver, tag: str, cleanup: Cleanup) -> None:
    """V88: a query job opened again by id (also by a client on another API version) keeps the
    job's version, ``maxRecords`` pages carry ``Sforce-NumberOfRecords``, a saved locator resumes
    the download, and an id of the other job kind is rejected."""
    check = sb.client()
    rows = [{"LastName": f"{tag}-jh{i}-{sb.kind}", "Company": "esf-sandbox"} for i in range(3)]
    ids = [r.id for r in sb.run(check.collections.create(rows, sobject="Lead")).succeeded]
    cleanup.add(*ids)
    try:
        assert len(ids) == 3
        sf = sb.client(api_version="68.0")
        soql = "SELECT Id FROM Lead WHERE LastName LIKE {} ORDER BY LastName"
        job = sb.run(sf.bulk.start_query(soql, f"{tag}-jh%-{sb.kind}"))
        with pytest.raises(esf.BulkJobNotComplete):
            sb.collect(job.rows())

        other = sb.client(api_version="60.0")
        again = sb.run(other.bulk.query_job(job.id))  # status GET in another version
        assert again.info.api_version == "68.0"
        sb.run(again.wait(poll=POLL))

        def read(it: Any) -> list[tuple[Any, ...]]:
            out: list[tuple[Any, ...]] = []
            if sb.is_async:

                async def go() -> None:
                    async for page in it:
                        out.append((page.next_locator, page.size, [r["Id"] async for r in page.rows()]))

                sb.run(go())
            else:
                for page in it:
                    out.append((page.next_locator, page.size, [r["Id"] for r in page.rows()]))
            return out

        pages = read(again.pages(max_records=1))
        assert [p[2][0][:15] for p in pages] == [i[:15] for i in ids]
        assert [p[1] for p in pages] == [1, 1, 1]
        assert pages[-1][0] is None
        resumed = read(again.pages(locator=pages[0][0], max_records=1))
        assert [p[2] for p in resumed] == [p[2] for p in pages[1:]]

        with pytest.raises(esf.NotFound) as ei:  # 404 NOT_FOUND, not the query job's info
            sb.run(sf.bulk.ingest_job([job.id]))
        assert ei.value.error_code == "NOT_FOUND"
    finally:
        _delete(sb, check, ids, cleanup)
