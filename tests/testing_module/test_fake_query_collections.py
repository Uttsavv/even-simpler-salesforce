"""FakeSalesforce: scripted query/search, collections, raw request, limits."""

from __future__ import annotations

import asyncio
import re

import pytest

import even_simpler_salesforce as esf
from even_simpler_salesforce.testing import (
    AsyncFakeSalesforce,
    FakeRequest,
    FakeSalesforce,
    UnscriptedCall,
)

SOQL = "SELECT Id, Name FROM Lead WHERE Status = 'Open'"


def _rows(fake: FakeSalesforce, n: int) -> list[dict[str, object]]:
    return [{"Id": fake.new_id("Lead"), "Name": f"L{i}"} for i in range(n)]


# --- query --------------------------------------------------------------------------------


def test_unscripted_query_raises_clear_error(esf_fake: FakeSalesforce) -> None:
    with pytest.raises(UnscriptedCall, match="no query script matches"):
        esf_fake.query(SOQL)
    esf_fake.on_query("SELECT Id FROM Account", rows=[])
    with pytest.raises(UnscriptedCall, match="registered: 'SELECT Id FROM Account'"):
        esf_fake.query(SOQL)
    assert not isinstance(UnscriptedCall("x"), esf.SalesforceError)


def test_query_text_match_ignores_whitespace(esf_fake: FakeSalesforce) -> None:
    rows = _rows(esf_fake, 3)
    script = esf_fake.on_query(SOQL, rows=rows)
    page = esf_fake.query("SELECT Id, Name\n  FROM Lead\n WHERE Status = 'Open'")
    assert page.done
    assert page.cursor is None
    assert page.total_size == 3
    assert [r["Name"] for r in page] == ["L0", "L1", "L2"]
    assert page.records[0]["attributes"]["type"] == "Lead"
    assert script.hits == 1


def test_query_regex_predicate_and_callable_rows(esf_fake: FakeSalesforce) -> None:
    esf_fake.on_query(re.compile(r"FROM Account"), rows=[{"Name": "Acme"}])
    esf_fake.on_query(lambda q: "Contact" in q, rows=lambda q: [{"Name": q[-7:]}])
    assert esf_fake.query("SELECT Name FROM Account").records[0]["Name"] == "Acme"
    assert esf_fake.query("SELECT Name FROM Contact").records[0]["Name"] == "Contact"


def test_query_newest_script_wins_and_times(esf_fake: FakeSalesforce) -> None:
    esf_fake.on_query(SOQL, rows=[{"Name": "default"}])
    esf_fake.on_query(SOQL, rows=[{"Name": "once"}], times=1)
    assert esf_fake.query(SOQL).records[0]["Name"] == "once"
    assert esf_fake.query(SOQL).records[0]["Name"] == "default"


def test_query_rows_from_store(esf_fake: FakeSalesforce) -> None:
    esf_fake.on_query("SELECT Id FROM Lead", rows=lambda q: esf_fake.records("Lead"))
    esf_fake.sobject("Lead").create({"LastName": "A"})
    esf_fake.sobject("Lead").create({"LastName": "B"})
    assert len(esf_fake.query("SELECT Id FROM Lead")) == 2


def test_pagination_with_cursor(esf_fake: FakeSalesforce) -> None:
    rows = _rows(esf_fake, 450)
    esf_fake.on_query(SOQL, rows=rows)
    first = esf_fake.query(SOQL, page_size=200)
    assert len(first) == 200
    assert not first.done
    assert first.total_size == 450
    assert first.cursor is not None
    second = esf_fake.query_more(first.cursor)
    third = esf_fake.query_more(second.cursor)  # type: ignore[arg-type]
    assert [len(first), len(second), len(third)] == [200, 200, 50]
    assert third.done
    assert third.cursor is None
    names = [r["Name"] for p in (first, second, third) for r in p]
    assert names == [f"L{i}" for i in range(450)]
    # cursors are reusable (stateless) and the page size can change
    again = esf_fake.query_more(first.cursor, page_size=250)
    assert len(again) == 250
    assert again.done
    ops = [c.operation for c in esf_fake.calls]
    assert ops == ["query", "query_more", "query_more", "query_more"]


def test_page_size_validation_and_default(esf_fake: FakeSalesforce) -> None:
    esf_fake.on_query(SOQL, rows=_rows(esf_fake, 2500))
    with pytest.raises(ValueError):
        esf_fake.query(SOQL, page_size=100)
    page = esf_fake.query(SOQL)
    assert len(page) == 2000


