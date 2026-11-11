"""``core/graphql.py``: document scanning, bodies, errors and connection paging (§9.6).

Response shapes follow the Salesforce GraphQL API Developer Guide (synthetic, no recording):
https://developer.salesforce.com/docs/platform/graphql/guide/requests-and-responses.html and
https://developer.salesforce.com/docs/platform/graphql/guide/paginate-use.html
"""

from __future__ import annotations

import json
from typing import Any

import pytest

import even_simpler_salesforce as esf
from even_simpler_salesforce.core.graphql import (
    Operation,
    check_cursor_query,
    connection_page,
    graphql_body,
    graphql_error,
    graphql_sobject,
    has_mutation,
    parse_graphql_data,
    parse_path,
    scan_operations,
)
from even_simpler_salesforce.core.http import RawResponse, RequestSpec

SPEC = RequestSpec("POST", "graphql", operation="graphql")


def _resp(body: Any, status: int = 200, content: bytes | None = None) -> RawResponse:
    return RawResponse(
        status,
        {"content-type": "application/json", "sforce-request-id": "req-1"},
        json.dumps(body).encode() if content is None else content,
    )


# --- scanning ------------------------------------------------------------------------------


def test_scan_named_query_with_variables() -> None:
    doc = "query Leads($after: String, $first: Int = 10) @live { uiapi { query { Lead { x } } } }"
    assert scan_operations(doc) == [Operation("query", "Leads", ("after", "first"))]


def test_scan_shorthand_and_fragments() -> None:
    doc = "fragment F on Lead { Id } { uiapi { ...F } }"
    assert scan_operations(doc) == [Operation("query", None, ())]
    assert scan_operations("{ a }") == [Operation("query", None, ())]


def test_scan_several_operations_and_mutation() -> None:
    doc = "query A { a } mutation B($x: ID) { uiapi { LeadCreate(input: {x: $x}) { y } } }"
    assert [(o.kind, o.name, o.variables) for o in scan_operations(doc)] == [
        ("query", "A", ()),
        ("mutation", "B", ("x",)),
    ]
    assert has_mutation(doc)
    assert not has_mutation("query { mutation }")  # a field named mutation is not an operation


def test_scan_ignores_strings_and_comments() -> None:
    doc = (
        "# mutation in a comment\n"
        'query Q($after: String) { a(where: "mutation { }", b: """ mutation \\""" { """) }\n'
        "# trailing comment without newline"
    )
    assert scan_operations(doc) == [Operation("query", "Q", ("after",))]


def test_scan_tolerates_malformed_documents() -> None:
    assert scan_operations("{ uiapi ") == [Operation("query", None, ())]
    assert scan_operations("} }") == []
    assert scan_operations('{ a(b: "open') == [Operation("query", None, ())]
    assert scan_operations('{ a(b: """open') == [Operation("query", None, ())]
    assert scan_operations("query($ : Int, $x: Int) { a }") == [Operation("query", None, ("x",))]
    assert scan_operations("query Q(") == [Operation("query", "Q", ())]
    assert scan_operations("query") == [Operation("query", None, ())]


def test_check_cursor_query() -> None:
    op = check_cursor_query("query Leads($after: String) { a }")
    assert op.variables == ("after",)
    with pytest.raises(ValueError, match=r"\$after"):
        check_cursor_query("query Leads($cursor: String) { a }")
    with pytest.raises(ValueError, match="exactly one query"):
        check_cursor_query("query A($after: String) { a } query B { b }")
    with pytest.raises(ValueError, match="exactly one query"):
        check_cursor_query("mutation M($after: String) { a }")


# --- bodies and paths ----------------------------------------------------------------------


def test_graphql_body() -> None:
    assert graphql_body("{ a }", None) == {"query": "{ a }"}
    assert graphql_body("{ a }", {"x": 1}) == {"query": "{ a }", "variables": {"x": 1}}
    with pytest.raises(TypeError):
        graphql_body(b"{ a }", None)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        graphql_body(" \n", None)
    with pytest.raises(TypeError):
        graphql_body("{ a }", [("x", 1)])  # type: ignore[arg-type]


