"""Core REST behaviour checked against a real org, on both clients (spec §21: V19, V21, V22,
V24, V25, V26, V29, V30, V80).

Needs the fixtures in ``tests/sandbox/org/core`` and ``tests/sandbox/org/records``
(``scripts/sandbox_setup_org.py``): ``Lead.Esf_Ext__c`` (unique external id),
``Lead.Esf_Ext_Multi__c`` (non-unique external id), the ``Esf_Sandbox`` Lead assignment rule
(``Company`` = ``ESF_ASSIGN`` → queue ``Esf_Sandbox_Queue``) and the custom object
``Esf_Sandbox_Item__c``. The V24 duplicate check relies on the org's active *Standard Lead
Duplicate Rule* (alert on create), which new orgs have.

Every Lead carries the run tag in ``LastName`` and is deleted. Set
``ESF_SANDBOX_RECORD_DIR=<dir>`` to also record the sync client's traffic with
``esf.testing.record`` (one file per test) for ``tests/fixtures/recorded``.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

import even_simpler_salesforce as esf
from even_simpler_salesforce.cli import _describe_rows  # pyright: ignore[reportPrivateUsage]
from even_simpler_salesforce.core.http import url_segment

from .conftest import Cleanup, Driver, new_row

pytestmark = pytest.mark.sandbox

EXT = "Esf_Ext__c"
EXT_MULTI = "Esf_Ext_Multi__c"
ITEM = "Esf_Sandbox_Item__c"


def _record(sb: Driver, name: str) -> dict[str, Any]:
    """``http_client=`` that records the sync client's traffic, when recording is on."""
    folder = os.environ.get("ESF_SANDBOX_RECORD_DIR")
    if not folder or sb.is_async:
        return {}
    return {"http_client": httpx.Client(transport=esf.testing.record(Path(folder) / f"{name}.json"))}


def _owner_is_queue(sf: Any, sb: Driver, rec_id: str) -> bool:
    owner = sb.run(sf.sobject("Lead").get(rec_id, ["OwnerId"]))["OwnerId"]
    return str(owner).startswith("00G")


# --- V19: external id values ending like a format suffix -------------------------------------


@pytest.mark.usefixtures("writes")
@pytest.mark.parametrize("suffix", [".json", ".xml", ".html"])
def test_external_id_with_format_suffix(sb: Driver, tag: str, cleanup: Cleanup, suffix: str) -> None:
    """V19: values ending in ``.json``/``.xml``/``.html`` work through the SOQL/Collections fallback.

    Salesforce reads those endings of ``sobjects/{o}/{field}/{value}`` as a response format and
    answers 404 (see ``test_format_suffix_breaks_the_plain_path``). Format suffixes are documented
    only up to API 57.0 (REST API Developer Guide, *REST Resources and Requests*,
    https://developer.salesforce.com/docs/atlas.en-us.api_rest.meta/api_rest/intro_rest_resources.htm).
    """
    sf = sb.client(**_record(sb, f"v19_suffix{suffix.replace('.', '_')}"))
    lead = sf.sobject("Lead")
    value = f"{tag}-{sb.kind}-{uuid.uuid4().hex[:6]}{suffix}"
    created = sb.run(lead.upsert(EXT, value, new_row("Lead", tag, f"-v19-{sb.kind}")))
    cleanup.add(created.id)
    assert created.created is True
    assert str(created.id).startswith("00Q")
    again = sb.run(lead.upsert(EXT, value, {"FirstName": "Esf"}))
    assert again == esf.UpsertResult(id=created.id, created=False)
    got = sb.run(lead.get_by_external_id(EXT, value, ["Id", "FirstName", EXT]))
    assert got["Id"] == created.id
    assert got["FirstName"] == "Esf"
    assert got[EXT] == value
    full = sb.run(lead.get_by_external_id(EXT, value))
    assert full["attributes"]["type"] == "Lead"
    assert full[EXT] == value
    ops = {(e["operation"], e["path_template"]) for e in sb.recorder.requests()}
    assert ("sobject.upsert", f"/sobjects/Lead/{EXT}/:value") in ops
    assert ("sobject.get_by_external_id", "/query") in ops
    assert ("sobject.get", "/sobjects/Lead/:id") in ops

    with pytest.raises(esf.NotFound) as nf:
        sb.run(lead.get_by_external_id(EXT, f"{tag}-missing{suffix}"))
    assert nf.value.error_code == "NOT_FOUND"
    assert nf.value.status == 404
    with pytest.raises(esf.InvalidField) as inv:
        sb.run(lead.upsert(EXT, value, {EXT: value, "FirstName": "x"}))
    assert inv.value.error_code == "INVALID_FIELD"
    with pytest.raises(esf.ValidationRuleFailed):
        sb.run(lead.upsert(EXT, f"{tag}-vr{suffix}", new_row("Lead", tag, "-v19vr", Company="ESF_VR")))
    sb.run(lead.delete(created.id))
    cleanup.discard(created.id)


