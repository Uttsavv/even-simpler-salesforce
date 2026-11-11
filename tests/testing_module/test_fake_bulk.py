"""``fake.bulk``: Bulk API 2.0 on ``FakeSalesforce`` / ``AsyncFakeSalesforce`` (§9.11, §14)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

import even_simpler_salesforce as esf
from even_simpler_salesforce.testing import AsyncFakeSalesforce, FakeSalesforce


def test_ingest_insert_applies_rows_with_csv_null_rules() -> None:
    fake = FakeSalesforce()
    job = fake.bulk.ingest(
        "Lead",
        "insert",
        rows=[
            {"LastName": "Doe", "Company": "Acme", "Description": None},
            {"LastName": "Roe", "Company": "Globex"},
        ],
    )
    assert job.wait(poll=esf.Backoff(start=1), timeout=10).state is esf.BulkJobState.JOB_COMPLETE
    assert job.refresh() is job.info
    assert job.abort() is job.info
    assert job.info.records_processed == 2
    assert job.info.records_failed == 0
    assert job.ids == (job.info.id,)
    assert job.parts == (job.info,)
    assert job.info.id.startswith("750")
    assert "insert Lead" in repr(job)
    ok = list(job.successful_results())
    assert [r["sf__Created"] for r in ok] == ["true", "true"]
    assert ok[0]["LastName"] == "Doe"
    stored = fake.record("Lead", ok[0]["sf__Id"])
    assert stored is not None
    assert stored["Description"] is None  # "#N/A" sets null
    assert "Description" not in (fake.record("Lead", ok[1]["sf__Id"]) or {})  # empty cell: unchanged
    assert list(job.failed_results()) == []
    assert list(job.unprocessed_records()) == []
    (call,) = fake.calls_for("bulk.ingest", sobject="Lead")
    assert call.args["rows"] == 2
    assert call.args["columns"] == ("LastName", "Company", "Description")
    with pytest.raises(ValueError, match="timeout"):
        job.wait(timeout=0)


def test_ingest_update_upsert_delete_and_failures(tmp_path: Path) -> None:
    fake = FakeSalesforce()
    lead = fake.add("Lead", {"LastName": "Doe", "Company": "Acme", "Ext__c": "E1"})
    fake.reject(
        "Lead",
        esf.RequiredFieldMissing(
            "Required fields are missing: [Company]", fields=("Company",), error_code="REQUIRED_FIELD_MISSING"
        ),
        when=lambda data: "Company" in data and data["Company"] is None,
    )
    path = tmp_path / "upsert.csv"
    path.write_bytes(b"\xef\xbb\xbfExt__c,LastName,Company\nE1,Doe2,\nE2,New,#N/A\n")
    job = fake.bulk.ingest("Lead", "upsert", external_id_field="Ext__c", rows=path)
    (ok,) = list(job.successful_results())
    assert (ok["sf__Id"], ok["sf__Created"]) == (lead, "false")
    assert (fake.record("Lead", lead) or {})["LastName"] == "Doe2"
    (failure,) = list(job.failed_results())
    assert failure.error.error_code == "REQUIRED_FIELD_MISSING"
    assert failure.fields["Ext__c"] == "E2"
    assert job.info.records_failed == 1

    fake.bulk.ingest("Lead", "update", rows=f"Id,LastName\n{lead},Doe3\n".encode())
    assert (fake.record("Lead", lead) or {})["LastName"] == "Doe3"
    fake.bulk.ingest("Lead", "delete", rows=[{"Id": lead}])
    assert fake.record("Lead", lead) is None
    missing = fake.bulk.ingest("Lead", "delete", rows=[{"Id": lead}])
    assert next(iter(missing.failed_results())).id == lead


@pytest.mark.parametrize(
    ("rows", "error"),
    [([{"A": "#N/A"}], ValueError), ([], ValueError), (b"A\r\n1\r\n", ValueError), (7, TypeError)],
)
def test_ingest_validates_like_the_real_client(rows: Any, error: type[Exception]) -> None:
    fake = FakeSalesforce()
    with pytest.raises(error):
        fake.bulk.ingest("Lead", "insert", rows=rows)


def test_query_returns_scripted_rows_as_csv_strings() -> None:
    fake = FakeSalesforce()
    fake.on_query(
        "SELECT Id, Name, Owner.Name, IsDeleted, Amount FROM Lead WHERE Company = 'O\\'Neil'",
        rows=[{"Id": "00Q1", "Name": "", "Owner": {"Name": "Ann"}, "IsDeleted": False, "Amount": 1.5}],
    )
    rows = list(
        fake.bulk.query(
            "SELECT Id, Name, Owner.Name, IsDeleted, Amount FROM Lead WHERE Company = {}",
            "O'Neil",
            include_deleted=True,
            parallel_downloads=3,
        )
    )
    assert rows == [{"Id": "00Q1", "Name": None, "Owner.Name": "Ann", "IsDeleted": "false", "Amount": "1.5"}]
    (call,) = fake.calls_for("bulk.query")
    assert call.args["include_deleted"] is True
    with pytest.raises(ValueError, match="parallel_downloads"):
        list(fake.bulk.query("SELECT Id FROM Lead", parallel_downloads=0))
    with pytest.raises(ValueError, match="timeout"):
        list(fake.bulk.query("SELECT Id FROM Lead", timeout=0))
    with pytest.raises(ValueError, match=r"FIELDS\(ALL\)"):
        list(fake.bulk.query("SELECT FIELDS(ALL) FROM Lead LIMIT 200"))


def test_fail_next_and_expand_compound_fields() -> None:
    fake = FakeSalesforce()
    fake.fail_next("bulk.query", esf.BulkJobFailed("failed", job_id="750x", state="Failed"))
    with pytest.raises(esf.BulkJobFailed):
        list(fake.bulk.query("SELECT Id FROM Lead"))
    fake.set_describe(
        "Account",
        {
            "fields": [
                {"name": "BillingAddress", "type": "address"},
                {"name": "BillingCity", "type": "string", "compoundFieldName": "BillingAddress"},
            ]
        },
    )
    assert fake.bulk.expand_compound_fields("Account", ["Id", "BillingAddress"]) == ["Id", "BillingCity"]
    with pytest.raises(TypeError):
        fake.bulk.expand_compound_fields("Account", "BillingAddress")  # type: ignore[arg-type]


def test_async_fake() -> None:
    fake = AsyncFakeSalesforce()
    fake.on_query("SELECT Id FROM Lead", rows=[{"Id": "00Q1"}])

    async def go() -> None:
        job = await fake.bulk.ingest("Lead", "insert", rows=[{"LastName": "Doe", "Company": "Acme"}])
        assert (await job.wait()).state is esf.BulkJobState.JOB_COMPLETE
        assert await job.refresh() is job.info
        assert await job.abort() is job.info
        assert job.ids == (job.parts[0].id,)
        assert repr(job).startswith("<AsyncFakeIngestJob")
        assert len([r async for r in job.successful_results()]) == 1
        assert [r async for r in job.failed_results()] == []
        assert [r async for r in job.unprocessed_records()] == []
        assert [r async for r in fake.bulk.query("SELECT Id FROM Lead")] == [{"Id": "00Q1"}]
        fake.set_describe("Account", {"fields": []})
        assert await fake.bulk.expand_compound_fields("Account", ["Id"]) == ["Id"]

    asyncio.run(go())