def test_bad_cursors_rejected(esf_fake: FakeSalesforce) -> None:
    with pytest.raises(esf.MalformedQuery):
        esf_fake.query_more("garbage!")
    # a well-formed cursor for an unknown locator
    from even_simpler_salesforce.core.cursor import encode_cursor

    foreign = encode_cursor("/services/data/v68.0/query/01g000000000001-200")
    with pytest.raises(esf.MalformedQuery) as ei:
        esf_fake.query_more(foreign)
    assert ei.value.error_code == "INVALID_QUERY_LOCATOR"
    assert esf_fake.calls[-1].operation == "query_more"


def test_signed_cursors() -> None:
    fake = FakeSalesforce(cursor_key=b"k")
    fake.on_query(SOQL, rows=_rows(fake, 300))
    page = fake.query(SOQL, page_size=200)
    assert page.cursor is not None
    assert "." in page.cursor
    assert len(fake.query_more(page.cursor)) == 100
    other = FakeSalesforce(cursor_key=b"other")
    other.on_query(SOQL, rows=_rows(other, 300))
    with pytest.raises(esf.MalformedQuery):
        other.query_more(page.cursor)


def test_query_iter_include_deleted_and_model(esf_fake: FakeSalesforce) -> None:
    esf_fake.on_query(SOQL, rows=_rows(esf_fake, 401))
    names = [r["Name"] for r in esf_fake.query_iter(SOQL, page_size=200)]
    assert len(names) == 401
    assert [c.operation for c in esf_fake.calls] == ["query", "query_more", "query_more"]

    def model(**kw: object) -> str:
        return str(kw["Name"])

    page = esf_fake.query(SOQL, include_deleted=True, page_size=200, model=model)
    assert page.records[0] == "L0"
    assert list(esf_fake.query_iter(SOQL, model=model))[:2] == ["L0", "L1"]


def test_count(esf_fake: FakeSalesforce) -> None:
    esf_fake.on_query("SELECT COUNT() FROM Lead", total_size=1234)
    assert esf_fake.count("SELECT COUNT() FROM Lead") == 1234
    esf_fake.on_query("SELECT COUNT() FROM Account", rows=[{}, {}])
    assert esf_fake.count("SELECT COUNT() FROM Account") == 2
    assert esf_fake.calls[-1].operation == "query"


def test_query_errors_injected(esf_fake: FakeSalesforce) -> None:
    esf_fake.on_query(SOQL, rows=_rows(esf_fake, 300))
    esf_fake.fail_next("query", esf.MalformedQuery("unexpected token", error_code="MALFORMED_QUERY"))
    with pytest.raises(esf.MalformedQuery):
        esf_fake.query(SOQL)
    page = esf_fake.query(SOQL, page_size=200)
    esf_fake.fail_next("query_more", esf.ServerError("down"))
    with pytest.raises(esf.ServerError):
        esf_fake.query_more(page.cursor)  # type: ignore[arg-type]


def test_search(esf_fake: FakeSalesforce) -> None:
    with pytest.raises(UnscriptedCall, match=r"on_search"):
        esf_fake.search("FIND {Acme}")
    esf_fake.on_search("FIND {Acme}", rows=[{"attributes": {"type": "Account"}, "Name": "Acme"}])
    assert esf_fake.search("FIND {Acme}")[0]["Name"] == "Acme"
    esf_fake.on_search("Acme", rows=[{"Name": "p"}])
    assert esf_fake.parameterized_search("Acme", sobjects=["Account"], fields=["Name"])[0]["Name"] == "p"


# --- collections --------------------------------------------------------------------------


def test_collections_create_update_delete_retrieve(esf_fake: FakeSalesforce) -> None:
    res = esf_fake.collections.create([{"LastName": f"L{i}"} for i in range(450)], sobject="Lead")
    assert isinstance(res, esf.BatchResult)
    assert len(res) == 450
    assert all(isinstance(r, esf.RecordResult) and r.ok for r in res)
    ids = [r.id for r in res]
    assert all(i and i.startswith("00Q") for i in ids)
    res.raise_for_errors()

    upd = esf_fake.collections.update("Lead", [{"Id": i, "Status": "Working"} for i in ids[:3]])
    assert [r.ok for r in upd] == [True, True, True]
    got = esf_fake.collections.retrieve("Lead", [ids[0], esf_fake.new_id("Lead")], ["Status"])
    assert got[0] is not None
    assert got[0]["Status"] == "Working"
    assert got[1] is None

    dele = esf_fake.collections.delete(ids[:2])
    assert [r.ok for r in dele] == [True, True]
    assert len(esf_fake.records("Lead")) == 448
    assert [c.operation for c in esf_fake.calls] == [
        "collections.create",
        "collections.update",
        "collections.retrieve",
        "collections.delete",
    ]


