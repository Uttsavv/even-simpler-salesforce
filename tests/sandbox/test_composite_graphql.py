"""Composite family and GraphQL against a real org, on both clients (big-bang.md §21 V50-V59).

Writes: every Lead carries the run tag in ``LastName`` and is deleted. Needs the validation rule
of ``tests/sandbox/org/core`` (``Company = ESF_VR`` fails with ``FIELD_CUSTOM_VALIDATION_EXCEPTION``)
and, for V51, ``Lead.Esf_GraphQL_Hidden__c`` from ``tests/sandbox/org/graphql`` (a field without
field-level security). Deploy both with ``scripts/sandbox_setup_org.py``.

Set ``ESF_SANDBOX_RECORD_DIR=tests/fixtures/recorded`` to re-record the contract-test fixtures
(``composite/*.json``, ``graphql/*.json``) from the sync run; read every file before committing.

Sources (developer.salesforce.com answers automated clients with 403; read through search
excerpts, 2026-11-12):

- Composite, 25 subrequests of which at most 5 sObject Collections or query operations
  (Query and QueryAll included):
  https://developer.salesforce.com/docs/atlas.en-us.api_rest.meta/api_rest/requests_composite.htm
- allOrNone and ``PROCESSING_HALTED``:
  https://developer.salesforce.com/docs/atlas.en-us.api_rest.meta/api_rest/resources_composite_allornone.htm
- Composite Graph:
  https://developer.salesforce.com/docs/atlas.en-us.api_rest.meta/api_rest/resources_composite_graph.htm
- sObject Tree:
  https://developer.salesforce.com/docs/atlas.en-us.api_rest.meta/api_rest/requests_composite_sobject_tree.htm
- Batch: https://developer.salesforce.com/docs/atlas.en-us.api_rest.meta/api_rest/requests_composite_batch.htm
- GraphQL paging (default ``first`` 10, at most 2,000; 4,000 records without ``upperBound``):
  https://developer.salesforce.com/docs/platform/graphql/guide/paginate-use.html and
  https://developer.salesforce.com/docs/platform/graphql/guide/paginate-use-upperbound.html
- GraphQL mutations (Beta v59.0-v65.0, GA v66.0; ``uiapi(input: {allOrNone})``, default true):
  https://developer.salesforce.com/docs/platform/graphql/guide/mutations-use.html
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import httpx
import pytest

import even_simpler_salesforce as esf

from .conftest import Cleanup, Driver, new_row

pytestmark = [pytest.mark.sandbox, pytest.mark.usefixtures("writes")]

VR = "ESF_VR"  # Company value the deployed validation rule rejects
QUERY_ONE = "query?q=SELECT+Id+FROM+Lead+LIMIT+1"
HALTED_MSG = "The transaction was rolled back since another operation in the same transaction failed."


def _client(sb: Driver, fixture: str | None = None) -> Any:
    """A client; the sync one records to ``$ESF_SANDBOX_RECORD_DIR/<fixture>`` when set."""
    out = os.environ.get("ESF_SANDBOX_RECORD_DIR")
    if fixture is None or not out or sb.is_async:
        return sb.client()
    path = Path(out) / fixture
    path.unlink(missing_ok=True)
    return sb.client(
        http_client=httpx.Client(transport=esf.testing.record(path, httpx.HTTPTransport()), timeout=120)
    )


def _leads(sb: Driver, sf: Any, like: str) -> list[dict[str, Any]]:
    soql = esf.soql("SELECT Id, LastName, Title FROM Lead WHERE LastName LIKE {} ORDER BY LastName", like)
    return sb.collect(sf.query_iter(soql))


# --- Composite (V55, V56) ---------------------------------------------------------------------


def test_composite_references_and_result_shape(sb: Driver, tag: str, cleanup: Cleanup) -> None:
    """V55: one entry per subrequest; ``@{ref.field}`` in the URL, the body and a SOQL literal."""
    sf = _client(sb, "composite/composite_ok.json")
    name = f"{tag}-cok-{sb.kind}"
    c = sf.composite.builder()
    lead = c.create("Lead", new_row("Lead", name), ref_id="lead")
    got = c.get("Lead", lead.ref("id"), ["Id", "LastName"], ref_id="got")
    c.query("SELECT Id FROM Lead WHERE Id = {}", lead.ref("id"), ref_id="found")
    c.update("Lead", lead.ref("id"), {"Title": got.ref("LastName")}, ref_id="titled")
    result = sb.run(c.send())
    cleanup.add(result["lead"].body["id"])
    assert [r.status for r in result] == [201, 200, 200, 204]
    assert result["lead"].headers["Location"].endswith(result["lead"].body["id"])
    assert result["got"].body["LastName"] == name
    assert result["found"].body["records"][0]["Id"] == result["lead"].body["id"]
    assert result["titled"].body is None
    result.raise_for_errors()
    assert [r["Title"] for r in _leads(sb, sf, name)] == [name]


def test_composite_all_or_none_raises_the_real_cause(sb: Driver, tag: str, cleanup: Cleanup) -> None:
    """V55: allOrNone=true; every other subrequest reports 400 PROCESSING_HALTED, nothing saved."""
    sf = _client(sb, "composite/composite_rolled_back.json")
    name = f"{tag}-crb-{sb.kind}"
    c = sf.composite.builder(all_or_none=True)
    ok = c.create("Lead", new_row("Lead", name), ref_id="ok")
    c.create("Lead", new_row("Lead", name, Company=VR), ref_id="bad")
    c.get("Lead", ok.ref("id"), ["Id"], ref_id="read")
    result = sb.run(c.send())
    assert [r.status for r in result] == [400, 400, 400]
    assert [r.halted for r in result] == [True, False, True]
    assert result["ok"].errors[0].error_code == "PROCESSING_HALTED"
    assert result["ok"].errors[0].message == HALTED_MSG
    with pytest.raises(esf.ValidationRuleFailed) as info:
        result.raise_for_errors()
    assert len(info.value.errors) == 3
    assert _leads(sb, sf, name) == []


def test_composite_without_all_or_none_runs_the_rest(sb: Driver, tag: str, cleanup: Cleanup) -> None:
    """V55: allOrNone=false; the failed subrequest fails alone."""
    sf = _client(sb, "composite/composite_partial.json")
    name = f"{tag}-cpa-{sb.kind}"
    c = sf.composite.builder()
    c.create("Lead", new_row("Lead", name), ref_id="ok")
    c.create("Lead", new_row("Lead", name, Company=VR), ref_id="bad")
    result = sb.run(c.send())
    cleanup.add(result["ok"].body["id"])
    assert [r.status for r in result] == [201, 400]
    assert result["bad"].errors[0].error_code == "FIELD_CUSTOM_VALIDATION_EXCEPTION"
    assert isinstance(result["bad"].error, esf.ValidationRuleFailed)
    assert len(_leads(sb, sf, name)) == 1


@pytest.mark.parametrize(
    "path",
    [
        "query?q=SELECT+Id+FROM+Lead+LIMIT+1",
        "queryAll?q=SELECT+Id+FROM+Lead+LIMIT+1",
        "composite/sobjects/Lead?ids={id}&fields=Id",
    ],
)
def test_composite_heavy_subrequest_limit(sb: Driver, path: str) -> None:
    """V56: the 6th sObject Collections/query subrequest gets 400 PROCESSING_HALTED.

    The builder refuses it before sending; the raw request shows what Salesforce answers.
    ``query/{locator}`` counts too (checked by hand on 2026-11-12); describe and sObject rows
    don't.
    """
    sf = sb.client()
    lead_id = sb.run(sf.query("SELECT Id FROM Lead LIMIT 1")).records[0]["Id"]
    url = path.format(id=lead_id)
    c = sf.composite.builder()
    for _ in range(5):
        c.request("GET", url)
    with pytest.raises(ValueError, match="at most 5"):
        c.request("GET", url)
    prefix = f"/services/data/v{sf.api_version}/"
    body = sb.run(
        sf.request(
            "POST",
            "composite",
            json={
                "allOrNone": False,
                "compositeRequest": [
                    {"method": "GET", "url": prefix + url, "referenceId": f"r{i}"} for i in range(6)
                ],
            },
        )
    )
    statuses = [r["httpStatusCode"] for r in body["compositeResponse"]]
    assert statuses == [200] * 5 + [400]
    err = body["compositeResponse"][5]["body"][0]
    assert err["errorCode"] == "PROCESSING_HALTED"
    assert "Limit number of query or Collections" in err["message"]


def test_composite_describe_does_not_count(sb: Driver) -> None:
    """V56: six describe subrequests run; the client lets them through."""
    sf = sb.client()
    c = sf.composite.builder()
    for _ in range(6):
        c.request("GET", "sobjects/Lead/describe")
    result = sb.run(c.send())
    assert [r.status for r in result] == [200] * 6


# --- Composite Graph (V57) --------------------------------------------------------------------


def test_graph_failure_rolls_back_only_its_graph(sb: Driver, tag: str, cleanup: Cleanup) -> None:
    sf = _client(sb, "composite/graph_mixed.json")
    good, bad = f"{tag}-g1-{sb.kind}", f"{tag}-g2-{sb.kind}"
    req = sf.composite.graph()
    g1 = req.graph("g1")
    lead = g1.create("Lead", new_row("Lead", good), ref_id="lead")
    g1.update("Lead", lead.ref("id"), {"Title": "esf-sandbox"}, ref_id="titled")
    g2 = req.graph("g2")
    g2.create("Lead", new_row("Lead", bad), ref_id="ok")
    g2.create("Lead", new_row("Lead", bad, Company=VR), ref_id="bad")
    first, second = sb.run(req.send())
    cleanup.add(first.results["lead"].body["id"])
    assert (first.graph_id, first.ok, second.graph_id, second.ok) == ("g1", True, "g2", False)
    assert [r.status for r in first.results] == [201, 204]
    assert [r.status for r in second.results] == [400, 400]
    assert second.results["ok"].halted
    with pytest.raises(esf.ValidationRuleFailed):
        second.raise_for_errors()
    assert [r["Title"] for r in _leads(sb, sf, good)] == ["esf-sandbox"]
    assert _leads(sb, sf, bad) == []


def test_graph_rejects_query_subrequests(sb: Driver) -> None:
    """V57: the builder refuses a query; Salesforce answers 404 OPERATION_NOT_ALLOWED for one."""
    sf = sb.client()
    with pytest.raises(ValueError, match="query"):
        sf.composite.graph().graph().query("SELECT Id FROM Lead LIMIT 1")
    body = sb.run(
        sf.request(
            "POST",
            "composite/graph",
            json={
                "graphs": [
                    {
                        "graphId": "g1",
                        "compositeRequest": [
                            {
                                "method": "GET",
                                "url": f"/services/data/v{sf.api_version}/{QUERY_ONE}",
                                "referenceId": "q",
                            }
                        ],
                    }
                ]
            },
        )
    )
    graph = body["graphs"][0]
    assert graph["isSuccessful"] is False
    sub = graph["graphResponse"]["compositeResponse"][0]
    assert sub["httpStatusCode"] == 404
    assert sub["body"][0]["errorCode"] == "OPERATION_NOT_ALLOWED"


# --- sObject Tree (V58) -----------------------------------------------------------------------


def test_tree_creates_records_and_children(sb: Driver, tag: str, cleanup: Cleanup) -> None:
    sf = _client(sb, "composite/tree_ok.json")
    name = f"{tag}-tok-{sb.kind}"
    refs = sb.run(
        sf.composite.tree(
            "Lead",
            [
                {
                    "attributes": {"referenceId": "lead1"},
                    **new_row("Lead", name, "-1"),
                    "Tasks": {
                        "records": [{"attributes": {"type": "Task", "referenceId": "task1"}, "Subject": name}]
                    },
                },
                {"attributes": {"referenceId": "lead2"}, **new_row("Lead", name, "-2")},
            ],
        )
    )
    cleanup.add(refs.get("lead1"), refs.get("lead2"))
    assert sorted(refs) == ["lead1", "lead2", "task1"]
    assert refs["task1"].startswith("00T")
    assert len(_leads(sb, sf, name + "%")) == 2
    sb.run(sf.collections.delete([refs["lead1"], refs["lead2"]]))
    cleanup.discard(refs["lead1"], refs["lead2"])
    tasks = sb.run(sf.query(esf.soql("SELECT Id FROM Task WHERE Subject = {}", name))).records
    assert tasks == []  # deleting the Lead deleted its Task


def test_tree_failure_saves_nothing(sb: Driver, tag: str) -> None:
    """V58: 400 ``hasErrors``; ``results`` lists only the failed records, with ``statusCode``."""
    sf = _client(sb, "composite/tree_failed.json")
    name = f"{tag}-tfail-{sb.kind}"
    with pytest.raises(esf.ValidationRuleFailed) as info:
        sb.run(
            sf.composite.tree(
                "Lead",
                [
                    {"attributes": {"referenceId": "good"}, **new_row("Lead", name)},
                    {"attributes": {"referenceId": "rule"}, **new_row("Lead", name, Company=VR)},
                    {"attributes": {"referenceId": "missing"}, "LastName": name},
                ],
            )
        )
    exc = info.value
    assert exc.status == 400
    assert [(e.error_code, e.extended["referenceId"]) for e in exc.errors] == [
        ("FIELD_CUSTOM_VALIDATION_EXCEPTION", "rule"),
        ("REQUIRED_FIELD_MISSING", "missing"),
    ]
    assert exc.errors[1].fields == ("Company",)
    assert _leads(sb, sf, name) == []


# --- Batch (V59) ------------------------------------------------------------------------------


@pytest.mark.parametrize("halt", [False, True])
def test_batch_results(sb: Driver, tag: str, cleanup: Cleanup, halt: bool) -> None:
    fixture = "composite/batch_halted.json" if halt else "composite/batch_mixed.json"
    sf = _client(sb, fixture)
    name = f"{tag}-b{int(halt)}-{sb.kind}"
    b = sf.composite.batch(halt_on_error=halt)
    b.create("Lead", new_row("Lead", name))
    b.create("Lead", new_row("Lead", name, Company=VR))
    b.query("SELECT Id FROM Lead LIMIT 1")
    b.update("Lead", "00Q000000000000AAA", {"Title": "x"})
    result = sb.run(b.send())
    cleanup.add(result[0].body["id"])
    assert result[0].status == 201
    assert result[1].status == 400
    assert isinstance(result[1].error, esf.ValidationRuleFailed)
    if halt:
        assert [r.status for r in result][2:] == [412, 412]
        assert all(r.halted and r.errors[0].error_code == "BATCH_PROCESSING_HALTED" for r in result[2:])
    else:
        assert result[2].status == 200
        assert result[2].body["done"] is True
        assert result[3].status == 400
        assert result[3].errors[0].error_code == "MALFORMED_ID"
    with pytest.raises(esf.ValidationRuleFailed):
        result.raise_for_errors()
    assert len(_leads(sb, sf, name)) == 1


# --- GraphQL (V50-V53) ------------------------------------------------------------------------

PAGE = """
query Leads($like: String, $after: String) {
  uiapi { query {
    Lead(first: 2, after: $after, where: {LastName: {like: $like}}, orderBy: {LastName: {order: ASC}}) {
      edges { node { Id LastName { value } } }
      pageInfo { hasNextPage endCursor }
      totalCount
    }
  } }
}
"""


def test_graphql_pages_with_after(sb: Driver, tag: str, cleanup: Cleanup) -> None:
    """V52: ``after: endCursor`` and ``pageInfo``; the iterator follows the cursor."""
    sf = sb.client()
    name = f"{tag}-gql-{sb.kind}-"
    created = sb.run(sf.collections.create([new_row("Lead", name, str(i)) for i in range(3)], sobject="Lead"))
    ids = [r.id for r in created if r.id]
    cleanup.add(*ids)
    rec = _client(sb, "graphql/leads_paged.json")
    nodes = sb.collect(rec.graphql_iter(PAGE, {"like": name + "%"}, path="uiapi.query.Lead"))
    assert [n["LastName"]["value"] for n in nodes] == [f"{name}{i}" for i in range(3)]
    first = sb.run(sf.graphql(PAGE, {"like": name + "%"}))["uiapi"]["query"]["Lead"]
    assert first["totalCount"] == 3
    assert first["pageInfo"]["hasNextPage"] is True


def test_graphql_first_default_and_maximum(sb: Driver) -> None:
    """V52: ``first`` defaults to 10 and can't exceed 2,000 (``DataFetchingException``)."""
    sf = sb.client()
    q = "query { uiapi { query { Lead%s { edges { node { Id } } totalCount } } } }"
    conn = sb.run(sf.graphql(q % ""))["uiapi"]["query"]["Lead"]
    assert len(conn["edges"]) == min(10, conn["totalCount"])
    with pytest.raises(esf.GraphQLError) as info:
        sb.run(sf.graphql(q % "(first: 2001, upperBound: 5000)"))
    assert info.value.error_code == "DataFetchingException"
    assert "less than equal to 2000" in info.value.errors[0].message
    with pytest.raises(esf.GraphQLError) as info:
        sb.run(sf.graphql(q % "(first: 2001)"))
    assert "upperBound" in info.value.errors[0].message


