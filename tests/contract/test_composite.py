"""Composite family: ``sf.composite`` (§9.4), on both clients.

Every response here is **synthetic** (``tests/fixtures/recorded/composite/*.json``), hand-written
from the REST API Developer Guide because no org recording exists yet. developer.salesforce.com
answers automated clients with 403, so the pages were read through search excerpts; the open
points are V55-V59 in big-bang.md §21.

- Composite (``compositeRequest``, ``referenceId``, ``@{ref.field}``, 25 subrequests of which
  at most 5 sObject Collections or query operations; HTTP 200 with one result per subrequest):
  https://developer.salesforce.com/docs/atlas.en-us.api_rest.meta/api_rest/resources_composite_composite_post.htm
  https://developer.salesforce.com/docs/atlas.en-us.api_rest.meta/api_rest/requests_composite.htm
- allOrNone and ``PROCESSING_HALTED`` on the subrequests that didn't cause the failure:
  https://developer.salesforce.com/docs/atlas.en-us.api_rest.meta/api_rest/resources_composite_allornone.htm
- Composite Graph (``graphs[]`` with ``graphId``, ``graphResponse``, ``isSuccessful``; 500
  subrequests per graph):
  https://developer.salesforce.com/docs/atlas.en-us.api_rest.meta/api_rest/resources_composite_graph.htm
- sObject Tree (``attributes.type`` and ``referenceId``; ``hasErrors`` and ``results``):
  https://developer.salesforce.com/docs/atlas.en-us.api_rest.meta/api_rest/requests_composite_sobject_tree.htm
- Batch (``batchRequests``, ``haltOnError``, ``richInput``; ``results[]`` with ``statusCode``
  and ``result``; ``BATCH_PROCESSING_HALTED`` with 412):
  https://developer.salesforce.com/docs/atlas.en-us.api_rest.meta/api_rest/requests_composite_batch.htm
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
import respx

import even_simpler_salesforce as esf

from ..conftest import API, DATA, Harness, Recorder
from ..fixtures import load_fixture

ACC = "001xx0000000001AAA"


def sent(route: respx.Route, i: int = 0) -> Any:
    return json.loads(route.calls[i].request.content)


def three(sf: Any) -> Any:
    c = sf.composite.builder(all_or_none=True)
    acc = c.create("Account", {"Name": "Acme"}, ref_id="acc")
    c.create("Contact", {"LastName": "Doe", "AccountId": acc.ref("id")})
    c.query("SELECT Id FROM Contact WHERE AccountId = {}", acc.ref("id"))
    return c


# --- Composite ------------------------------------------------------------------------------------


def test_builder_sends_references_and_parses_results(
    h: Harness, mock: respx.MockRouter, rec: Recorder
) -> None:
    """Synthetic: a Composite request whose later subrequests use ``@{acc.id}``."""
    route = mock.post(f"{DATA}/composite").respond(json=load_fixture("composite/composite_ok.json"))
    sf = h.client(recorder=rec)
    result = h.run(three(sf).send())
    body = sent(route)
    assert body["allOrNone"] is True
    assert body["collateSubrequests"] is False
    reqs = body["compositeRequest"]
    assert [r["referenceId"] for r in reqs] == ["acc", "Contact2", "query3"]
    assert reqs[0] == {
        "method": "POST",
        "url": f"/services/data/v{API}/sobjects/Account",
        "referenceId": "acc",
        "body": {"Name": "Acme"},
    }
    assert reqs[1]["body"]["AccountId"] == "@{acc.id}"
    assert reqs[2]["method"] == "GET"
    assert "body" not in reqs[2]
    assert "%27@{acc.id}%27" in reqs[2]["url"]  # the reference itself isn't percent-encoded
    assert isinstance(result, esf.CompositeResult)
    assert [r.status for r in result] == [201, 201, 200]
    assert result["acc"].body["id"] == ACC
    assert result[1].headers["Location"].endswith("/Contact/003xx0000000001AAA")
    assert result["query3"].body["totalSize"] == 1
    assert not result.failed
    assert len(result.succeeded) == 3
    result.raise_for_errors()  # nothing failed
    (event,) = rec.requests
    assert event["operation"] == "composite.send"
    assert event["records"] == 3


def test_rolled_back_request_raises_the_real_cause(h: Harness, mock: respx.MockRouter) -> None:
    """Synthetic: with allOrNone the other subrequests report ``PROCESSING_HALTED``."""

    class AddressRequired(esf.ValidationRuleFailed): ...

    mock.post(f"{DATA}/composite").respond(json=load_fixture("composite/composite_rolled_back.json"))
    sf = h.client(error_tags={"ADDR_REQUIRED": AddressRequired})
    result = h.run(three(sf).send())
    assert len(result.failed) == 3
    assert [r.halted for r in result] == [True, False, True]
    assert isinstance(result["acc"].error, esf.RecordError)
    with pytest.raises(AddressRequired) as ei:
        result.raise_for_errors()
    err = ei.value
    assert err.message == "Street is required"
    assert err.fields == ("Street",)
    assert err.status == 400
    assert err.operation == "composite.send"
    assert err.method == "POST"
    assert err.path == "/sobjects/Contact"
    assert len(err.errors) == 3
    assert [e.extended["referenceId"] for e in err.errors] == ["acc", "Contact2", "query3"]
    # The stored error is not changed by raising.
    assert len(result["Contact2"].errors) == 1


def test_subrequest_status_picks_the_class(h: Harness, mock: respx.MockRouter) -> None:
    mock.post(f"{DATA}/composite").respond(
        json={
            "compositeResponse": [
                {
                    "body": [{"errorCode": "NOT_FOUND", "message": "The requested resource does not exist"}],
                    "httpHeaders": {},
                    "httpStatusCode": 404,
                    "referenceId": "Account1",
                },
                {"body": None, "httpHeaders": {}, "httpStatusCode": 204, "referenceId": "Account2"},
                {"body": None, "httpHeaders": {}, "httpStatusCode": 500, "referenceId": "Account3"},
            ]
        }
    )
    sf = h.client()
    c = sf.composite.builder()
    c.get("Account", ACC, ["Id", "Name"])
    c.delete("Account", ACC)
    c.update("Account", ACC, {"Name": "x"})
    result = h.run(c.send())
    assert isinstance(result[0].error, esf.NotFound)
    assert result[0].error.method == "GET"
    assert result[1].ok
    assert isinstance(result[2].error, esf.ServerError)
    assert result[2].error.message == "HTTP 500"
    with pytest.raises(esf.NotFound):
        result.raise_for_errors()
    with pytest.raises(KeyError):
        result["nope"]


def test_read_only_request_is_retried_after_a_timeout(h: Harness, mock: respx.MockRouter) -> None:
    ok = {
        "compositeResponse": [
            {"body": {"Id": ACC}, "httpHeaders": {}, "httpStatusCode": 200, "referenceId": "Account1"}
        ]
    }
    route = mock.post(f"{DATA}/composite")
    route.side_effect = [httpx.ReadTimeout("slow"), httpx.Response(200, json=ok)]
    sf = h.client()
    c = sf.composite.builder()
    c.get("Account", ACC)
    assert h.run(c.send())[0].body == {"Id": ACC}
    assert route.call_count == 2


def test_write_is_never_resent_after_a_timeout(h: Harness, mock: respx.MockRouter) -> None:
    """ADR 0001: a composite request with a write may have committed, so it isn't resent."""
    route = mock.post(f"{DATA}/composite").mock(side_effect=httpx.ReadTimeout("slow"))
    sf = h.client()
    with pytest.raises(esf.RequestTimeout):
        h.run(three(sf).send())
    assert route.call_count == 1


