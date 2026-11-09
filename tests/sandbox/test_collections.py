"""Collections smoke tests with more than 200 rows (so chunking is real), on both clients.

Heavy writes: skipped unless ``ESF_SANDBOX_ALLOW_WRITES=1``. Each run creates ``ROWS`` Leads
per client flavour (LastName = run tag), pages through them with ``query``/``query_more``/
``query_iter``, updates, retrieves and deletes them. Upsert needs
``ESF_SANDBOX_EXTERNAL_ID_FIELD``. Collections limits (V8): 200 records per write request,
2,000 ids per retrieve — *sObject Collections*
(https://developer.salesforce.com/docs/atlas.en-us.api_rest.meta/api_rest/resources_composite_sobjects_collections.htm).
"""

from __future__ import annotations

import pytest

import even_simpler_salesforce as esf

from .conftest import Cleanup, Driver, new_row

pytestmark = [pytest.mark.sandbox, pytest.mark.usefixtures("heavy_writes")]

ROWS = 250  # > 200: two create/update/delete requests, query pages of 200 + 50


def _ok_ids(result: esf.BatchResult[esf.RecordResult]) -> list[str]:
    assert not result.failed, [r.errors for r in result.failed][:3]
    return [r.id for r in result if r.id]


def test_collections_round_trip(sb: Driver, tag: str, cleanup: Cleanup) -> None:
    sf = sb.client()
    prefix = f"{tag}-coll-{sb.kind}-"
    rows = [new_row("Lead", prefix, f"{i:04d}") for i in range(ROWS)]
    created = sb.run(sf.collections.create(rows, sobject="Lead"))
    ids = _ok_ids(created)
    cleanup.add(*ids)
    assert len(ids) == ROWS
    assert len(sb.recorder.requests("collections.create")) == 2
    try:
        where = esf.soql("WHERE LastName LIKE {}", prefix + "%")
        assert sb.run(sf.count(f"SELECT COUNT() FROM Lead {where}")) == ROWS
        soql = f"SELECT Id, LastName FROM Lead {where} ORDER BY LastName"

        page = sb.run(sf.query(soql, page_size=200))
        assert page.total_size == ROWS
        assert page.cursor is not None
        assert not page.done
        rest = sb.run(sf.query_more(page.cursor, page_size=200))
        assert rest.done
        assert len(page.records) + len(rest.records) == ROWS
        names = [r["LastName"] for r in sb.collect(sf.query_iter(soql, page_size=200))]
        assert names == [r["LastName"] for r in rows]  # order kept across pages

        updated = sb.run(
            sf.collections.update("Lead", [{"Id": i, "Company": "esf-sandbox-bulk"} for i in ids])
        )
        assert len(_ok_ids(updated)) == ROWS
        got = sb.run(sf.collections.retrieve("Lead", ids, ["Id", "Company", "LastName"]))
        assert len(got) == ROWS
        assert all(g is not None and g["Company"] == "esf-sandbox-bulk" for g in got)
        assert [g["LastName"] for g in got if g] == [r["LastName"] for r in rows]  # input order
    finally:
        deleted = sb.run(sf.collections.delete(ids))
    assert len(_ok_ids(deleted)) == ROWS
    cleanup.discard(*ids)
    assert len(sb.recorder.requests("collections.delete")) == 2
    gone = sb.run(sf.collections.retrieve("Lead", ids[:5], ["Id"]))
    assert gone == [None] * 5


def test_collections_partial_failure_is_reported(sb: Driver, tag: str, cleanup: Cleanup) -> None:
    """allOrNone=false: a row missing a required field fails alone; the others are created."""
    sf = sb.client()
    rows = [new_row("Lead", tag, f"-partial-{sb.kind}-{i}") for i in range(3)]
    rows[1] = {"FirstName": "esf-sandbox"}  # no LastName/Company
    result = sb.run(sf.collections.create(rows, sobject="Lead"))
    cleanup.add(*(r.id for r in result if r.id))
    try:
        assert [r.ok for r in result] == [True, False, True]
        assert result[1].errors[0].error_code == "REQUIRED_FIELD_MISSING"
    finally:
        sb.run(sf.collections.delete([r.id for r in result if r.id]))
    cleanup.discard(*(r.id for r in result if r.id))


def test_collections_upsert(sb: Driver, tag: str, cleanup: Cleanup, external_id: tuple[str, str]) -> None:
    obj, field = external_id
    sf = sb.client()
    prefix = f"{tag}-cups-{sb.kind}-"
    rows = [{field: f"{prefix}{i:04d}", **new_row(obj, prefix, f"{i:04d}")} for i in range(ROWS)]
    first = sb.run(sf.collections.upsert(obj, field, rows))
    ids = _ok_ids(first)
    cleanup.add(*ids)
    try:
        assert all(r.created for r in first)
        second = sb.run(
            sf.collections.upsert(obj, field, [{field: r[field], "LastName": r["LastName"]} for r in rows])
        )
        assert _ok_ids(second) == ids
        assert not any(r.created for r in second)
    finally:
        sb.run(sf.collections.delete(ids))
    cleanup.discard(*ids)
