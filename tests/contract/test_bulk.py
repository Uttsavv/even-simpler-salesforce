"""``sf.bulk`` (§9.11) against hand-written responses, for both clients.

All fixtures under ``tests/fixtures/recorded/bulk`` and every CSV body in this file are
**synthetic** (no org to record from). Salesforce behaviour relied on, from the *Bulk API 2.0
and Bulk API Developer Guide* (https://developer.salesforce.com/docs/atlas.en-us.api_asynch.meta/api_asynch/):

- *Create a Job* (``create_job.htm``): ``POST jobs/ingest`` with ``object``, ``operation``,
  ``externalIdFieldName``, ``contentType``, ``lineEnding``, ``columnDelimiter``.
- *Upload Job Data* (``upload_job_data.htm``): ``PUT jobs/ingest/{id}/batches`` with
  ``Content-Type: text/csv``; one upload per job; at most 150 MB base64, so 100 MB raw.
- *Close or Abort a Job* (``close_job.htm``): ``PATCH jobs/ingest/{id}`` with
  ``{"state": "UploadComplete"}`` or ``{"state": "Aborted"}``.
- *Get Job Successful/Failed/Unprocessed Record Results*: ``successfulResults/``
  (``sf__Id``, ``sf__Created``), ``failedResults/`` (``sf__Id``, ``sf__Error``) and
  ``unprocessedrecords/``.
- *Prepare CSV Files* (``datafiles_prepare_csv.htm``): UTF-8; ``#N/A`` sets null; an empty
  value leaves the field unchanged.
- *Create a Query Job* and *Get Results for a Query Job* (``query_get_job_results.htm``):
  ``query`` or ``queryAll``; ``Sforce-Locator`` pages, ``null`` on the last page; "Use the same
  API version ... Otherwise, the call returns a 409 error."
- *Get Parallel Results for a Query Job*
  (https://developer.salesforce.com/docs/atlas.en-us.api_asynch.meta/api_bulk_v2/query_get_parallel_job_results.htm):
  ``GET jobs/query/{id}/resultPages``, API 58.0+, up to five ``resultUrl`` per response.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
import tracemalloc
from collections.abc import AsyncIterator, Callable, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

import even_simpler_salesforce as esf
from even_simpler_salesforce.core import bulk as core_bulk

from ..conftest import DATA, TOKEN_URL, Harness, Recorder, token_body
from ..fixtures import load_fixture

JOB = "750xx0000000001AAA"
QJOB = "750xx0000000002AAA"
JOBS = f"/services/data/v68.0/jobs/ingest/{JOB}"
QJOBS = f"/services/data/v68.0/jobs/query/{QJOB}"


def job(name: str = "bulk/ingest_job.json", **kw: Any) -> dict[str, Any]:
    body: dict[str, Any] = load_fixture(name)
    body.update(kw)
    return body


def csv_response(
    text: str | bytes, *, locator: str | None = None, charset: str = "ISO-8859-1"
) -> httpx.Response:
    headers = {"content-type": f"text/csv; charset={charset}"}
    if locator is not None:
        headers["sforce-locator"] = locator
    return httpx.Response(200, headers=headers, content=text.encode() if isinstance(text, str) else text)


def ingest_routes(mock: respx.MockRouter, *, final: str = "JobComplete") -> dict[str, respx.Route]:
    return {
        "create": mock.post(f"{DATA}/jobs/ingest").respond(json=job()),
        "upload": mock.put(path=f"{JOBS}/batches").respond(201),
        "close": mock.patch(path=JOBS).respond(json=job(state="UploadComplete")),
        "status": mock.get(path=JOBS).respond(json=job("bulk/ingest_job_complete.json", state=final)),
    }


def query_routes(
    mock: respx.MockRouter, *pages: httpx.Response, final: str = "JobComplete"
) -> dict[str, respx.Route]:
    results = mock.get(path=f"{QJOBS}/results")
    results.side_effect = list(pages)
    return {
        "create": mock.post(f"{DATA}/jobs/query").respond(json=job("bulk/query_job.json")),
        "status": mock.get(path=QJOBS).respond(json=job("bulk/query_job.json", state=final)),
        "results": results,
    }


def first_and_stop(h: Harness, it: Any) -> Any:
    """Read one item, then stop iterating (closing the iterator)."""
    if h.is_async:

        async def go() -> Any:
            async for item in it:
                await it.aclose()
                return item
            return None

        return h.run(go())
    for item in it:
        it.close()
        return item
    return None


# --- ingest ---------------------------------------------------------------------------------------


def test_ingest_rows_creates_uploads_and_closes(h: Harness, mock: respx.MockRouter, rec: Recorder) -> None:
    """None -> #N/A (null), a missing key -> empty cell (unchanged), UTF-8 text/csv upload."""
    routes = ingest_routes(mock)
    sf = h.client(recorder=rec)
    rows = [
        {"LastName": "Doe", "Company": "Acme", "Description": None},
        {"LastName": 'Zoë, "Z"', "Company": "日本"},
    ]
    j = h.run(sf.bulk.ingest("Lead", "insert", rows=rows))
    assert json.loads(routes["create"].calls.last.request.content) == {
        "object": "Lead",
        "operation": "insert",
        "contentType": "CSV",
        "lineEnding": "LF",
        "columnDelimiter": "COMMA",
    }
    put = routes["upload"].calls.last.request
    assert put.headers["content-type"] == "text/csv; charset=UTF-8"
    assert put.content == 'LastName,Company,Description\nDoe,Acme,#N/A\n"Zoë, ""Z""",日本,\n'.encode()
    assert put.headers["content-length"] == str(len(put.content))
    assert json.loads(routes["close"].calls.last.request.content) == {"state": "UploadComplete"}
    assert j.ids == (JOB,)
    assert j.info.state is esf.BulkJobState.UPLOAD_COMPLETE
    assert JOB in repr(j)
    ops = [e.fields["operation"] for e in rec.requests]
    assert ops == ["bulk.ingest.create", "bulk.ingest.upload", "bulk.ingest.close"]
    states = [e.fields["state"] for e in rec.named("sf.bulk.job")]
    assert states == ["Open", "UploadComplete"]
    assert rec.named("sf.bulk.job")[0].fields["job_id"] == JOB


