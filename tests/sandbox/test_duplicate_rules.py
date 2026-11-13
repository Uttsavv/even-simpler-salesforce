"""``esf.DuplicateRules`` (``Sforce-Duplicate-Rule-Header``) against a real org, on both clients.

Needs the fixtures in ``tests/sandbox/org/duplicates`` (``scripts/sandbox_setup_org.py --folder
duplicates``): the matching rule ``Esf_Sandbox_Lead_Email_Match`` (same email) and the duplicate
rule ``Esf_Sandbox_Lead_Duplicates`` (action "Allow" with an alert, on create and edit, only for
Leads with ``Company`` = ``esf-sandbox-duprule``). The org's *Standard Lead Duplicate Rule* alerts
on create only (on edit it only reports), so it can't show the header on updates.
Each test creates a Lead and duplicates of it (same email, all carrying the run tag) and
deletes them all.

Header reference: REST API Developer Guide, *Duplicate Rule Header*,
https://developer.salesforce.com/docs/atlas.en-us.api_rest.meta/api_rest/headers_duplicaterules.htm

Set ``ESF_SANDBOX_RECORD_DIR=<dir>`` to also record the sync client's traffic with
``esf.testing.record`` (one file per test) for ``tests/fixtures/recorded``.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import httpx
import pytest

import even_simpler_salesforce as esf

from .conftest import Cleanup, Driver

pytestmark = pytest.mark.sandbox

EXT = "Esf_Ext__c"
COMPANY = "esf-sandbox-duprule"
RULE = "Esf_Sandbox_Lead_Duplicates"


def _record(sb: Driver, name: str) -> dict[str, Any]:
    """``http_client=`` that records the sync client's traffic, when recording is on."""
    folder = os.environ.get("ESF_SANDBOX_RECORD_DIR")
    if not folder or sb.is_async:
        return {}
    return {"http_client": httpx.Client(transport=esf.testing.record(Path(folder) / f"{name}.json"))}


def _dup(tag: str, sb: Driver, name: str) -> dict[str, Any]:
    return {
        "FirstName": "Esfdup",
        "LastName": f"{tag}-{name}-{sb.kind}",
        "Company": COMPANY,
        "Email": f"{tag}-{name}-{sb.kind}@example.com",
    }


def _matched_records(dup: esf.DuplicateDetected | esf.ErrorDetail) -> list[dict[str, Any]]:
    result = dup.duplicates if isinstance(dup, esf.DuplicateDetected) else dup.extended["duplicateResult"]
    return [m["record"] for mr in result["matchResults"] for m in mr["matchRecords"]]


def test_fixture_duplicate_rule_is_active(sb: Driver) -> None:
    sf = sb.client()
    rows = sb.run(sf.query("SELECT DeveloperName, IsActive FROM DuplicateRule WHERE SobjectType = 'Lead'"))
    active = {r["DeveloperName"] for r in rows.records if r["IsActive"]}
    if RULE not in active:
        pytest.skip(f"sandbox: deploy tests/sandbox/org/duplicates ({RULE} is not active)")
    assert RULE in active


@pytest.mark.usefixtures("writes")
def test_single_record_duplicate_rules(sb: Driver, tag: str, cleanup: Cleanup) -> None:
    """No header: blocked with ``DuplicateDetected``; ``allow_save=True`` saves; details carry fields."""
    sf = sb.client(**_record(sb, "duplicate_rules_sobject"))
    lead = sf.sobject("Lead")
    row = _dup(tag, sb, "one")
    first = sb.run(lead.create(row, assignment_rule_id=False))
    cleanup.add(first)

    with pytest.raises(esf.DuplicateDetected) as plain:
        sb.run(lead.create(row, assignment_rule_id=False))
    ids_only = _matched_records(plain.value)
    assert [r["Id"] for r in ids_only] == [first]
    assert set(ids_only[0]) <= {"attributes", "Id"}

    with pytest.raises(esf.DuplicateDetected) as detailed:
        sb.run(lead.create(row, duplicate_rules=esf.DuplicateRules(include_record_details=True)))
    records = _matched_records(detailed.value)
    assert [r["Id"] for r in records] == [first]
    assert set(records[0]) - {"attributes", "Id"}, records[0]  # the matched record's fields

    rules = esf.DuplicateRules(allow_save=True, run_as_current_user=True)
    second = sb.run(lead.create(row, assignment_rule_id=False, duplicate_rules=rules))
    cleanup.add(second)
    assert second != first

    # update: make a third lead a duplicate of the first
    third = sb.run(lead.create(_dup(tag, sb, "one-other"), assignment_rule_id=False))
    cleanup.add(third)
    with pytest.raises(esf.DuplicateDetected):
        sb.run(lead.update(third, {"Email": row["Email"]}))
    sb.run(
        lead.update(
            third,
            {"Email": row["Email"]},
            duplicate_rules=esf.DuplicateRules(allow_save=True),
        )
    )

    # upsert that creates: same check
    up_row = row | {"Esf_Ext__c": f"{tag}-dup-{sb.kind}"}
    with pytest.raises(esf.DuplicateDetected):
        sb.run(lead.upsert(EXT, up_row[EXT], {k: v for k, v in up_row.items() if k != EXT}))
    made = sb.run(
        lead.upsert(
            EXT,
            up_row[EXT],
            {k: v for k, v in up_row.items() if k != EXT},
            duplicate_rules=esf.DuplicateRules(allow_save=True),
        )
    )
    cleanup.add(made.id)
    assert made.created is True

    sb.run(sf.collections.delete([first, second, third, str(made.id)]))
    cleanup.discard(first, second, third, str(made.id))