def test_write_is_not_resent_after_a_5xx(h: Harness, mock: respx.MockRouter) -> None:
    route = mock.post(f"{DATA}/composite").respond(
        503, json=[{"errorCode": "SERVER_UNAVAILABLE", "message": "x"}]
    )
    sf = h.client()
    with pytest.raises(esf.ServerError):
        h.run(three(sf).send())
    assert route.call_count == 1


def test_top_level_error_raises_typed_error(h: Harness, mock: respx.MockRouter) -> None:
    mock.post(f"{DATA}/composite").respond(
        400, json=[{"errorCode": "INVALID_FIELD", "message": "No such column 'Foo__c'"}]
    )
    sf = h.client()
    with pytest.raises(esf.InvalidField) as ei:
        h.run(three(sf).send())
    assert ei.value.operation == "composite.send"


@pytest.mark.parametrize(
    "body",
    [
        {"nope": []},
        {"compositeResponse": "x"},
        {"compositeResponse": [{"referenceId": "acc", "httpStatusCode": 201}]},
        {"compositeResponse": [1, 2, 3]},
        {"compositeResponse": [{"referenceId": r, "httpStatusCode": 200} for r in ("a", "b", "c")]},
        {"compositeResponse": [{"referenceId": r} for r in ("acc", "Contact2", "query3")]},
    ],
)
def test_unexpected_bodies(h: Harness, mock: respx.MockRouter, body: Any) -> None:
    mock.post(f"{DATA}/composite").respond(json=body)
    sf = h.client()
    with pytest.raises(esf.UnexpectedResponse):
        h.run(three(sf).send())


