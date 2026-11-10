"""FakeSalesforce: records, ids, errors, call log."""

from __future__ import annotations

import asyncio
import re
from datetime import datetime, timedelta, timezone

import pytest

import even_simpler_salesforce as esf
from even_simpler_salesforce.testing import AsyncFakeSalesforce, FakeSalesforce, key_prefix
from even_simpler_salesforce.testing._ids import make_id, to_18

ID18 = re.compile(r"^[A-Za-z0-9]{18}$")


def test_to_18_matches_known_ids() -> None:
    # Known real-world pairs (checksum algorithm is public).
    assert to_18("001A0000006Vm9r") == "001A0000006Vm9rIAC"
    assert to_18("003000000000001") == "003000000000001AAA"


def test_ids_have_prefix_and_are_unique(esf_fake: FakeSalesforce) -> None:
    lead = esf_fake.sobject("Lead")
    ids = {lead.create({"LastName": f"L{i}", "Company": "Acme"}) for i in range(50)}
    assert len(ids) == 50
    for i in ids:
        assert ID18.match(i)
        assert i.startswith("00Q")
        assert to_18(i[:15]) == i
    acc = esf_fake.sobject("Account").create({"Name": "Acme"})
    assert acc.startswith("001")
    custom = esf_fake.sobject("Invoice__c").create({"Name": "I-1"})
    assert custom.startswith(key_prefix("Invoice__c"))
    assert key_prefix("Invoice__c")[0] == "a"


def test_key_prefix_override() -> None:
    fake = FakeSalesforce(key_prefixes={"Invoice__c": "a0X"})
    assert fake.sobject("Invoice__c").create({}).startswith("a0X")
    assert fake.new_id("Invoice__c").startswith("a0X")


def test_make_id_overflow() -> None:
    with pytest.raises(ValueError):
        make_id("001", 62**9)


def test_crud_round_trip(esf_fake: FakeSalesforce) -> None:
    lead = esf_fake.sobject("Lead")
    lid = lead.create({"LastName": "Doe", "Company": "Acme", "Status": "Open"})
    rec = lead.get(lid)
    assert rec["Id"] == lid
    assert rec["attributes"] == {"type": "Lead", "url": f"/services/data/v68.0/sobjects/Lead/{lid}"}
    assert rec["LastName"] == "Doe"
    assert lead.get(lid, fields=["Id", "Status", "Email"]) == {
        "attributes": rec["attributes"],
        "Id": lid,
        "Status": "Open",
        "Email": None,
    }
    assert lead.get(lid[:15])["Id"] == lid  # 15-char ids work too
    lead.update(lid, {"Status": "Working"})
    assert lead.get(lid)["Status"] == "Working"
    assert esf_fake.record("Lead", lid)["Status"] == "Working"  # type: ignore[index]
    lead.delete(lid)
    with pytest.raises(esf.NotFound) as ei:
        lead.get(lid)
    assert ei.value.error_code == "ENTITY_IS_DELETED"
    with pytest.raises(esf.NotFound):
        lead.delete(lid)
    assert esf_fake.records("Lead") == []


def test_missing_records_raise_not_found(esf_fake: FakeSalesforce) -> None:
    missing = esf_fake.new_id("Lead")
    for fn in (
        lambda: esf_fake.sobject("Lead").get(missing),
        lambda: esf_fake.sobject("Lead").update(missing, {"Status": "x"}),
        lambda: esf_fake.sobject("Lead").delete(missing),
    ):
        with pytest.raises(esf.NotFound) as ei:
            fn()
        assert ei.value.error_code == "NOT_FOUND"
        assert ei.value.status == 404
        assert ei.value.sobject == "Lead"
        assert ei.value.operation is not None
        assert ei.value.operation.startswith("sobject.")


def test_invalid_ids_and_names_raise_value_error(esf_fake: FakeSalesforce) -> None:
    with pytest.raises(ValueError):
        esf_fake.sobject("Lead").get("not-an-id")
    with pytest.raises(ValueError):
        esf_fake.sobject("Lead; DROP")
    with pytest.raises(ValueError):
        esf_fake.sobject("Lead").get(esf_fake.add("Lead", {"LastName": "x"}), fields=["Id, Name"])


