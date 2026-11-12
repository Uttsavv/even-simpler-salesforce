"""Composite family: ``sf.composite`` (§9.4), on both clients.

The responses in ``tests/fixtures/recorded/composite/*.json`` are recorded from a real org
(Developer Edition, API v68.0, 2026-11-12) by ``tests/sandbox/test_composite_graphql.py``; each
test here builds the same request with the client and checks it matches the recorded one. The
small inline bodies (malformed responses, statuses the org didn't produce) are **synthetic**.
developer.salesforce.com answers automated clients with 403, so the pages were read through
search excerpts; the evidence is V55-V59 in big-bang.md §21.

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
from ..fixtures import load_fixture, load_request

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
    """Recorded: ``@{lead.id}`` in a URL, a SOQL literal and ``@{got.LastName}`` in a body."""
    fixture = "composite/composite_ok.json"
    route = mock.post(f"{DATA}/composite").respond(json=load_fixture(fixture))
    recorded = load_request(fixture)
    sf = h.client(recorder=rec)
    c = sf.composite.builder()
    lead = c.create("Lead", recorded["compositeRequest"][0]["body"], ref_id="lead")
    got = c.get("Lead", lead.ref("id"), ["Id", "LastName"], ref_id="got")
    c.query("SELECT Id FROM Lead WHERE Id = {}", lead.ref("id"), ref_id="found")
    c.update("Lead", lead.ref("id"), {"Title": got.ref("LastName")}, ref_id="titled")
    result = h.run(c.send())
    assert sent(route) == recorded
    assert "%27@{lead.id}%27" in recorded["compositeRequest"][2]["url"]  # the reference isn't encoded
    assert isinstance(result, esf.CompositeResult)
    assert [r.status for r in result] == [201, 200, 200, 204]
    lead_id = result["lead"].body["id"]
    assert result["lead"].headers["Location"] == f"/services/data/v{API}/sobjects/Lead/{lead_id}"
    assert result["got"].body["Id"] == lead_id
    assert result["found"].body["totalSize"] == 1
    assert result["titled"].body is None
    assert not result.failed
    assert len(result.succeeded) == 4
    result.raise_for_errors()  # nothing failed
    (event,) = rec.requests
    assert event["operation"] == "composite.send"
    assert event["records"] == 4


def test_rolled_back_request_raises_the_real_cause(h: Harness, mock: respx.MockRouter) -> None:
    """Recorded: with allOrNone the other subrequests report 400 ``PROCESSING_HALTED``."""
    fixture = "composite/composite_rolled_back.json"
    mock.post(f"{DATA}/composite").respond(json=load_fixture(fixture))
    recorded = load_request(fixture)
    sf = h.client()
    c = sf.composite.builder(all_or_none=True)
    ok = c.create("Lead", recorded["compositeRequest"][0]["body"], ref_id="ok")
    c.create("Lead", recorded["compositeRequest"][1]["body"], ref_id="bad")
    c.get("Lead", ok.ref("id"), ["Id"], ref_id="read")
    result = h.run(c.send())
    assert len(result.failed) == 3
    assert [r.halted for r in result] == [True, False, True]
    assert isinstance(result["ok"].error, esf.RecordError)
    assert result["ok"].errors[0].error_code == "PROCESSING_HALTED"
    with pytest.raises(esf.ValidationRuleFailed) as ei:
        result.raise_for_errors()
    err = ei.value
    assert err.error_code == "FIELD_CUSTOM_VALIDATION_EXCEPTION"
    assert err.message == "esf-sandbox: validation rule probe"
    assert err.status == 400
    assert err.operation == "composite.send"
    assert err.method == "POST"
    assert err.path == "/sobjects/Lead"
    assert len(err.errors) == 3
    assert [e.extended["referenceId"] for e in err.errors] == ["ok", "bad", "read"]
    # The stored error is not changed by raising.
    assert len(result["bad"].errors) == 1


def test_failed_subrequest_without_all_or_none(h: Harness, mock: respx.MockRouter) -> None:
    """Recorded: allOrNone=false; the failed subrequest fails alone."""
    fixture = "composite/composite_partial.json"
    mock.post(f"{DATA}/composite").respond(json=load_fixture(fixture))
    recorded = load_request(fixture)
    sf = h.client()
    c = sf.composite.builder()
    c.create("Lead", recorded["compositeRequest"][0]["body"], ref_id="ok")
    c.create("Lead", recorded["compositeRequest"][1]["body"], ref_id="bad")
    result = h.run(c.send())
    assert [r.status for r in result] == [201, 400]
    assert [r.ok for r in result] == [True, False]
    assert not result["bad"].halted
    assert isinstance(result["bad"].error, esf.ValidationRuleFailed)
    with pytest.raises(esf.ValidationRuleFailed):
        result.raise_for_errors()


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
    """Recorded: two graphs; the second fails and reports ``isSuccessful: false``."""
    fixture = "composite/graph_mixed.json"
    route = mock.post(f"{DATA}/composite/graph").respond(json=load_fixture(fixture))
    recorded = load_request(fixture)
    rows = [[sub.get("body") for sub in g["compositeRequest"]] for g in recorded["graphs"]]
    sf = h.client(recorder=rec)
    g = sf.composite.graph()
    g1 = g.graph("g1")
    lead = g1.create("Lead", rows[0][0], ref_id="lead")
    g1.update("Lead", lead.ref("id"), {"Title": "esf-sandbox"}, ref_id="titled")
    g2 = g.graph("g2")
    g2.create("Lead", rows[1][0], ref_id="ok")
    g2.create("Lead", rows[1][1], ref_id="bad")
    results = h.run(g.send())
    assert sent(route) == recorded
    assert [(r.graph_id, r.ok) for r in results] == [("g1", True), ("g2", False)]
    assert isinstance(results[0], esf.GraphResult)
    results[0].raise_for_errors()
    assert [r.status for r in results[0].results] == [201, 204]
    assert results[1].results["ok"].halted
    with pytest.raises(esf.ValidationRuleFailed) as ei:
        results[1].raise_for_errors()
    assert ei.value.operation == "composite.graph"
    (event,) = rec.requests
    assert (event["operation"], event["records"]) == ("composite.graph", 4)


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
    """Recorded: 201 with ``hasErrors: false`` and one result per record, children included."""
    fixture = "composite/tree_ok.json"
    route = mock.post(f"{DATA}/composite/tree/Lead").respond(201, json=load_fixture(fixture))
    recorded = load_request(fixture)
    first, second = recorded["records"]
    sf = h.client(recorder=rec)
    records = [
        {
            "attributes": {"referenceId": "lead1"},
            "LastName": first["LastName"],
            "Company": first["Company"],
            "Tasks": {
                "records": [
                    {
                        "attributes": {"type": "Task", "referenceId": "task1"},
                        "Subject": first["Tasks"]["records"][0]["Subject"],
                    }
                ]
            },
        },
        {
            "attributes": {"referenceId": "lead2"},
            "LastName": second["LastName"],
            "Company": second["Company"],
        },
    ]
    ids = h.run(sf.composite.tree("Lead", records))
    assert sent(route) == recorded
    assert sorted(ids) == ["lead1", "lead2", "task1"]
    assert ids["task1"].startswith("00T")
    (event,) = rec.requests
    assert (event["operation"], event["sobject"], event["records"]) == ("composite.tree", "Lead", 3)


def test_tree_generates_reference_ids(h: Harness, mock: respx.MockRouter) -> None:
    """Records without ``referenceId`` get ``ref1``, ``ref2``... depth first (synthetic body)."""
    route = mock.post(f"{DATA}/composite/tree/Account").respond(
        201,
        json={
            "hasErrors": False,
            "results": [
                {"referenceId": "acme", "id": ACC},
                {"referenceId": "ref1", "id": "003xx0000000003AAA"},
                {"referenceId": "ref2", "id": "001xx0000000004AAA"},
            ],
        },
    )
    sf = h.client()
    records = [
        {
            "attributes": {"referenceId": "acme"},
            "Name": "Acme",
            "Contacts": {"records": [{"attributes": {"type": "Contact"}, "LastName": "Doe"}]},
        },
        {"Name": "Globex"},
    ]
    ids = h.run(sf.composite.tree("Account", records))
    assert ids == {"acme": ACC, "ref1": "003xx0000000003AAA", "ref2": "001xx0000000004AAA"}
    body = sent(route)
    assert body["records"][0]["attributes"] == {"referenceId": "acme", "type": "Account"}
    assert body["records"][0]["Contacts"]["records"][0]["attributes"] == {
        "type": "Contact",
        "referenceId": "ref1",
    }
    assert body["records"][1]["attributes"] == {"type": "Account", "referenceId": "ref2"}
    assert "attributes" not in records[1]  # the input isn't changed


def test_tree_failure_raises_typed_error(h: Harness, mock: respx.MockRouter) -> None:
    """Recorded: 400 with ``hasErrors: true``; ``results`` lists only the failed records."""
    fixture = "composite/tree_failed.json"
    route = mock.post(f"{DATA}/composite/tree/Lead").respond(400, json=load_fixture(fixture))
    recorded = load_request(fixture)
    sf = h.client()
    with pytest.raises(esf.ValidationRuleFailed) as ei:
        h.run(sf.composite.tree("Lead", recorded["records"]))
    err = ei.value
    assert err.error_code == "FIELD_CUSTOM_VALIDATION_EXCEPTION"
    assert err.status == 400
    assert err.operation == "composite.tree"
    assert err.sobject == "Lead"
    assert [(e.error_code, e.extended["referenceId"]) for e in err.errors] == [
        ("FIELD_CUSTOM_VALIDATION_EXCEPTION", "rule"),
        ("REQUIRED_FIELD_MISSING", "missing"),
    ]
    assert err.errors[1].fields == ("Company",)
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


@pytest.mark.parametrize("halt", [False, True])
def test_batch_runs_independent_subrequests(
    h: Harness, mock: respx.MockRouter, rec: Recorder, halt: bool
) -> None:
    """Recorded: ``hasErrors`` and ``results[]``; with haltOnError the rest get 412."""
    fixture = "composite/batch_halted.json" if halt else "composite/batch_mixed.json"
    route = mock.post(f"{DATA}/composite/batch").respond(json=load_fixture(fixture))
    recorded = load_request(fixture)
    rows = [r.get("richInput") for r in recorded["batchRequests"]]
    sf = h.client(recorder=rec)
    b = sf.composite.batch(halt_on_error=halt)
    created = b.create("Lead", rows[0])
    b.create("Lead", rows[1])
    b.query("SELECT Id FROM Lead LIMIT 1")
    b.update("Lead", recorded["batchRequests"][3]["url"].rsplit("/", 1)[1], {"Title": "x"})
    result = h.run(b.send())
    assert sent(route) == recorded
    assert recorded["batchRequests"][0] == {
        "method": "POST",
        "url": f"v{API}/sobjects/Lead",
        "richInput": rows[0],
    }
    assert result[created.reference_id].body["success"] is True
    assert isinstance(result[1].error, esf.ValidationRuleFailed)
    if halt:
        assert [r.status for r in result] == [201, 400, 412, 412]
        assert [r.halted for r in result] == [False, False, True, True]
    else:
        assert [r.status for r in result] == [201, 400, 200, 400]
        assert result[2].body["done"] is True
        assert result[3].errors[0].error_code == "MALFORMED_ID"
    with pytest.raises(esf.ValidationRuleFailed):
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