def test_collections_create_with_attributes_type(esf_fake: FakeSalesforce) -> None:
    res = esf_fake.collections.create(
        [
            {"attributes": {"type": "Account"}, "Name": "A"},
            {"attributes": {"type": "Contact"}, "LastName": "C"},
        ]
    )
    assert res[0].id.startswith("001")  # type: ignore[union-attr]
    assert res[1].id.startswith("003")  # type: ignore[union-attr]
    with pytest.raises(ValueError):
        esf_fake.collections.create([{"Name": "untyped"}])
    with pytest.raises(ValueError):
        esf_fake.collections.update("Lead", [{"Status": "no id"}])


def test_collections_partial_failure(esf_fake: FakeSalesforce) -> None:
    ids = esf_fake.add_many("Lead", [{"LastName": "A"}, {"LastName": "B"}])
    missing = esf_fake.new_id("Lead")
    esf_fake.reject(
        "Lead",
        esf.ValidationRuleFailed("bad status", error_code="FIELD_CUSTOM_VALIDATION_EXCEPTION"),
        when=lambda d: d.get("Status") == "Bad",
    )
    res = esf_fake.collections.update(
        "Lead",
        [
            {"Id": ids[0], "Status": "Good"},
            {"Id": ids[1], "Status": "Bad"},
            {"Id": missing, "Status": "Good"},
        ],
    )
    assert [r.ok for r in res] == [True, False, False]
    assert res[1].id == ids[1]
    assert res[1].errors[0].error_code == "FIELD_CUSTOM_VALIDATION_EXCEPTION"
    assert res[2].errors[0].error_code == "NOT_FOUND"
    assert len(res.failed) == 2
    with pytest.raises(esf.PartialFailure) as ei:
        res.raise_for_errors()
    assert ei.value.error_code == "FIELD_CUSTOM_VALIDATION_EXCEPTION"
    assert esf_fake.record("Lead", ids[0])["Status"] == "Good"  # type: ignore[index]

    dele = esf_fake.collections.delete([ids[0], missing])
    assert [r.ok for r in dele] == [True, False]


def test_collections_all_or_none_rolls_back_chunk(esf_fake: FakeSalesforce) -> None:
    esf_fake.reject(
        "Lead",
        esf.RequiredFieldMissing("LastName", error_code="REQUIRED_FIELD_MISSING"),
        when=lambda d: not d.get("LastName"),
    )
    rows = [{"LastName": f"L{i}"} for i in range(250)]
    rows[210] = {"Company": "no last name"}
    with pytest.raises(esf.RequiredFieldMissing) as ei:
        esf_fake.collections.create(rows, sobject="Lead", all_or_none=True)
    assert ei.value.operation == "collections.create"
    # first chunk (200) committed, second chunk rolled back
    assert len(esf_fake.records("Lead")) == 200


def test_collections_all_or_none_update_rollback(esf_fake: FakeSalesforce) -> None:
    ids = esf_fake.add_many("Lead", [{"LastName": "A"}, {"LastName": "B"}])
    with pytest.raises(esf.NotFound):
        esf_fake.collections.update(
            "Lead",
            [{"Id": ids[0], "LastName": "changed"}, {"Id": esf_fake.new_id("Lead"), "LastName": "x"}],
            all_or_none=True,
        )
    assert esf_fake.record("Lead", ids[0])["LastName"] == "A"  # type: ignore[index]


def test_collections_upsert(esf_fake: FakeSalesforce) -> None:
    esf_fake.add("Account", {"Ext__c": "A-1", "Name": "old"})
    res = esf_fake.collections.upsert(
        "Account",
        "Ext__c",
        [{"Ext__c": "A-1", "Name": "new"}, {"Ext__c": "A-2", "Name": "two"}, {"Name": "x"}],
    )
    assert [(r.ok, r.created) for r in res] == [(True, False), (True, True), (False, None)]
    assert res[2].errors[0].error_code == "REQUIRED_FIELD_MISSING"
    assert sorted(r["Name"] for r in esf_fake.records("Account")) == ["new", "two"]


def test_collections_fail_next(esf_fake: FakeSalesforce) -> None:
    esf_fake.fail_next("collections.create", esf.RequestLimitExceeded("slow down"))
    with pytest.raises(esf.RequestLimitExceeded):
        esf_fake.collections.create([{"LastName": "x"}], sobject="Lead")
    assert esf_fake.records("Lead") == []