def test_ingest_upsert_bytes_and_options(h: Harness, mock: respx.MockRouter) -> None:
    routes = ingest_routes(mock)
    sf = h.client()
    data = b"Ext__c\tLastName\r\nE1\tDoe\r\n"
    h.run(
        sf.bulk.ingest(
            "Lead",
            operation="upsert",
            external_id_field="Ext__c",
            rows=data,
            line_ending="CRLF",
            column_delimiter="TAB",
        )
    )
    body = json.loads(routes["create"].calls.last.request.content)
    assert body["externalIdFieldName"] == "Ext__c"
    assert (body["lineEnding"], body["columnDelimiter"]) == ("CRLF", "TAB")
    assert routes["upload"].calls.last.request.content == data


def test_ingest_csv_path_is_streamed_and_bom_dropped(
    h: Harness, mock: respx.MockRouter, tmp_path: Path
) -> None:
    routes = ingest_routes(mock)
    path = tmp_path / "leads.csv"
    path.write_bytes(b"\xef\xbb\xbfLastName\nDoe\n")
    sf = h.client()
    h.run(sf.bulk.ingest("Lead", "insert", rows=path))
    h.run(sf.bulk.ingest("Lead", "insert", rows=str(path)))
    assert routes["upload"].calls.last.request.content == b"LastName\nDoe\n"


@pytest.mark.parametrize(
    ("kw", "error", "message"),
    [
        ({"rows": [{"Name": "#N/A"}]}, ValueError, "ambiguous"),
        ({"rows": []}, ValueError, "rows is empty"),
        ({"rows": [{"A": 1}, {"B": 2}]}, ValueError, "not in the first row"),
        ({"rows": b"A\r\n1\r\n"}, ValueError, "line_ending='CRLF'"),
        ({"rows": b"A\n\xff\n"}, ValueError, "not UTF-8"),
        ({"rows": b"A\n"}, ValueError, "no data rows"),
        ({"rows": 42}, TypeError, "iterable of dicts"),
        ({"rows": [{"A": 1}], "operation": "upsert"}, ValueError, "external_id_field"),
        ({"rows": [{"A": 1}], "sobject": "Bad Name"}, ValueError, None),
    ],
)
def test_ingest_validates_before_sending(
    h: Harness, mock: respx.MockRouter, kw: dict[str, Any], error: type[Exception], message: str | None
) -> None:
    routes = ingest_routes(mock)
    sf = h.client()
    args = {"sobject": "Lead", "operation": "insert", **kw}
    with pytest.raises(error, match=message):
        h.run(sf.bulk.ingest(args.pop("sobject"), args.pop("operation"), **args))
    assert not routes["create"].called


def test_ingest_missing_file_sends_nothing(h: Harness, mock: respx.MockRouter, tmp_path: Path) -> None:
    routes = ingest_routes(mock)
    sf = h.client()
    with pytest.raises(FileNotFoundError):
        h.run(sf.bulk.ingest("Lead", "insert", rows=tmp_path / "missing.csv"))
    (tmp_path / "empty.csv").write_bytes(b"")
    with pytest.raises(ValueError, match="no data rows"):
        h.run(sf.bulk.ingest("Lead", "insert", rows=tmp_path / "empty.csv"))
    assert not routes["create"].called


