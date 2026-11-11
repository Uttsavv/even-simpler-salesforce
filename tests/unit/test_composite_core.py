"""``core.composite``, the ``composite.tree`` rule in ``core.classify`` and ``CompositeResult``.

Bodies are **synthetic**, shaped after the REST API Developer Guide; see
tests/contract/test_composite.py for the URLs.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

import even_simpler_salesforce as esf
from even_simpler_salesforce.core import composite as C
from even_simpler_salesforce.core.classify import classify
from even_simpler_salesforce.core.http import RawResponse, RequestSpec


def test_references_in_walks_nested_values() -> None:
    value = {"@{k.id}": ["@{a.id}", ("x @{b.records[0].Id}",), {"n": 1}], "z": None}
    assert C.references_in(value) == {"k", "a", "b"}
    assert C.references_in(3) == set()


def test_reference_fields() -> None:
    ref = C.Reference("acc")
    assert ref.ref("id") == "@{acc.id}"
    assert ref.ref("records[0].Owner.Name") == "@{acc.records[0].Owner.Name}"
    for bad in ("", "a b", "x}", "a..b", 3):
        with pytest.raises(ValueError, match="invalid reference field"):
            ref.ref(bad)  # type: ignore[arg-type]


def test_generated_ref_ids_skip_taken_names() -> None:
    c = C.CompositeRequest()
    c.create("Account", {}, ref_id="Account2")
    assert c.create("Account", {}).reference_id == "Account3"


def test_request_kinds_and_heavy_limit() -> None:
    c = C.CompositeRequest()
    c.request("POST", "composite/sobjects", body={"records": []})
    c.request("GET", "composite/sobjects/Account?ids=x&fields=Id")
    c.request("GET", "queryAll?q=SELECT+Id+FROM+Account")
    c.query("SELECT Id FROM Account")
    c.request("GET", "query/01gxx-2000")
    c.request("GET", "limits")  # not heavy
    assert [s.kind for s in c.subrequests] == [
        "collections",
        "collections",
        "query",
        "query",
        "query",
        "other",
    ]
    with pytest.raises(ValueError, match="at most 5 of the 25"):
        c.query("SELECT Id FROM Contact")
    assert len(c) == 6


@pytest.mark.parametrize(
    ("method", "path", "body", "match"),
    [
        ("TRACE", "limits", None, "unsupported subrequest method"),
        ("GET", "limits", {"a": 1}, "can't carry a body"),
        ("GET", "", None, "non-empty"),
        ("GET", "/services/data/v60.0/limits", None, "relative"),
        ("GET", "https://evil.example/x", None, "relative"),
        ("GET", "sobjects/../limits", None, "traversal"),
    ],
)
def test_request_validation(method: str, path: str, body: Any, match: str) -> None:
    with pytest.raises(ValueError, match=match):
        C.CompositeRequest().request(method, path, body=body)


def test_graph_and_batch_rules() -> None:
    g = C.GraphRequest().graph()
    with pytest.raises(ValueError, match="can't contain query"):
        g.query("SELECT Id FROM Account")
    with pytest.raises(ValueError, match="can't contain query"):
        g.request("GET", "query?q=x")
    b = C.BatchRequest()
    a = b.create("Account", {})
    with pytest.raises(ValueError, match="can't use references"):
        b.create("Contact", {"AccountId": a.ref("id")})
    for _ in range(24):
        b.query("SELECT Id FROM Account")  # Batch has no 5-of-25 rule
    with pytest.raises(ValueError, match="at most 25"):
        b.create("Account", {})


def test_graph_ids() -> None:
    req = C.GraphRequest()
    req.graph("graph2")
    assert req.graph().graph_id == "graph3"
    with pytest.raises(ValueError, match="duplicate graph_id"):
        req.graph("graph2")
    with pytest.raises(ValueError, match="invalid graph_id"):
        req.graph("1x")
    with pytest.raises(ValueError, match="at least one subrequest"):
        C.graph_payload(req, "60.0")


def test_batch_payload_uses_rich_input() -> None:
    b = C.BatchRequest(halt_on_error=True)
    b.update("Account", "001xx0000000001AAA", {"Name": "x"})
    b.get("Account", "001xx0000000001AAA")
    assert C.batch_payload(b, "60.0") == {
        "batchRequests": [
            {
                "method": "PATCH",
                "url": "v60.0/sobjects/Account/001xx0000000001AAA",
                "richInput": {"Name": "x"},
            },
            {"method": "GET", "url": "v60.0/sobjects/Account/001xx0000000001AAA"},
        ],
        "haltOnError": True,
    }
    assert not C.is_read_only(b.subrequests)
    assert C.is_read_only(b.subrequests[1:])


def test_tree_payload_assigns_types_and_refs() -> None:
    body, refs = C.tree_payload(
        "Account",
        [
            {
                "attributes": {"referenceId": "ref1"},
                "Name": "A",
                "Contacts": {"records": [{"attributes": {"type": "Contact"}, "LastName": "D"}]},
                "Meta": {"not": "children"},
            }
        ],
    )
    assert refs == ["ref1", "ref2"]
    root = body["records"][0]
    assert root["attributes"] == {"type": "Account", "referenceId": "ref1"}
    assert root["Contacts"]["records"][0]["attributes"] == {"type": "Contact", "referenceId": "ref2"}
    assert root["Meta"] == {"not": "children"}


def _nest(levels: int) -> dict[str, Any]:
    row: dict[str, Any] = {"attributes": {"type": "Contact"}, "LastName": "x"}
    for _ in range(levels - 2):
        row = {"attributes": {"type": "Contact"}, "Kids": {"records": [row]}}
    return {"Name": "root", "Contacts": {"records": [row]}}


@pytest.mark.parametrize(
    ("records", "match"),
    [
        ([], "at least one record"),
        (["x"], "must be a dict"),
        ([{"attributes": {"referenceId": "1a"}}], "invalid referenceId"),
        ([{"attributes": {"referenceId": "a"}}, {"attributes": {"referenceId": "a"}}], "duplicate"),
        ([{"attributes": {"type": "Contact"}}], "root records must be Account"),
        ([{"Contacts": {"records": [{"LastName": "x"}]}}], "need attributes.type"),
        ([{"Name": str(i)} for i in range(201)], "at most 200 records"),
        ([_nest(6)], "at most 5 levels"),
        (
            [{f"R{i}": {"records": [{"attributes": {"type": f"T{i}__c"}}]} for i in range(5)}],
            "at most 5 record types",
        ),
    ],
)
def test_tree_limits(records: list[Any], match: str) -> None:
    with pytest.raises((ValueError, TypeError), match=match):
        C.tree_payload("Account", records)


def test_tree_at_the_limits_is_accepted() -> None:
    _, refs = C.tree_payload("Account", [_nest(5)])
    assert len(refs) == 5
    _, refs = C.tree_payload("Account", [{"Name": str(i)} for i in range(200)])
    assert len(refs) == 200


def test_tree_generated_refs_skip_given_ones() -> None:
    _, refs = C.tree_payload("Account", [{"Name": "a"}, {"attributes": {"referenceId": "ref1"}}])
    assert refs == ["ref2", "ref1"]


def _tree_spec() -> RequestSpec:
    return RequestSpec("POST", "composite/tree/Account", operation="composite.tree", dml=True)


def test_parse_tree() -> None:
    ok = {"hasErrors": False, "results": [{"referenceId": "ref1", "id": "001xx0000000001AAA"}]}
    assert C.parse_tree(201, ok, spec=_tree_spec()) == {"ref1": "001xx0000000001AAA"}
    failed = {
        "hasErrors": True,
        "results": [
            {"errors": [{"statusCode": "REQUIRED_FIELD_MISSING", "message": "m", "fields": ["Name"]}]},
            {"referenceId": "ref2", "errors": [{"statusCode": "INVALID_FIELD", "message": "n"}]},
        ],
    }
    with pytest.raises(esf.RequiredFieldMissing) as ei:
        C.parse_tree(400, failed, spec=_tree_spec())
    assert "referenceId" not in ei.value.errors[0].extended
    assert ei.value.errors[1].extended["referenceId"] == "ref2"
    with pytest.raises(esf.UnexpectedResponse, match="without error details"):
        C.parse_tree(400, {"hasErrors": True, "results": [{"referenceId": "r"}]}, spec=_tree_spec())
    for bad in ({}, {"results": {}}, []):
        with pytest.raises(esf.UnexpectedResponse):
            C.parse_tree(201, bad, spec=_tree_spec())


def test_classify_returns_tree_errors_to_the_area() -> None:
    def resp(status: int, body: Any) -> RawResponse:
        return RawResponse(status, {"content-type": "application/json"}, json.dumps(body).encode())

    tree = {"hasErrors": True, "results": []}
    assert classify(resp(400, tree), _tree_spec()) is None
    err = classify(resp(400, [{"errorCode": "INVALID_FIELD", "message": "x"}]), _tree_spec())
    assert isinstance(err, esf.InvalidField)
    assert isinstance(classify(resp(500, tree), _tree_spec()), esf.ServerError)
    other = RequestSpec("POST", "composite", operation="composite.send")
    assert classify(resp(400, tree), other) is not None


def test_subrequest_error_without_details() -> None:
    sub = C.Subrequest("GET", "limits", "lim")
    err = C.subrequest_error(sub, 503, None, operation="composite.send", reference_id="lim")
    assert isinstance(err, esf.ServerError)
    assert err.message == "HTTP 503"
    assert err.errors[0].extended == {"referenceId": "lim"}


def test_parse_errors() -> None:
    c = C.CompositeRequest()
    c.create("Account", {}, ref_id="a")
    for bad in (
        {},
        {"compositeResponse": {}},
        {"compositeResponse": ["x"]},
        {"compositeResponse": [{"referenceId": "b", "httpStatusCode": 201}]},
        {"compositeResponse": [{"referenceId": "a", "httpStatusCode": "201"}]},
        {"compositeResponse": [{"referenceId": "a", "httpStatusCode": True}]},
    ):
        with pytest.raises(esf.UnexpectedResponse):
            C.parse_composite(bad, c)
    ok = C.parse_composite(
        {"compositeResponse": [{"referenceId": "a", "httpStatusCode": 201, "httpHeaders": None}]}, c
    )
    assert ok["a"].headers == {}
    with pytest.raises(KeyError):
        ok["missing"]


def test_composite_result_without_a_cause() -> None:
    """Only halted subrequests failed: the first one picks the class."""
    sub = C.Subrequest("POST", "sobjects/Account", "a")
    body = [{"errorCode": "PROCESSING_HALTED", "message": "halted"}]
    err = C.subrequest_error(sub, 400, body, operation="composite.send", reference_id="a")
    result = esf.CompositeResult([esf.SubrequestResult("a", 400, body, {}, err)])
    assert result[0].halted
    assert len(result) == 1
    with pytest.raises(esf.SalesforceError) as ei:
        result.raise_for_errors()
    assert ei.value.error_code == "PROCESSING_HALTED"
    assert esf.SubrequestResult("b", 200).errors == ()
