"""``fake.composite``: Composite, Composite Graph, Batch and sObject Tree on the in-memory fake."""

from __future__ import annotations

import asyncio
import re

import pytest

import even_simpler_salesforce as esf
from even_simpler_salesforce.testing import AsyncFakeSalesforce, FakeSalesforce


def test_composite_resolves_references() -> None:
    fake = FakeSalesforce()
    # The fake has no SOQL engine: script the query (the reference is already resolved).
    fake.on_query(re.compile(r"FROM Contact WHERE AccountId = '001"), lambda q: fake.records("Contact"))
    c = fake.composite.builder()
    acc = c.create("Account", {"Name": "Acme"}, ref_id="acc")
    con = c.create("Contact", {"LastName": "Doe", "AccountId": acc.ref("id")})
    c.update("Account", acc.ref("id"), {"Name": "Acme Ltd"})
    c.get("Account", acc.ref("id"), ["Name"], ref_id="read")
    q = c.query("SELECT Id, LastName FROM Contact WHERE AccountId = {}", acc.ref("id"))
    c.upsert("Account", "Ext__c", "e-1", {"Name": "Other"}, ref_id="up")
    c.delete("Contact", con.ref("id"))
    result = c.send()
    assert [r.status for r in result] == [201, 201, 204, 200, 200, 201, 204]
    acc_id = result["acc"].body["id"]
    assert result["acc"].headers["Location"].endswith(f"/Account/{acc_id}")
    assert result["read"].body["Name"] == "Acme Ltd"
    assert result[q.reference_id].body["totalSize"] == 1
    assert result["up"].body["created"] is True
    assert fake.records("Contact") == []
    (call,) = fake.calls
    assert call.operation == "composite.send"
    assert call.args["all_or_none"] is False
    assert len(call.args["subrequests"]) == 7


def test_composite_all_or_none_rolls_back() -> None:
    fake = FakeSalesforce()
    fake.reject("Contact", esf.ValidationRuleFailed("Street is required"))
    c = fake.composite.builder(all_or_none=True)
    acc = c.create("Account", {"Name": "Acme"}, ref_id="acc")
    c.create("Contact", {"LastName": "Doe", "AccountId": acc.ref("id")}, ref_id="con")
    c.create("Account", {"Name": "Later"}, ref_id="later")
    result = c.send()
    assert fake.records("Account") == []
    assert [r.halted for r in result] == [True, False, True]
    assert result["con"].status == 400
    with pytest.raises(esf.ValidationRuleFailed) as ei:
        result.raise_for_errors()
    assert ei.value.operation == "composite.send"
    assert [e.extended["referenceId"] for e in ei.value.errors] == ["acc", "con", "later"]


def test_composite_without_all_or_none_keeps_successes() -> None:
    fake = FakeSalesforce()
    c = fake.composite.builder()
    c.get("Account", "001xx0000000009AAA", ref_id="missing")
    c.create("Account", {"Name": "Acme"}, ref_id="acc")
    result = c.send()
    assert result["missing"].status == 404
    assert isinstance(result["missing"].error, esf.NotFound)
    assert result["acc"].ok
    assert len(fake.records("Account")) == 1


def test_reference_to_a_failed_result_halts() -> None:
    fake = FakeSalesforce()
    c = fake.composite.builder()
    bad = c.get("Account", "001xx0000000009AAA", ref_id="bad")
    c.update("Account", bad.ref("Id"), {"Name": "x"}, ref_id="dep")
    good = c.create("Account", {"Name": "Acme"}, ref_id="good")
    c.update("Account", good.ref("missing_field"), {"Name": "y"}, ref_id="nofield")
    c.update("Account", good.ref("id.x"), {"Name": "z"}, ref_id="notmap")
    c.update("Account", good.ref("errors[0]"), {"Name": "z"}, ref_id="noitem")
    c.create("Note", {"Title": f"for {good.ref('id')}"}, ref_id="note")
    result = c.send()
    assert result["dep"].halted
    assert "No value for @{bad.Id}" in result["dep"].error.message  # type: ignore[union-attr]
    assert result["nofield"].halted
    assert result["notmap"].halted
    assert result["noitem"].halted
    assert result["note"].ok


def test_raw_request_subrequest() -> None:
    fake = FakeSalesforce()
    acc = fake.add("Account", {"Name": "Acme"})
    fake.on_request("GET", "limits", {"DailyApiRequests": {"Max": 1, "Remaining": 1}})
    fake.on_request("DELETE", f"sobjects/Account/{acc}")
    c = fake.composite.builder()
    c.request("GET", "limits", ref_id="lim")
    c.request("DELETE", f"sobjects/Account/{acc}", ref_id="gone")
    result = c.send()
    assert result["lim"].body["DailyApiRequests"]["Max"] == 1
    assert result["gone"].status == 204


