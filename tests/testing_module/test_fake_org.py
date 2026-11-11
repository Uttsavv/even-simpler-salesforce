"""``FakeSalesforce`` support for lead convert, merge and platform events (§14)."""

from __future__ import annotations

import asyncio

import pytest

import even_simpler_salesforce as esf
from even_simpler_salesforce.testing import AsyncFakeSalesforce, FakeSalesforce


def seed_lead(fake: FakeSalesforce, **extra: str) -> str:
    data = {"FirstName": "Ada", "LastName": "Lovelace", "Company": "Analytical", "Email": "ada@example.com"}
    return fake.add("Lead", {**data, **extra})


def test_convert_creates_account_contact_opportunity() -> None:
    fake = FakeSalesforce()
    lead = seed_lead(fake)
    res = fake.leads.convert(lead, converted_status="Closed - Converted", opportunity_name="Big deal")
    assert res.ok
    assert res.lead_id == lead
    assert res.account_id
    assert res.contact_id
    assert res.opportunity_id
    assert fake.record("Account", res.account_id)["Name"] == "Analytical"  # type: ignore[index]
    contact = fake.record("Contact", res.contact_id)
    assert contact is not None
    assert (contact["LastName"], contact["Email"], contact["AccountId"]) == (
        "Lovelace",
        "ada@example.com",
        res.account_id,
    )
    assert fake.record("Opportunity", res.opportunity_id)["Name"] == "Big deal"  # type: ignore[index]
    stored = fake.record("Lead", lead)
    assert stored is not None
    assert stored["IsConverted"] is True
    assert stored["Status"] == "Closed - Converted"
    assert stored["ConvertedContactId"] == res.contact_id
    assert [c.operation for c in fake.calls] == ["leads.convert"]


def test_convert_into_existing_records_without_opportunity() -> None:
    fake = FakeSalesforce()
    acc = fake.add("Account", {"Name": "Existing"})
    con = fake.add("Contact", {"LastName": "Existing", "AccountId": acc})
    owner = fake.new_id("User")
    lead = seed_lead(fake)
    res = fake.leads.convert(
        lead,
        converted_status="Closed",
        account_id=acc,
        contact_id=con,
        do_not_create_opportunity=True,
        owner_id=owner,
    )
    assert (res.account_id, res.contact_id, res.opportunity_id) == (acc, con, None)
    assert len(fake.records("Account")) == 1
    assert fake.records("Opportunity") == []
    assert fake.record("Lead", lead)["OwnerId"] == owner  # type: ignore[index]


def test_convert_errors() -> None:
    fake = FakeSalesforce()
    lead = seed_lead(fake)
    fake.leads.convert(lead, converted_status="Closed")
    with pytest.raises(esf.RecordError) as ei:
        fake.leads.convert(lead, converted_status="Closed")
    assert ei.value.error_code == "CANNOT_UPDATE_CONVERTED_LEAD"
    with pytest.raises(esf.NotFound):
        fake.leads.convert(fake.new_id("Lead"), converted_status="Closed")
    # A failed conversion leaves nothing behind.
    other = seed_lead(fake, LastName="Babbage")
    before = len(fake.records("Account"))
    with pytest.raises(esf.NotFound):
        fake.leads.convert(other, converted_status="Closed", account_id=fake.new_id("Account"))
    assert len(fake.records("Account")) == before
    assert "IsConverted" not in (fake.record("Lead", other) or {})
    fake.fail_next("leads.convert", esf.ServerError("boom"))
    with pytest.raises(esf.ServerError):
        fake.leads.convert(other, converted_status="Closed")


def test_convert_many_per_lead_results_and_reject() -> None:
    fake = FakeSalesforce()
    good = seed_lead(fake)
    bad = seed_lead(fake, LastName="Rejected")
    fake.reject(
        "Lead",
        esf.ValidationRuleFailed("nope", error_code="FIELD_CUSTOM_VALIDATION_EXCEPTION"),
        when=lambda r: r.get("LastName") == "Rejected",
    )
    batch = fake.leads.convert_many([esf.LeadConvert(good, "Closed"), esf.LeadConvert(bad, "Closed")])
    assert [r.ok for r in batch] == [True, False]
    assert batch[1].lead_id == bad
    assert batch[1].errors[0].error_code == "FIELD_CUSTOM_VALIDATION_EXCEPTION"
    assert fake.record("Lead", bad).get("IsConverted") is None  # type: ignore[union-attr]
    assert batch.operation == "leads.convert"
    with pytest.raises(TypeError):
        fake.leads.convert_many([good])  # type: ignore[list-item]


