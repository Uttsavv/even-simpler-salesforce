"""``core.actions`` and the ``actions.*`` / ``apex.*`` rules in ``core.classify`` (no I/O).

Bodies are **synthetic**, shaped after the Salesforce Actions Developer Guide (Invoking
Actions) and the Apex Developer Guide (Apex REST); see tests/contract/test_actions.py for the
URLs.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, ClassVar

import pytest

import even_simpler_salesforce as esf
from even_simpler_salesforce.core import actions as A
from even_simpler_salesforce.core.classify import classify
from even_simpler_salesforce.core.http import RawResponse, RequestSpec


def _resp(
    status: int, body: Any = None, *, text: str | None = None, ctype: str = "application/json"
) -> RawResponse:
    content = text.encode() if text is not None else (b"" if body is None else json.dumps(body).encode())
    return RawResponse(status, {"content-type": ctype}, content)


def test_paths() -> None:
    assert A.action_list_path("standard") == "actions/standard"
    assert A.action_list_path("emailAlert") == "actions/custom/emailAlert"
    assert A.action_path("flow", "ns__Assign_Lead") == "actions/custom/flow/ns__Assign_Lead"
    assert A.action_path("emailAlert", "Lead.Welcome") == "actions/custom/emailAlert/Lead.Welcome"
    for bad in ("", "a/b", "../x", "1a", "a.b.c", None):
        with pytest.raises(ValueError):
            A.check_action_name(bad)  # type: ignore[arg-type]
    for bad in ("", "a/b", "a_b", 3):
        with pytest.raises(ValueError):
            A.check_action_kind(bad)  # type: ignore[arg-type]


def test_parse_results() -> None:
    body = [
        {"actionName": "f", "isSuccess": True, "outputValues": {"x": 1}, "errors": None},
        {"isSuccess": False, "outputValues": None, "errors": [{"statusCode": "X", "message": "m"}]},
    ]
    ok, bad = A.parse_action_results(body, 2)
    assert ok == esf.ActionResult(ok=True, outputs={"x": 1}, action_name="f")
    assert not bad.ok
    assert bad.outputs == {}
    assert bad.action_name is None
    assert bad.errors[0].error_code == "X"
    with pytest.raises(esf.UnexpectedResponse):
        A.parse_action_results(body, 3)
    with pytest.raises(esf.UnexpectedResponse):
        A.parse_action_results({"x": 1}, 1)
    with pytest.raises(esf.UnexpectedResponse):
        A.parse_action_results([{"outputValues": {}}], 1)
    # isSuccess must be exactly true
    assert A.parse_action_results([{"isSuccess": "true"}], 1)[0].ok is False


def test_action_error() -> None:
    spec = RequestSpec("POST", "actions/custom/flow/F", operation="actions.flow")
    res = esf.ActionResult(ok=False, errors=(esf.ErrorDetail("UNKNOWN_THING", "boom"),))
    err = A.action_error(res, status=400, spec=spec)
    assert type(err) is esf.RecordError
    assert (err.status, err.operation, err.error_code) == (400, "actions.flow", "UNKNOWN_THING")
    empty = A.action_error(esf.ActionResult(ok=False), status=200, spec=spec)
    assert isinstance(empty, esf.RecordError)
    assert "without error details" in empty.message


@dataclass(frozen=True, kw_only=True)
class Out:
    total: int | None = field(default=None, metadata={"api_name": "Total__c"})
    note: str | None = None


@dataclass(frozen=True, kw_only=True)
class Act(esf.Action[Out]):
    __action__ = ("apex", "Calc")
    amount: int = field(metadata={"api_name": "Amount"})
    note: str | None = None


class Sub(Act):
    pass


class Plain(esf.Action):  # type: ignore[type-arg]  # no type argument
    __action__ = ("flow", "F")


def test_inputs_and_results() -> None:
    assert A.action_inputs(Act(amount=3)) == ("apex", "Calc", {"Amount": 3})
    assert A.action_inputs(Act(amount=3, note="n"))[2] == {"Amount": 3, "note": "n"}
    assert A.action_result_type(Act) is Out
    assert A.action_result_type(Sub) is Out
    assert A.action_result_type(Plain) is None
    assert A.action_result_type(int) is None
    assert A.build_action_result(Out, {"Total__c": 5, "extra": 1}) == Out(total=5)
    assert A.build_action_result(None, {"a": 1}) == {"a": 1}
    assert A.build_action_result(dict, {"a": 1}) == {"a": 1}


def test_inputs_errors() -> None:
    with pytest.raises(TypeError, match="must be a dataclass"):
        A.action_inputs(Plain())

    class NoSpec:
        pass

    with pytest.raises(TypeError, match="__action__"):
        A.action_inputs(NoSpec())

    @dataclass
    class BadKind(esf.Action[Out]):
        __action__: ClassVar[tuple[str, str]] = ("quickAction", "X")

    with pytest.raises(ValueError, match="kind"):
        A.action_inputs(BadKind())


def test_pydantic_inputs_and_results() -> None:
    pydantic = pytest.importorskip("pydantic")

    class R(pydantic.BaseModel):
        total: int = pydantic.Field(alias="Total__c")

    class P(pydantic.BaseModel, esf.Action[R]):
        __action__: ClassVar[tuple[str, str]] = ("flow", "F")
        amount: int = pydantic.Field(alias="Amount")
        note: str | None = None

    assert A.action_inputs(P(Amount=2)) == ("flow", "F", {"Amount": 2})
    assert A.action_result_type(P) is R
    assert A.build_action_result(R, {"Total__c": 4}).total == 4


# --- classify --------------------------------------------------------------------------


ACT = RequestSpec("POST", "actions/custom/flow/F", operation="actions.flow")
APEX = RequestSpec("POST", "/services/apexrest/x", operation="apex.post")


def test_classify_action_result_list_on_400_is_success() -> None:
    results = [{"isSuccess": False, "errors": [{"statusCode": "X", "message": "m"}]}]
    assert classify(_resp(400, results), ACT) is None
    assert classify(_resp(200, results), ACT) is None
    # other bodies keep the REST mapping
    assert isinstance(classify(_resp(400, [{"errorCode": "X", "message": "m"}]), ACT), esf.RequestError)
    assert isinstance(classify(_resp(400, []), ACT), esf.RequestError)
    assert isinstance(classify(_resp(400, [{"isSuccess": False}, 1]), ACT), esf.RequestError)
    assert isinstance(classify(_resp(500, results), ACT), esf.ServerError)
    assert isinstance(classify(_resp(400, text="<html>", ctype="text/html"), ACT), esf.UnexpectedResponse)


def test_classify_apex() -> None:
    err = classify(_resp(422, {"error": "CLOSED"}), APEX)
    assert isinstance(err, esf.ApexError)
    assert err.body == {"error": "CLOSED"}
    assert err.status == 422
    err = classify(_resp(400, [{"errorCode": "INVALID_FIELD", "message": "bad", "fields": ["X"]}]), APEX)
    assert isinstance(err, esf.ApexError)
    assert err.error_code == "INVALID_FIELD"
    assert err.fields == ("X",)
    err = classify(_resp(500, [{"errorCode": "APEX_ERROR", "message": "NPE"}]), APEX)
    assert isinstance(err, esf.ApexError)
    assert err.body == [{"errorCode": "APEX_ERROR", "message": "NPE"}]
    err = classify(_resp(500, {"message": "custom 500"}), APEX)
    assert isinstance(err, esf.ApexError)
    # kept as they are
    assert type(classify(_resp(503), APEX)) is esf.ServerError
    unavailable = classify(_resp(503, [{"errorCode": "SERVER_UNAVAILABLE", "message": "x"}]), APEX)
    assert type(unavailable) is esf.ServerError
    assert unavailable.retryable
    assert isinstance(
        classify(_resp(401, [{"errorCode": "INVALID_SESSION_ID", "message": "x"}]), APEX), esf.SessionExpired
    )
    assert isinstance(
        classify(_resp(403, [{"errorCode": "REQUEST_LIMIT_EXCEEDED", "message": "x"}]), APEX),
        esf.RequestLimitExceeded,
    )
    assert isinstance(classify(_resp(502, text="<html>", ctype="text/html"), APEX), esf.UnexpectedResponse)
    assert classify(_resp(200, {"ok": True}), APEX) is None


def test_check_action_inputs_caps_apex_and_flow_only() -> None:
    A.check_action_inputs("apex", A.ACTION_INPUT_LIMIT)
    A.check_action_inputs("flow", 0)
    A.check_action_inputs("standard", A.ACTION_INPUT_LIMIT + 1)  # not verified: not checked
    for kind in ("apex", "flow"):
        with pytest.raises(ValueError, match="at most 2000"):
            A.check_action_inputs(kind, A.ACTION_INPUT_LIMIT + 1)
