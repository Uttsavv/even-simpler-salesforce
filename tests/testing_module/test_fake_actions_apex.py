"""``FakeSalesforce`` support for ``actions``, ``apex`` and ``run`` (§14)."""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from typing import Any

import pytest

import even_simpler_salesforce as esf
from even_simpler_salesforce.testing import AsyncFakeSalesforce, FakeSalesforce, UnscriptedCall


@dataclass(frozen=True, kw_only=True)
class Out:
    assigned_to: str | None = field(default=None, metadata={"api_name": "assignedTo"})


@dataclass(frozen=True, kw_only=True)
class Assign(esf.Action[Out]):
    __action__ = ("flow", "Assign_Lead")
    lead_id: str = field(metadata={"api_name": "leadId"})


def test_single_bulk_and_errors() -> None:
    fake = FakeSalesforce()

    def handler(inputs: dict[str, Any]) -> dict[str, Any]:
        if not inputs.get("leadId"):
            raise esf.RequiredFieldMissing("leadId is required", error_code="REQUIRED_FIELD_MISSING")
        return {"assignedTo": "005" + inputs["leadId"]}

    fake.on_action("flow", "Assign_Lead", handler)
    assert fake.actions.flow("Assign_Lead", {"leadId": "1"}) == {"assignedTo": "0051"}
    res = fake.actions.flow("Assign_Lead", [{"leadId": "2"}, {}])
    assert [r.ok for r in res] == [True, False]
    assert res.failed[0].errors[0].error_code == "REQUIRED_FIELD_MISSING"
    with pytest.raises(esf.RequiredFieldMissing) as ei:
        fake.actions.flow("Assign_Lead", {})
    assert ei.value.operation == "actions.flow"
    assert fake.run(Assign(lead_id="3")) == Out(assigned_to="0053")
    assert [c.operation for c in fake.calls] == ["actions.flow"] * 4
    assert fake.calls[0].args == {"name": "Assign_Lead", "inputs": {"leadId": "1"}}
    assert len(fake.actions.flow("Assign_Lead", [])) == 0
    assert len(fake.calls) == 4  # like the real client, an empty batch makes no call


def test_static_outputs_error_instance_and_times() -> None:
    fake = FakeSalesforce()
    fake.on_action("apex", "Calc", {"total": 1})
    fake.on_action("apex", "Calc", esf.ApexError("boom", error_code="APEX_ERROR"), times=1)
    with pytest.raises(esf.ApexError):
        fake.actions.apex("Calc", {})
    assert fake.actions.apex("Calc", {}) == {"total": 1}
    fake.on_action("standard", "chatterPost")
    assert fake.actions.standard("chatterPost", {"text": "x"}) == {}
    with pytest.raises(UnscriptedCall):
        fake.actions.flow("Unknown", {})
    with pytest.raises(TypeError):
        fake.actions.flow("Unknown", "x")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        fake.on_action("flow", "bad/name")
    fake.fail_next("actions.apex", esf.RowLocked("locked"))
    with pytest.raises(esf.RowLocked):
        fake.actions.apex("Calc", {})


def test_list_and_describe() -> None:
    fake = FakeSalesforce()
    fake.on_action("flow", "A", {})
    fake.set_action_describe("flow", "B", {"label": "Bee", "inputs": [{"name": "x"}], "outputs": []})
    assert [a["name"] for a in fake.actions.list("flow")] == ["B", "A"]
    assert fake.actions.list("flow")[0]["label"] == "Bee"
    assert fake.actions.list("apex") == []
    assert fake.actions.describe("flow", "B")["inputs"] == [{"name": "x"}]
    assert fake.actions.describe("flow", "A")["inputs"] == []
    with pytest.raises(esf.NotFound):
        fake.actions.describe("flow", "C")


def test_apex() -> None:
    fake = FakeSalesforce()
    fake.on_apex("POST", "/orders/submit", lambda req: {"got": req.json, "path": req.path})
    fake.on_apex("GET", re.compile(r"/orders/\d+"), {"state": "open"})
    fake.on_apex("DELETE", "/orders/1", esf.ApexError("closed", status=422, body={"error": "CLOSED"}))
    assert fake.apex.post("/orders/submit", {"id": 1}) == {
        "got": {"id": 1},
        "path": "/services/apexrest/orders/submit",
    }
    assert fake.apex.get("/orders/42") == {"state": "open"}
    with pytest.raises(esf.ApexError) as ei:
        fake.apex.delete("/orders/1")
    assert ei.value.body == {"error": "CLOSED"}
    fake.on_apex("*", "/x", None)
    assert fake.apex.put("/x", {}) is None
    assert fake.apex.patch("/x", {}) is None
    with pytest.raises(UnscriptedCall):
        fake.apex.get("/nothing")
    with pytest.raises(ValueError):
        fake.apex.get("no-slash")
    assert [c.operation for c in fake.calls][:3] == ["apex.post", "apex.get", "apex.delete"]


def test_async_fake() -> None:
    async def main() -> None:
        async with AsyncFakeSalesforce() as fake:
            fake.on_action("flow", "Assign_Lead", {"assignedTo": "005"})
            fake.set_action_describe("flow", "Assign_Lead", {"inputs": []})
            fake.on_action("standard", "s", {})
            fake.on_action("apex", "c", {})
            assert await fake.actions.flow("Assign_Lead", {"leadId": "1"}) == {"assignedTo": "005"}
            assert await fake.actions.standard("s", {}) == {}
            assert await fake.actions.apex("c", {}) == {}
            assert len(await fake.actions.list("flow")) == 1
            assert await fake.actions.describe("flow", "Assign_Lead") == {"inputs": []}
            assert await fake.run(Assign(lead_id="1")) == Out(assigned_to="005")
            fake.on_apex("*", "/a", {"ok": 1})
            assert await fake.apex.get("/a") == {"ok": 1}
            assert await fake.apex.post("/a", {}) == {"ok": 1}
            assert await fake.apex.put("/a", {}) == {"ok": 1}
            assert await fake.apex.patch("/a", {}) == {"ok": 1}
            assert await fake.apex.delete("/a") == {"ok": 1}
            assert fake.tagged("job").actions is not None

    asyncio.run(main())


def test_views_share_handlers() -> None:
    fake = FakeSalesforce()
    fake.on_action("flow", "F", {"a": 1})
    assert fake.tagged("job").actions.flow("F", {}) == {"a": 1}
    assert fake.calls[-1].caller == "job"