@pytest.mark.parametrize(
    ("fixture", "query", "code", "text"),
    [
        (
            "graphql/validation_error.json",
            "query { uiapi { query { Lead(first: 1) { edges { node { Id Nmae { value } } } } } } }",
            "ValidationError",
            "Field 'Nmae' in type 'Lead' is undefined",
        ),
        (
            "graphql/unknown_object.json",
            "query { uiapi { query { NoSuchObject__c(first: 1) { edges { node { Id } } } } } }",
            "ValidationError",
            "Field 'NoSuchObject__c' in type 'RecordQuery' is undefined",
        ),
        (
            "graphql/syntax_error.json",
            "query { uiapi { query { Lead(first: 1) { edges { node { Id } } } ",
            "InvalidSyntax",
            "Invalid syntax with offending token '<EOF>'",
        ),
        (
            "graphql/hidden_field.json",
            "query { uiapi { query { Lead(first: 1) { edges { node { Id Esf_GraphQL_Hidden__c { value } } } } } } }",  # noqa: E501
            "ValidationError",
            "Field 'Esf_GraphQL_Hidden__c' in type 'Lead' is undefined",
        ),
    ],
)
def test_graphql_rejected_documents(sb: Driver, fixture: str, query: str, code: str, text: str) -> None:
    """V50/V51: ``200 OK``, ``data: {}``, one entry with ``locations``, empty ``paths``.

    A field without field-level security looks exactly like a field that doesn't exist (V51).
    """
    sf = _client(sb, fixture)
    with pytest.raises(esf.GraphQLError) as info:
        sb.run(sf.graphql(query))
    exc = info.value
    assert exc.status == 200
    assert exc.error_code == code
    assert exc.data == {}
    assert text in exc.errors[0].message
    assert "&#39;" in exc.graphql_errors[0]["message"]  # sent HTML-escaped
    assert exc.errors[0].extended["path"] == []
    assert exc.errors[0].extended["locations"][0]["line"] == 1


