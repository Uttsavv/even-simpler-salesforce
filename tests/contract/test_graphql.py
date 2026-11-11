"""GraphQL API: ``sf.graphql`` and ``sf.graphql_iter`` (§9.6), on both clients.

Every response here is **synthetic** (``tests/fixtures/recorded/graphql/*.json``), hand-written
from the Salesforce GraphQL API Developer Guide because no org recording exists yet:

- Request and Response Structure (``POST /services/data/vXX.X/graphql``, body ``query`` and
  ``variables``, response ``data`` and ``errors``):
  https://developer.salesforce.com/docs/platform/graphql/guide/requests-and-responses.html
- Status Codes and Error Responses (``200 OK`` with ``errors``; ``extensions.ErrorType`` such
  as ``InvalidSyntax`` and ``ValidationError``; ``locations``):
  https://developer.salesforce.com/docs/platform/graphql/guide/query-status.html
- Paginate with Record Connections (``first``, ``after: endCursor``,
  ``pageInfo { hasNextPage endCursor }``):
  https://developer.salesforce.com/docs/platform/graphql/guide/paginate-use.html

developer.salesforce.com refuses automated fetches, so these pages were read through search
excerpts. The exact error bodies, partial data next to ``errors`` and the 4xx statuses are open
in big-bang.md §21 (V50, V51).
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
import respx

import even_simpler_salesforce as esf

from ..conftest import DATA, Harness, Recorder
from ..fixtures import load_fixture

URL = f"{DATA}/graphql"
LEADS = (
    "query Leads($after: String) { uiapi { query { Lead(first: 2, after: $after) "
    "{ edges { node { Id Name { value } } } pageInfo { hasNextPage endCursor } } } } }"
)


def _sent(route: respx.Route, i: int = 0) -> Any:
    return json.loads(route.calls[i].request.content)


def test_graphql_returns_data(h: Harness, mock: respx.MockRouter, rec: Recorder) -> None:
    route = mock.post(URL).respond(json=load_fixture("graphql/leads_page2.json"))
    sf = h.client(recorder=rec)
    data = h.run(sf.graphql(LEADS, {"after": None}))
    assert data["uiapi"]["query"]["Lead"]["edges"][0]["node"]["Name"]["value"] == "Alan Turing"
    assert _sent(route) == {"query": LEADS, "variables": {"after": None}}
    assert route.calls[0].request.headers["content-type"] == "application/json"
    (event,) = rec.requests
    assert event["operation"] == "graphql"
    assert event["outcome"] == "ok"
    assert event["path_template"] == "/graphql"


def test_graphql_without_variables(h: Harness, mock: respx.MockRouter) -> None:
    route = mock.post(URL).respond(json={"data": {"uiapi": {}}})
    sf = h.client()
    assert h.run(sf.graphql("{ uiapi { query { Lead { edges { node { Id } } } } } }")) == {"uiapi": {}}
    assert "variables" not in _sent(route)


def test_errors_with_200_raise_graphql_error(h: Harness, mock: respx.MockRouter, rec: Recorder) -> None:
    """Synthetic: Salesforce answers ``200 OK`` with an ``errors`` array (V50)."""
    mock.post(URL).respond(json=load_fixture("graphql/validation_error.json"))
    sf = h.client(recorder=rec)
    with pytest.raises(esf.GraphQLError) as ei:
        h.run(sf.graphql(LEADS))
    err = ei.value
    assert err.status == 200
    assert err.error_code == "ValidationError"
    assert "FieldUndefined" in err.message
    assert err.data is None
    assert err.graphql_errors[0]["locations"] == [{"line": 1, "column": 62}]
    assert err.errors[0].extended["extensions"] == {"ErrorType": "ValidationError"}
    assert err.operation == "graphql"
    assert err.docs_url is not None
    assert err.docs_url.endswith("#graphqlerror")
    (event,) = rec.requests
    assert event["outcome"] == "error"
    assert event["error_class"] == "GraphQLError"
    assert event["error_code"] == "ValidationError"


def test_partial_data_is_on_the_error(h: Harness, mock: respx.MockRouter) -> None:
    """Synthetic: ``data`` and ``errors`` together (GraphQL spec; V51)."""
    mock.post(URL).respond(json=load_fixture("graphql/partial_error.json"))
    sf = h.client()
    with pytest.raises(esf.GraphQLError) as ei:
        h.run(sf.graphql(LEADS))
    err = ei.value
    assert err.message == "Account is not supported by UI API (and 1 more)"
    assert err.data["uiapi"]["query"]["Lead"]["edges"][0]["node"]["Id"] == "00Qxx0000000001AAA"
    assert [e["path"] for e in err.graphql_errors] == [["uiapi", "query", "Account"]] * 2
    assert len(err.errors) == 2


def test_errors_with_4xx_status(h: Harness, mock: respx.MockRouter) -> None:
    mock.post(URL).respond(
        400, json={"errors": [{"message": "Invalid Syntax", "extensions": {"ErrorType": "InvalidSyntax"}}]}
    )
    sf = h.client()
    with pytest.raises(esf.GraphQLError) as ei:
        h.run(sf.graphql("{ uiapi "))
    assert ei.value.status == 400
    assert ei.value.error_code == "InvalidSyntax"


def test_rest_error_body_is_classified_as_usual(h: Harness, mock: respx.MockRouter) -> None:
    mock.post(URL).respond(403, json=[{"errorCode": "API_DISABLED_FOR_ORG", "message": "API is not enabled"}])
    sf = h.client()
    with pytest.raises(esf.Forbidden):
        h.run(sf.graphql(LEADS))


def test_response_without_data(h: Harness, mock: respx.MockRouter) -> None:
    mock.post(URL).respond(json={"errors": []})
    sf = h.client()
    with pytest.raises(esf.UnexpectedResponse, match="no 'data'"):
        h.run(sf.graphql(LEADS))


def test_query_is_retried_after_5xx(h: Harness, mock: respx.MockRouter) -> None:
    """A query is a read (§7.4): resent after a 503."""
    route = mock.post(URL).mock(
        side_effect=[
            httpx.Response(503, json=[{"errorCode": "SERVER_UNAVAILABLE", "message": "down"}]),
            httpx.Response(200, json=load_fixture("graphql/leads_page2.json")),
        ]
    )
    sf = h.client()
    h.run(sf.graphql(LEADS))
    assert route.call_count == 2


def test_mutation_is_not_retried(h: Harness, mock: respx.MockRouter) -> None:
    """A mutation writes: never resent after it may have reached Salesforce (ADR 0001)."""
    route = mock.post(URL).respond(503, json=[{"errorCode": "SERVER_UNAVAILABLE", "message": "down"}])
    sf = h.client()
    with pytest.raises(esf.ServerError):
        h.run(
            sf.graphql('mutation { uiapi { LeadCreate(input: {Lead: {LastName: "X"}}) { Record { Id } } } }')
        )
    assert route.call_count == 1


def test_invalid_arguments_rejected_before_sending(h: Harness, mock: respx.MockRouter) -> None:
    sf = h.client()
    with pytest.raises(ValueError):
        h.run(sf.graphql("  "))
    with pytest.raises(TypeError):
        h.run(sf.graphql(LEADS, [("after", None)]))  # type: ignore[arg-type]
    assert not mock.calls


def test_iter_follows_end_cursor(h: Harness, mock: respx.MockRouter, rec: Recorder) -> None:
    route = mock.post(URL).mock(
        side_effect=[
            httpx.Response(200, json=load_fixture("graphql/leads_page1.json")),
            httpx.Response(200, json=load_fixture("graphql/leads_page2.json")),
        ]
    )
    sf = h.client(recorder=rec)
    nodes = h.collect(sf.graphql_iter(LEADS, {"first": 2}, path="uiapi.query.Lead"))
    assert [n["Name"]["value"] for n in nodes] == ["Ada Lovelace", "Grace Hopper", "Alan Turing"]
    assert _sent(route, 0)["variables"] == {"first": 2}
    assert _sent(route, 1)["variables"] == {"first": 2, "after": "djE6MQ=="}
    assert [e["sobject"] for e in rec.requests] == ["Lead", "Lead"]


def test_iter_needs_after_variable(h: Harness, mock: respx.MockRouter) -> None:
    sf = h.client()
    with pytest.raises(ValueError, match=r"\$after"):
        h.collect(
            sf.graphql_iter("query { uiapi { query { Lead { edges { node { Id } } } } } }", path="uiapi")
        )
    with pytest.raises(ValueError, match="path"):
        h.collect(sf.graphql_iter(LEADS, path="uiapi..Lead"))
    assert not mock.calls


def test_iter_rejects_a_repeated_cursor(h: Harness, mock: respx.MockRouter) -> None:
    mock.post(URL).respond(json=load_fixture("graphql/leads_page1.json"))
    sf = h.client()
    with pytest.raises(esf.UnexpectedResponse, match="same endCursor"):
        h.collect(sf.graphql_iter(LEADS, path="uiapi.query.Lead"))


def test_iter_wrong_path(h: Harness, mock: respx.MockRouter) -> None:
    mock.post(URL).respond(json=load_fixture("graphql/leads_page2.json"))
    sf = h.client()
    with pytest.raises(esf.UnexpectedResponse, match="uiapi.query.Contact"):
        h.collect(sf.graphql_iter(LEADS, path="uiapi.query.Contact"))


def test_iter_raises_graphql_error(h: Harness, mock: respx.MockRouter) -> None:
    mock.post(URL).respond(json=load_fixture("graphql/validation_error.json"))
    sf = h.client()
    with pytest.raises(esf.GraphQLError):
        h.collect(sf.graphql_iter(LEADS, path="uiapi.query.Lead"))