def test_create_rejects_id_field(esf_fake: FakeSalesforce) -> None:
    with pytest.raises(esf.InvalidField) as ei:
        esf_fake.sobject("Lead").create({"Id": esf_fake.new_id("Lead")})
    assert ei.value.error_code == "INVALID_FIELD_FOR_INSERT_UPDATE"


def test_external_id_get_and_upsert(esf_fake: FakeSalesforce) -> None:
    acc = esf_fake.sobject("Account")
    res = acc.upsert("External_Id__c", "A-42", {"Name": "Acme"})
    assert res.created is True
    assert res.id is not None
    assert res.id.startswith("001")
    again = acc.upsert("External_Id__c", "A-42", {"Name": "Acme Inc"})
    assert again == esf.UpsertResult(id=res.id, created=False)
    got = acc.get_by_external_id("External_Id__c", "A-42", fields=["Name"])
    assert got["Name"] == "Acme Inc"
    with pytest.raises(esf.NotFound):
        acc.get_by_external_id("External_Id__c", "nope")
    esf_fake.add("Account", {"External_Id__c": "DUP", "Name": "1"})
    esf_fake.add("Account", {"External_Id__c": "DUP", "Name": "2"})
    with pytest.raises(esf.MultipleMatches) as ei:
        acc.upsert("External_Id__c", "DUP", {"Name": "x"})
    assert len(ei.value.matches) == 2
    with pytest.raises(esf.MultipleMatches):
        acc.get_by_external_id("External_Id__c", "DUP")


def test_model_conversion(esf_fake: FakeSalesforce) -> None:
    from dataclasses import dataclass

    @dataclass
    class Lead:
        Id: str
        LastName: str

    lid = esf_fake.sobject("Lead").create({"LastName": "Doe"})
    got = esf_fake.sobject("Lead").get(lid, fields=["Id", "LastName"], model=Lead)
    assert got == Lead(Id=lid, LastName="Doe")


def test_conditional_requests() -> None:
    now = [datetime(2026, 1, 1, tzinfo=timezone.utc)]
    fake = FakeSalesforce(clock=lambda: now[0])
    lead = fake.sobject("Lead")
    lid = lead.create({"LastName": "Doe"})
    assert lead.get(lid, if_modified_since=now[0] + timedelta(seconds=1)) is None
    assert lead.get(lid, if_modified_since=now[0] - timedelta(seconds=1)) is not None
    now[0] += timedelta(hours=1)
    lead.update(lid, {"LastName": "Roe"})
    with pytest.raises(esf.Conflict) as ei:
        lead.update(lid, {"LastName": "x"}, if_unmodified_since=now[0] - timedelta(minutes=5))
    assert ei.value.status == 412
    # Salesforce ignores conditional headers on DELETE (ADR 0012), so delete takes none.
    with pytest.raises(TypeError):
        lead.delete(lid, if_unmodified_since=now[0])  # type: ignore[call-arg]


def test_updated_and_deleted_windows() -> None:
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    now = [t0]
    fake = FakeSalesforce(clock=lambda: now[0])
    lead = fake.sobject("Lead")
    a = lead.create({"LastName": "A"})
    now[0] = t0 + timedelta(hours=2)
    b = lead.create({"LastName": "B"})
    lead.delete(a)
    upd = lead.updated(t0 + timedelta(hours=1), t0 + timedelta(hours=3))
    assert upd["ids"] == [b]
    dele = lead.deleted(t0, t0 + timedelta(hours=3))
    assert [d["id"] for d in dele["deletedRecords"]] == [a[:15]]


def test_describe(esf_fake: FakeSalesforce) -> None:
    esf_fake.sobject("Lead").create({"LastName": "Doe", "Company": "Acme"})
    d = esf_fake.sobject("Lead").describe()
    assert d["name"] == "Lead"
    assert d["keyPrefix"] == "00Q"
    assert {f["name"] for f in d["fields"]} >= {"Id", "LastName", "Company"}
    esf_fake.set_describe("Lead", {"name": "Lead", "fields": [{"name": "Custom__c"}]})
    assert esf_fake.sobject("Lead").describe()["fields"] == [{"name": "Custom__c"}]
    assert esf_fake.sobject("Lead").metadata()["objectDescribe"]["name"] == "Lead"
    names = [s["name"] for s in esf_fake.describe_global()["sobjects"]]
    assert "Lead" in names