def test_graphql_partial_data(sb: Driver) -> None:
    """V51: a field that fails while fetching is ``null`` next to the data that resolved."""
    sf = _client(sb, "graphql/partial_error.json")
    q = (
        "query { uiapi { ok: query { Lead(first: 1) { edges { node { Id } } } } "
        "bad: query { Lead(first: 2001) { edges { node { Id } } } } } }"
    )
    with pytest.raises(esf.GraphQLError) as info:
        sb.run(sf.graphql(q))
    exc = info.value
    assert exc.error_code == "DataFetchingException"
    assert exc.data["uiapi"]["bad"] == {"Lead": None}
    assert len(exc.data["uiapi"]["ok"]["Lead"]["edges"]) == 1
    assert exc.errors[0].extended["path"] == ["uiapi", "bad", "Lead"]


CREATE = """
mutation Create($input: LeadCreateInput!) {
  uiapi { LeadCreate(input: $input) { Record { Id LastName { value } } } }
}
"""


def test_graphql_mutation_creates_and_deletes(sb: Driver, tag: str, cleanup: Cleanup) -> None:
    """V53: ``LeadCreate`` returns the record; ``LeadDelete`` takes a ``RecordDeleteInput``."""
    sf = _client(sb, "graphql/mutation_create.json")
    name = f"{tag}-gmut-{sb.kind}"
    data = sb.run(sf.graphql(CREATE, {"input": {"Lead": new_row("Lead", name)}}))
    record = data["uiapi"]["LeadCreate"]["Record"]
    cleanup.add(record["Id"])
    assert record["LastName"]["value"] == name
    deleted = sb.run(
        sf.graphql(
            "mutation Del($input: RecordDeleteInput!) { uiapi { LeadDelete(input: $input) { Id } } }",
            {"input": {"Id": record["Id"]}},
        )
    )
    assert deleted["uiapi"]["LeadDelete"]["Id"] == record["Id"]
    cleanup.discard(record["Id"])
    assert _leads(sb, sf, name) == []