def test_limits_are_checked_before_sending(h: Harness, mock: respx.MockRouter) -> None:
    sf = h.client()
    c = sf.composite.builder()
    for i in range(25):
        c.create("Account", {"Name": f"A{i}"})
    with pytest.raises(ValueError, match="at most 25 subrequests"):
        c.create("Account", {"Name": "too many"})
    c = sf.composite.builder()
    for _ in range(3):
        c.query("SELECT Id FROM Account")
    c.request("POST", "composite/sobjects", body={"records": []})
    c.request("GET", "queryAll?q=SELECT+Id+FROM+Account")
    with pytest.raises(ValueError, match="at most 5 of the 25 subrequests .* sObject Collections or query"):
        c.query("SELECT Id FROM Contact")
    with pytest.raises(ValueError, match="at most 5"):
        c.request("PATCH", "composite/sobjects", body={"records": []})
    c.request("GET", "sobjects/Account/describe")  # other resources still fit
    with pytest.raises(ValueError, match="at least one subrequest"):
        h.run(sf.composite.builder().send())
    assert not mock.calls


def test_ref_ids_and_references_are_checked(h: Harness) -> None:
    sf = h.client()
    c = sf.composite.builder()
    acc = c.create("Account", {"Name": "A"}, ref_id="acc")
    assert acc == esf.Reference("acc")
    assert acc.ref("records[0].Id") == "@{acc.records[0].Id}"
    with pytest.raises(ValueError, match="duplicate ref_id"):
        c.create("Account", {"Name": "B"}, ref_id="acc")
    for bad in ("1abc", "a-b", ""):
        with pytest.raises(ValueError, match="invalid ref_id"):
            c.create("Account", {}, ref_id=bad)
    with pytest.raises(ValueError, match="invalid reference field"):
        acc.ref("id}")
    with pytest.raises(ValueError, match="'other', which isn't an earlier subrequest"):
        c.create("Contact", {"AccountId": esf.Reference("other").ref("id")})
    with pytest.raises(ValueError, match="invalid Salesforce id"):
        c.update("Account", "nope", {})
    with pytest.raises(ValueError, match="invalid sObject"):
        c.create("Account/../x", {})
    with pytest.raises(ValueError, match="unsupported subrequest method"):
        c.request("HEAD", "sobjects")
    with pytest.raises(ValueError, match="GET subrequests can't carry a body"):
        c.request("GET", "sobjects", body={})
    for path in ("/services/data/v68.0/sobjects", "https://evil.example/x", "sobjects/../limits", ""):
        with pytest.raises(ValueError):
            c.request("GET", path)
    # Auto-generated ids skip names that are taken.
    c2 = sf.composite.builder()
    c2.create("Account", {}, ref_id="Account2")
    assert c2.create("Account", {}).reference_id == "Account3"
    assert len(c2) == 2


def test_upsert_update_delete_urls(h: Harness, mock: respx.MockRouter) -> None:
    route = mock.post(f"{DATA}/composite").respond(
        json={
            "compositeResponse": [
                {
                    "body": {"id": ACC, "success": True, "errors": [], "created": True},
                    "httpStatusCode": 201,
                    "referenceId": "up",
                },
                {"body": None, "httpStatusCode": 204, "referenceId": "upd"},
                {"body": None, "httpStatusCode": 204, "referenceId": "del"},
            ]
        }
    )
    sf = h.client()
    c = sf.composite.builder(collate_subrequests=True)
    up = c.upsert("Account", "External_Id__c", "a/b 1", {"Name": "Acme"}, ref_id="up")
    c.update("Account", up.ref("id"), {"Rating": "Hot"}, ref_id="upd")
    c.delete("Account", up.ref("id"), ref_id="del")
    result = h.run(c.send())
    urls = [r["url"].removeprefix(f"/services/data/v{API}/") for r in sent(route)["compositeRequest"]]
    assert urls == [
        "sobjects/Account/External_Id__c/a%2Fb%201",
        "sobjects/Account/@{up.id}",
        "sobjects/Account/@{up.id}",
    ]
    assert sent(route)["collateSubrequests"] is True
    assert result["up"].body["created"] is True
    assert result[2].headers == {}