@pytest.mark.usefixtures("writes")
def test_external_id_suffix_multiple_matches(sb: Driver, tag: str, cleanup: Cleanup) -> None:
    """V19: on a non-unique external id, the fallback raises ``MultipleMatches`` like HTTP 300."""
    sf = sb.client()
    lead = sf.sobject("Lead")
    plain = f"{tag}-{sb.kind}-multi"
    dotted = plain + ".json"
    ids = [
        sb.run(lead.create(new_row("Lead", tag, f"-v19m-{i}", **{EXT_MULTI: v}), assignment_rule_id=False))
        for i, v in enumerate((plain, plain, dotted, dotted))
    ]
    cleanup.add(*ids)
    errors: list[esf.MultipleMatches] = []
    for value in (plain, dotted):
        for call in (
            lambda v=value: lead.get_by_external_id(EXT_MULTI, v),
            lambda v=value: lead.upsert(EXT_MULTI, v, {"FirstName": "x"}),
        ):
            with pytest.raises(esf.MultipleMatches) as ei:
                sb.run(call())
            errors.append(ei.value)
    for err, expected in zip(errors, [ids[:2], ids[:2], ids[2:], ids[2:]], strict=True):
        assert err.status == 300
        assert sorted(m.rsplit("/", 1)[-1] for m in err.matches) == sorted(expected)
        assert all(m.startswith(f"/services/data/v{sf.api_version}/sobjects/Lead/") for m in err.matches)
    sb.run(sf.collections.delete(ids))
    cleanup.discard(*ids)


@pytest.mark.usefixtures("writes")
def test_format_suffix_breaks_the_plain_path(sb: Driver, tag: str, cleanup: Cleanup) -> None:
    """V19 evidence: the path lookup fails for ``.json``/``.xml``/``.html`` only (lower case).

    If this starts to pass for a suffix, Salesforce fixed it and the fallback can be narrowed.
    """
    sf = sb.client(client_name=None)
    broken = [".json", ".xml", ".html"]
    working = [".JSON", ".Xml", ".htm", ".csv", ".txt", ".json.x", "."]
    values = {s: f"{tag}-{sb.kind}-p{i}{s}" for i, s in enumerate(broken + working)}
    result = sb.run(
        sf.collections.create(
            [new_row("Lead", tag, f"-v19p{i}", **{EXT: v}) for i, v in enumerate(values.values())],
            sobject="Lead",
            assignment_rule_id=False,
        )
    )
    ids = [r.id for r in result if r.id]
    cleanup.add(*ids)
    assert not result.failed
    status: dict[str, int] = {}
    for suffix, value in values.items():
        try:
            sb.run(sf.request("GET", f"sobjects/Lead/{EXT}/{url_segment(value)}", params={"fields": "Id"}))
            status[suffix] = 200
        except esf.NotFound as exc:
            status[suffix] = exc.status or 0
        except esf.UnexpectedResponse as exc:  # ".xml" makes Salesforce answer in XML
            status[suffix] = exc.status or 0
    assert status == {**dict.fromkeys(broken, 404), **dict.fromkeys(working, 200)}
    sb.run(sf.collections.delete(ids))
    cleanup.discard(*ids)


# --- V21: api_version the org doesn't offer --------------------------------------------------


