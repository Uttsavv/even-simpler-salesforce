"""``duplicate_rules=`` (``Sforce-Duplicate-Rule-Header``) on both clients.

The ``*_replayed`` tests replay real-org recordings from ``tests/sandbox/test_duplicate_rules.py``
(DE org, API v68.0): ``sobject/duplicate_rules.json`` and ``collections/duplicate_rules.json``.
Header reference: REST API Developer Guide, *Duplicate Rule Header*,
https://developer.salesforce.com/docs/atlas.en-us.api_rest.meta/api_rest/headers_duplicaterules.htm
"""

from __future__ import annotations

import warnings
from collections.abc import Callable
from typing import Any

import httpx
import pytest
import respx

import even_simpler_salesforce as esf
from even_simpler_salesforce.testing import AsyncFakeSalesforce, FakeSalesforce, replay

from ..conftest import DATA, Harness
from ..fixtures import RECORDED

LEAD_ID = "00Qxx0000000001AAA"
EXT = "Esf_Ext__c"
HEADER = "Sforce-Duplicate-Rule-Header"


def _replaying(h: Harness, name: str) -> tuple[Any, Any, list[httpx.Request]]:
    """A client on a recording, plus the requests it sent."""
    transport = replay(RECORDED / name)
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return transport.handler(request)  # type: ignore[attr-defined, no-any-return]

    mock = httpx.MockTransport(handler)
    http = httpx.AsyncClient(transport=mock) if h.is_async else httpx.Client(transport=mock)
    return h.client(http_client=http), transport, seen


def _matched(result: Any) -> list[dict[str, Any]]:
    return [m["record"] for mr in result["matchResults"] for m in mr["matchRecords"]]


def test_sobject_duplicate_rules_replayed(h: Harness) -> None:
    sf, transport, seen = _replaying(h, "sobject/duplicate_rules.json")
    lead = sf.sobject("Lead")
    row = {"LastName": "x", "Company": "esf-sandbox-duprule", "Email": "x@example.com"}
    first = h.run(lead.create(row, assignment_rule_id=False))

    with pytest.raises(esf.DuplicateDetected) as plain:
        h.run(lead.create(row, assignment_rule_id=False))
    assert plain.value.status == 400
    assert plain.value.operation == "sobject.create"
    ids_only = _matched(plain.value.duplicates)
    assert [r["Id"] for r in ids_only] == [first]
    assert set(ids_only[0]) == {"attributes", "Id"}

    with pytest.raises(esf.DuplicateDetected) as detailed:
        h.run(lead.create(row, duplicate_rules=esf.DuplicateRules(include_record_details=True)))
    dup = detailed.value.duplicates
    assert dup["duplicateRule"] == "Standard_Lead_Duplicate_Rule"
    (record,) = _matched(dup)
    assert record["Id"] == first
    assert record["Company"] == "esf-sandbox-duprule"
    assert {"Email", "Phone", "OwnerId"} <= set(record)
    assert seen[-1].headers[HEADER] == "allowSave=false; includeRecordDetails=true; runAsCurrentUser=false"

    rules = esf.DuplicateRules(allow_save=True, run_as_current_user=True)
    assert h.run(lead.create(row, assignment_rule_id=False, duplicate_rules=rules)) != first
    assert seen[-1].headers[HEADER] == "allowSave=true; includeRecordDetails=false; runAsCurrentUser=true"

    third = h.run(lead.create(row, assignment_rule_id=False))
    with pytest.raises(esf.DuplicateDetected) as upd:
        h.run(lead.update(third, {"Email": "x@example.com"}))
    assert upd.value.duplicates["duplicateRule"] == "Esf_Sandbox_Lead_Duplicates"
    assert HEADER not in seen[-1].headers
    h.run(lead.update(third, {"Email": "x@example.com"}, duplicate_rules=esf.DuplicateRules(allow_save=True)))
    assert seen[-1].headers[HEADER].startswith("allowSave=true;")

    value = "esf-sandbox-36f7f2d56c19-dup-sync"
    with pytest.raises(esf.DuplicateDetected):
        h.run(lead.upsert(EXT, value, row))
    made = h.run(lead.upsert(EXT, value, row, duplicate_rules=esf.DuplicateRules(allow_save=True)))
    assert made.created is True
    h.run(sf.collections.delete([f"00Q00000000000{i}EAA" for i in (1, 3, 4, 5)]))
    assert transport.handler.remaining == 0