def test_ingest_splits_large_data_over_several_jobs(
    h: Harness, mock: respx.MockRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§15.1 chunker guard: data over the per-job limit goes to several jobs, each with the header."""
    monkeypatch.setattr(core_bulk, "MAX_PART_BYTES", 40)
    ids = [f"750xx000000000{n}AAA" for n in (1, 2, 3)]
    create = mock.post(f"{DATA}/jobs/ingest")
    create.side_effect = [httpx.Response(200, json=job(id=i)) for i in ids]
    uploads = {i: mock.put(path=f"/services/data/v68.0/jobs/ingest/{i}/batches").respond(201) for i in ids}
    for i in ids:
        mock.patch(path=f"/services/data/v68.0/jobs/ingest/{i}").respond(
            json=job(id=i, state="UploadComplete")
        )
        mock.get(path=f"/services/data/v68.0/jobs/ingest/{i}").respond(
            json=job("bulk/ingest_job_complete.json", id=i)
        )
        mock.get(path=f"/services/data/v68.0/jobs/ingest/{i}/successfulResults/").mock(
            return_value=csv_response(f'"sf__Id","sf__Created",LastName\n{i[:3]}Q,true,X{i[14]}\n')
        )
    sf = h.client()
    rows = [{"LastName": f"Name{n:02d}"} for n in range(9)]  # 9 bytes a row, header 9 bytes
    j = h.run(sf.bulk.ingest("Lead", "insert", rows=rows))
    assert j.ids == tuple(ids)
    bodies = [uploads[i].calls.last.request.content for i in ids]
    assert all(b.startswith(b"LastName\n") and len(b) <= 40 for b in bodies)
    assert b"".join(b.removeprefix(b"LastName\n") for b in bodies) == b"".join(
        f"Name{n:02d}\n".encode() for n in range(9)
    )
    info = h.run(j.wait())
    assert info.state is esf.BulkJobState.JOB_COMPLETE
    assert info.records_processed == 9
    assert len(j.parts) == 3
    assert [r["LastName"] for r in h.collect(j.successful_results())] == ["X1", "X2", "X3"]


def test_ingest_upload_failure_aborts_the_job_and_names_finished_ones(
    h: Harness, mock: respx.MockRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(core_bulk, "MAX_PART_BYTES", 20)
    ids = ["750xx0000000001AAA", "750xx0000000002AAA"]
    create = mock.post(f"{DATA}/jobs/ingest")
    create.side_effect = [httpx.Response(200, json=job(id=i)) for i in ids]
    mock.put(path=f"/services/data/v68.0/jobs/ingest/{ids[0]}/batches").respond(201)
    mock.put(path=f"/services/data/v68.0/jobs/ingest/{ids[1]}/batches").respond(
        400, json=[{"errorCode": "INVALIDJOBSTATE", "message": "Job is not open"}]
    )
    first = mock.patch(path=f"/services/data/v68.0/jobs/ingest/{ids[0]}").respond(
        json=job(id=ids[0], state="UploadComplete")
    )
    second = mock.patch(path=f"/services/data/v68.0/jobs/ingest/{ids[1]}").respond(
        json=job(id=ids[1], state="Aborted")
    )
    sf = h.client()
    with pytest.raises(esf.RequestError) as ei:
        h.run(sf.bulk.ingest("Lead", "insert", rows=[{"LastName": f"N{n:03d}"} for n in range(3)]))
    assert json.loads(first.calls.last.request.content) == {"state": "UploadComplete"}
    assert json.loads(second.calls.last.request.content) == {"state": "Aborted"}
    assert f"job {ids[1]} was aborted" in str(ei.value)
    assert f"jobs {ids[0]} were uploaded and keep running" in str(ei.value)


def test_ingest_create_failure_is_typed(h: Harness, mock: respx.MockRouter) -> None:
    mock.post(f"{DATA}/jobs/ingest").respond(
        400, json=[{"errorCode": "INVALIDJOB", "message": "InvalidJob : Invalid object: Leed"}]
    )
    sf = h.client()
    with pytest.raises(esf.RequestError) as ei:
        h.run(sf.bulk.ingest("Leed", "insert", rows=[{"A": 1}]))
    assert ei.value.operation == "bulk.ingest.create"


def test_upload_is_resent_whole_after_token_refresh(h: Harness, mock: respx.MockRouter) -> None:
    routes = ingest_routes(mock)
    mock.post(TOKEN_URL).respond(json=token_body("AT-fresh-0002"))
    routes["upload"].side_effect = [
        httpx.Response(401, json=[{"errorCode": "INVALID_SESSION_ID", "message": "Session expired"}]),
        httpx.Response(201),
    ]
    sf = h.client()
    h.run(sf.bulk.ingest("Lead", "insert", rows=b"LastName\nDoe\n"))
    assert routes["upload"].call_count == 2
    assert routes["upload"].calls.last.request.content == b"LastName\nDoe\n"


# --- waiting --------------------------------------------------------------------------------------


def test_wait_polls_with_backoff_until_complete(h: Harness, mock: respx.MockRouter, rec: Recorder) -> None:
    routes = ingest_routes(mock)
    complete = job("bulk/ingest_job_complete.json")
    routes["status"].side_effect = [
        httpx.Response(200, json=job(state="InProgress")),
        httpx.Response(200, json=job(state="InProgress", numberRecordsProcessed=2)),
        httpx.Response(200, json=job(state="InProgress", numberRecordsProcessed=2, numberRecordsFailed=1)),
        httpx.Response(200, json=complete),
    ]
    sf = h.client(recorder=rec)
    j = h.run(sf.bulk.ingest("Lead", "insert", rows=[{"LastName": "Doe"}]))
    info = h.run(j.wait(poll=esf.Backoff(start=2, max=3), timeout=3600))
    assert sf.sleeps == [2, 3, 3]
    assert info.state is esf.BulkJobState.JOB_COMPLETE
    assert (info.records_processed, info.records_failed, info.total_processing_ms) == (3, 1, 334)
    assert info.api_version == "68.0"
    assert j.info == info
    assert h.run(j.wait()) == info  # already final: no more polling
    assert routes["status"].call_count == 4
    assert h.run(j.refresh()) == info
    events = rec.named("sf.bulk.job")
    # Every poll that changed the state, numberRecordsProcessed or numberRecordsFailed emits.
    assert [
        (e.fields["state"], e.fields["records_processed"], e.fields["records_failed"]) for e in events
    ] == [
        ("Open", 0, 0),
        ("UploadComplete", 0, 0),
        ("InProgress", 0, 0),
        ("InProgress", 2, 0),
        ("InProgress", 2, 1),
        ("JobComplete", 3, 1),
    ]


@pytest.mark.parametrize("state", ["Failed", "Aborted"])
def test_wait_raises_bulk_job_failed(h: Harness, mock: respx.MockRouter, state: str) -> None:
    routes = ingest_routes(mock)
    routes["status"].respond(json=job(state=state, errorMessage="InvalidBatch : Field name not found : Foo"))
    sf = h.client()
    j = h.run(sf.bulk.ingest("Lead", "insert", rows=[{"LastName": "Doe"}]))
    with pytest.raises(esf.BulkJobFailed) as ei:
        h.run(j.wait())
    err = ei.value
    assert (err.job_id, err.state) == (JOB, state)
    assert err.error_message == "InvalidBatch : Field name not found : Foo"
    assert err.operation == "bulk.ingest"
    assert err.sobject == "Lead"
    assert f"Bulk job {JOB} ended in state {state}: InvalidBatch" in str(err)


def test_unknown_state_raises_instead_of_polling_forever(h: Harness, mock: respx.MockRouter) -> None:
    """simple-salesforce #517: an unexpected state must not be polled until the end of time."""
    routes = ingest_routes(mock)
    routes["status"].respond(json=job(state="Paused"))
    sf = h.client()
    j = h.run(sf.bulk.ingest("Lead", "insert", rows=[{"LastName": "Doe"}]))
    with pytest.raises(esf.UnexpectedResponse, match="unknown state 'Paused'"):
        h.run(j.wait())
    assert routes["status"].call_count == 1


def test_wait_times_out(h: Harness, mock: respx.MockRouter) -> None:
    routes = ingest_routes(mock)
    routes["status"].respond(json=job(state="InProgress"))
    sf = h.client()
    j = h.run(sf.bulk.ingest("Lead", "insert", rows=[{"LastName": "Doe"}]))
    with pytest.raises(esf.BulkJobTimeout) as ei:
        h.run(j.wait(poll=esf.Backoff(start=4, max=4), timeout=10))
    assert sf.sleeps == [4, 4, 2]
    assert (ei.value.job_id, ei.value.state) == (JOB, "InProgress")
    assert "keeps running" in str(ei.value)
    with pytest.raises(ValueError, match="timeout"):
        h.run(j.wait(timeout=0))


def test_abort(h: Harness, mock: respx.MockRouter) -> None:
    routes = ingest_routes(mock)
    sf = h.client()
    j = h.run(sf.bulk.ingest("Lead", "insert", rows=[{"LastName": "Doe"}]))
    routes["close"].respond(json=job(state="Aborted"))
    assert h.run(j.abort()).state is esf.BulkJobState.ABORTED
    assert json.loads(routes["close"].calls.last.request.content) == {"state": "Aborted"}
    calls = routes["close"].call_count
    h.run(j.abort())  # already final: nothing to send
    assert routes["close"].call_count == calls


# --- results --------------------------------------------------------------------------------------


def test_results_are_parsed_and_always_utf8(h: Harness, mock: respx.MockRouter, rec: Recorder) -> None:
    """simple-salesforce #706/#710: result CSVs are UTF-8 whatever charset the response names."""
    ingest_routes(mock)
    ok = mock.get(path=f"{JOBS}/successfulResults/").mock(
        return_value=csv_response('"sf__Id","sf__Created",LastName\n00Qxx0000000001AAA,true,Zoë\n')
    )
    mock.get(path=f"{JOBS}/failedResults/").mock(
        return_value=csv_response(
            '"sf__Id","sf__Error",LastName,Email\n'
            ",REQUIRED_FIELD_MISSING:Required fields are missing: [Company]:Company --,Doe,d@example.com\n"
        )
    )
    mock.get(path=f"{JOBS}/unprocessedrecords/").mock(return_value=csv_response("LastName\nRoe\n"))
    sf = h.client(recorder=rec)
    j = h.run(sf.bulk.ingest("Lead", "insert", rows=[{"LastName": "Doe"}]))
    assert h.collect(j.successful_results()) == [
        {"sf__Id": "00Qxx0000000001AAA", "sf__Created": "true", "LastName": "Zoë"}
    ]
    assert ok.calls.last.request.headers["accept"] == "text/csv"
    (failure,) = h.collect(j.failed_results())
    assert failure == esf.BulkFailure(
        error=esf.ErrorDetail(
            "REQUIRED_FIELD_MISSING", "Required fields are missing: [Company]", ("Company",)
        ),
        id=None,
        fields={"LastName": "Doe", "Email": "d@example.com"},
    )
    assert h.collect(j.unprocessed_records()) == [{"LastName": "Roe"}]
    results = [e for e in rec.requests if e.fields["operation"] == "bulk.ingest.results"]
    assert len(results) == 3
    assert all(e.fields["outcome"] == "ok" for e in results)


def test_results_use_the_api_version_of_the_job(h: Harness, mock: respx.MockRouter) -> None:
    """A different API version than the one that created the job returns 409, so the job pins it."""
    mock.post(f"{DATA}/jobs/ingest").respond(json=job(apiVersion=66.0))
    old = f"/services/data/v66.0/jobs/ingest/{JOB}"
    upload = mock.put(path=f"{old}/batches").respond(201)
    mock.patch(path=old).respond(json=job(apiVersion=66.0, state="UploadComplete"))
    status = mock.get(path=old).respond(json=job("bulk/ingest_job_complete.json", apiVersion=66.0))
    results = mock.get(path=f"{old}/successfulResults/").mock(return_value=csv_response("sf__Id\n"))
    sf = h.client()
    j = h.run(sf.bulk.ingest("Lead", "insert", rows=[{"LastName": "Doe"}]))
    assert h.run(j.wait()).api_version == "66.0"
    assert h.collect(j.successful_results()) == []
    assert upload.called
    assert status.called
    assert results.called


# --- query ----------------------------------------------------------------------------------------


def test_query_follows_locators_in_order(h: Harness, mock: respx.MockRouter, rec: Recorder) -> None:
    routes = query_routes(
        mock,
        csv_response('"Id","Name","Owner.Name"\n00Qxx0000000001AAA,Zoë,\n', locator="MTAwMDA"),
        csv_response('"Id","Name","Owner.Name"\n00Qxx0000000002AAA,"Doe, J",Ann\n', locator="null"),
    )
    sf = h.client(recorder=rec)
    rows = h.collect(sf.bulk.query("SELECT Id, Name, Owner.Name FROM Lead WHERE Company = {}", "O'Neil"))
    assert rows == [
        {"Id": "00Qxx0000000001AAA", "Name": "Zoë", "Owner.Name": None},
        {"Id": "00Qxx0000000002AAA", "Name": "Doe, J", "Owner.Name": "Ann"},
    ]
    body = json.loads(routes["create"].calls.last.request.content)
    assert body == {
        "operation": "query",
        "query": "SELECT Id, Name, Owner.Name FROM Lead WHERE Company = 'O\\'Neil'",
        "contentType": "CSV",
        "columnDelimiter": "COMMA",
        "lineEnding": "LF",
    }
    first, second = routes["results"].calls
    assert "locator" not in first.request.url.params
    assert second.request.url.params["locator"] == "MTAwMDA"
    assert first.request.headers["accept"] == "text/csv"
    create = next(e for e in rec.requests if e.fields["operation"] == "bulk.query.create")
    assert create.fields["sobject"] == "Lead"


def test_query_include_deleted_uses_query_all(h: Harness, mock: respx.MockRouter) -> None:
    routes = query_routes(mock, csv_response("Id\n"))
    sf = h.client()
    assert h.collect(sf.bulk.query("SELECT Id FROM Lead", include_deleted=True)) == []
    assert json.loads(routes["create"].calls.last.request.content)["operation"] == "queryAll"


def test_query_waits_for_the_job(h: Harness, mock: respx.MockRouter, rec: Recorder) -> None:
    """Query job info has ``numberRecordsProcessed`` but no ``numberRecordsFailed``
    (*Get Information About a Query Job*; big-bang.md §21 V82)."""
    routes = query_routes(mock, csv_response("Id\n1\n"))
    routes["status"].side_effect = [
        httpx.Response(200, json=job("bulk/query_job.json", state="InProgress")),
        httpx.Response(200, json=job("bulk/query_job.json", state="InProgress", numberRecordsProcessed=500)),
        httpx.Response(200, json=job("bulk/query_job.json", state="InProgress", numberRecordsProcessed=500)),
        httpx.Response(200, json=job("bulk/query_job.json", state="JobComplete", numberRecordsProcessed=900)),
    ]
    sf = h.client(recorder=rec)
    rows = h.collect(sf.bulk.query("SELECT Id FROM Lead", poll=esf.Backoff(start=1, max=1)))
    assert rows == [{"Id": "1"}]
    assert sf.sleeps == [1, 1, 1]
    events = [(e.fields["state"], e.fields["records_processed"]) for e in rec.named("sf.bulk.job")]
    # created, then each poll that changed the state or the progress; the unchanged poll is quiet
    assert events == [("UploadComplete", 0), ("InProgress", 0), ("InProgress", 500), ("JobComplete", 900)]
    assert all(e.fields["records_failed"] is None for e in rec.named("sf.bulk.job"))


def test_query_stop_early_closes_the_stream(h: Harness, mock: respx.MockRouter, rec: Recorder) -> None:
    routes = query_routes(mock, csv_response("Id\n1\n2\n", locator="Mg"), csv_response("Id\n3\n"))
    sf = h.client(recorder=rec)
    assert first_and_stop(h, sf.bulk.query("SELECT Id FROM Lead")) == {"Id": "1"}
    assert routes["results"].call_count == 1
    (event,) = [e for e in rec.requests if e.fields["operation"] == "bulk.query.results"]
    assert event.fields["outcome"] == "ok"


def test_query_compound_field_failure_has_a_hint(h: Harness, mock: respx.MockRouter) -> None:
    """Bulk queries can't select compound address or geolocation fields (*Bulk API 2.0 Query*)."""
    describe = {
        "name": "Account",
        "fields": [
            {"name": "Id", "type": "id"},
            {"name": "BillingAddress", "type": "address"},
            {"name": "BillingStreet", "type": "textarea", "compoundFieldName": "BillingAddress"},
            {"name": "BillingCity", "type": "string", "compoundFieldName": "BillingAddress"},
        ],
    }
    mock.get(f"{DATA}/sobjects/Account/describe").respond(json=describe)
    mock.post(f"{DATA}/jobs/query").respond(json=job("bulk/query_job.json", object="Account"))
    mock.get(path=QJOBS).respond(
        json=job(
            "bulk/query_job.json", object="Account", state="Failed", errorMessage="FUNCTIONALITY_NOT_ENABLED"
        )
    )
    sf = h.client()
    fields = h.run(sf.bulk.expand_compound_fields("Account", ["Id", "BillingAddress"]))
    assert fields == ["Id", "BillingStreet", "BillingCity"]
    with pytest.raises(esf.BulkJobFailed) as ei:
        h.collect(sf.bulk.query("SELECT Id, BillingAddress FROM Account"))
    assert ei.value.operation == "bulk.query"
    assert ei.value.hint is not None
    assert "BillingAddress -> BillingStreet, BillingCity" in ei.value.hint
    assert "BillingAddress -> BillingStreet" in str(ei.value)


def test_query_create_error_gets_the_compound_hint(h: Harness, mock: respx.MockRouter) -> None:
    mock.post(f"{DATA}/jobs/query").respond(
        400, json=[{"errorCode": "INVALIDJOB", "message": "compound fields are not supported by Bulk API"}]
    )
    sf = h.client()
    with pytest.raises(esf.RequestError) as ei:
        h.collect(sf.bulk.query("SELECT Id, BillingAddress FROM Account"))
    assert ei.value.hint is not None
    assert "components" in ei.value.hint


def test_query_rejects_bad_arguments(h: Harness, mock: respx.MockRouter) -> None:
    create = mock.post(f"{DATA}/jobs/query").respond(json=job("bulk/query_job.json"))
    sf = h.client()
    with pytest.raises(ValueError, match="parallel_downloads"):
        h.collect(sf.bulk.query("SELECT Id FROM Lead", parallel_downloads=0))
    # Bulk API 2.0 query jobs don't run FIELDS(ALL) or FIELDS(CUSTOM); see core/bulk.py.
    with pytest.raises(ValueError, match=r"don't support FIELDS\(CUSTOM\)"):
        h.collect(sf.bulk.query("SELECT fields(custom) FROM Lead LIMIT {}", 200))
    assert not create.called
    with pytest.raises(TypeError, match="not a string"):
        h.run(sf.bulk.expand_compound_fields("Account", "BillingAddress"))  # type: ignore[arg-type]


# --- parallel downloads ---------------------------------------------------------------------------


class _Pages:
    """Serves resultPages and result pages, counting how many page downloads overlap."""

    def __init__(self, delay: float = 0.02) -> None:
        self.delay = delay
        self.lock = threading.Lock()
        self.in_flight = 0
        self.max_in_flight = 0

    def pages(self, request: httpx.Request) -> httpx.Response:
        body = load_fixture("bulk/result_pages.json")
        if request.url.params.get("locator") == "NA":
            body = {
                "resultPages": [
                    {"resultUrl": f"{QJOBS}/results?locator=NA&maxRecords=2"},
                    {"resultUrl": f"{QJOBS}/results?locator=NQ&maxRecords=2"},
                ],
                "nextRecordsUrl": None,
                "done": True,
            }
        return httpx.Response(200, json=body)

    def _enter(self) -> None:
        with self.lock:
            self.in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self.in_flight)

    def _leave(self, request: httpx.Request) -> httpx.Response:
        with self.lock:
            self.in_flight -= 1
        loc = request.url.params["locator"]
        return csv_response(f"Id\n{loc}-1\n{loc}-2\n")

    async def async_page(self, request: httpx.Request) -> httpx.Response:
        self._enter()
        await asyncio.sleep(self.delay)
        return self._leave(request)

    def sync_page(self, request: httpx.Request) -> httpx.Response:
        self._enter()
        time.sleep(self.delay)
        return self._leave(request)