# --- Composite Graph ------------------------------------------------------------------------------


def test_graph_failure_rolls_back_only_its_graph(h: Harness, mock: respx.MockRouter, rec: Recorder) -> None:
    """Synthetic: two graphs; the second fails and reports ``isSuccessful: false``."""
    route = mock.post(f"{DATA}/composite/graph").respond(json=load_fixture("composite/graph_mixed.json"))
    sf = h.client(recorder=rec)
    g = sf.composite.graph()
    g1 = g.graph("g1")
    g1.create("Account", {"Name": "Acme"}, ref_id="acc")
    g2 = g.graph("g2")
    acc2 = g2.create("Account", {"Name": "Globex"}, ref_id="acc2")
    g2.create("Contact", {"AccountId": acc2.ref("id")})
    results = h.run(g.send())
    body = sent(route)
    assert [x["graphId"] for x in body["graphs"]] == ["g1", "g2"]
    assert body["graphs"][1]["compositeRequest"][1]["body"] == {"AccountId": "@{acc2.id}"}
    assert body["graphs"][0]["compositeRequest"][0]["url"] == f"/services/data/v{API}/sobjects/Account"
    assert [(r.graph_id, r.ok) for r in results] == [("g1", True), ("g2", False)]
    assert isinstance(results[0], esf.GraphResult)
    results[0].raise_for_errors()
    assert results[0].results["acc"].body["id"] == "001xx0000000002AAA"
    with pytest.raises(esf.RequiredFieldMissing) as ei:
        results[1].raise_for_errors()
    assert ei.value.operation == "composite.graph"
    (event,) = rec.requests
    assert (event["operation"], event["records"]) == ("composite.graph", 3)


def test_graph_without_error_details(h: Harness, mock: respx.MockRouter) -> None:
    ok = {"body": {"id": ACC}, "httpStatusCode": 201, "referenceId": "Account1"}
    mock.post(f"{DATA}/composite/graph").respond(
        json={
            "graphs": [
                {"graphId": "graph1", "graphResponse": {"compositeResponse": [ok]}, "isSuccessful": False}
            ]
        }
    )
    sf = h.client()
    g = sf.composite.graph()
    g.graph().create("Account", {"Name": "x"})
    (res,) = h.run(g.send())
    with pytest.raises(esf.UnexpectedResponse, match="graph1"):
        res.raise_for_errors()


def test_graph_limits_and_ids(h: Harness, mock: respx.MockRouter) -> None:
    sf = h.client()
    g = sf.composite.graph()
    with pytest.raises(ValueError, match="at least one graph"):
        h.run(g.send())
    big = g.graph()
    assert big.graph_id == "graph1"
    for i in range(500):
        big.create("Account", {"Name": f"A{i}"})
    with pytest.raises(ValueError, match="a graph takes at most 500 subrequests"):
        big.create("Account", {"Name": "too many"})
    with pytest.raises(ValueError, match="can't contain query"):
        big.query("SELECT Id FROM Account")
    with pytest.raises(ValueError, match="can't contain query"):
        big.request("GET", "query?q=SELECT+Id+FROM+Account")
    with pytest.raises(ValueError, match="duplicate graph_id"):
        g.graph("graph1")
    with pytest.raises(ValueError, match="invalid graph_id"):
        g.graph("no spaces")
    g.graph("graph2")
    assert g.graph().graph_id == "graph3"
    with pytest.raises(ValueError, match="at least one subrequest to a graph"):
        h.run(g.send())
    assert not mock.calls


