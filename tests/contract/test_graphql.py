"""GraphQL API: ``sf.graphql`` and ``sf.graphql_iter`` (§9.6), on both clients.

The responses in ``tests/fixtures/recorded/graphql/*.json`` are recorded from a real org
(Developer Edition, API v68.0, 2026-11-12) by ``tests/sandbox/test_composite_graphql.py``. Small
inline bodies (a 4xx with ``errors``, a REST error list, malformed responses) are **synthetic**.
Documentation:

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
excerpts. What the org showed (V50-V53 in big-bang.md §21): every GraphQL error comes with
``200 OK``; the field path is sent as ``paths`` and messages are HTML-escaped; a rejected
document has ``data: {}``; only a ``DataFetchingException`` comes with partial data.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
import respx

import even_simpler_salesforce as esf

from ..conftest import DATA, Harness, Recorder
from ..fixtures import load_fixture, load_request

URL = f"{DATA}/graphql"
PAGED = "graphql/leads_paged.json"
LEADS: str = load_request(PAGED)["query"]
LIKE: dict[str, Any] = load_request(PAGED)["variables"]
NAME = LIKE["like"].rstrip("%")


def _sent(route: respx.Route, i: int = 0) -> Any:
    return json.loads(route.calls[i].request.content)


def test_graphql_returns_data(h: Harness, mock: respx.MockRouter, rec: Recorder) -> None:
    """Recorded: a successful response carries ``"errors": []`` and ``"extensions": {}``."""
    route = mock.post(URL).respond(json=load_fixture(PAGED, 1))
    sf = h.client(recorder=rec)
    after = load_request(PAGED, 1)["variables"]["after"]
    data = h.run(sf.graphql(LEADS, {**LIKE, "after": after}))
    assert data["uiapi"]["query"]["Lead"]["edges"][0]["node"]["LastName"]["value"] == NAME + "2"
    assert _sent(route) == load_request(PAGED, 1)
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
    """Recorded: an unknown field is ``200 OK`` with ``data: {}`` and one ``errors`` entry (V50)."""
    mock.post(URL).respond(json=load_fixture("graphql/validation_error.json"))
    sf = h.client(recorder=rec)
    with pytest.raises(esf.GraphQLError) as ei:
        h.run(sf.graphql(load_request("graphql/validation_error.json")["query"]))
    err = ei.value
    assert err.status == 200
    assert err.error_code == "ValidationError"
    assert err.message.endswith("Field 'Nmae' in type 'Lead' is undefined")  # HTML-unescaped
    assert "&#39;Nmae&#39;" in err.graphql_errors[0]["message"]  # as Salesforce sent it
    assert err.data == {}
    assert err.graphql_errors[0]["locations"] == [{"column": 60, "line": 1}]
    assert err.errors[0].extended["extensions"] == {"ErrorType": "ValidationError"}
    assert err.errors[0].extended["path"] == []  # sent as "paths"
    assert err.operation == "graphql"
    assert err.docs_url is not None
    assert err.docs_url.endswith("#graphqlerror")
    (event,) = rec.requests
    assert event["outcome"] == "error"
    assert event["error_class"] == "GraphQLError"
    assert event["error_code"] == "ValidationError"


@pytest.mark.parametrize(
    ("fixture", "code", "text"),
    [
        (
            "graphql/syntax_error.json",
            "InvalidSyntax",
            "Invalid syntax with offending token '<EOF>' at line 1",
        ),
        ("graphql/unknown_object.json", "ValidationError", "Field 'NoSuchObject__c' in type 'RecordQuery'"),
        ("graphql/hidden_field.json", "ValidationError", "Field 'Esf_GraphQL_Hidden__c' in type 'Lead'"),
    ],
)
def test_rejected_documents(h: Harness, mock: respx.MockRouter, fixture: str, code: str, text: str) -> None:
    """Recorded: a syntax error, an unknown object and a field without field-level security."""
    mock.post(URL).respond(json=load_fixture(fixture))
    sf = h.client()
    with pytest.raises(esf.GraphQLError) as ei:
        h.run(sf.graphql(load_request(fixture)["query"]))
    assert (ei.value.status, ei.value.error_code, ei.value.data) == (200, code, {})
    assert text in ei.value.message


def test_partial_data_is_on_the_error(h: Harness, mock: respx.MockRouter) -> None:
    """Recorded: a ``DataFetchingException`` leaves ``null`` next to the data that resolved (V51)."""
    fixture = "graphql/partial_error.json"
    mock.post(URL).respond(json=load_fixture(fixture))
    sf = h.client()
    with pytest.raises(esf.GraphQLError) as ei:
        h.run(sf.graphql(load_request(fixture)["query"]))
    err = ei.value
    assert err.error_code == "DataFetchingException"
    assert "without using upperBound" in err.message
    assert len(err.data["uiapi"]["ok"]["Lead"]["edges"]) == 1
    assert err.data["uiapi"]["bad"] == {"Lead": None}
    assert [e.extended["path"] for e in err.errors] == [["uiapi", "bad", "Lead"]]
    assert err.graphql_errors[0]["paths"] == ["uiapi", "bad", "Lead"]


def test_mutation_creates_a_record(h: Harness, mock: respx.MockRouter) -> None:
    """Recorded: ``LeadCreate`` returns the record; ``LeadDelete`` returns its id (V53)."""
    fixture = "graphql/mutation_create.json"
    route = mock.post(URL).mock(
        side_effect=[httpx.Response(200, json=load_fixture(fixture, i)) for i in (0, 1)]
    )
    create, delete = load_request(fixture, 0), load_request(fixture, 1)
    sf = h.client()
    data = h.run(sf.graphql(create["query"], create["variables"]))
    record = data["uiapi"]["LeadCreate"]["Record"]
    assert record["LastName"]["value"] == create["variables"]["input"]["Lead"]["LastName"]
    assert _sent(route) == create
    gone = h.run(sf.graphql(delete["query"], {"input": {"Id": record["Id"]}}))
    assert gone["uiapi"]["LeadDelete"]["Id"] == record["Id"]


@pytest.mark.parametrize("fixture", ["graphql/mutation_failed.json", "graphql/mutation_rolled_back.json"])
def test_failed_mutation(h: Harness, mock: respx.MockRouter, fixture: str) -> None:
    """Recorded: a validation rule fails a mutation with ``DataFetchingException`` (V53).

    Each failed operation gets its cause and a rolled-back notice; with two operations in one
    all-or-none mutation the first entry is the notice for the operation that didn't fail.
    """
    mock.post(URL).respond(json=load_fixture(fixture))
    req = load_request(fixture)
    sf = h.client()
    with pytest.raises(esf.GraphQLError) as ei:
        h.run(sf.graphql(req["query"], req["variables"]))
    err = ei.value
    assert err.error_code == "DataFetchingException"
    assert all(v is None for v in err.data["uiapi"].values())
    causes = [e for e in err.errors if "validation rule probe" in e.message]
    assert len(causes) == 1
    assert causes[0].extended["path"][0] == "uiapi"
    rolled_back = "The transaction was rolled back since another operation in the same transaction failed."
    assert (err.errors[0].message == rolled_back) is fixture.endswith("rolled_back.json")


def test_errors_with_4xx_status(h: Harness, mock: respx.MockRouter) -> None:
    """Synthetic: the org answered every GraphQL error with 200, but a 4xx with ``errors`` is
    still a :class:`GraphQLError`."""
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
            httpx.Response(200, json=load_fixture(PAGED, 1)),
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
    """Recorded: two pages of ``first: 2``; the second request sets ``after`` to ``endCursor``."""
    route = mock.post(URL).mock(
        side_effect=[httpx.Response(200, json=load_fixture(PAGED, i)) for i in (0, 1)]
    )
    sf = h.client(recorder=rec)
    nodes = h.collect(sf.graphql_iter(LEADS, LIKE, path="uiapi.query.Lead"))
    assert [n["LastName"]["value"] for n in nodes] == [f"{NAME}{i}" for i in range(3)]
    assert _sent(route, 0) == load_request(PAGED, 0)
    assert _sent(route, 1) == load_request(PAGED, 1)
    assert (
        _sent(route, 1)["variables"]["after"]
        == load_fixture(PAGED, 0)["data"]["uiapi"]["query"]["Lead"]["pageInfo"]["endCursor"]
    )
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
    mock.post(URL).respond(json=load_fixture(PAGED, 0))
    sf = h.client()
    with pytest.raises(esf.UnexpectedResponse, match="same endCursor"):
        h.collect(sf.graphql_iter(LEADS, path="uiapi.query.Lead"))


def test_iter_wrong_path(h: Harness, mock: respx.MockRouter) -> None:
    mock.post(URL).respond(json=load_fixture(PAGED, 1))
    sf = h.client()
    with pytest.raises(esf.UnexpectedResponse, match="uiapi.query.Contact"):
        h.collect(sf.graphql_iter(LEADS, path="uiapi.query.Contact"))


def test_iter_raises_graphql_error(h: Harness, mock: respx.MockRouter) -> None:
    mock.post(URL).respond(json=load_fixture("graphql/validation_error.json"))
    sf = h.client()
    with pytest.raises(esf.GraphQLError):
        h.collect(sf.graphql_iter(LEADS, path="uiapi.query.Lead"))