def test_unoffered_api_version(sb: Driver) -> None:
    """V21: a version newer than the org's answers 404 ``NOT_FOUND``, not ``UNSUPPORTED_API_VERSION``.

    The client refuses versions older than its minimum (v59.0) before sending, so a version
    "not offered" can only be one newer than the org's release.
    """
    offered = sb.run(sb.client().request("GET", "/services/data/"))
    newest = max(float(v["version"]) for v in offered)
    future = f"{int(newest) + 1}.0"
    sf = sb.client(api_version=future, **_record(sb, "v21_unoffered_api_version"))
    with pytest.raises(esf.NotFound) as ei:
        sb.run(sf.limits())
    assert ei.value.status == 404
    assert ei.value.error_code == "NOT_FOUND"
    assert ei.value.message == "The requested resource does not exist"


# --- V22: query?explain= ---------------------------------------------------------------------


def test_explain_plans(sb: Driver) -> None:
    """V22: ``query?explain=`` returns ``plans[]`` with the documented fields (Beta)."""
    sf = sb.client(**_record(sb, "v22_explain"))
    plans = sb.run(sf.explain("SELECT Id FROM Lead WHERE Esf_Ext__c = {}", "esf-sandbox-x"))
    assert plans
    assert plans[0].leading_operation == "Index"
    assert plans[0].fields == ("Esf_Ext__c",)
    assert all(p.sobject_type == "Lead" for p in plans)
    assert {p.leading_operation for p in plans} <= {"Index", "Other", "TableScan", "Sharing"}
    assert [p.relative_cost for p in plans] == sorted(p.relative_cost for p in plans)
    for p in plans:
        for note in p.notes:
            assert note.description
            assert note.table_enum_or_id == "Lead"


# --- V24: write headers on sObject Collections -----------------------------------------------


@pytest.mark.usefixtures("writes")
def test_collections_assignment_rule_header(sb: Driver, tag: str, cleanup: Cleanup) -> None:
    """V24: ``Sforce-Auto-Assign`` takes effect on Collections create, update and upsert."""
    sf = sb.client(**_record(sb, "v24_collections_auto_assign"))
    coll = sf.collections

    def row(suffix: str, **kw: Any) -> dict[str, Any]:
        return new_row("Lead", tag, f"-v24-{sb.kind}-{suffix}", Company="ESF_ASSIGN", **kw)

    made = sb.run(coll.create([row("default"), row("true")], sobject="Lead", assignment_rule_id=True))
    off = sb.run(coll.create([row("false")], sobject="Lead", assignment_rule_id=False))
    ids = [r.id for r in [*made, *off] if r.id]
    cleanup.add(*ids)
    assert [_owner_is_queue(sf, sb, i) for i in ids] == [True, True, False]

    sb.run(coll.update("Lead", [{"Id": ids[2], "FirstName": "a"}], assignment_rule_id=False))
    assert not _owner_is_queue(sf, sb, ids[2])
    sb.run(coll.update("Lead", [{"Id": ids[2], "FirstName": "b"}], assignment_rule_id=True))
    assert _owner_is_queue(sf, sb, ids[2])

    up = sb.run(
        coll.upsert("Lead", EXT, [row("ups", **{EXT: f"{tag}-v24-{sb.kind}"})], assignment_rule_id=False)
    )
    cleanup.add(*[r.id for r in up if r.id])
    assert up.results[0].created is True
    assert not _owner_is_queue(sf, sb, str(up.results[0].id))
    sb.run(coll.delete([*ids, str(up.results[0].id)]))
    cleanup.discard(*ids, str(up.results[0].id))


@pytest.mark.usefixtures("writes")
def test_collections_duplicate_rule_header(sb: Driver, tag: str, cleanup: Cleanup) -> None:
    """V24: ``Sforce-Duplicate-Rule-Header: allowSave=true`` saves past an alert via Collections."""
    sf = sb.client(**_record(sb, "v24_collections_duplicates"))
    dup = {
        "FirstName": "Esfdup",
        "LastName": f"{tag}-v24dup-{sb.kind}",
        "Company": "esf-sandbox-dup",
        "Email": f"{tag}-{sb.kind}@example.com",
    }
    first = sb.run(sf.sobject("Lead").create(dup, assignment_rule_id=False))
    cleanup.add(first)
    blocked = sb.run(sf.collections.create([dup], sobject="Lead"))
    assert [e.error_code for e in blocked.results[0].errors] == ["DUPLICATES_DETECTED"]
    saved = sb.run(sf.collections.create([dup], sobject="Lead", allow_save=True))
    assert saved.results[0].ok
    cleanup.add(saved.results[0].id)
    sb.run(sf.collections.delete([first, str(saved.results[0].id)]))
    cleanup.discard(first, str(saved.results[0].id))


