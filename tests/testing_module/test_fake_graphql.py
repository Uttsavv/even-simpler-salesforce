"""``FakeSalesforce`` support for ``graphql`` and ``graphql_iter`` (§9.6, §14)."""

from __future__ import annotations

import asyncio
import re
from typing import Any

import pytest

import even_simpler_salesforce as esf
from even_simpler_salesforce.testing import AsyncFakeSalesforce, FakeSalesforce, GraphQLScript, UnscriptedCall

LEADS = (
    "query Leads($after: String) { uiapi { query { Lead(first: 2, after: $after) "
    "{ edges { node { Id } } pageInfo { hasNextPage endCursor } } } } }"
)


def test_scripted_data_by_text_regex_and_predicate() -> None:
    fake = FakeSalesforce()
    fake.on_graphql("{  uiapi { a } }", {"uiapi": {"a": 1}})
    assert fake.graphql("{ uiapi {\n a } }") == {"uiapi": {"a": 1}}
    fake.on_graphql(re.compile(r"\bContact\b"), lambda q, v: {"n": v["n"]})
    assert fake.graphql("{ Contact }", {"n": 3}) == {"n": 3}
    script = fake.on_graphql(lambda q: q.startswith("query Z"), {"z": True}, times=1)
    assert isinstance(script, GraphQLScript)
    assert fake.graphql("query Z { z }") == {"z": True}
    assert script.hits == 1
    with pytest.raises(UnscriptedCall, match=r"on_graphql.*registered: .*<lambda>.*Contact.*uiapi"):
        fake.graphql("query Z { z }")
    (call, *_) = fake.calls_for("graphql")
    assert call.args == {"query": "{ uiapi {\n a } }", "variables": {}}


def test_unscripted_without_any_script() -> None:
    with pytest.raises(UnscriptedCall, match="none registered"):
        FakeSalesforce().graphql("{ a }")


def test_errors_raise_and_are_recorded() -> None:
    fake = FakeSalesforce()
    err = esf.GraphQLError("bad field", error_code="ValidationError", data={"uiapi": None})
    fake.on_graphql("{ a }", err)
    with pytest.raises(esf.GraphQLError) as ei:
        fake.graphql("{ a }")
    assert ei.value.data == {"uiapi": None}
    assert ei.value.operation == "graphql"
    assert fake.calls_for("graphql")[0].error is not None
    fake.fail_next("graphql", esf.ServerError("down"))
    with pytest.raises(esf.ServerError):
        fake.graphql("{ a }")


def test_validates_like_the_client() -> None:
    fake = FakeSalesforce()
    with pytest.raises(ValueError):
        fake.graphql(" ")
    with pytest.raises(ValueError, match=r"\$after"):
        list(fake.graphql_iter("{ a }", path="uiapi.query.Lead"))
    with pytest.raises(ValueError, match="path"):
        fake.on_graphql("{ a }", nodes=[], path="a..b")
    with pytest.raises(ValueError, match="needs path"):
        fake.on_graphql("{ a }", nodes=[])
    with pytest.raises(ValueError, match="page_size"):
        fake.on_graphql("{ a }", nodes=[], path="a", page_size=0)


def test_nodes_are_paged_by_after() -> None:
    fake = FakeSalesforce()
    nodes = [{"Id": f"00Q{i}"} for i in range(5)]
    fake.on_graphql(LEADS, nodes=nodes, path="uiapi.query.Lead", page_size=2)
    assert list(fake.graphql_iter(LEADS, path="uiapi.query.Lead")) == nodes
    calls = fake.calls_for("graphql", sobject="Lead")
    assert [c.args["variables"].get("after") for c in calls] == [None, "fake-cursor-2", "fake-cursor-4"]

    page: dict[str, Any] = fake.graphql(LEADS)["uiapi"]["query"]["Lead"]
    assert page["totalCount"] == 5
    assert page["pageInfo"] == {
        "hasNextPage": True,
        "hasPreviousPage": False,
        "startCursor": "fake-cursor-0",
        "endCursor": "fake-cursor-2",
    }
    with pytest.raises(ValueError, match="not a cursor"):
        fake.graphql(LEADS, {"after": "elsewhere"})


def test_empty_connection() -> None:
    fake = FakeSalesforce()
    fake.on_graphql(LEADS, nodes=[], path="uiapi.query.Lead")
    assert list(fake.graphql_iter(LEADS, path="uiapi.query.Lead")) == []
    info = fake.graphql(LEADS)["uiapi"]["query"]["Lead"]["pageInfo"]
    assert info["startCursor"] is None


def test_async_fake() -> None:
    async def go() -> None:
        async with AsyncFakeSalesforce() as fake:
            fake.on_graphql(
                LEADS, nodes=[{"Id": "1"}, {"Id": "2"}, {"Id": "3"}], path="uiapi.query.Lead", page_size=2
            )
            assert (await fake.graphql(LEADS))["uiapi"]["query"]["Lead"]["edges"][0] == {"node": {"Id": "1"}}
            assert [n["Id"] async for n in fake.graphql_iter(LEADS, path="uiapi.query.Lead")] == [
                "1",
                "2",
                "3",
            ]

    asyncio.run(go())