def test_graphql_mutation_failure(sb: Driver, tag: str) -> None:
    """V53: the cause first, then a rolled-back notice for the same path; nothing saved."""
    sf = _client(sb, "graphql/mutation_failed.json")
    name = f"{tag}-gmf-{sb.kind}"
    with pytest.raises(esf.GraphQLError) as info:
        sb.run(sf.graphql(CREATE, {"input": {"Lead": new_row("Lead", name, Company=VR)}}))
    exc = info.value
    assert exc.error_code == "DataFetchingException"
    assert exc.data == {"uiapi": {"LeadCreate": None}}
    assert [e.message for e in exc.errors][1:] == [HALTED_MSG]
    assert exc.errors[0].message != HALTED_MSG
    assert exc.errors[0].extended["path"] == ["uiapi", "LeadCreate"]
    assert _leads(sb, sf, name) == []


def test_graphql_mutation_all_or_none(sb: Driver, tag: str, cleanup: Cleanup) -> None:
    """V53: with ``allOrNone`` (the default) a later failure rolls back an earlier create.

    Salesforce lists the rollback notices of earlier operations first, so ``errors[0]`` is not
    always the cause: look through ``exc.errors``.
    """
    sf = _client(sb, "graphql/mutation_rolled_back.json")
    name = f"{tag}-gma-{sb.kind}"
    q = """
    mutation Two($ok: LeadCreateInput!, $bad: LeadCreateInput!) {
      uiapi(input: {allOrNone: true}) {
        ok: LeadCreate(input: $ok) { Record { Id } }
        bad: LeadCreate(input: $bad) { Record { Id } }
      }
    }
    """
    variables = {
        "ok": {"Lead": new_row("Lead", name)},
        "bad": {"Lead": new_row("Lead", name, Company=VR)},
    }
    with pytest.raises(esf.GraphQLError) as info:
        sb.run(sf.graphql(q, variables))
    exc = info.value
    assert exc.data == {"uiapi": {"ok": None, "bad": None}}
    assert exc.errors[0].message == HALTED_MSG
    assert any(e.message != HALTED_MSG and e.extended["path"] == ["uiapi", "bad"] for e in exc.errors)
    assert _leads(sb, sf, name) == []
