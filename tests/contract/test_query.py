from __future__ import annotations

import dataclasses
import json
from typing import Any

import httpx
import pytest
import respx

import even_simpler_salesforce as esf
from even_simpler_salesforce.core.cursor import encode_cursor

from ..conftest import DATA, INSTANCE, Harness, Recorder
from ..fixtures import load_fixture

SOQL = "SELECT Id, Name FROM Lead WHERE Status = 'Open'"
LOC1 = "/services/data/v68.0/query/01gxx0000000001AAA-2"


def test_query_one_page_and_cursor(h: Harness, mock: respx.MockRouter) -> None:
    route = mock.get(f"{DATA}/query").respond(json=load_fixture("query/page1.json"))
    sf = h.client()
    page = h.run(sf.query(SOQL, page_size=200))
    assert isinstance(page, esf.QueryResult)
    assert [r["Name"] for r in page.records] == ["A", "B"]
    assert page.total_size == 3
    assert page.done is False
    assert isinstance(page.cursor, esf.Cursor)
    assert LOC1 not in page.cursor
    req = route.calls.last.request
    assert req.url.params["q"] == SOQL
    assert req.headers["Sforce-Query-Options"] == "batchSize=200"


def test_query_more(h: Harness, mock: respx.MockRouter) -> None:
    mock.get(f"{DATA}/query").respond(json=load_fixture("query/page1.json"))
    more = mock.get(f"{INSTANCE}{LOC1}").respond(json=load_fixture("query/page2.json"))
    sf = h.client()
    page = h.run(sf.query(SOQL))
    page2 = h.run(sf.query_more(page.cursor, page_size=500))
    assert [r["Name"] for r in page2] == ["C"]
    assert page2.done is True
    assert page2.cursor is None
    assert more.calls.last.request.headers["Sforce-Query-Options"] == "batchSize=500"


def test_query_more_rejects_tampered_and_foreign_cursors(h: Harness, mock: respx.MockRouter) -> None:
    sf = h.client(cursor_key=b"secret-key")
    mock.get(f"{DATA}/query").respond(json=load_fixture("query/page1.json"))
    page = h.run(sf.query(SOQL))
    assert "." in page.cursor  # signed
    payload, tag = page.cursor.split(".")
    forged = encode_cursor("/services/data/v68.0/query/01gxx0000000666AAA-2")
    for bad in (forged, f"{forged}.{tag}", payload, page.cursor + "x", "not a cursor"):
        with pytest.raises(esf.MalformedQuery):
            h.run(sf.query_more(bad))
    unsigned_sf = h.client()
    with pytest.raises(esf.MalformedQuery):
        h.run(unsigned_sf.query_more(page.cursor))
    assert len(mock.calls) == 1  # nothing was sent for the bad cursors


def test_query_include_deleted_and_model(h: Harness, mock: respx.MockRouter) -> None:
    @dataclasses.dataclass
    class Row:
        Id: str
        Name: str

    route = mock.get(f"{DATA}/queryAll").respond(json=load_fixture("query/page2.json"))
    sf = h.client()
    page = h.run(sf.query(SOQL, include_deleted=True, model=Row))
    assert page.records == [Row("00Qxx0000000003AAA", "C")]
    assert "Sforce-Query-Options" not in route.calls.last.request.headers


@pytest.mark.parametrize("size", [199, 2001, 0])
def test_query_page_size_bounds(h: Harness, mock: respx.MockRouter, size: int) -> None:
    sf = h.client()
    with pytest.raises(ValueError):
        h.run(sf.query(SOQL, page_size=size))
    assert not mock.calls


def test_query_bad_shape(h: Harness, mock: respx.MockRouter) -> None:
    mock.get(f"{DATA}/query").respond(json={"nope": 1})
    sf = h.client()
    with pytest.raises(esf.UnexpectedResponse):
        h.run(sf.query(SOQL))


def test_query_bad_next_records_url(h: Harness, mock: respx.MockRouter) -> None:
    body = load_fixture("query/page1.json")
    body["nextRecordsUrl"] = "https://evil.example.com/steal"
    mock.get(f"{DATA}/query").respond(json=body)
    sf = h.client()
    with pytest.raises(esf.MalformedQuery):
        h.run(sf.query(SOQL))
    with pytest.raises(esf.MalformedQuery):
        h.collect(sf.query_iter(SOQL))


def test_query_malformed(h: Harness, mock: respx.MockRouter) -> None:
    mock.get(f"{DATA}/query").respond(400, json=load_fixture("errors/invalid_field.json"))
    sf = h.client()
    with pytest.raises(esf.InvalidField) as ei:
        h.run(sf.query("SELECT Nope FROM Lead"))
    assert ei.value.operation == "query"
    assert ei.value.sobject == "Lead"


def test_query_iter_follows_pages(h: Harness, mock: respx.MockRouter, rec: Recorder) -> None:
    mock.get(f"{DATA}/query").respond(json=load_fixture("query/page1.json"))
    more = mock.get(f"{INSTANCE}{LOC1}").respond(json=load_fixture("query/page2.json"))
    sf = h.client(recorder=rec)
    rows = h.collect(sf.query_iter(SOQL, page_size=1000))
    assert [r["Name"] for r in rows] == ["A", "B", "C"]
    assert more.calls.last.request.headers["Sforce-Query-Options"] == "batchSize=1000"
    assert [e["operation"] for e in rec.requests] == ["query", "query_more"]
    assert rec.requests[1]["sobject"] == "Lead"
    assert rec.requests[1]["path_template"] == "/query/:locator"
    assert [e["records"] for e in rec.requests] == [2, 1]