def test_collections_duplicate_rules_replayed(h: Harness) -> None:
    """Collections take the header; their errors carry no ``duplicateResult`` (org-observed)."""
    sf, transport, seen = _replaying(h, "collections/duplicate_rules.json")
    coll = sf.collections
    row = {"LastName": "x", "Company": "esf-sandbox-duprule", "Email": "x@example.com"}
    h.run(sf.sobject("Lead").create(row))
    save = esf.DuplicateRules(allow_save=True, include_record_details=True, run_as_current_user=True)

    blocked = h.run(
        coll.create([row], sobject="Lead", duplicate_rules=esf.DuplicateRules(include_record_details=True))
    )
    (err,) = blocked.results[0].errors
    assert err.error_code == "DUPLICATES_DETECTED"
    assert "duplicateResult" not in err.extended
    assert h.run(coll.create([row], sobject="Lead", duplicate_rules=save)).results[0].ok
    assert seen[-1].headers[HEADER] == "allowSave=true; includeRecordDetails=true; runAsCurrentUser=true"

    other = h.run(coll.create([row], sobject="Lead")).results[0].id
    change = [{"Id": other, "Email": "x@example.com"}]
    assert [e.error_code for e in h.run(coll.update("Lead", change)).results[0].errors] == [
        "DUPLICATES_DETECTED"
    ]
    assert h.run(coll.update("Lead", change, duplicate_rules=save)).results[0].ok

    up = [row | {EXT: "x"}]
    assert [e.error_code for e in h.run(coll.upsert("Lead", EXT, up)).results[0].errors] == [
        "DUPLICATES_DETECTED"
    ]
    saved = h.run(coll.upsert("Lead", EXT, up, duplicate_rules=save))
    assert saved.results[0].created is True
    assert seen[-1].headers[HEADER] == "allowSave=true; includeRecordDetails=true; runAsCurrentUser=true"
    h.run(coll.delete([f"00Q00000000000{i}EAA" for i in (1, 2, 3, 4)]))
    assert transport.handler.remaining == 0


def test_all_or_none_duplicate_has_no_details(h: Harness, mock: respx.MockRouter) -> None:
    """With ``all_or_none=True`` the raised error has ``duplicates=None``: Collections sends none."""
    body = [
        {"success": False, "errors": [{"statusCode": "DUPLICATES_DETECTED", "message": "dup", "fields": []}]}
    ]
    mock.post(f"{DATA}/composite/sobjects").respond(json=body)  # shape as recorded above
    sf = h.client()
    with pytest.raises(esf.DuplicateDetected) as ei:
        h.run(sf.collections.create([{"LastName": "x"}], sobject="Lead", all_or_none=True))
    assert ei.value.duplicates is None


def test_default_rules_send_salesforce_defaults(h: Harness, mock: respx.MockRouter) -> None:
    route = mock.post(f"{DATA}/sobjects/Lead/").respond(201, json={"id": LEAD_ID})
    sf = h.client()
    h.run(sf.sobject("Lead").create({"LastName": "x"}, duplicate_rules=esf.DuplicateRules()))
    assert route.calls.last.request.headers[HEADER] == (
        "allowSave=false; includeRecordDetails=false; runAsCurrentUser=false"
    )
    h.run(sf.sobject("Lead").create({"LastName": "x"}))
    assert HEADER not in route.calls.last.request.headers


# --- the deprecated allow_save= ---------------------------------------------------------------


def _writes(sf: Any) -> dict[str, Callable[..., Any]]:
    lead = sf.sobject("Lead")
    return {
        "sobject.create": lambda **kw: lead.create({"LastName": "x"}, **kw),
        "sobject.update": lambda **kw: lead.update(LEAD_ID, {"LastName": "x"}, **kw),
        "sobject.upsert": lambda **kw: lead.upsert(EXT, "a", {"LastName": "x"}, **kw),
        "sobject.upsert_fallback": lambda **kw: lead.upsert(EXT, "a.json", {"LastName": "x"}, **kw),
        "collections.create": lambda **kw: sf.collections.create([{"LastName": "x"}], sobject="Lead", **kw),
        "collections.update": lambda **kw: sf.collections.update("Lead", [{"Id": LEAD_ID}], **kw),
        "collections.upsert": lambda **kw: sf.collections.upsert("Lead", EXT, [{EXT: "a"}], **kw),
    }