def test_merge_deletes_duplicates_and_repoints_children() -> None:
    fake = FakeSalesforce()
    master = fake.add("Account", {"Name": "Keep"})
    dup1 = fake.add("Account", {"Name": "Dup 1"})
    dup2 = fake.add("Account", {"Name": "Dup 2"})
    child = fake.add("Contact", {"LastName": "Child", "AccountId": dup1[:15]})
    res = fake.merge(master, [dup1, dup2], sobject="Account")
    assert res == esf.MergeResult(ok=True, id=master, merged_ids=(dup1, dup2), updated_related_ids=(child,))
    assert [r["Id"] for r in fake.records("Account")] == [master]
    assert fake.record("Contact", child)["AccountId"] == master  # type: ignore[index]
    with pytest.raises(esf.NotFound):
        fake.merge(master, [dup1], sobject="Account")


@pytest.mark.parametrize(
    ("dups", "sobject", "exc"),
    [
        (["001xx0000000002AAA"], "Case", ValueError),
        ([], "Account", ValueError),
        (["001xx0000000001AAA"], "Account", ValueError),
        ("001xx0000000002AAA", "Account", TypeError),
    ],
)
def test_merge_validation_matches_real_client(dups: object, sobject: str, exc: type[Exception]) -> None:
    fake = FakeSalesforce()
    with pytest.raises(exc):
        fake.merge("001xx0000000001AAA", dups, sobject=sobject)  # type: ignore[arg-type]
    assert fake.calls == []


def test_publish_and_published() -> None:
    fake = FakeSalesforce()
    res = fake.platform_events.publish("Order_Event__e", {"Order_Number__c": "42"})
    assert res.ok
    assert res.id
    assert res.uuid
    batch = fake.platform_events.publish_many("Order_Event__e", [{"Order_Number__c": "43"}])
    assert batch[0].ok
    assert batch.sobject == "Order_Event__e"
    assert fake.published("Order_Event__e") == [{"Order_Number__c": "42"}, {"Order_Number__c": "43"}]
    assert fake.published("Other__e") == []
    assert [c.operation for c in fake.calls] == ["platform_events.publish", "collections.create"]
    with pytest.raises(ValueError, match="__e"):
        fake.platform_events.publish("Account", {})


def test_publish_rejections_map_to_event_errors() -> None:
    fake = FakeSalesforce()
    fake.reject(
        "Order_Event__e",
        esf.RecordError("limit", error_code="LIMIT_EXCEEDED"),
        when=lambda r: r.get("Big__c") is True,
    )
    with pytest.raises(esf.EventPublishFailed):
        fake.platform_events.publish("Order_Event__e", {"Big__c": True})
    batch = fake.platform_events.publish_many("Order_Event__e", [{"Big__c": True}, {"Big__c": False}])
    assert [r.ok for r in batch] == [False, True]
    assert batch[0].errors[0].error_code == "LIMIT_EXCEEDED"
    assert len(fake.published("Order_Event__e")) == 1
    fake.fail_next("platform_events.publish", esf.EventPublishUnavailable("down"))
    with pytest.raises(esf.EventPublishUnavailable):
        fake.platform_events.publish("Order_Event__e", {})


def test_async_fake() -> None:
    async def main() -> None:
        async with AsyncFakeSalesforce() as fake:
            lead = fake.add("Lead", {"LastName": "Doe", "Company": "Acme"})
            res = await fake.leads.convert(lead, converted_status="Closed")
            assert res.ok
            other = fake.add("Lead", {"LastName": "Roe", "Company": "Acme"})
            batch = await fake.leads.convert_many([esf.LeadConvert(other, "Closed")])
            assert batch[0].ok
            assert res.account_id
            assert batch[0].account_id
            merged = await fake.merge(res.account_id, [batch[0].account_id], sobject="Account")
            assert merged.ok
            assert (await fake.platform_events.publish("E__e", {"A__c": 1})).ok
            assert (await fake.platform_events.publish_many("E__e", [{"A__c": 2}]))[0].ok
            assert fake.published("E__e") == [{"A__c": 1}, {"A__c": 2}]
            assert fake.sync.with_auth(esf.StaticToken("x", "https://a.my.salesforce.com")).leads

    asyncio.run(main())