def test_parse_path_and_sobject() -> None:
    assert parse_path("uiapi.query.Lead") == ("uiapi", "query", "Lead")
    for bad in ("", "a..b", ".a", "a.", "a/b", "1a", None):
        with pytest.raises(ValueError):
            parse_path(bad)  # type: ignore[arg-type]
    assert graphql_sobject(("uiapi", "query", "Lead")) == "Lead"
    assert graphql_sobject(("uiapi", "aggregate", "Lead")) is None
    assert graphql_sobject(("leads",)) is None


# --- errors and data -----------------------------------------------------------------------


def test_graphql_error_shape() -> None:
    body = {
        "data": {"uiapi": None},
        "errors": [
            {
                "message": "bad field",
                "locations": [{"line": 1, "column": 3}],
                "path": ["uiapi"],
                "extensions": {"ErrorType": "ValidationError"},
                "other": 1,
            },
            "not an object",
            {"extensions": "odd"},
        ],
    }
    err = graphql_error(_resp(body), SPEC)
    assert isinstance(err, esf.GraphQLError)
    assert err.message == "bad field (and 2 more)"
    assert err.error_code == "ValidationError"
    assert err.data == {"uiapi": None}
    assert err.status == 200
    assert err.request_id == "req-1"
    assert err.path == "/graphql"
    assert err.method == "POST"
    assert err.graphql_errors[1] == {"message": "not an object"}
    assert err.errors[0].extended == {
        "path": ["uiapi"],
        "locations": [{"line": 1, "column": 3}],
        "extensions": {"ErrorType": "ValidationError"},
    }
    assert err.errors[2].error_code is None
    assert err.errors[2].message == ""
    assert not err.retryable


def test_graphql_error_without_message() -> None:
    err = graphql_error(_resp({"errors": [{}]}), SPEC)
    assert err is not None
    assert err.message == "GraphQL request failed"


@pytest.mark.parametrize(
    "resp",
    [
        _resp({"data": {}, "errors": []}),
        _resp({"data": {}}),
        _resp([{"errorCode": "X", "message": "y"}]),
        _resp(None, content=b"<html>proxy</html>"),
    ],
)
def test_graphql_error_none_for_other_bodies(resp: RawResponse) -> None:
    assert graphql_error(resp, SPEC) is None


def test_classify_uses_graphql_errors_only_for_graphql() -> None:
    from even_simpler_salesforce.core.classify import classify

    resp = _resp({"errors": [{"message": "m"}]})
    assert isinstance(classify(resp, SPEC), esf.GraphQLError)
    assert classify(resp, RequestSpec("POST", "x", operation="raw")) is None


def test_parse_graphql_data() -> None:
    assert parse_graphql_data({"data": {"a": 1}, "errors": []}) == {"a": 1}
    for bad in ({"data": None}, {}, [], None):
        with pytest.raises(esf.UnexpectedResponse):
            parse_graphql_data(bad)


# --- connections ---------------------------------------------------------------------------


def _conn(edges: Any, info: Any) -> dict[str, Any]:
    return {"uiapi": {"query": {"Lead": {"edges": edges, "pageInfo": info}}}}


PATH = ("uiapi", "query", "Lead")


def test_connection_page() -> None:
    data = _conn([{"node": {"Id": "1"}}, {"node": {"Id": "2"}}], {"hasNextPage": True, "endCursor": "c1"})
    assert connection_page(data, PATH) == ([{"Id": "1"}, {"Id": "2"}], "c1")
    last = _conn([], {"hasNextPage": False, "endCursor": None})
    assert connection_page(last, PATH) == ([], None)


@pytest.mark.parametrize(
    ("data", "match"),
    [
        ({"uiapi": {"query": {}}}, "uiapi.query.Lead"),
        ({"uiapi": None}, "'uiapi'"),
        ({"uiapi": {"query": "x"}}, "uiapi.query.Lead"),
        ({"uiapi": {"query": {"Lead": [1]}}}, "not a connection"),
        (_conn(None, {"hasNextPage": False}), "pageInfo"),
        (_conn([], None), "pageInfo"),
        (_conn([{"cursor": "x"}], {"hasNextPage": False}), "without 'node'"),
        (_conn(["x"], {"hasNextPage": False}), "without 'node'"),
        (_conn([], {"hasNextPage": True}), "no endCursor"),
        (_conn([], {"hasNextPage": True, "endCursor": ""}), "no endCursor"),
    ],
)
def test_connection_page_rejects_bad_shapes(data: dict[str, Any], match: str) -> None:
    with pytest.raises(esf.UnexpectedResponse, match=match):
        connection_page(data, PATH)