@pytest.mark.parametrize("n", [2, 8])
def test_parallel_downloads_keep_order_and_bound_concurrency(
    h: Harness, mock: respx.MockRouter, n: int
) -> None:
    pages = _Pages()
    mock.post(f"{DATA}/jobs/query").respond(json=job("bulk/query_job.json"))
    mock.get(path=QJOBS).respond(json=job("bulk/query_job.json", state="JobComplete"))
    listing = mock.get(path=f"{QJOBS}/resultPages").mock(side_effect=pages.pages)
    mock.get(path=f"{QJOBS}/results").mock(side_effect=pages.async_page if h.is_async else pages.sync_page)
    sf = h.client()
    rows = h.collect(sf.bulk.query("SELECT Id FROM Lead", parallel_downloads=n))
    assert [r["Id"] for r in rows] == [f"{loc}-{i}" for loc in ("MQ", "Mg", "Mw", "NA", "NQ") for i in (1, 2)]
    assert listing.call_count == 2
    assert 1 < pages.max_in_flight <= min(n, 3)


def test_parallel_downloads_reject_foreign_urls(h: Harness, mock: respx.MockRouter) -> None:
    mock.post(f"{DATA}/jobs/query").respond(json=job("bulk/query_job.json"))
    mock.get(path=QJOBS).respond(json=job("bulk/query_job.json", state="JobComplete"))
    mock.get(path=f"{QJOBS}/resultPages").respond(
        json={"resultPages": [{"resultUrl": "https://evil.example.com/steal"}], "done": True}
    )
    sf = h.client()
    with pytest.raises(esf.UnexpectedResponse, match="not a result of this job"):
        h.collect(sf.bulk.query("SELECT Id FROM Lead", parallel_downloads=3))


