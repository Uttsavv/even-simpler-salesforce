"""Invocable actions: ``sf.actions`` and ``sf.run`` (§9.7), on both clients.

Responses in ``tests/fixtures/recorded/actions`` are recorded from a Developer Edition org (API
v68.0, 2026-11-12, V31-V33 in big-bang.md §21) against ``EsfSandboxAction`` (Apex, input
``name``; ``name='fail'`` throws) and ``Esf_Sandbox_Action_Flow`` (autolaunched Flow, same
input; ``name='fail'`` makes that interview fail), see ``tests/sandbox/org``. Observed:

- a call where every input failed is HTTP 400 with the usual result list; a Flow call where only
  some failed is HTTP 200. An Apex exception fails every input of the request (one transaction).
- results are in input order. ``sortOrder`` is the input index for Apex and ``-1`` for Flows.
- a failed Flow interview still has ``Flow__InterviewGuid`` and ``Flow__InterviewStatus`` =
  ``Error`` in ``outputValues``.

``flow_custom_error.json`` and inline bodies are **synthetic**. Sources:

- Invoking Actions (body ``{"inputs": [...]}``; one result per input with ``actionName``,
  ``errors``, ``isSuccess``, ``outputValues``):
  https://developer.salesforce.com/docs/atlas.en-us.api_action.meta/api_action/actions_intro_invoking.htm
- Flow Actions (``actions/custom/flow/{name}``, ``Flow__InterviewStatus`` output):
  https://developer.salesforce.com/docs/atlas.en-us.api_action.meta/api_action/actions_obj_flow.htm
- Apex Actions (``actions/custom/apex/{class}``, the class name, not the method):
  https://developer.salesforce.com/docs/atlas.en-us.api_action.meta/api_action/actions_obj_apex.htm

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
from ..fixtures import load_fixture, load_response

FLOW_NAME = "Esf_Sandbox_Action_Flow"
FLOW = f"{DATA}/actions/custom/flow/{FLOW_NAME}"
APEX = f"{DATA}/actions/custom/apex/EsfSandboxAction"
ASSIGN = f"{DATA}/actions/custom/flow/Assign_Lead"  # synthetic bodies only


def _respond(route: respx.Route, fixture: str) -> int:
    status, body = load_response(f"actions/{fixture}")
    route.respond(status, json=body)
    return status


def test_flow_single_returns_outputs(h: Harness, mock: respx.MockRouter, rec: Recorder) -> None:
    route = mock.post(FLOW)
    assert _respond(route, "flow_ok.json") == 200
    sf = h.client(recorder=rec)
    out = h.run(sf.actions.flow(FLOW_NAME, {"name": "esf"}))
    assert out["greeting"] == "hello esf"
    assert out["Flow__InterviewStatus"] == "Finished"
    assert json.loads(route.calls[0].request.content) == {"inputs": [{"name": "esf"}]}
    (event,) = rec.requests
    assert event["operation"] == "actions.flow"
    assert event["records"] == 1


def test_single_failure_raises_typed_error_with_tag(h: Harness, mock: respx.MockRouter) -> None:
    """Synthetic ``flow_custom_error.json`` (a tagged message), with the recorded status 400."""

    class RegionRequired(esf.ValidationRuleFailed): ...

    mock.post(ASSIGN).respond(400, json=load_fixture("actions/flow_custom_error.json"))
    sf = h.client(error_tags={"REGION_REQUIRED": RegionRequired})
    with pytest.raises(RegionRequired) as ei:
        h.run(sf.actions.flow("Assign_Lead", {"leadId": "00Qxx0000000001AAA"}))
    err = ei.value
    assert err.message == "Region is required"
    assert err.tag == "REGION_REQUIRED"
    assert err.status == 400
    assert err.operation == "actions.flow"
    assert err.error_code == "FIELD_CUSTOM_VALIDATION_EXCEPTION"


@pytest.mark.parametrize(
    ("kind", "fixture", "cls", "code"),
    [
        ("apex", "apex_failed.json", esf.ApexActionFailed, "APEX_FAILED"),
        ("apex", "apex_missing_input.json", esf.RequiredFieldMissing, "REQUIRED_FIELD_MISSING"),
        ("flow", "flow_failed.json", esf.RequiredFieldMissing, "REQUIRED_FIELD_MISSING"),
    ],
)
def test_recorded_single_failure(
    h: Harness, mock: respx.MockRouter, kind: str, fixture: str, cls: type[esf.SalesforceError], code: str
) -> None:
    """Recorded: one failed input is HTTP 400 with a one-entry result list (V31)."""
    name = "EsfSandboxAction" if kind == "apex" else FLOW_NAME
    route = mock.post(APEX if kind == "apex" else FLOW)
    assert _respond(route, fixture) == 400
    sf = h.client()
    call = sf.actions.apex if kind == "apex" else sf.actions.flow
    with pytest.raises(cls) as ei:
        h.run(call(name, {"name": "fail"}))
    assert type(ei.value) is cls
    assert ei.value.error_code == code
    assert ei.value.status == 400
    assert ei.value.operation == f"actions.{kind}"
    assert route.call_count == 1
    if cls is esf.ApexActionFailed:  # still a RecordError; the hint names the debug log
        assert isinstance(ei.value, esf.RecordError)
        assert ei.value.hint
        assert "debug log" in ei.value.hint


def test_single_failure_with_http_200(h: Harness, mock: respx.MockRouter) -> None:
    """Synthetic: the same result list with HTTP 200 raises too."""
    mock.post(ASSIGN).respond(200, json=load_fixture("actions/flow_custom_error.json"))
    sf = h.client()
    with pytest.raises(esf.ValidationRuleFailed):
        h.run(sf.actions.flow("Assign_Lead", {"leadId": "x"}))


def test_single_failure_without_details(h: Harness, mock: respx.MockRouter) -> None:
    mock.post(ASSIGN).respond(400, json=[{"actionName": "Assign_Lead", "isSuccess": False, "errors": []}])
    sf = h.client()
    with pytest.raises(esf.RecordError, match="without error details"):
        h.run(sf.actions.flow("Assign_Lead", {}))


def test_bulk_returns_batch_result(h: Harness, mock: respx.MockRouter) -> None:
    """Recorded ``flow_mixed.json``: HTTP 200; only the 'fail' interview failed (V31)."""
    route = mock.post(FLOW)
    assert _respond(route, "flow_mixed.json") == 200
    sf = h.client()
    res = h.run(sf.actions.flow(FLOW_NAME, [{"name": "a"}, {"name": "fail"}, {"name": "c"}]))
    assert isinstance(res, esf.BatchResult)
    assert [r.ok for r in res] == [True, False, True]
    assert [r.outputs.get("greeting") for r in res] == ["hello a", None, "hello c"]
    assert res[1].outputs["Flow__InterviewStatus"] == "Error"  # a failed Flow keeps its outputs
    assert res[0].action_name == FLOW_NAME
    assert res.failed[0].errors[0].error_code == "REQUIRED_FIELD_MISSING"
    assert res.operation == "actions.flow"
    assert len(json.loads(route.calls[0].request.content)["inputs"]) == 3
    with pytest.raises(esf.PartialFailure) as ei:
        res.raise_for_errors()
    assert ei.value.error_code == "REQUIRED_FIELD_MISSING"


def test_apex_exception_fails_every_input(h: Harness, mock: respx.MockRouter) -> None:
    """Recorded ``apex_mixed.json``: one input throws, so all three fail, with HTTP 400."""
    assert _respond(mock.post(APEX), "apex_mixed.json") == 400
    sf = h.client()
    res = h.run(sf.actions.apex("EsfSandboxAction", [{"name": "a"}, {"name": "fail"}, {"name": "c"}]))
    assert [r.ok for r in res] == [False, False, False]
    assert {e.error_code for r in res for e in r.errors} == {"APEX_FAILED"}
    assert all(r.outputs == {} for r in res)


@pytest.mark.parametrize("kind", ["apex", "flow"])
def test_results_keep_input_order(h: Harness, mock: respx.MockRouter, kind: str) -> None:
    """Recorded with 12 inputs ``n00``..``n11`` (V32): results come back in input order."""
    route = mock.post(APEX if kind == "apex" else FLOW)
    _respond(route, f"{kind}_order.json")
    sf = h.client()
    call = sf.actions.apex if kind == "apex" else sf.actions.flow
    name = "EsfSandboxAction" if kind == "apex" else FLOW_NAME
    res = h.run(call(name, [{"name": f"n{i:02d}"} for i in range(12)]))
    assert [r.outputs["greeting"] for r in res] == [f"hello n{i:02d}" for i in range(12)]
    # sortOrder is the input index for Apex and -1 for Flows; the client doesn't rely on it.
    _, body = load_response(f"actions/{kind}_order.json")
    expected = list(range(12)) if kind == "apex" else [-1] * 12
    assert [r["sortOrder"] for r in body] == expected


def test_bulk_empty_sends_nothing(h: Harness, mock: respx.MockRouter) -> None:
    sf = h.client()
    assert len(h.run(sf.actions.apex("OrderService", []))) == 0
    assert not mock.calls
    with pytest.raises(ValueError):
        h.run(sf.actions.apex("bad name", []))  # the name is checked also when nothing is sent


def test_more_than_2000_inputs_raise_before_sending(h: Harness, mock: respx.MockRouter) -> None:
    """V32: Salesforce runs the first 2,000 inputs and fails the rest, so nothing is sent."""
    sf = h.client()
    with pytest.raises(ValueError, match="2001 inputs for a flow action"):
        h.run(sf.actions.flow("Assign_Lead", [{"leadId": str(i)} for i in range(2001)]))
    with pytest.raises(ValueError, match="apex"):
        h.run(sf.actions.apex("OrderService", [{}] * 2001))
    assert not mock.calls


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
    route = mock.post(ASSIGN).mock(side_effect=httpx.ReadTimeout("slow"))
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
    mock.post(ASSIGN).respond(json=[])
    sf = h.client()
    with pytest.raises(esf.UnexpectedResponse):
        h.run(sf.actions.flow("Assign_Lead", {}))


def test_list_and_describe(h: Harness, mock: respx.MockRouter) -> None:
    """Recorded lists and describes (V33)."""
    _respond(mock.get(f"{DATA}/actions/custom/flow"), "list_flow.json")
    _respond(mock.get(f"{DATA}/actions/custom/apex"), "list_apex.json")
    _respond(mock.get(FLOW), "describe_flow.json")
    _respond(mock.get(APEX), "describe_apex.json")
    _respond(mock.get(f"{DATA}/actions/standard/chatterPost"), "describe_standard.json")
    std = mock.get(f"{DATA}/actions/standard").respond(json={"actions": []})
    sf = h.client()
    flows = h.run(sf.actions.list("flow"))
    assert {"label", "name", "type", "url"} <= set(flows[0])
    assert any(a["name"] == FLOW_NAME and a["type"] == "FLOW" for a in flows)
    assert h.run(sf.actions.list("apex")) == [
        {
            "label": "ESF Sandbox Echo",
            "name": "EsfSandboxAction",
            "type": "APEX",
            "url": "/services/data/v68.0/actions/custom/apex/EsfSandboxAction",
        }
    ]
    assert h.run(sf.actions.list("standard")) == []
    assert std.called
    d = h.run(sf.actions.describe("flow", FLOW_NAME))
    assert d["type"] == "FLOW"
    assert [(i["name"], i["type"], i["required"], i["maxOccurs"]) for i in d["inputs"]] == [
        ("name", "STRING", False, 1)  # Flow input variables are never "required"
    ]
    assert [(o["name"], o["type"]) for o in d["outputs"]] == [
        ("greeting", "STRING"),
        ("Flow__InterviewStatus", "PICKLIST"),
        ("Flow__InterviewGuid", "STRING"),
    ]
    a = h.run(sf.actions.describe("apex", "EsfSandboxAction"))
    assert [(i["name"], i["type"], i["required"]) for i in a["inputs"]] == [("name", "STRING", True)]
    assert "sObjectType" in a["inputs"][0]  # inputs spell it sObjectType, outputs sobjectType
    assert "sobjectType" in a["outputs"][0]
    c = h.run(sf.actions.describe("standard", "chatterPost"))
    assert c["type"] == "CHATTERPOST"
    assert {i["name"] for i in c["inputs"] if i["required"]} == {"text", "subjectNameOrId"}


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
class EchoResult:
    greeting: str | None = field(default=None, metadata={"api_name": "greeting"})
    status: str | None = field(default=None, metadata={"api_name": "Flow__InterviewStatus"})


@dataclass(frozen=True, kw_only=True)
class Echo(esf.Action[EchoResult]):
    __action__ = ("flow", FLOW_NAME)
    name: str = field(metadata={"api_name": "name"})
    extra: str | None = field(default=None, metadata={"api_name": "extra"})


def test_run_typed_action(h: Harness, mock: respx.MockRouter) -> None:
    route = mock.post(FLOW)
    _respond(route, "flow_ok.json")
    sf = h.client()
    result = h.run(sf.run(Echo(name="esf")))
    assert result == EchoResult(greeting="hello esf", status="Finished")
    # None fields are not sent
    assert json.loads(route.calls[0].request.content) == {"inputs": [{"name": "esf"}]}


def test_run_typed_action_failure(h: Harness, mock: respx.MockRouter) -> None:
    _respond(mock.post(FLOW), "flow_failed.json")
    sf = h.client()
    with pytest.raises(esf.RequiredFieldMissing):
        h.run(sf.run(Echo(name="fail")))


def test_run_pydantic_action(h: Harness, mock: respx.MockRouter) -> None:
    pydantic = pytest.importorskip("pydantic")
    from typing import ClassVar

    class Result(pydantic.BaseModel):
        status: str | None = pydantic.Field(default=None, alias="Flow__InterviewStatus")
        greeting: str | None = None

    class PydEcho(pydantic.BaseModel, esf.Action[Result]):
        __action__: ClassVar[tuple[str, str]] = ("flow", FLOW_NAME)
        model_config = pydantic.ConfigDict(populate_by_name=True)
        who: str = pydantic.Field(alias="name")
        extra: str | None = None

    route = mock.post(FLOW)
    _respond(route, "flow_ok.json")
    sf = h.client()
    out: Any = h.run(sf.run(PydEcho(who="esf")))
    assert (out.greeting, out.status) == ("hello esf", "Finished")
    assert json.loads(route.calls[0].request.content) == {"inputs": [{"name": "esf"}]}