# --- V25: If-Unmodified-Since on DELETE ------------------------------------------------------


@pytest.mark.usefixtures("writes")
def test_delete_with_if_unmodified_since(sb: Driver, tag: str, cleanup: Cleanup) -> None:
    """V25: Salesforce rejects the header on DELETE and keeps the record.

    A date before the last change gives 500 ``UNKNOWN_EXCEPTION``; a date in the future gives
    400 ``BAD_HEADER``. Either way nothing is deleted.

    *Conditional Request Headers* says DELETE doesn't support them
    (https://developer.salesforce.com/docs/atlas.en-us.api_rest.meta/api_rest/intro_rest_conditional_requests.htm);
    the library's ``delete()`` takes no precondition.
    """
    sf = sb.client(retry=esf.RetryPolicy(max_attempts=1), **_record(sb, "v25_delete_if_unmodified_since"))
    lead = sf.sobject("Lead")
    rec_id = sb.run(lead.create(new_row("Lead", tag, f"-v25-{sb.kind}")))
    cleanup.add(rec_id)
    now = datetime.now(timezone.utc)
    cases = [
        (now - timedelta(days=3650), esf.ServerError, 500, "UNKNOWN_EXCEPTION"),
        (now + timedelta(days=1), esf.RequestError, 400, "BAD_HEADER"),
    ]
    for when, cls, status, code in cases:
        header = {"If-Unmodified-Since": format_datetime(when, usegmt=True)}
        with pytest.raises(cls) as ei:
            sb.run(sf.request("DELETE", f"sobjects/Lead/{rec_id}", headers=header))
        assert ei.value.status == status
        assert ei.value.error_code == code
        assert sb.run(lead.get(rec_id, ["Id"]))["Id"] == rec_id  # not deleted
    sb.run(lead.delete(rec_id))
    cleanup.discard(rec_id)


# --- V26: defaultNamespace without a namespace -----------------------------------------------


def test_default_namespace_without_namespace(sb: Driver) -> None:
    """V26: an org without a namespace accepts any ``defaultNamespace`` value and ignores it.

    So the org can't confirm the prefix format; that needs an org with a registered namespace
    (or a managed package installed).
    """
    sf = sb.client(client_name=None)
    org = sb.run(sf.query("SELECT NamespacePrefix FROM Organization"))
    assert org.records[0]["NamespacePrefix"] is None
    for value in ("esfnope", "1bad", "a__b", "abcdefghijklmnop", "_x"):
        body = sb.run(
            sf.request(
                "GET",
                "query",
                params={"q": "SELECT Id, Esf_Ext__c FROM Lead LIMIT 1"},
                headers={"Sforce-Call-Options": f"defaultNamespace={value}"},
            )
        )
        assert body["done"] is True
        for rec in body["records"]:
            assert set(rec) == {"attributes", "Id", EXT}


# --- V29: error message shapes ---------------------------------------------------------------


@pytest.mark.usefixtures("writes")
def test_invalid_field_message_shapes(sb: Driver, tag: str, cleanup: Cleanup) -> None:
    """V29: ``INVALID_FIELD`` reads "on entity 'Lead'" for queries, "on sobject of type Lead" for DML."""
    sf = sb.client(**_record(sb, "v29_invalid_field"))
    with pytest.raises(esf.InvalidField) as q:
        sb.run(sf.query("SELECT Nope__x FROM Lead"))
    assert q.value.status == 400
    assert "No such column 'Nope__x' on entity 'Lead'." in q.value.message
    with pytest.raises(esf.InvalidField) as d:
        sb.run(sf.sobject("Lead").create(new_row("Lead", tag, "-v29", Nope__x="1")))
    assert d.value.status == 400
    assert d.value.message == "No such column 'Nope__x' on sobject of type Lead"
    # With a cached describe, both shapes get a suggestion.
    sb.run(sf.sobject("Lead").describe())
    with pytest.raises(esf.InvalidField) as h:
        sb.run(sf.query("SELECT Compny FROM Lead"))
    assert str(h.value.hint).startswith("did you mean 'Company'")