@pytest.mark.usefixtures("writes")
def test_collections_duplicate_rules(sb: Driver, tag: str, cleanup: Cleanup) -> None:
    """The header works on Collections create, update and upsert.

    ``includeRecordDetails`` is accepted, but the Collections response has no
    ``duplicateResult`` at all (the *sObject Collections* examples show the same error shape).
    """
    sf = sb.client(**_record(sb, "duplicate_rules_collections"))
    coll = sf.collections
    row = _dup(tag, sb, "coll")
    first = sb.run(sf.sobject("Lead").create(row, assignment_rule_id=False))
    cleanup.add(first)
    details = esf.DuplicateRules(include_record_details=True)
    save = esf.DuplicateRules(allow_save=True, include_record_details=True, run_as_current_user=True)

    # create
    blocked = sb.run(coll.create([row], sobject="Lead", duplicate_rules=details))
    (err,) = blocked.results[0].errors
    assert err.error_code == "DUPLICATES_DETECTED"
    # Org-observed (v68.0): Collections errors carry no duplicateResult, even with
    # includeRecordDetails=true; only the single-record resources return the matched records.
    assert "duplicateResult" not in err.extended
    saved = sb.run(coll.create([row], sobject="Lead", duplicate_rules=save))
    assert saved.results[0].ok, saved.results[0].errors
    cleanup.add(saved.results[0].id)

    # update
    other = sb.run(coll.create([_dup(tag, sb, "coll-other")], sobject="Lead"))
    other_id = str(other.results[0].id)
    cleanup.add(other_id)
    change = {"Id": other_id, "Email": row["Email"]}
    blocked_u = sb.run(coll.update("Lead", [change]))
    assert [e.error_code for e in blocked_u.results[0].errors] == ["DUPLICATES_DETECTED"]
    saved_u = sb.run(coll.update("Lead", [change], duplicate_rules=save))
    assert saved_u.results[0].ok, saved_u.results[0].errors

    # upsert (creates)
    up = row | {EXT: f"{tag}-colldup-{sb.kind}"}
    blocked_x = sb.run(coll.upsert("Lead", EXT, [up]))
    assert [e.error_code for e in blocked_x.results[0].errors] == ["DUPLICATES_DETECTED"]
    saved_x = sb.run(coll.upsert("Lead", EXT, [up], duplicate_rules=save))
    assert saved_x.results[0].ok, saved_x.results[0].errors
    assert saved_x.results[0].created is True
    cleanup.add(saved_x.results[0].id)

    ids = [first, str(saved.results[0].id), other_id, str(saved_x.results[0].id)]
    sb.run(coll.delete(ids))
    cleanup.discard(*ids)


@pytest.mark.usefixtures("writes")
def test_allow_save_alias_still_works(sb: Driver, tag: str, cleanup: Cleanup) -> None:
    """The deprecated ``allow_save=`` still saves past the alert."""
    sf = sb.client()
    row = _dup(tag, sb, "alias")
    first = sb.run(sf.sobject("Lead").create(row, assignment_rule_id=False))
    cleanup.add(first)
    with pytest.warns(esf.EsfDeprecationWarning):
        second = sb.run(sf.sobject("Lead").create(row, allow_save=True))
    cleanup.add(second)
    sb.run(sf.collections.delete([first, second]))
    cleanup.discard(first, second)