def test_graphs_roll_back_independently() -> None:
    fake = FakeSalesforce()
    fake.reject("Contact", esf.RequiredFieldMissing("LastName missing", fields=("LastName",)))
    g = fake.composite.graph()
    one = g.graph("ok")
    acc = one.create("Account", {"Name": "Kept"})
    one.update("Account", acc.ref("id"), {"Name": "Kept 2"})
    two = g.graph("bad")
    acc2 = two.create("Account", {"Name": "Gone"})
    two.create("Contact", {"AccountId": acc2.ref("id")})
    ok, bad = g.send()
    assert ok.ok
    assert not bad.ok
    ok.raise_for_errors()
    with pytest.raises(esf.RequiredFieldMissing):
        bad.raise_for_errors()
    assert [r["Name"] for r in fake.records("Account")] == ["Kept 2"]
    assert fake.calls[0].args["graphs"].keys() == {"ok", "bad"}


def test_batch_halt_on_error() -> None:
    fake = FakeSalesforce()
    b = fake.composite.batch(halt_on_error=True)
    b.create("Account", {"Name": "A"})
    b.get("Account", "001xx0000000009AAA")
    b.create("Account", {"Name": "B"})
    result = b.send()
    assert [r.status for r in result] == [201, 404, 412]
    assert result[2].halted
    with pytest.raises(esf.NotFound):
        result.raise_for_errors()
    assert len(fake.records("Account")) == 1

    free = fake.composite.batch()
    free.get("Account", "001xx0000000009AAA")
    free.create("Account", {"Name": "C"})
    assert [r.status for r in free.send()] == [404, 201]


def test_tree_links_children_and_rolls_back() -> None:
    fake = FakeSalesforce()
    ids = fake.composite.tree(
        "Account",
        [
            {
                "attributes": {"referenceId": "acme"},
                "Name": "Acme",
                "Contacts": {"records": [{"attributes": {"type": "Contact"}, "LastName": "Doe"}]},
                "Projects__r": {"records": [{"attributes": {"type": "Project__c"}, "Name": "P"}]},
            }
        ],
    )
    assert set(ids) == {"acme", "ref1", "ref2"}
    assert fake.record("Contact", ids["ref1"])["AccountId"] == ids["acme"]  # type: ignore[index]
    assert fake.record("Project__c", ids["ref2"])["AccountId"] == ids["acme"]  # type: ignore[index]

    fake.reject("Contact", esf.RequiredFieldMissing("Last Name missing", fields=("LastName",)))
    with pytest.raises(esf.RequiredFieldMissing) as ei:
        fake.composite.tree(
            "Account",
            [{"Name": "Gone", "Contacts": {"records": [{"attributes": {"type": "Contact"}}]}}],
        )
    assert ei.value.errors[0].extended["referenceId"] == "ref2"
    assert ei.value.status == 400
    assert len(fake.records("Account")) == 1
    assert [c.operation for c in fake.calls] == ["composite.tree", "composite.tree"]


def test_tree_custom_parent_field() -> None:
    fake = FakeSalesforce()
    ids = fake.composite.tree(
        "Project__c",
        [{"Name": "P", "Tasks__r": {"records": [{"attributes": {"type": "Task__c"}, "Name": "T"}]}}],
    )
    assert fake.record("Task__c", ids["ref2"])["Project__c"] == ids["ref1"]  # type: ignore[index]


def test_fake_checks_the_same_limits() -> None:
    fake = FakeSalesforce()
    with pytest.raises(ValueError, match="at least one"):
        fake.composite.builder().send()
    with pytest.raises(ValueError, match="at least one graph"):
        fake.composite.graph().send()
    with pytest.raises(ValueError, match="at least one"):
        fake.composite.batch().send()
    with pytest.raises(ValueError, match="200 records"):
        fake.composite.tree("Account", [{"Name": str(i)} for i in range(201)])
    assert fake.calls == []


def test_fail_next_fails_the_whole_call() -> None:
    fake = FakeSalesforce()
    fake.fail_next("composite.send", esf.ServerError("boom"))
    c = fake.composite.builder()
    c.create("Account", {"Name": "A"})
    with pytest.raises(esf.ServerError):
        c.send()
    assert fake.records("Account") == []


def test_async_fake() -> None:
    fake = AsyncFakeSalesforce()

    async def main() -> None:
        c = fake.composite.builder(all_or_none=True, collate_subrequests=True)
        acc = c.create("Account", {"Name": "Acme"})
        c.create("Contact", {"LastName": "Doe", "AccountId": acc.ref("id")})
        assert all(r.ok for r in await c.send())
        g = fake.composite.graph()
        g.graph().create("Account", {"Name": "G"})
        (graph,) = await g.send()
        assert graph.ok
        b = fake.composite.batch()
        b.create("Account", {"Name": "B"})
        assert (await b.send())[0].status == 201
        ids = await fake.composite.tree("Account", [{"Name": "T"}])
        assert list(ids) == ["ref1"]

    asyncio.run(main())
    assert len(fake.records("Account")) == 4