def test_returned_records_are_copies(esf_fake: FakeSalesforce) -> None:
    lid = esf_fake.sobject("Lead").create({"LastName": "Doe", "Tags": ["a"]})
    rec = esf_fake.sobject("Lead").get(lid)
    rec["Tags"].append("b")
    rec["LastName"] = "changed"
    assert esf_fake.sobject("Lead").get(lid)["Tags"] == ["a"]
    assert esf_fake.sobject("Lead").get(lid)["LastName"] == "Doe"


def test_calls_are_recorded(esf_fake: FakeSalesforce) -> None:
    lead = esf_fake.sobject("Lead")
    lid = lead.create({"LastName": "Doe"})
    lead.update(lid, {"Status": "Working"})
    with pytest.raises(esf.NotFound):
        lead.get(esf_fake.new_id("Lead"))
    ops = [c.operation for c in esf_fake.calls]
    assert ops == ["sobject.create", "sobject.update", "sobject.get"]
    upd = esf_fake.calls_for("sobject.update", sobject="Lead")[0]
    assert upd.args["id"] == lid
    assert upd.args["data"] == {"Status": "Working"}
    assert upd.error is None
    assert isinstance(esf_fake.calls[-1].error, esf.NotFound)
    esf_fake.reset_calls()
    assert esf_fake.calls == []


def test_seeding_does_not_record_calls(esf_fake: FakeSalesforce) -> None:
    ids = esf_fake.add_many("Contact", [{"LastName": "A"}, {"LastName": "B"}])
    fixed = esf_fake.add("Contact", {"Id": "003000000000001AAA", "LastName": "C"})
    assert fixed == "003000000000001AAA"
    assert len(esf_fake.records("Contact")) == 3
    assert esf_fake.sobject("Contact").get(ids[0])["LastName"] == "A"
    assert [c.operation for c in esf_fake.calls] == ["sobject.get"]


def test_fail_next(esf_fake: FakeSalesforce) -> None:
    lead = esf_fake.sobject("Lead")
    lid = lead.create({"LastName": "Doe"})
    esf_fake.fail_next("sobject.update", esf.ValidationRuleFailed("[ADDR] Street is required"))
    with pytest.raises(esf.ValidationRuleFailed) as ei:
        lead.update(lid, {"Street": ""})
    assert ei.value.operation == "sobject.update"
    assert ei.value.sobject == "Lead"
    lead.update(lid, {"Street": "Main"})  # only the next one fails
    assert esf_fake.record("Lead", lid)["Street"] == "Main"  # type: ignore[index]


def test_fail_next_filters_and_times(esf_fake: FakeSalesforce) -> None:
    esf_fake.fail_next("sobject.create", esf.RowLocked, sobject="Account", times=2)
    lid = esf_fake.sobject("Lead").create({"LastName": "ok"})  # other sObject unaffected
    assert lid
    for _ in range(2):
        with pytest.raises(esf.RowLocked):
            esf_fake.sobject("Account").create({"Name": "x"})
    esf_fake.sobject("Account").create({"Name": "x"})
    esf_fake.fail_next("*", esf.ServerError("boom"))
    with pytest.raises(esf.ServerError):
        esf_fake.limits()
    with pytest.raises(ValueError):
        esf_fake.fail_next("query", esf.ServerError(), times=0)


def test_failed_write_leaves_store_unchanged(esf_fake: FakeSalesforce) -> None:
    lid = esf_fake.sobject("Lead").create({"LastName": "Doe"})
    esf_fake.fail_next("sobject.update", esf.ValidationRuleFailed("no"))
    with pytest.raises(esf.ValidationRuleFailed):
        esf_fake.sobject("Lead").update(lid, {"LastName": "Roe"})
    assert esf_fake.record("Lead", lid)["LastName"] == "Doe"  # type: ignore[index]


def test_reject_rule(esf_fake: FakeSalesforce) -> None:
    esf_fake.reject(
        "Lead",
        esf.ValidationRuleFailed("Company is required", error_code="FIELD_CUSTOM_VALIDATION_EXCEPTION"),
        when=lambda d: not d.get("Company"),
    )
    with pytest.raises(esf.ValidationRuleFailed) as ei:
        esf_fake.sobject("Lead").create({"LastName": "Doe"})
    assert ei.value.operation == "sobject.create"
    assert esf_fake.sobject("Lead").create({"LastName": "Doe", "Company": "Acme"})