def test_invalid_session_body(sb: Driver, settings: Any) -> None:
    """V29: a rejected access token gives 401 ``INVALID_SESSION_ID`` "Session expired or invalid"."""
    probe = sb.client()
    sb.run(probe.limits())  # makes sure the client knows its instance URL
    instance = probe.instance_url
    cls = esf.AsyncSalesforce if sb.is_async else esf.Salesforce
    kw = _record(sb, "v29_invalid_session")
    sf = cls(esf.StaticToken("esf-sandbox-not-a-token", instance), **kw)
    try:
        with pytest.raises(esf.SessionExpired) as ei:
            sb.run(sf.limits())
    finally:
        sb.run(sf.aclose() if sb.is_async else sf.close())
    assert ei.value.status == 401
    assert ei.value.error_code == "INVALID_SESSION_ID"
    assert ei.value.message == "Session expired or invalid"


# --- V30: describe semantics -----------------------------------------------------------------


def test_describe_required_and_picklists(sb: Driver) -> None:
    """V30: "required" = not nillable, createable, not defaulted; inactive values are left out."""
    sf = sb.client(**_record(sb, "v30_describe"))
    lead = {r["name"]: r for r in _describe_rows(sb.run(sf.sobject("Lead").describe(cached=False)))}
    assert lead["LastName"]["required"] is True
    assert lead["Company"]["required"] is True
    for name in ("Id", "Name", "OwnerId", "Status", "IsConverted", "Email"):
        assert lead[name]["required"] is False, name
    raw_item = sb.run(sf.sobject(ITEM).describe(cached=False))
    item = {r["name"]: r for r in _describe_rows(raw_item)}
    assert item["Esf_Required__c"]["required"] is True
    assert item["Esf_Defaulted__c"]["required"] is False  # checkbox: nillable=false, defaulted
    assert item["Name"]["required"] is False
    assert item["Esf_Restricted__c"]["restricted"] is True
    assert item["Esf_Open__c"]["restricted"] is False
    # REST describe lists active values only: the inactive "Retired" is not in picklistValues.
    fields = {f["name"]: f for f in raw_item["fields"]}
    values = fields["Esf_Restricted__c"]["picklistValues"]
    assert [(v["value"], v["active"]) for v in values] == [("Alpha", True), ("Beta", True)]
    assert item["Esf_Restricted__c"]["values"] == ["Alpha", "Beta"]


# --- V80: null clears, omitted keeps ---------------------------------------------------------


@pytest.mark.usefixtures("writes")
def test_null_clears_omitted_keeps(sb: Driver, tag: str, cleanup: Cleanup) -> None:
    """V80: JSON ``null`` in PATCH clears a field; a key left out keeps its value."""
    sf = sb.client(**_record(sb, "v80_null_clears"))
    lead = sf.sobject("Lead")
    rec_id = sb.run(lead.create(new_row("Lead", tag, f"-v80-{sb.kind}", Rating="Hot", Title="Esf")))
    cleanup.add(rec_id)
    sb.run(lead.update(rec_id, {"Rating": None}))
    got = sb.run(lead.get(rec_id, ["Rating", "Title"]))
    assert (got["Rating"], got["Title"]) == (None, "Esf")
    sb.run(lead.update(rec_id, {"Rating": "Warm"}))
    result = sb.run(sf.collections.update("Lead", [{"Id": rec_id, "Rating": None}]))
    assert not result.failed
    got = sb.run(lead.get(rec_id, ["Rating", "Title"]))
    assert (got["Rating"], got["Title"]) == (None, "Esf")
    sb.run(lead.delete(rec_id))
    cleanup.discard(rec_id)
