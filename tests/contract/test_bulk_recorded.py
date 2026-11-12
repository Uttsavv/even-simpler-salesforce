"""``sf.bulk`` against **recorded** responses, for both clients.

The fixtures in ``tests/fixtures/recorded/bulk/*.json`` (except the ``synthetic`` ones) were
recorded with ``esf.testing.record`` from a Developer Edition org at API 68.0 (and 60.0 for
``result_pages_v60.json``) on 2026-11-12, by ``tests/sandbox/test_bulk.py`` with
``ESF_SANDBOX_RECORD_BULK=1``. Ids, hosts and SOQL literals are masked. Each test replays one
recording with the same ``sf.bulk`` calls the sandbox test made (big-bang.md §21 V18, V40-V44,
V81, V82).
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

import even_simpler_salesforce as esf
from even_simpler_salesforce.core.bulk import parse_job_info

from ..conftest import Harness
from ..fixtures import recorded_body, recording

POLL = esf.Backoff(start=1, max=3)


def replay_client(h: Harness, name: str, **kw: Any) -> Any:
    transport = esf.testing.replay(recording(f"bulk/{name}.json"))
    http: Any = httpx.AsyncClient(transport=transport) if h.is_async else httpx.Client(transport=transport)
    return h.client(esf.StaticToken("x", "https://example.my.salesforce.com"), http_client=http, **kw)


def ingest(h: Harness, sf: Any, *args: Any, **kw: Any) -> tuple[Any, list[dict[str, str]]]:
    job = h.run(sf.bulk.ingest(*args, **kw))
    h.run(job.wait(poll=POLL))
    return job, h.collect(job.successful_results())


def test_recorded_job_info_shapes() -> None:
    """V44: ``apiVersion`` is a JSON number and dates use ``.000+0000``; ingest job info has
    ``numberRecordsFailed``, query job info doesn't (V82)."""
    created = recorded_body("bulk/csv_semantics.json", "POST", "/jobs/ingest")
    assert created["apiVersion"] == 68.0
    assert isinstance(created["apiVersion"], float)
    assert created["createdDate"].endswith(".000+0000")
    info = parse_job_info(created, "60.0")
    assert info.api_version == "68.0"
    assert info.created_date is not None
    assert info.created_date.utcoffset() is not None
    done = recorded_body("bulk/failed_results.json", "GET", "/jobs/ingest/750000000000001AAA", -1)
    assert (done["state"], done["numberRecordsProcessed"], done["numberRecordsFailed"]) == (
        "JobComplete",
        7,
        6,
    )
    query = recorded_body("bulk/query_errors.json", "GET", "/jobs/query/750000000000001AAA", -1)
    assert query["state"] == "JobComplete"
    assert "numberRecordsFailed" not in query
    assert isinstance(query["numberRecordsProcessed"], int)


def test_recorded_csv_semantics(h: Harness) -> None:
    """V18/V41: replay of insert, update, upsert (insert, then update) and a BOM upload."""
    sf = replay_client(h, "csv_semantics")
    job, ok = ingest(h, sf, "Lead", "insert", rows=[{"LastName": "a", "Company": "c"}])
    assert job.info.state is esf.BulkJobState.JOB_COMPLETE
    assert (job.info.records_processed, job.info.records_failed) == (3, 0)
    assert {r["sf__Created"] for r in ok} == {"true"}
    first = next(r for r in ok if r["FirstName"])
    assert (first["FirstName"], first["Title"]) == ("Zoë Ünïcødé", "Dr. Ø")
    assert first["Description"] == '日本語 🙂 «q» "x", y\nz'
    # Rows sent with None come back as "#N/A"? No: the results echo empty cells for both.
    assert {r["Status"] for r in ok if not r["FirstName"]} == {""}
    _, ok = ingest(h, sf, "Lead", "update", rows=[{"Id": "00Q000000000003EAA"}])
    assert {r["sf__Created"] for r in ok} == {"false"}
    _, ok = ingest(h, sf, "Lead", "upsert", rows=[{"Esf_Ext__c": "x"}], external_id_field="Esf_Ext__c")
    assert {r["sf__Created"] for r in ok} == {"true"}
    _, ok = ingest(h, sf, "Lead", "upsert", rows=[{"Esf_Ext__c": "x"}], external_id_field="Esf_Ext__c")
    assert [r["sf__Created"] for r in ok] == ["false"]
    _, ok = ingest(h, sf, "Lead", "insert", rows=b"\xef\xbb\xbfLastName,Company\nb,c\n")
    assert list(ok[0]) == ["sf__Id", "sf__Created", "LastName", "Company"]


def test_recorded_failed_results(h: Harness) -> None:
    """V41: real ``sf__Error`` cells parse into code, message and fields."""
    sf = replay_client(h, "failed_results")
    job, ok = ingest(h, sf, "Lead", "insert", rows=[{"LastName": "a"}])
    assert len(ok) == 1
    errors = sorted(
        ((f.error.error_code, f.error.message, f.error.fields) for f in h.collect(job.failed_results())),
        key=repr,
    )
    assert errors == sorted(
        [
            ("FIELD_CUSTOM_VALIDATION_EXCEPTION", "esf-sandbox: validation rule probe", ()),
            ("REQUIRED_FIELD_MISSING", "Required fields are missing: [LastName]", ("LastName",)),
            ("INVALID_EMAIL_ADDRESS", "Email: invalid email address: not-an-email", ("Email",)),
            (
                "REQUIRED_FIELD_MISSING",
                "Required fields are missing: [LastName, Company]",
                ("LastName", "Company"),
            ),
            ("FIELD_CUSTOM_VALIDATION_EXCEPTION", "esf-sandbox: flow custom error probe", ()),
            ("FIELD_CUSTOM_VALIDATION_EXCEPTION", "esf-sandbox: addError probe", ()),
        ],
        key=repr,
    )


def test_recorded_query_errors(h: Harness) -> None:
    """V42/V81: compound fields and FIELDS(STANDARD) on an object with compound fields fail at
    job creation with 400 API_ERROR; FIELDS(STANDARD) on Campaign runs."""
    sf = replay_client(h, "query_errors")
    for soql in ("SELECT Id, BillingAddress FROM Account", "SELECT FIELDS(STANDARD) FROM Lead"):
        with pytest.raises(esf.RequestError) as ei:
            h.collect(sf.bulk.query(soql))
        assert (ei.value.status, ei.value.error_code) == (400, "API_ERROR")
        assert ei.value.message == "Selecting compound data not supported in Bulk Query"
        assert ei.value.hint is not None
    assert ei.value.hint.startswith("FIELDS(STANDARD) includes")
    assert h.collect(sf.bulk.query("SELECT FIELDS(STANDARD) FROM Campaign")) == []


@pytest.mark.parametrize("version", ["68.0", "60.0"])
def test_recorded_result_pages(h: Harness, version: str) -> None:
    """V43: ``resultPages``/``resultUrl`` (63.0+) and ``resultChunks``/``resultLink`` (before),
    both followed in the job's API version."""
    major = version.split(".")[0]
    sf = replay_client(h, f"result_pages_v{major}", api_version=version)
    body = recorded_body(f"bulk/result_pages_v{major}.json", "GET", "/resultPages")
    assert body["done"] is True
    assert body["nextRecordsUrl"] is None
    rows = h.collect(
        sf.bulk.query("SELECT Id, LastName FROM Lead WHERE LastName LIKE {}", "x%", parallel_downloads=3)
    )
    assert len(rows) == 3
    assert all(r["Id"].startswith("00Q") for r in rows)