def test_tagged_and_with_auth_share_state(esf_fake: FakeSalesforce) -> None:
    child = esf_fake.tagged(caller="job:backfill", priority="low")
    lid = child.sobject("Lead").create({"LastName": "Doe"})
    user = esf_fake.with_auth(esf.StaticToken("tok", "https://acme.my.salesforce.com"))
    assert user.sobject("Lead").get(lid)["LastName"] == "Doe"
    with esf.call_context(caller="route:/leads"):
        esf_fake.sobject("Lead").get(lid)
    callers = [(c.caller, c.priority) for c in esf_fake.calls]
    assert callers == [("job:backfill", "low"), (None, "normal"), ("route:/leads", "normal")]
    snap = esf_fake.usage.snapshot()
    assert snap.calls_by_operation == {"sobject.create": 1, "sobject.get": 2}
    assert snap.calls_by_caller["job:backfill"] == 1
    assert snap.calls_by_sobject == {"Lead": 3}
    user.close()  # closing a child is a no-op
    assert not esf_fake.closed


def test_context_manager_and_close() -> None:
    with FakeSalesforce() as fake:
        fake.sobject("Lead").create({"LastName": "x"})
    assert fake.closed
    with pytest.raises(esf.TransportError):
        fake.sobject("Lead").create({"LastName": "y"})


def test_budget_guard() -> None:
    fake = FakeSalesforce(budget=esf.Budget(pause_low_priority_above=0.5))
    fake.set_api_usage(900, 1000)
    with pytest.raises(esf.BudgetExceeded):
        fake.tagged(priority="low").limits()
    assert fake.limits()  # normal priority passes
    assert fake.usage.snapshot().api_used == 0  # limits() observed DailyApiRequests


def test_capture_rolls_up_fake_calls(esf_fake: FakeSalesforce) -> None:
    events: list[esf.Event] = []

    class Obs:
        def on_event(self, event: esf.Event) -> None:
            events.append(event)

    fake = FakeSalesforce(observers=[Obs()])
    fake.set_api_usage(12, 100)
    with esf.capture() as summary:
        lid = fake.sobject("Lead").create({"LastName": "Doe"})
        with pytest.raises(esf.NotFound):
            fake.sobject("Lead").get(fake.new_id("Lead"))
        fake.sobject("Lead").get(lid)
    fields = summary.as_fields()
    assert fields["sf_calls"] == 3
    assert fields["sf_errors"] == ["NOT_FOUND"]
    assert fields["sf_ops"] == {"sobject.create": 1, "sobject.get": 2}
    assert fields["sf_api_used"] == 12
    req = [e for e in events if e.name == "sf.request"]
    assert req[0]["path_template"] == "/sobjects/Lead"
    assert req[1]["path_template"] == "/sobjects/Lead/:id"
    assert req[1]["outcome"] == "error"


def test_async_fake_records() -> None:
    async def main() -> None:
        async with AsyncFakeSalesforce() as fake:
            lead = fake.sobject("Lead")
            lid = await lead.create({"LastName": "Doe"})
            await lead.update(lid, {"Status": "Working"})
            assert (await lead.get(lid))["Status"] == "Working"
            res = await lead.upsert("Email", "a@example.com", {"LastName": "A"})
            assert res.created
            assert (await lead.get_by_external_id("Email", "a@example.com"))["LastName"] == "A"
            assert (await lead.describe())["name"] == "Lead"
            assert (await lead.metadata())["objectDescribe"]["name"] == "Lead"
            now = datetime.now(timezone.utc)
            assert lid in (await lead.updated(now - timedelta(hours=1), now + timedelta(hours=1)))["ids"]
            await lead.delete(lid)
            assert (await lead.deleted(now - timedelta(hours=1), now + timedelta(hours=1)))["deletedRecords"]
            with pytest.raises(esf.NotFound):
                await lead.get(lid)
            assert (await fake.describe_global())["sobjects"]
            child = fake.tagged(caller="x").with_auth(esf.StaticToken("t", "https://a.my.salesforce.com"))
            await child.sobject("Lead").create({"LastName": "B"})
            assert fake.calls[-1].caller == "x"
            assert fake.sync.records("Lead")[-1]["LastName"] == "B"
            assert repr(fake).startswith("<AsyncFakeSalesforce")
        assert fake.closed

    asyncio.run(main())
