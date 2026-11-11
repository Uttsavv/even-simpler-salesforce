"""Invocable actions: ``sf.actions`` and ``sf.run`` (§9.7), on both clients.

Every response here is **synthetic** (``tests/fixtures/recorded/actions/*.json``), hand-written
from the Salesforce Actions Developer Guide because no org recording exists yet:

- Invoking Actions (body ``{"inputs": [...]}``; one result per input with ``actionName``,
  ``errors``, ``isSuccess``, ``outputValues``):
  https://developer.salesforce.com/docs/atlas.en-us.api_action.meta/api_action/actions_intro_invoking.htm
- Flow Actions (``actions/custom/flow/{name}``, ``Flow__InterviewStatus`` output):
  https://developer.salesforce.com/docs/atlas.en-us.api_action.meta/api_action/actions_obj_flow.htm
- Apex Actions (``actions/custom/apex/{class}``, the class name, not the method):
  https://developer.salesforce.com/docs/atlas.en-us.api_action.meta/api_action/actions_obj_apex.htm

The HTTP status of a failed invocation (400 with the same result list) and whether results
always keep input order are open in big-bang.md §21 (V31, V32).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest
import respx

import even_simpler_salesforce as esf

from ..conftest import DATA, Harness, Recorder
from ..fixtures import load_fixture

FLOW = f"{DATA}/actions/custom/flow/Assign_Lead"


def test_flow_single_returns_outputs(h: Harness, mock: respx.MockRouter, rec: Recorder) -> None:
    route = mock.post(FLOW).respond(json=load_fixture("actions/flow_ok.json"))
    sf = h.client(recorder=rec)
    out = h.run(sf.actions.flow("Assign_Lead", {"leadId": "00Qxx0000000001AAA"}))
    assert out["assignedTo"] == "005xx0000000001AAA"
    assert out["Flow__InterviewStatus"] == "Finished"
    assert json.loads(route.calls[0].request.content) == {"inputs": [{"leadId": "00Qxx0000000001AAA"}]}
    (event,) = rec.requests
    assert event["operation"] == "actions.flow"
    assert event["records"] == 1


def test_single_failure_raises_typed_error_with_tag(h: Harness, mock: respx.MockRouter) -> None:
    """Synthetic: a failed invocation answered with HTTP 400 and the result list (V31)."""

    class RegionRequired(esf.ValidationRuleFailed): ...

    mock.post(FLOW).respond(400, json=load_fixture("actions/flow_failed.json"))
    sf = h.client(error_tags={"REGION_REQUIRED": RegionRequired})
    with pytest.raises(RegionRequired) as ei:
        h.run(sf.actions.flow("Assign_Lead", {"leadId": "00Qxx0000000001AAA"}))
    err = ei.value
    assert err.message == "Region is required"
    assert err.tag == "REGION_REQUIRED"
    assert err.status == 400
    assert err.operation == "actions.flow"
    assert err.error_code == "FIELD_CUSTOM_VALIDATION_EXCEPTION"


def test_single_failure_with_http_200(h: Harness, mock: respx.MockRouter) -> None:
    mock.post(FLOW).respond(200, json=load_fixture("actions/flow_failed.json"))
    sf = h.client()
    with pytest.raises(esf.ValidationRuleFailed):
        h.run(sf.actions.flow("Assign_Lead", {"leadId": "x"}))


def test_single_failure_without_details(h: Harness, mock: respx.MockRouter) -> None:
    mock.post(FLOW).respond(400, json=[{"actionName": "Assign_Lead", "isSuccess": False, "errors": []}])
    sf = h.client()
    with pytest.raises(esf.RecordError, match="without error details"):
        h.run(sf.actions.flow("Assign_Lead", {}))


def test_bulk_returns_batch_result(h: Harness, mock: respx.MockRouter) -> None:
    route = mock.post(FLOW).respond(400, json=load_fixture("actions/bulk_mixed.json"))
    sf = h.client()
    res = h.run(sf.actions.flow("Assign_Lead", [{"leadId": "a"}, {}]))
    assert isinstance(res, esf.BatchResult)
    assert [r.ok for r in res] == [True, False]
    assert res[0].outputs == {"assignedTo": "005xx0000000001AAA"}
    assert res[0].action_name == "Assign_Lead"
    assert res.failed[0].errors[0].error_code == "REQUIRED_FIELD_MISSING"
    assert res.operation == "actions.flow"
    assert len(json.loads(route.calls[0].request.content)["inputs"]) == 2
    with pytest.raises(esf.PartialFailure) as ei:
        res.raise_for_errors()
    assert ei.value.fields == ("leadId",)


def test_bulk_empty_sends_nothing(h: Harness, mock: respx.MockRouter) -> None:
    sf = h.client()
    assert len(h.run(sf.actions.apex("OrderService", []))) == 0
    assert not mock.calls
    with pytest.raises(ValueError):
        h.run(sf.actions.apex("bad name", []))  # the name is checked also when nothing is sent


def test_inputs_must_be_dicts(h: Harness) -> None:
    sf = h.client()
    with pytest.raises(TypeError):
        h.run(sf.actions.flow("Assign_Lead", "leadId=1"))  # type: ignore[call-overload]


def test_standard_and_apex_paths(h: Harness, mock: respx.MockRouter) -> None:
    ok = [{"actionName": "x", "isSuccess": True, "outputValues": None, "errors": None}]
    std = mock.post(f"{DATA}/actions/standard/chatterPost").respond(json=ok)
    apex = mock.post(f"{DATA}/actions/custom/apex/ns__OrderService").respond(json=ok)
    sf = h.client()
    assert h.run(sf.actions.standard("chatterPost", {"text": "hi"})) == {}
    assert h.run(sf.actions.apex("ns__OrderService", {"orderId": "1"})) == {}
    assert std.called
    assert apex.called


def test_invalid_names_rejected_before_sending(h: Harness, mock: respx.MockRouter) -> None:
    sf = h.client()
    for bad in ("../limits", "a/b", "", "1x"):
        with pytest.raises(ValueError):
            h.run(sf.actions.flow(bad, {}))
    with pytest.raises(ValueError):
        h.run(sf.actions.list("bad/kind"))
    assert not mock.calls


def test_invocation_is_not_resent_after_timeout(h: Harness, mock: respx.MockRouter) -> None:
    """An action may have run when the request timed out, so it isn't retried (ADR 0001)."""
    route = mock.post(FLOW).mock(side_effect=httpx.ReadTimeout("slow"))
    sf = h.client()
    with pytest.raises(esf.RequestTimeout):
        h.run(sf.actions.flow("Assign_Lead", {}))
    assert route.call_count == 1