def test_collections_retrieve_validates(esf_fake: FakeSalesforce) -> None:
    with pytest.raises(ValueError):
        esf_fake.collections.retrieve("Lead", [esf_fake.new_id("Lead")], [])


# --- request & limits ---------------------------------------------------------------------


def test_request_handlers(esf_fake: FakeSalesforce) -> None:
    with pytest.raises(UnscriptedCall, match="on_request"):
        esf_fake.request("GET", "/services/apexrest/orders")
    esf_fake.on_request("GET", "/services/apexrest/orders", {"orders": [1, 2]})
    esf_fake.on_request("POST", "sobjects/Lead/describe/layouts", lambda req: {"echo": req.json})
    esf_fake.on_request("*", re.compile(r"/services/apexrest/fail/.*"), esf.ApexError("nope"))
    assert esf_fake.request("GET", "/services/apexrest/orders") == {"orders": [1, 2]}
    seen: list[FakeRequest] = []

    def handler(req: FakeRequest) -> dict[str, object]:
        seen.append(req)
        return {"ok": True}

    esf_fake.on_request("PATCH", "/services/data/v68.0/sobjects/Lead/x", handler)
    assert esf_fake.request("PATCH", "sobjects/Lead/x", json={"a": 1}, params={"p": 1}) == {"ok": True}
    assert seen[0].json == {"a": 1}
    assert seen[0].params == {"p": 1}
    assert esf_fake.request("POST", "sobjects/Lead/describe/layouts", json=[1]) == {"echo": [1]}
    with pytest.raises(esf.ApexError):
        esf_fake.request("DELETE", "/services/apexrest/fail/1", operation="apex.delete")
    assert esf_fake.calls[-1].operation == "apex.delete"
    with pytest.raises(ValueError):
        esf_fake.request("GET", "https://evil.example.com/x")
    with pytest.raises(ValueError):
        esf_fake.request("POST", "x", json={}, content=b"x")


def test_limits(esf_fake: FakeSalesforce) -> None:
    lim = esf_fake.limits()
    assert lim["DailyApiRequests"].max == 15000
    esf_fake.set_limits({"DailyApiRequests": esf.Limit(100, 40)})
    assert esf_fake.limits()["DailyApiRequests"].used == 60
    snap = esf_fake.usage.snapshot()
    assert snap.api_used == 60
    assert snap.api_max == 100
    esf_fake.start_limits_poller()  # no-op
    with pytest.raises(ValueError):
        esf_fake.start_limits_poller(1)


# --- async parity -------------------------------------------------------------------------


def test_async_query_collections_request() -> None:
    async def main() -> None:
        fake = AsyncFakeSalesforce()
        fake.on_query(SOQL, rows=[{"Id": fake.new_id("Lead"), "Name": f"L{i}"} for i in range(250)])
        page = await fake.query(SOQL, page_size=200)
        assert len(page) == 200
        rest = await fake.query_more(page.cursor)  # type: ignore[arg-type]
        assert rest.done
        rows = [r async for r in fake.query_iter(SOQL, page_size=200)]
        assert len(rows) == 250
        fake.on_query("SELECT COUNT() FROM Lead", total_size=7)
        assert await fake.count("SELECT COUNT() FROM Lead") == 7
        fake.on_search("FIND {x}", rows=[{"Name": "x"}])
        assert await fake.search("FIND {x}") == [{"Name": "x"}]
        assert await fake.parameterized_search("FIND {x}") == [{"Name": "x"}]

        res = await fake.collections.create([{"LastName": "A"}], sobject="Lead")
        lid = res[0].id
        assert lid
        await fake.collections.update("Lead", [{"Id": lid, "Status": "W"}])
        await fake.collections.upsert("Lead", "Email", [{"Email": "a@example.com"}])
        assert (await fake.collections.retrieve("Lead", [lid], ["Status"]))[0]["Status"] == "W"  # type: ignore[index]
        assert (await fake.collections.delete([lid]))[0].ok

        fake.on_request("GET", "/services/apexrest/x", {"y": 1})
        assert await fake.request("GET", "/services/apexrest/x") == {"y": 1}
        assert "DailyApiRequests" in await fake.limits()
        fake.start_limits_poller()
        fake.fail_next("query", esf.ServerError("x"))
        with pytest.raises(esf.ServerError):
            await fake.query(SOQL)
        await fake.aclose()
        with pytest.raises(esf.TransportError):
            await fake.query(SOQL)

    asyncio.run(main())