def test_parallel_download_error_is_raised(h: Harness, mock: respx.MockRouter) -> None:
    mock.post(f"{DATA}/jobs/query").respond(json=job("bulk/query_job.json"))
    mock.get(path=QJOBS).respond(json=job("bulk/query_job.json", state="JobComplete"))
    mock.get(path=f"{QJOBS}/resultPages").respond(
        json=load_fixture("bulk/result_pages.json") | {"done": True}
    )
    results = mock.get(path=f"{QJOBS}/results")
    results.side_effect = [
        csv_response("Id\n1\n"),
        httpx.Response(404, json=[{"errorCode": "NOT_FOUND", "message": "gone"}]),
        csv_response("Id\n3\n"),
    ]
    sf = h.client()
    with pytest.raises(esf.NotFound):
        h.collect(sf.bulk.query("SELECT Id FROM Lead", parallel_downloads=2))


# --- memory stays flat ----------------------------------------------------------------------------


class _Org:
    """An httpx transport playing a Bulk org: reads uploads chunk by chunk, streams results."""

    def __init__(self, result_bytes: int = 0) -> None:
        self.uploaded = 0
        self.result_bytes = result_bytes

    def _json(self, request: httpx.Request, body: dict[str, Any]) -> httpx.Response:
        return httpx.Response(200, json=body, request=request)

    def _route(self, request: httpx.Request) -> httpx.Response | None:
        path = request.url.path
        if request.method == "POST" and path.endswith("/jobs/ingest"):
            return self._json(request, job())
        if request.method == "POST" and path.endswith("/jobs/query"):
            return self._json(request, job("bulk/query_job.json"))
        if request.method == "PATCH":
            return self._json(request, job(state="UploadComplete"))
        if request.method == "GET" and path.endswith(QJOB):
            return self._json(request, job("bulk/query_job.json", state="JobComplete"))
        return None

    def _rows(self) -> Iterator[bytes]:
        yield b"Id,Name\n"
        line = b"00Qxx0000000001AAA," + b"n" * 45 + b"\n"
        block = line * (64 * 1024 // len(line))
        sent = 0
        while sent < self.result_bytes:
            yield block
            sent += len(block)


class _AsyncOrg(_Org, httpx.AsyncBaseTransport):
    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if request.method == "PUT":
            assert isinstance(request.stream, httpx.AsyncByteStream)
            async for chunk in request.stream:
                self.uploaded += len(chunk)
            return httpx.Response(201, request=request)
        found = self._route(request)
        if found is not None:
            return found

        async def body() -> AsyncIterator[bytes]:
            for chunk in self._rows():
                yield chunk

        return httpx.Response(200, headers={"content-type": "text/csv"}, content=body(), request=request)


class _SyncOrg(_Org, httpx.BaseTransport):
    def handle_request(self, request: httpx.Request) -> httpx.Response:
        if request.method == "PUT":
            assert isinstance(request.stream, httpx.SyncByteStream)
            for chunk in request.stream:
                self.uploaded += len(chunk)
            return httpx.Response(201, request=request)
        found = self._route(request)
        if found is not None:
            return found
        return httpx.Response(
            200, headers={"content-type": "text/csv"}, content=self._rows(), request=request
        )


def _org_client(h: Harness, org: _Org) -> Any:
    http: Any = httpx.AsyncClient(transport=org) if h.is_async else httpx.Client(transport=org)  # type: ignore[arg-type]
    return h.client(http_client=http)


def _peak(fn: Callable[[], Any]) -> int:
    tracemalloc.start()
    try:
        fn()
        return tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()


def test_ingest_from_path_keeps_memory_flat(h: Harness, tmp_path: Path) -> None:
    """simple-salesforce #738/#629: a 32 MiB CSV is streamed from disk, never read whole."""
    size = 32 * 1024 * 1024
    path = tmp_path / "big.csv"
    line = b"Doe," + b"x" * 59 + b"\n"
    with path.open("wb") as f:
        f.write(b"LastName,Description\n")
        block = line * 4096
        for _ in range(size // len(block)):
            f.write(block)
    org = (_AsyncOrg if h.is_async else _SyncOrg)()
    sf = _org_client(h, org)
    peak = _peak(lambda: h.run(sf.bulk.ingest("Lead", "insert", rows=path)))
    assert org.uploaded > size - len(block)
    assert peak < 2 * 1024 * 1024, f"peak {peak} bytes for a {size}-byte file"


def test_ingest_from_rows_keeps_memory_flat(h: Harness) -> None:
    """Rows are written to a temporary file as they come, not collected in a list."""
    n = 60_000
    org = (_AsyncOrg if h.is_async else _SyncOrg)()
    sf = _org_client(h, org)

    def rows() -> Iterator[dict[str, Any]]:
        for i in range(n):
            yield {"LastName": f"Doe{i:06d}", "Description": "y" * 90}

    peak = _peak(lambda: h.run(sf.bulk.ingest("Lead", "insert", rows=rows())))
    assert org.uploaded > n * 100
    # The spooled temporary file keeps its first 1 MiB in memory before it moves to disk.
    assert peak < 3 * 1024 * 1024, f"peak {peak} bytes for {org.uploaded} uploaded bytes"


def test_query_results_keep_memory_flat(h: Harness) -> None:
    """A 12 MiB result page is parsed as it streams in."""
    size = 12 * 1024 * 1024
    org = (_AsyncOrg if h.is_async else _SyncOrg)(result_bytes=size)
    sf = _org_client(h, org)
    count = [0]

    def run() -> None:
        it = sf.bulk.query("SELECT Id, Name FROM Lead")
        if h.is_async:

            async def go() -> None:
                async for _ in it:
                    count[0] += 1

            h.run(go())
        else:
            for _ in it:
                count[0] += 1

    peak = _peak(run)
    assert count[0] > size // 100
    assert peak < 2 * 1024 * 1024, f"peak {peak} bytes for a {size}-byte result"
