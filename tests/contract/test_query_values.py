"""Values in SOQL methods, query_one, query_first and explain, on both clients (§9.2)."""

from __future__ import annotations

from typing import Any

import pytest
import respx

import even_simpler_salesforce as esf

from ..conftest import DATA, Harness, Recorder
from ..fixtures import load_fixture

EMAIL = "o'brien@example.com"
QUOTED = "SELECT Id FROM Lead WHERE Email = 'o\\'brien@example.com'"


def page(*ids: str, total: int | None = None) -> dict[str, Any]:
    return {
        "totalSize": len(ids) if total is None else total,
        "done": True,
        "records": [{"attributes": {"type": "Lead"}, "Id": i} for i in ids],
    }


def sent_q(route: respx.Route) -> str:
    return route.calls.last.request.url.params["q"]


@pytest.mark.parametrize("method", ["query", "query_one", "query_first", "count"])
def test_values_are_quoted(h: Harness, mock: respx.MockRouter, method: str) -> None:
    route = mock.get(f"{DATA}/query").respond(json=page("00Q000000000001AAA"))
    sf = h.client()
    h.run(getattr(sf, method)("SELECT Id FROM Lead WHERE Email = {}", EMAIL))
    assert sent_q(route) == QUOTED


def test_query_iter_values(h: Harness, mock: respx.MockRouter) -> None:
    route = mock.get(f"{DATA}/query").respond(json=page("00Q000000000001AAA"))
    sf = h.client()
    rows = h.collect(sf.query_iter("SELECT Id FROM Lead WHERE Email = {}", EMAIL))
    assert len(rows) == 1
    assert sent_q(route) == QUOTED


def test_without_values_query_is_unchanged(h: Harness, mock: respx.MockRouter) -> None:
    route = mock.get(f"{DATA}/query").respond(json=page())
    sf = h.client()
    q = "SELECT Id FROM Lead WHERE Name = '{not a placeholder}'"
    h.run(sf.query(q))
    assert sent_q(route) == q


def test_named_placeholders_rejected(h: Harness) -> None:
    sf = h.client()
    with pytest.raises(ValueError, match="esf.soql"):
        h.run(sf.query("SELECT Id FROM Lead WHERE Email = {email}", EMAIL))


def test_query_one(h: Harness, mock: respx.MockRouter, rec: Recorder) -> None:
    route = mock.get(f"{DATA}/query")
    sf = h.client(recorder=rec)

    route.respond(json=page("00Q000000000001AAA"))
    row = h.run(sf.query_one("SELECT Id FROM Lead WHERE Email = {}", EMAIL))
    assert row["Id"] == "00Q000000000001AAA"
    assert "LIMIT" not in sent_q(route)  # never rewritten

    route.respond(json=page())
    with pytest.raises(esf.NotFound) as nf:
        h.run(sf.query_one("SELECT Id FROM Lead"))
    assert nf.value.status is None
    assert nf.value.operation == "query"
    assert nf.value.sobject == "Lead"

    route.respond(
        json={
            **page("00Q000000000001AAA", "00Q000000000002AAA", total=5),
            "done": False,
            "nextRecordsUrl": "/services/data/v68.0/query/01gD0000002HU6KIAW-2",
        }
    )
    with pytest.raises(esf.MultipleMatches) as mm:
        h.run(sf.query_one("SELECT Id FROM Lead"))
    assert mm.value.matches == ("00Q000000000001AAA", "00Q000000000002AAA")
    assert mm.value.status is None
    assert route.call_count == 3  # first page only, every time


def test_query_one_and_first_with_model(h: Harness, mock: respx.MockRouter) -> None:
    from pydantic import BaseModel

    class Lead(BaseModel):
        Id: str

    mock.get(f"{DATA}/query").respond(json=page("00Q000000000001AAA"))
    sf = h.client()
    assert h.run(sf.query_one("SELECT Id FROM Lead", model=Lead)) == Lead(Id="00Q000000000001AAA")
    assert h.run(sf.query_first("SELECT Id FROM Lead", model=Lead)) == Lead(Id="00Q000000000001AAA")


def test_query_first(h: Harness, mock: respx.MockRouter) -> None:
    route = mock.get(f"{DATA}/query")
    sf = h.client()
    route.respond(json=page("00Q000000000001AAA", "00Q000000000002AAA"))
    assert (
        h.run(sf.query_first("SELECT Id FROM Lead ORDER BY CreatedDate DESC"))["Id"] == "00Q000000000001AAA"
    )
    route.respond(json=page())
    assert h.run(sf.query_first("SELECT Id FROM Lead")) is None


def test_include_deleted_uses_query_all(h: Harness, mock: respx.MockRouter) -> None:
    route = mock.get(f"{DATA}/queryAll").respond(json=page("00Q000000000001AAA"))
    sf = h.client()
    h.run(sf.query_one("SELECT Id FROM Lead", include_deleted=True))
    h.run(sf.query_first("SELECT Id FROM Lead", include_deleted=True))
    assert route.call_count == 2


def test_explain(h: Harness, mock: respx.MockRouter, rec: Recorder) -> None:
    route = mock.get(f"{DATA}/query").respond(json=load_fixture("query/explain.json"))
    sf = h.client(recorder=rec)
    plans = h.run(sf.explain("SELECT Id FROM Lead WHERE Email = {}", EMAIL))
    assert [p.leading_operation for p in plans] == ["Index", "TableScan"]
    params = route.calls.last.request.url.params
    assert params["explain"] == QUOTED
    assert "q" not in params
    ev = rec.requests[-1]
    assert ev["operation"] == "query.explain"
    assert ev["sobject"] == "Lead"
    assert EMAIL not in repr(ev.fields)  # SOQL literals never logged