def test_graph_unexpected_bodies(h: Harness, mock: respx.MockRouter) -> None:
    sf = h.client()
    for body in ({"graphs": "x"}, {"graphs": [1]}, {"graphs": [{"graphId": "graph1"}]}):
        mock.post(f"{DATA}/composite/graph").respond(json=body)
        g = sf.composite.graph()
        g.graph().get("Account", ACC)
        with pytest.raises(esf.UnexpectedResponse):
            h.run(g.send())


# --- sObject Tree ---------------------------------------------------------------------------------


def test_tree_creates_nested_records(h: Harness, mock: respx.MockRouter, rec: Recorder) -> None:
    """Synthetic: 201 with ``hasErrors: false`` and one result per record."""
    route = mock.post(f"{DATA}/composite/tree/Account").respond(
        201, json=load_fixture("composite/tree_ok.json")
    )
    sf = h.client(recorder=rec)
    records = [
        {
            "attributes": {"referenceId": "acme"},
            "Name": "Acme",
            "Contacts": {"records": [{"attributes": {"type": "Contact"}, "LastName": "Doe"}]},
        },
        {"Name": "Globex"},
    ]
    ids = h.run(sf.composite.tree("Account", records))
    assert ids == {"acme": "001xx0000000003AAA", "ref1": "003xx0000000003AAA", "ref2": "001xx0000000004AAA"}
    body = sent(route)
    assert body["records"][0]["attributes"] == {"referenceId": "acme", "type": "Account"}
    assert body["records"][0]["Contacts"]["records"][0]["attributes"] == {
        "type": "Contact",
        "referenceId": "ref1",
    }
    assert body["records"][1]["attributes"] == {"type": "Account", "referenceId": "ref2"}
    assert "attributes" not in records[1]  # the input isn't changed
    (event,) = rec.requests
    assert (event["operation"], event["sobject"], event["records"]) == ("composite.tree", "Account", 3)


def test_tree_failure_raises_typed_error(h: Harness, mock: respx.MockRouter) -> None:
    """Synthetic: 400 with ``hasErrors: true``; nothing was saved."""
    route = mock.post(f"{DATA}/composite/tree/Contact").respond(
        400, json=load_fixture("composite/tree_failed.json")
    )
    sf = h.client()
    with pytest.raises(esf.RecordError) as ei:
        h.run(sf.composite.tree("Contact", [{"LastName": "Doe", "Email": "not-an-email"}]))
    err = ei.value
    assert err.error_code == "INVALID_EMAIL_ADDRESS"
    assert err.fields == ("Email",)
    assert err.status == 400
    assert err.operation == "composite.tree"
    assert err.sobject == "Contact"
    assert err.errors[0].extended["referenceId"] == "ref1"
    assert route.call_count == 1  # a write: never resent


def test_tree_other_errors(h: Harness, mock: respx.MockRouter) -> None:
    sf = h.client()
    mock.post(f"{DATA}/composite/tree/Account").respond(400, json={"hasErrors": True, "results": []})
    with pytest.raises(esf.UnexpectedResponse, match="without error details"):
        h.run(sf.composite.tree("Account", [{"Name": "x"}]))
    mock.post(f"{DATA}/composite/tree/Account").respond(
        400, json=[{"errorCode": "INVALID_TYPE", "message": "bad type"}]
    )
    with pytest.raises(esf.InvalidType):
        h.run(sf.composite.tree("Account", [{"Name": "x"}]))
    mock.post(f"{DATA}/composite/tree/Account").respond(201, json={"hasErrors": False, "results": "x"})
    with pytest.raises(esf.UnexpectedResponse):
        h.run(sf.composite.tree("Account", [{"Name": "x"}]))