def _mock_writes(mock: respx.MockRouter) -> respx.Route:
    ok = [{"id": LEAD_ID, "success": True, "errors": [], "created": True}]
    mock.route(path__startswith="/services/data/v68.0/composite/sobjects").respond(json=ok)
    mock.post(f"{DATA}/sobjects/Lead/").respond(201, json={"id": LEAD_ID})
    mock.patch(f"{DATA}/sobjects/Lead/{LEAD_ID}").respond(204)
    return mock.patch(f"{DATA}/sobjects/Lead/{EXT}/a").respond(201, json={"id": LEAD_ID, "created": True})


@pytest.mark.parametrize("flag", [True, False])
@pytest.mark.parametrize(
    "method",
    [
        "sobject.create",
        "sobject.update",
        "sobject.upsert",
        "sobject.upsert_fallback",
        "collections.create",
        "collections.update",
        "collections.upsert",
    ],
)
def test_allow_save_is_deprecated(h: Harness, mock: respx.MockRouter, method: str, flag: bool) -> None:
    _mock_writes(mock)
    sf = h.client()
    call = _writes(sf)[method]

    async def awaited() -> Any:  # so the warning points at a line in this file on both clients
        return await call(allow_save=flag)

    with pytest.warns(esf.EsfDeprecationWarning) as caught:
        h.run(awaited() if h.is_async else call(allow_save=flag))
    (w,) = caught.list
    assert f"duplicate_rules=esf.DuplicateRules(allow_save={flag})" in str(w.message)
    assert "0.7" in str(w.message)
    assert w.filename == __file__
    assert mock.calls.last.request.headers[HEADER] == (
        f"allowSave={str(flag).lower()}; includeRecordDetails=false; runAsCurrentUser=false"
    )


@pytest.mark.parametrize(
    "method", ["sobject.create", "sobject.update", "sobject.upsert", "collections.upsert"]
)
def test_allow_save_and_duplicate_rules_together(h: Harness, mock: respx.MockRouter, method: str) -> None:
    _mock_writes(mock)
    sf = h.client()
    with pytest.raises(TypeError, match="not both"):
        h.run(_writes(sf)[method](allow_save=True, duplicate_rules=esf.DuplicateRules()))
    assert not mock.calls


def test_duplicate_rules_sends_no_warning(h: Harness, mock: respx.MockRouter) -> None:
    _mock_writes(mock)
    sf = h.client()
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        for call in _writes(sf).values():
            h.run(call(duplicate_rules=esf.DuplicateRules(allow_save=True)))
    assert mock.calls.last.request.headers[HEADER].startswith("allowSave=true;")


# --- FakeSalesforce ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind", ["sync", "async"])
def test_fake_records_duplicate_rules(kind: str) -> None:
    fake: Any = FakeSalesforce() if kind == "sync" else AsyncFakeSalesforce()
    h = Harness(kind)
    rules = esf.DuplicateRules(allow_save=True, include_record_details=True)
    rid = h.run(fake.sobject("Lead").create({"LastName": "x"}, duplicate_rules=rules))
    h.run(fake.sobject("Lead").update(rid, {"LastName": "y"}))
    h.run(fake.collections.create([{"LastName": "z"}], sobject="Lead", duplicate_rules=rules))
    create, update = fake.calls_for("sobject.create")[0], fake.calls_for("sobject.update")[0]
    assert create.args["duplicate_rules"] == rules
    assert create.args["allow_save"] is True  # kept for tests written against 0.1
    assert update.args["duplicate_rules"] is None
    assert update.args["allow_save"] is None
    assert fake.calls_for("collections.create")[0].args["duplicate_rules"] == rules

    async def awaited() -> Any:
        return await fake.sobject("Lead").upsert(EXT, "a", {"LastName": "x"}, allow_save=False)

    with pytest.warns(esf.EsfDeprecationWarning) as caught:
        h.run(
            awaited()
            if kind == "async"
            else fake.sobject("Lead").upsert(EXT, "a", {"LastName": "x"}, allow_save=False)
        )
    assert caught.list[0].filename == __file__
    assert fake.calls_for("sobject.upsert")[0].args["duplicate_rules"] == esf.DuplicateRules()
    with pytest.raises(TypeError):
        h.run(fake.collections.update("Lead", [{"Id": rid}], allow_save=True, duplicate_rules=rules))
    h.loop.close()
