"""Invocable actions, Apex REST and platform event publishing against a real org (V31-V34, V36).

Needs the org fixtures from ``scripts/sandbox_setup_org.py`` (``EsfSandboxAction``,
``EsfSandboxRest``, ``ESF_Sandbox_Event__e``) plus ``tests/sandbox/org/actions`` (the autolaunched
Flow ``Esf_Sandbox_Action_Flow``: input ``name``, output ``greeting``; ``name='fail'`` makes the
interview fail). Deploy that folder with ``sf.metadata.deploy`` (see the setup script).

Nothing here creates records: the failing Flow interview tries to insert a Lead without its
required fields and is rolled back; events are only queued.

Sources:

- Actions Developer Guide, *Invoking Actions* (body ``{"inputs": [...]}``, result list):
  https://developer.salesforce.com/docs/atlas.en-us.api_action.meta/api_action/actions_intro_invoking.htm
  (developer.salesforce.com blocks non-browser clients; read in the PDF
  https://resources.docs.salesforce.com/256/latest/en-us/sfdc/pdf/api_action.pdf). The guide
  documents neither the HTTP status of a failed call, ``sortOrder``, nor an inputs-per-request
  limit; those assertions are org-observed (API v68.0, 2026-11-12).
- Apex Developer Guide, *Exposing Apex Classes as REST Web Services* (``APEX_ERROR``):
  https://developer.salesforce.com/docs/atlas.en-us.apexcode.meta/apexcode/apex_rest_intro.htm
- Platform Events Developer Guide, *Publish Event Messages with Salesforce APIs* and *EventUuid*:
  https://developer.salesforce.com/docs/atlas.en-us.platform_events.meta/platform_events/platform_events_publish_api.htm
"""

from __future__ import annotations

import re
from typing import Any

import pytest

import even_simpler_salesforce as esf

from .conftest import Driver

pytestmark = [pytest.mark.sandbox, pytest.mark.usefixtures("writes")]

APEX_ACTION = "EsfSandboxAction"
FLOW = "Esf_Sandbox_Action_Flow"
EVENT = "ESF_Sandbox_Event__e"
UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def _needs_flow(sb: Driver, sf: Any) -> None:
    names = {a["name"] for a in sb.run(sf.actions.list("flow"))}
    if FLOW not in names:
        pytest.skip(f"sandbox: deploy tests/sandbox/org/actions ({FLOW}) first")


# --- V31: failures -------------------------------------------------------------------------


@pytest.mark.parametrize("kind", ["apex", "flow"])
def test_single_failure_raises_with_status_400(sb: Driver, kind: str) -> None:
    sf = sb.client()
    if kind == "flow":
        _needs_flow(sb, sf)
    call = sf.actions.apex if kind == "apex" else sf.actions.flow
    with pytest.raises(esf.RecordError) as ei:
        sb.run(call(APEX_ACTION if kind == "apex" else FLOW, {"name": "fail"}))
    err = ei.value
    assert err.status == 400
    assert err.error_code == ("APEX_FAILED" if kind == "apex" else "REQUIRED_FIELD_MISSING")
    assert isinstance(err, esf.ApexActionFailed if kind == "apex" else esf.RequiredFieldMissing)
    assert err.errors[0].fields == ()
    assert len(sb.recorder.requests(f"actions.{kind}")) == 1  # never resent


def test_apex_missing_required_input(sb: Driver) -> None:
    sf = sb.client()
    with pytest.raises(esf.RequiredFieldMissing) as ei:
        sb.run(sf.actions.apex(APEX_ACTION, {}))
    assert ei.value.status == 400
    assert "name" in ei.value.message


def test_flow_mixed_batch_fails_only_one_input(sb: Driver) -> None:
    sf = sb.client()
    _needs_flow(sb, sf)
    res = sb.run(sf.actions.flow(FLOW, [{"name": "a"}, {"name": "fail"}, {"name": "c"}]))
    assert [r.ok for r in res] == [True, False, True]
    assert [r.outputs.get("greeting") for r in res] == ["hello a", None, "hello c"]
    assert res[1].outputs["Flow__InterviewStatus"] == "Error"
    assert res[1].errors[0].error_code == "REQUIRED_FIELD_MISSING"
    with pytest.raises(esf.PartialFailure):
        res.raise_for_errors()


def test_apex_exception_fails_the_whole_batch(sb: Driver) -> None:
    """An exception aborts the one Apex transaction, so every input of the request fails."""
    sf = sb.client()
    res = sb.run(sf.actions.apex(APEX_ACTION, [{"name": "a"}, {"name": "fail"}, {"name": "c"}]))
    assert [r.ok for r in res] == [False, False, False]
    assert {e.error_code for r in res for e in r.errors} == {"APEX_FAILED"}


# --- V32: order and the per-request limit ---------------------------------------------------


@pytest.mark.parametrize("kind", ["apex", "flow"])
def test_results_keep_input_order(sb: Driver, kind: str) -> None:
    sf = sb.client()
    if kind == "flow":
        _needs_flow(sb, sf)
    call = sf.actions.apex if kind == "apex" else sf.actions.flow
    names = [f"n{i:03d}" for i in range(50)][::-1]
    res = sb.run(call(APEX_ACTION if kind == "apex" else FLOW, [{"name": n} for n in names]))
    assert [r.outputs["greeting"] for r in res] == [f"hello {n}" for n in names]