def test_tree_limits_are_checked_before_sending(h: Harness, mock: respx.MockRouter) -> None:
    sf = h.client()

    def nest(depth: int) -> dict[str, Any]:
        rec: dict[str, Any] = {"attributes": {"type": "Account"}, "Name": f"L{depth}"}
        if depth > 1:
            rec["ChildAccounts"] = {"records": [nest(depth - 1)]}
        return rec

    with pytest.raises(ValueError, match="at most 5 levels"):
        h.run(sf.composite.tree("Account", [nest(6)]))
    with pytest.raises(ValueError, match="at most 200 records, got 201"):
        h.run(sf.composite.tree("Account", [{"Name": str(i)} for i in range(201)]))
    six = [{"attributes": {"type": t}, "Name": "x"} for t in ("Contact", "Case", "Note", "Task", "Event")]
    with pytest.raises(ValueError, match="at most 5 record types, got 6"):
        h.run(sf.composite.tree("Account", [{"Name": "A", "Kids": {"records": six}}]))
    with pytest.raises(ValueError, match="need attributes.type"):
        h.run(sf.composite.tree("Account", [{"Name": "A", "Contacts": {"records": [{"LastName": "x"}]}}]))
    with pytest.raises(ValueError, match="root records must be Account"):
        h.run(sf.composite.tree("Account", [{"attributes": {"type": "Contact"}}]))
    with pytest.raises(ValueError, match="duplicate referenceId"):
        h.run(sf.composite.tree("Account", [{"attributes": {"referenceId": "a"}}] * 2))
    with pytest.raises(ValueError, match="invalid referenceId"):
        h.run(sf.composite.tree("Account", [{"attributes": {"referenceId": "a b"}}]))
    with pytest.raises(ValueError, match="at least one record"):
        h.run(sf.composite.tree("Account", []))
    with pytest.raises(TypeError):
        h.run(sf.composite.tree("Account", ["x"]))  # type: ignore[list-item]
    assert not mock.calls


# --- Batch ----------------------------------------------------------------------------------------


def test_batch_runs_independent_subrequests(h: Harness, mock: respx.MockRouter, rec: Recorder) -> None:
    """Synthetic: a 200 with ``hasErrors: true``, a 404 and a halted 412."""
    route = mock.post(f"{DATA}/composite/batch").respond(json=load_fixture("composite/batch_mixed.json"))
    sf = h.client(recorder=rec)
    b = sf.composite.batch(halt_on_error=True)
    first = b.get("Account", ACC, ["Name"])
    b.get("Account", "001xx0000000009AAA")
    b.update("Account", ACC, {"Name": "Acme 2"})
    result = h.run(b.send())
    body = sent(route)
    assert body["haltOnError"] is True
    assert body["batchRequests"][0] == {"method": "GET", "url": f"v{API}/sobjects/Account/{ACC}?fields=Name"}
    assert body["batchRequests"][2]["richInput"] == {"Name": "Acme 2"}
    assert result[first.reference_id].body["Name"] == "Acme"
    assert [r.status for r in result] == [200, 404, 412]
    assert result[2].halted
    with pytest.raises(esf.NotFound):
        result.raise_for_errors()
    (event,) = rec.requests
    assert event["operation"] == "composite.batch"


def test_batch_of_reads_is_retried(h: Harness, mock: respx.MockRouter) -> None:
    ok = {
        "hasErrors": False,
        "results": [{"statusCode": 200, "result": {"totalSize": 0, "done": True, "records": []}}],
    }
    route = mock.post(f"{DATA}/composite/batch")
    route.side_effect = [httpx.ReadTimeout("slow"), httpx.Response(200, json=ok)]
    sf = h.client()
    b = sf.composite.batch()
    b.query("SELECT Id FROM Account WHERE Name = {}", "O'Brien")
    result = h.run(b.send())
    assert result[0].body["totalSize"] == 0
    assert route.call_count == 2
    assert "O%5C%27Brien" in sent(route, 1)["batchRequests"][0]["url"]  # quoted like sf.query


def test_batch_limits(h: Harness, mock: respx.MockRouter) -> None:
    sf = h.client()
    b = sf.composite.batch()
    acc = b.create("Account", {"Name": "A"})
    with pytest.raises(ValueError, match="can't use references"):
        b.create("Contact", {"AccountId": acc.ref("id")})
    for i in range(24):
        b.query(f"SELECT Id FROM Account LIMIT {i + 1}")  # no 5-query limit in a Batch
    with pytest.raises(ValueError, match="a Batch request takes at most 25 subrequests"):
        b.get("Account", ACC)
    with pytest.raises(ValueError, match="at least one subrequest"):
        h.run(sf.composite.batch().send())
    assert not mock.calls


def test_batch_unexpected_bodies(h: Harness, mock: respx.MockRouter) -> None:
    sf = h.client()
    for body in ({"results": [1]}, {"results": []}, {"results": [{"result": None}]}, [1]):
        mock.post(f"{DATA}/composite/batch").respond(json=body)
        b = sf.composite.batch()
        b.get("Account", ACC)
        with pytest.raises(esf.UnexpectedResponse):
            h.run(b.send())