def test_other_errors_stay_rest_errors(h: Harness, mock: respx.MockRouter) -> None:
    """A 400 that is not a result list (e.g. an unknown flow) keeps the REST mapping."""
    mock.post(f"{DATA}/actions/custom/flow/Nope").respond(
        404, json=[{"errorCode": "NOT_FOUND", "message": "The requested resource does not exist"}]
    )
    mock.post(f"{DATA}/actions/custom/flow/Bad").respond(
        400, json=[{"errorCode": "JSON_PARSER_ERROR", "message": "x"}]
    )
    sf = h.client()
    with pytest.raises(esf.NotFound):
        h.run(sf.actions.flow("Nope", {}))
    with pytest.raises(esf.RequestError):
        h.run(sf.actions.flow("Bad", {}))


def test_malformed_result_list(h: Harness, mock: respx.MockRouter) -> None:
    mock.post(FLOW).respond(json=[])
    sf = h.client()
    with pytest.raises(esf.UnexpectedResponse):
        h.run(sf.actions.flow("Assign_Lead", {}))


def test_list_and_describe(h: Harness, mock: respx.MockRouter) -> None:
    mock.get(f"{DATA}/actions/custom/flow").respond(json=load_fixture("actions/list_flow.json"))
    mock.get(FLOW).respond(json=load_fixture("actions/describe_flow.json"))
    std = mock.get(f"{DATA}/actions/standard").respond(json={"actions": []})
    sf = h.client()
    assert [a["name"] for a in h.run(sf.actions.list("flow"))] == ["Assign_Lead"]
    assert h.run(sf.actions.list("standard")) == []
    assert std.called
    d = h.run(sf.actions.describe("flow", "Assign_Lead"))
    assert [i["name"] for i in d["inputs"]] == ["leadId", "region"]


def test_list_and_describe_unexpected_shapes(h: Harness, mock: respx.MockRouter) -> None:
    mock.get(f"{DATA}/actions/custom/apex").respond(json=[])
    mock.get(f"{DATA}/actions/custom/apex/X").respond(json=[])
    sf = h.client()
    with pytest.raises(esf.UnexpectedResponse):
        h.run(sf.actions.list("apex"))
    with pytest.raises(esf.UnexpectedResponse):
        h.run(sf.actions.describe("apex", "X"))


# --- typed stubs ---------------------------------------------------------------------------


@dataclass(frozen=True, kw_only=True)
class AssignLeadResult:
    assigned_to: str | None = field(default=None, metadata={"api_name": "assignedTo"})
    status: str | None = field(default=None, metadata={"api_name": "Flow__InterviewStatus"})


@dataclass(frozen=True, kw_only=True)
class AssignLead(esf.Action[AssignLeadResult]):
    __action__ = ("flow", "Assign_Lead")
    lead_id: str = field(metadata={"api_name": "leadId"})
    region: str | None = field(default=None, metadata={"api_name": "region"})


def test_run_typed_action(h: Harness, mock: respx.MockRouter) -> None:
    route = mock.post(FLOW).respond(json=load_fixture("actions/flow_ok.json"))
    sf = h.client()
    result = h.run(sf.run(AssignLead(lead_id="00Qxx0000000001AAA")))
    assert result == AssignLeadResult(assigned_to="005xx0000000001AAA", status="Finished")
    # None fields are not sent
    assert json.loads(route.calls[0].request.content) == {"inputs": [{"leadId": "00Qxx0000000001AAA"}]}


def test_run_typed_action_failure(h: Harness, mock: respx.MockRouter) -> None:
    mock.post(FLOW).respond(400, json=load_fixture("actions/flow_failed.json"))
    sf = h.client()
    with pytest.raises(esf.ValidationRuleFailed):
        h.run(sf.run(AssignLead(lead_id="x", region="North")))


def test_run_pydantic_action(h: Harness, mock: respx.MockRouter) -> None:
    pydantic = pytest.importorskip("pydantic")
    from typing import ClassVar

    class Result(pydantic.BaseModel):
        assigned_to: str | None = pydantic.Field(default=None, alias="assignedTo")

    class Assign(pydantic.BaseModel, esf.Action[Result]):
        __action__: ClassVar[tuple[str, str]] = ("flow", "Assign_Lead")
        model_config = pydantic.ConfigDict(populate_by_name=True)
        lead_id: str = pydantic.Field(alias="leadId")
        region: str | None = None

    route = mock.post(FLOW).respond(json=load_fixture("actions/flow_ok.json"))
    sf = h.client()
    out: Any = h.run(sf.run(Assign(lead_id="00Qxx0000000001AAA")))
    assert out.assigned_to == "005xx0000000001AAA"
    assert json.loads(route.calls[0].request.content) == {"inputs": [{"leadId": "00Qxx0000000001AAA"}]}