@pytest.mark.parametrize("kind", ["apex", "flow"])
def test_more_than_2000_inputs_runs_the_first_2000(sb: Driver, kind: str, heavy_writes: None) -> None:
    """Org-observed: 2,001 inputs → HTTP 400; the first 2,000 run, the last fails.

    So the client refuses such a list before sending it; the raw request shows the org still
    behaves this way.
    """
    sf = sb.client()
    if kind == "flow":
        _needs_flow(sb, sf)
    name = APEX_ACTION if kind == "apex" else FLOW
    call = sf.actions.apex if kind == "apex" else sf.actions.flow
    inputs = [{"name": f"n{i}"} for i in range(2001)]
    with pytest.raises(ValueError, match="2001 inputs"):
        sb.run(call(name, inputs))
    with pytest.raises(esf.SalesforceError) as ei:
        sb.run(sf.request("POST", f"actions/custom/{kind}/{name}", json={"inputs": inputs}))
    assert ei.value.status == 400
    # A list of results: the first inputs ran. The error keeps only the first 4 KiB of the body,
    # so the failed last entry ("Cannot specify more than 2000 requests") is not in it; that
    # message was seen in the 2026-11-12 run (big-bang.md §21 V32).
    raw = ei.value.raw.decode() if isinstance(ei.value.raw, bytes) else str(ei.value.raw)
    assert raw.startswith('[{"actionName":')
    assert '"isSuccess":true' in raw


# --- V33: list and describe -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "name"), [("apex", APEX_ACTION), ("flow", FLOW), ("standard", "chatterPost")]
)
def test_list_and_describe_shape(sb: Driver, kind: str, name: str) -> None:
    sf = sb.client()
    listed = {a["name"]: a for a in sb.run(sf.actions.list(kind))}
    if name not in listed:
        pytest.skip(f"sandbox: {kind}:{name} is not in this org")
    assert set(listed[name]) == {"label", "name", "type", "url"}
    d = sb.run(sf.actions.describe(kind, name))
    assert d["name"] == name
    assert d["type"] == listed[name]["type"]
    for p in [*d["inputs"], *d["outputs"]]:
        assert {"name", "label", "type", "maxOccurs", "description", "picklistValues"} <= set(p)
        assert p["type"] == p["type"].upper()
    assert all({"required", "sObjectType"} <= set(p) for p in d["inputs"])
    assert all("sobjectType" in p for p in d["outputs"])


# --- V34: Apex REST -------------------------------------------------------------------------


def test_apex_rest_get_post(sb: Driver) -> None:
    sf = sb.client()
    got = sb.run(sf.apex.get("/esf-sandbox/x", params={"a": "1"}))
    assert got == {"ok": True, "method": "GET", "name": None, "params": {"a": "1"}}
    posted = sb.run(sf.apex.post("/esf-sandbox/x", {"name": "esf"}))
    assert posted["name"] == "esf"


def test_apex_rest_unhandled_exception_is_500_apex_error(sb: Driver) -> None:
    sf = sb.client()
    with pytest.raises(esf.ApexError) as ei:
        sb.run(sf.apex.get("/esf-sandbox/x", params={"mode": "throw"}))
    assert ei.value.status == 500
    assert ei.value.error_code == "APEX_ERROR"
    assert ei.value.message.startswith("System.MathException")
    assert len(sb.recorder.requests("apex.get")) == 1  # not resent


def test_apex_rest_unmapped_url_is_404_not_found(sb: Driver) -> None:
    sf = sb.client()
    with pytest.raises(esf.ApexError) as ei:
        sb.run(sf.apex.get("/esf-sandbox-nope/x"))
    assert ei.value.status == 404
    assert ei.value.error_code == "NOT_FOUND"


# --- V36: platform events -------------------------------------------------------------------


def test_publish_returns_uuid(sb: Driver, tag: str) -> None:
    sf = sb.client()
    res = sb.run(sf.platform_events.publish(EVENT, {"Message__c": tag}))
    assert res.ok
    assert res.uuid is not None
    assert UUID.match(res.uuid)
    assert res.id is not None
    assert res.id.startswith("e00")
    assert res.errors == ()


def test_publish_many_and_bad_field(sb: Driver, tag: str) -> None:
    sf = sb.client()
    batch = sb.run(sf.platform_events.publish_many(EVENT, [{"Message__c": tag}, {"Bogus__c": "x"}]))
    assert [r.ok for r in batch] == [True, False]
    assert batch[0].uuid is not None
    assert batch[1].errors[0].error_code == "INVALID_FIELD"
    with pytest.raises(esf.InvalidField) as ei:
        sb.run(sf.platform_events.publish(EVENT, {"Bogus__c": "x"}))
    assert ei.value.status == 400
    with pytest.raises(esf.StringTooLong):
        sb.run(sf.platform_events.publish(EVENT, {"Message__c": "x" * 300}))