def test_query_iter_is_lazy(h: Harness, mock: respx.MockRouter) -> None:
    mock.get(f"{DATA}/query").respond(json=load_fixture("query/page1.json"))
    more = mock.get(f"{INSTANCE}{LOC1}").respond(json=load_fixture("query/page2.json"))
    sf = h.client()
    it = sf.query_iter(SOQL)
    if h.is_async:

        async def first_two() -> list[Any]:
            out = [await it.__anext__(), await it.__anext__()]
            await it.aclose()
            return out

        two = h.run(first_two())
    else:
        two = [next(it), next(it)]
        it.close()
    assert len(two) == 2
    assert not more.called


def test_query_iter_model(h: Harness, mock: respx.MockRouter) -> None:
    @dataclasses.dataclass
    class Row:
        Id: str
        Name: str

    mock.get(f"{DATA}/query").respond(json=load_fixture("query/page2.json"))
    sf = h.client()
    assert h.collect(sf.query_iter(SOQL, model=Row)) == [Row("00Qxx0000000003AAA", "C")]


def test_query_iter_expand_children(h: Harness, mock: respx.MockRouter) -> None:
    mock.get(f"{DATA}/query").respond(json=load_fixture("query/children_parent.json"))
    child = mock.get(f"{INSTANCE}/services/data/v68.0/query/01gxx0000000009AAA-1").respond(
        json=load_fixture("query/children_more.json")
    )
    sf = h.client()
    soql = "SELECT Id, (SELECT Id FROM Contacts) FROM Account"
    rows = h.collect(sf.query_iter(soql, expand_children=True))
    contacts = rows[0]["Contacts"]
    assert [c["Id"] for c in contacts["records"]] == [
        "003xx0000000001AAA",
        "003xx0000000002AAA",
        "003xx0000000003AAA",
    ]
    assert contacts["done"] is True
    assert "nextRecordsUrl" not in contacts
    assert child.call_count == 1

    # Without expand_children the truncated child is returned as is.
    rows2 = h.collect(sf.query_iter(soql))
    assert len(rows2[0]["Contacts"]["records"]) == 1
    assert child.call_count == 1


def test_query_iter_expand_children_rejects_bad_child_url(h: Harness, mock: respx.MockRouter) -> None:
    body = load_fixture("query/children_parent.json")
    body["records"][0]["Contacts"]["nextRecordsUrl"] = "/services/data/v68.0/sobjects/User"
    mock.get(f"{DATA}/query").respond(json=body)
    sf = h.client()
    with pytest.raises(esf.MalformedQuery):
        h.collect(sf.query_iter("SELECT Id FROM Account", expand_children=True))


def test_count(h: Harness, mock: respx.MockRouter) -> None:
    route = mock.get(f"{DATA}/query").respond(json=load_fixture("query/count.json"))
    sf = h.client()
    assert h.run(sf.count("SELECT COUNT() FROM Lead")) == 42
    assert route.calls.last.request.url.params["q"] == "SELECT COUNT() FROM Lead"


def test_search(h: Harness, mock: respx.MockRouter, rec: Recorder) -> None:
    route = mock.get(f"{DATA}/search")
    route.side_effect = [
        httpx.Response(200, json=load_fixture("query/search.json")),
        httpx.Response(200, json=[{"Id": "x"}]),
        httpx.Response(200, json=None),
    ]
    sf = h.client(recorder=rec)
    sosl = "FIND {Acme} IN ALL FIELDS RETURNING Lead(Id)"
    assert h.run(sf.search(sosl)) == [{"attributes": {"type": "Lead"}, "Id": "00Qxx0000000001AAA"}]
    assert route.calls.last.request.url.params["q"] == sosl
    assert h.run(sf.search(sosl)) == [{"Id": "x"}]
    assert h.run(sf.search(sosl)) == []
    assert rec.requests[0]["operation"] == "search"
    assert rec.requests[0]["soql_hash"]


def test_parameterized_search(h: Harness, mock: respx.MockRouter) -> None:
    route = mock.post(f"{DATA}/parameterizedSearch")
    route.side_effect = [
        httpx.Response(200, json=load_fixture("query/search.json")),
        httpx.Response(200, json=[]),
        httpx.Response(200, json={"searchRecords": None}),
    ]
    sf = h.client()
    out = h.run(
        sf.parameterized_search(
            "Acme",
            sobjects=["Lead", {"name": "Account", "limit": 5}],
            fields=["Id", "Name"],
            limit=10,
            dataCategory="x",
        )
    )
    assert out[0]["Id"] == "00Qxx0000000001AAA"
    body = json.loads(route.calls.last.request.content)
    assert body == {
        "q": "Acme",
        "dataCategory": "x",
        "sobjects": [{"name": "Lead"}, {"name": "Account", "limit": 5}],
        "fields": ["Id", "Name"],
        "overallLimit": 10,
    }
    assert h.run(sf.parameterized_search("Acme")) == []
    assert json.loads(route.calls.last.request.content) == {"q": "Acme"}
    assert h.run(sf.parameterized_search("Acme")) == []
    with pytest.raises(ValueError):
        h.run(sf.parameterized_search("Acme", sobjects=["Bad Name"]))


def test_query_retries_timeout_because_idempotent(h: Harness, mock: respx.MockRouter) -> None:
    route = mock.get(f"{DATA}/query")
    route.side_effect = [
        httpx.ReadTimeout("slow"),
        httpx.Response(200, json=load_fixture("query/page2.json")),
    ]
    sf = h.client()
    assert len(h.run(sf.query(SOQL)).records) == 1
    assert sf.sleeps == [0.25]
