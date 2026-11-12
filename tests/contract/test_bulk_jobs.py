"""Bulk API 2.0 job handles (§9.11) for both clients: ``start_query``, ``query_job``,
``ingest_job`` and resumable result pages.

Job info bodies are the recorded ones from ``test_bulk.py``. The CSV pages are **synthetic**.
Salesforce behaviour relied on, from the *Bulk API 2.0 and Bulk API Developer Guide*
(https://resources.docs.salesforce.com/latest/latest/en-us/sfdc/pdf/api_asynch.pdf):

- *Get Results for a Query Job*: "The job must have the state JobComplete"; ``locator`` and
  ``maxRecords`` query parameters; "As long as the associated job exists, the locator string
  for a set of results does not change. You can use the locator to retrieve a set of results
  multiple times."; ``Sforce-NumberOfRecords`` and ``Sforce-Locator`` response headers.
- *Get Information About a Query Job*: a deleted job returns 404 ``NOT_FOUND``.
- *Abort a Query Job*: ``PATCH jobs/query/{id}`` with ``{"state": "Aborted"}``.

And from *Salesforce Developer Limits and Allocations Quick Reference*
(https://resources.docs.salesforce.com/latest/latest/en-us/sfdc/pdf/salesforce_app_limits_cheatsheet.pdf):
query job results can be retrieved within 7 days of job completion.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
import respx

import even_simpler_salesforce as esf

from ..conftest import DATA, Harness, Recorder
from .test_bulk import JOB, JOBS, QJOB, QJOBS, csv_response, ingest_routes, job, query_routes

JOB2 = "750xx0000000003AAA"


def read_pages(h: Harness, it: Any, *, stop_after: int | None = None) -> list[tuple[Any, ...]]:
    """(locator, next_locator, size, rows) per page; stop after ``stop_after`` pages."""
    out: list[tuple[Any, ...]] = []
    if h.is_async:

        async def go() -> None:
            async for page in it:
                assert "next_locator=" in repr(page)
                rows = [r async for r in page.rows()]
                again = [r async for r in page.rows()]
                assert again == rows  # a page can be read again
                out.append((page.locator, page.next_locator, page.size, rows))
                if stop_after is not None and len(out) == stop_after:
                    await it.aclose()
                    return

        h.run(go())
        return out
    for page in it:
        assert "next_locator=" in repr(page)
        rows = list(page.rows())
        assert list(page.rows()) == rows
        out.append((page.locator, page.next_locator, page.size, rows))
        if stop_after is not None and len(out) == stop_after:
            it.close()
            break
    return out


def numbered(text: str, locator: str, count: int) -> httpx.Response:
    response = csv_response(text, locator=locator)
    response.headers["sforce-numberofrecords"] = str(count)
    return response


# --- start_query ----------------------------------------------------------------------------------


def test_start_query_returns_the_job_without_waiting(
    h: Harness, mock: respx.MockRouter, rec: Recorder
) -> None:
    routes = query_routes(mock, csv_response("Id,Name\n1,Ann\n2,\n"))
    sf = h.client(recorder=rec)
    j = h.run(sf.bulk.start_query("SELECT Id, Name FROM Lead WHERE Status = {}", "Open"))
    assert j.id == QJOB
    assert j.info.state is esf.BulkJobState.UPLOAD_COMPLETE
    assert QJOB in repr(j)
    assert json.loads(routes["create"].calls.last.request.content)["query"] == (
        "SELECT Id, Name FROM Lead WHERE Status = 'Open'"
    )
    assert not routes["status"].called
    assert h.run(j.wait()).state is esf.BulkJobState.JOB_COMPLETE
    assert h.collect(j.rows()) == [{"Id": "1", "Name": "Ann"}, {"Id": "2", "Name": None}]


def test_start_query_validates_before_sending(h: Harness, mock: respx.MockRouter) -> None:
    sf = h.client()
    with pytest.raises(ValueError, match="FIELDS"):
        h.run(sf.bulk.start_query("SELECT FIELDS(ALL) FROM Lead LIMIT 200"))
    assert not mock.calls


def test_start_query_create_error_gets_the_compound_hint(h: Harness, mock: respx.MockRouter) -> None:
    describe = {"name": "Account", "fields": [{"name": "BillingAddress", "type": "address"}]}
    mock.get(f"{DATA}/sobjects/Account/describe").respond(json=describe)
    mock.post(f"{DATA}/jobs/query").respond(
        400,
        json=[{"errorCode": "API_ERROR", "message": "Selecting compound data not supported in Bulk Query"}],
    )
    sf = h.client()
    h.run(sf.sobject("Account").describe())  # cache the describe
    with pytest.raises(esf.RequestError) as ei:
        h.run(sf.bulk.start_query("SELECT Id, BillingAddress FROM Account"))
    assert ei.value.hint is not None
    assert "BillingAddress" in ei.value.hint


def test_rows_before_complete_raises_not_complete(h: Harness, mock: respx.MockRouter) -> None:
    routes = query_routes(mock)
    sf = h.client()
    j = h.run(sf.bulk.start_query("SELECT Id FROM Lead"))
    with pytest.raises(esf.BulkJobNotComplete) as ei:
        h.collect(j.rows())
    assert (ei.value.job_id, ei.value.state) == (QJOB, "UploadComplete")
    assert ei.value.operation == "bulk.query.results"
    assert "call wait()" in str(ei.value)
    with pytest.raises(esf.BulkJobNotComplete):
        h.collect(j.pages())
    assert not routes["results"].called


@pytest.mark.parametrize("state", ["Failed", "Aborted"])
def test_rows_of_a_failed_job_raise_bulk_job_failed(h: Harness, mock: respx.MockRouter, state: str) -> None:
    mock.get(path=QJOBS).respond(json=job("query", state=state, errorMessage="boom"))
    sf = h.client()
    j = h.run(sf.bulk.query_job(QJOB))
    with pytest.raises(esf.BulkJobFailed) as ei:
        h.collect(j.rows())
    assert (ei.value.state, ei.value.error_message) == (state, "boom")
    with pytest.raises(esf.BulkJobFailed):
        h.collect(j.pages())


def test_rows_reject_bad_parallel_downloads(h: Harness, mock: respx.MockRouter) -> None:
    mock.get(path=QJOBS).respond(json=job("query", state="JobComplete"))
    sf = h.client()
    j = h.run(sf.bulk.query_job(QJOB))
    with pytest.raises(ValueError, match="parallel_downloads"):
        h.collect(j.rows(parallel_downloads=0))


def test_query_job_wait_failure_keeps_the_last_state(h: Harness, mock: respx.MockRouter) -> None:
    routes = query_routes(mock, final="Failed")
    sf = h.client()
    j = h.run(sf.bulk.start_query("SELECT Id FROM Lead"))
    with pytest.raises(esf.BulkJobFailed):
        h.run(j.wait())
    assert j.info.state is esf.BulkJobState.FAILED
    assert routes["status"].call_count == 1


def test_query_job_refresh_and_abort(h: Harness, mock: respx.MockRouter, rec: Recorder) -> None:
    routes = query_routes(mock, final="InProgress")
    abort = mock.patch(path=QJOBS).respond(json=job("query", state="Aborted"))
    sf = h.client(recorder=rec)
    j = h.run(sf.bulk.start_query("SELECT Id FROM Lead"))
    assert h.run(j.refresh()).state is esf.BulkJobState.IN_PROGRESS
    assert h.run(j.abort()).state is esf.BulkJobState.ABORTED
    assert json.loads(abort.calls.last.request.content) == {"state": "Aborted"}
    assert h.run(j.refresh()).state is esf.BulkJobState.ABORTED  # final: no call
    h.run(j.abort())
    assert (routes["status"].call_count, abort.call_count) == (1, 1)
    assert "bulk.query.abort" in [e.fields["operation"] for e in rec.requests]


# --- open a job again -----------------------------------------------------------------------------


def test_query_job_reopens_and_pins_the_job_api_version(h: Harness, mock: respx.MockRouter) -> None:
    """Results need the API version that created the job (409 otherwise)."""
    status = mock.get(path=QJOBS).respond(json=job("query", state="JobComplete", apiVersion=66.0))
    old = f"/services/data/v66.0/jobs/query/{QJOB}"
    results = mock.get(path=f"{old}/results").mock(return_value=csv_response("Id\n1\n"))
    sf = h.client()
    j = h.run(sf.bulk.query_job(QJOB))
    assert (j.id, j.info.api_version) == (QJOB, "66.0")
    assert h.collect(j.rows()) == [{"Id": "1"}]
    assert status.called
    assert results.called


def test_query_job_rejects_an_ingest_job_and_bad_ids(h: Harness, mock: respx.MockRouter) -> None:
    # Synthetic: the org answers 404 for the other kind (V88); this guards a wrong-kind response.
    mock.get(path=QJOBS).respond(json=job())  # an ingest job's info (operation insert)
    sf = h.client()
    with pytest.raises(ValueError, match="ingest job"):
        h.run(sf.bulk.query_job(QJOB))
    with pytest.raises(ValueError):
        h.run(sf.bulk.query_job("not-an-id"))


def test_query_job_unknown_id_is_not_found(h: Harness, mock: respx.MockRouter) -> None:
    mock.get(path=QJOBS).respond(
        404, json=[{"errorCode": "NOT_FOUND", "message": "The requested resource does not exist"}]
    )
    sf = h.client()
    with pytest.raises(esf.NotFound) as ei:
        h.run(sf.bulk.query_job(QJOB))
    assert ei.value.operation == "bulk.query.status"


def test_ingest_job_reopens_every_part(h: Harness, mock: respx.MockRouter) -> None:
    second = f"/services/data/v68.0/jobs/ingest/{JOB2}"
    mock.get(path=JOBS).respond(json=job("ingest_complete", state="JobComplete"))
    mock.get(path=second).respond(json=job("ingest_complete", id=JOB2, state="InProgress"))
    mock.get(path=f"{JOBS}/failedResults/").mock(return_value=csv_response("sf__Id,sf__Error,LastName\n"))
    sf = h.client()
    j = h.run(sf.bulk.ingest_job([JOB, JOB2]))
    assert j.ids == (JOB, JOB2)
    assert j.info.state is esf.BulkJobState.IN_PROGRESS  # the least finished part
    assert [p.state for p in j.parts] == [esf.BulkJobState.JOB_COMPLETE, esf.BulkJobState.IN_PROGRESS]


def test_ingest_job_round_trips_job_ids(h: Harness, mock: respx.MockRouter) -> None:
    routes = ingest_routes(mock)
    sf = h.client()
    j = h.run(sf.bulk.ingest("Lead", "insert", rows=[{"LastName": "Doe"}]))
    again = h.run(sf.bulk.ingest_job(j.ids))
    assert again.ids == j.ids
    assert h.run(again.wait()).state is esf.BulkJobState.JOB_COMPLETE
    assert routes["status"].called


def test_ingest_job_rejects_bad_arguments(h: Harness, mock: respx.MockRouter) -> None:
    sf = h.client()
    with pytest.raises(TypeError, match="not a string"):
        h.run(sf.bulk.ingest_job(JOB))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="empty"):
        h.run(sf.bulk.ingest_job([]))
    with pytest.raises(ValueError):
        h.run(sf.bulk.ingest_job([JOB, "nope"]))
    assert not mock.calls
    mock.get(path=f"/services/data/v68.0/jobs/ingest/{QJOB}").respond(json=job("query"))
    with pytest.raises(ValueError, match="query job"):
        h.run(sf.bulk.ingest_job([QJOB]))


# --- pages ----------------------------------------------------------------------------------------


def _complete_query_job(h: Harness, mock: respx.MockRouter) -> Any:
    mock.get(path=QJOBS).respond(json=job("query", state="JobComplete"))
    return h.run(h.client().bulk.query_job(QJOB))


def test_pages_report_locators_and_sizes(h: Harness, mock: respx.MockRouter, rec: Recorder) -> None:
    results = mock.get(path=f"{QJOBS}/results")
    results.side_effect = [
        numbered("Id,Name\n1,Ann\n2,\n", "MjAw", 2),
        numbered("Id,Name\n3,Bo\n", "null", 1),
    ]
    j = _complete_query_job(h, mock)
    pages = read_pages(h, j.pages(max_records=2))
    assert pages == [
        (None, "MjAw", 2, [{"Id": "1", "Name": "Ann"}, {"Id": "2", "Name": None}]),
        ("MjAw", None, 1, [{"Id": "3", "Name": "Bo"}]),
    ]
    first, second = results.calls
    assert dict(first.request.url.params) == {"maxRecords": "2"}
    assert dict(second.request.url.params) == {"locator": "MjAw", "maxRecords": "2"}
    assert first.request.headers["accept"] == "text/csv"


def test_pages_resume_from_a_saved_locator(h: Harness, mock: respx.MockRouter) -> None:
    results = mock.get(path=f"{QJOBS}/results")
    results.side_effect = [csv_response("Id\n3\n", locator="null")]
    j = _complete_query_job(h, mock)
    pages = read_pages(h, j.pages(locator="MjAw"))
    assert pages == [("MjAw", None, None, [{"Id": "3"}])]
    assert dict(results.calls.last.request.url.params) == {"locator": "MjAw"}


def test_pages_stop_early_and_closed_pages(h: Harness, mock: respx.MockRouter) -> None:
    results = mock.get(path=f"{QJOBS}/results")
    results.side_effect = [csv_response("Id\n1\n", locator="MQ"), csv_response("Id\n2\n", locator="null")]
    j = _complete_query_job(h, mock)
    assert read_pages(h, j.pages(), stop_after=1) == [(None, "MQ", None, [{"Id": "1"}])]
    assert results.call_count == 1

    # A page kept past the iteration is closed.
    results.side_effect = [csv_response("Id\n1\n", locator="null")]
    kept = h.collect(j.pages())
    assert len(kept) == 1
    with pytest.raises(ValueError, match="closed"):
        h.collect(kept[0].rows())


@pytest.mark.parametrize(
    "kw", [{"locator": ""}, {"locator": 5}, {"max_records": 0}, {"max_records": True}, {"max_records": "10"}]
)
def test_pages_reject_bad_arguments(h: Harness, mock: respx.MockRouter, kw: dict[str, Any]) -> None:
    j = _complete_query_job(h, mock)
    with pytest.raises(ValueError):
        h.collect(j.pages(**kw))


def test_query_still_runs_in_one_call(h: Harness, mock: respx.MockRouter) -> None:
    """``sf.bulk.query()`` is ``start_query()``, ``wait()`` and ``rows()`` in one call."""
    query_routes(mock, csv_response("Id\n1\n"))
    sf = h.client()
    with pytest.raises(ValueError, match="parallel_downloads"):
        h.collect(sf.bulk.query("SELECT Id FROM Lead", parallel_downloads=0))
    assert not mock.calls
    assert h.collect(sf.bulk.query("SELECT Id FROM Lead")) == [{"Id": "1"}]
