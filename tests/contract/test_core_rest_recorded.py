"""Core REST behaviour replayed from real-org recordings (``tests/sandbox/test_core_rest.py``).

Covers the external-id fallback (V19), the Collections write headers (V24), DELETE with a
precondition (V25), and an ``api_version`` the org doesn't offer (V21), on both clients.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
import respx

import even_simpler_salesforce as esf
from even_simpler_salesforce.testing import replay

from ..conftest import DATA, Harness, Recorder
from ..fixtures import RECORDED, load_fixture, load_interactions

LEAD_ID = "00Q000000000001EAA"
EXT = "Esf_Ext__c"


def _replaying(h: Harness, name: str, **kw: Any) -> Any:
    transport = replay(RECORDED / name)
    http = httpx.AsyncClient(transport=transport) if h.is_async else httpx.Client(transport=transport)
    return h.client(http_client=http, **kw), transport


# --- V19 -------------------------------------------------------------------------------------


def test_external_id_suffix_fallback_replayed(h: Harness, rec: Recorder) -> None:
    """A value ending in ``.json`` goes through Collections upsert and a SOQL lookup."""
    sf, transport = _replaying(h, "sobject/external_id_suffix.json", recorder=rec)
    lead = sf.sobject("Lead")
    value = "esf-sandbox-x.json"
    assert h.run(lead.upsert(EXT, value, {"LastName": "x", "Company": "esf-sandbox"})) == esf.UpsertResult(
        LEAD_ID, True
    )
    assert h.run(lead.upsert(EXT, value, {"FirstName": "Esf"})) == esf.UpsertResult(LEAD_ID, False)
    got = h.run(lead.get_by_external_id(EXT, value, ["Id", "FirstName", EXT]))
    assert got["Id"] == LEAD_ID
    full = h.run(lead.get_by_external_id(EXT, value))
    assert full["attributes"]["type"] == "Lead"
    with pytest.raises(esf.NotFound) as nf:
        h.run(lead.get_by_external_id(EXT, "missing.json"))
    assert (nf.value.error_code, nf.value.status) == ("NOT_FOUND", 404)
    assert nf.value.operation == "sobject.get_by_external_id"
    assert nf.value.path == f"/sobjects/Lead/{EXT}/:value"
    with pytest.raises(esf.InvalidField) as inv:  # rejected locally, like the path upsert does
        h.run(lead.upsert(EXT, value, {"esf_ext__C": value}))
    assert inv.value.error_code == "INVALID_FIELD"
    assert inv.value.operation == "sobject.upsert"
    with pytest.raises(esf.ValidationRuleFailed) as vr:
        h.run(lead.upsert(EXT, "esf-sandbox-vr.json", {"LastName": "x", "Company": "ESF_VR"}))
    assert vr.value.message == "esf-sandbox: validation rule probe"
    assert vr.value.status == 400
    assert vr.value.operation == "sobject.upsert"
    h.run(lead.delete(LEAD_ID))
    assert transport.handler.remaining == 0  # type: ignore[attr-defined]
    ops = [(e["operation"], e["path_template"]) for e in rec.requests]
    assert ops[:3] == [
        ("sobject.upsert", f"/sobjects/Lead/{EXT}/:value"),
        ("sobject.upsert", f"/sobjects/Lead/{EXT}/:value"),
        ("sobject.get_by_external_id", "/query"),
    ]
    assert all(value not in repr(e.fields) for e in rec.requests)  # SOQL literal not logged


def test_external_id_suffix_upsert_request(h: Harness, mock: respx.MockRouter) -> None:
    """The Collections body carries the value and the headers are forwarded."""
    route = mock.patch(f"{DATA}/composite/sobjects/Lead/{EXT}").respond(
        json=load_interactions("sobject/external_id_suffix.json")[0]["response"]["json"]
    )
    sf = h.client()
    out = h.run(
        sf.sobject("Lead").upsert(
            EXT, "a/b.XML", {"LastName": "x"}, assignment_rule_id=False, allow_save=True
        )
    )
    assert out == esf.UpsertResult(LEAD_ID, True)
    req = route.calls.last.request
    assert json.loads(req.content) == {
        "allOrNone": False,
        "records": [{"attributes": {"type": "Lead"}, "LastName": "x", EXT: "a/b.XML"}],
    }
    assert req.headers["Sforce-Auto-Assign"] == "FALSE"
    assert req.headers["Sforce-Duplicate-Rule-Header"].startswith("allowSave=true")


def test_external_id_suffix_multiple_matches(h: Harness, mock: respx.MockRouter) -> None:
    """Several matches raise ``MultipleMatches`` with record URLs, as HTTP 300 does on the path."""
    a, b = "00Qxx0000000001AAA", "00Qxx0000000002AAA"
    page = {"totalSize": 2, "done": True, "records": [{"Id": a}, {"Id": b}]}
    mock.get(f"{DATA}/query").respond(json=page)
    dup = [
        {
            "success": False,
            "created": False,
            "errors": [
                {
                    "statusCode": "DUPLICATE_EXTERNAL_ID",
                    "message": f"ESF Ext Multi: more than one record found for external id field: [{a}, {b}]",
                    "fields": ["Esf_Ext_Multi__c"],
                }
            ],
        }
    ]  # shape as returned by the org (tests/sandbox/test_core_rest.py)
    mock.patch(f"{DATA}/composite/sobjects/Lead/Esf_Ext_Multi__c").respond(json=dup)
    sf = h.client()
    lead = sf.sobject("Lead")
    urls = (f"/services/data/v68.0/sobjects/Lead/{a}", f"/services/data/v68.0/sobjects/Lead/{b}")
    with pytest.raises(esf.MultipleMatches) as g:
        h.run(lead.get_by_external_id("Esf_Ext_Multi__c", "v.html"))
    with pytest.raises(esf.MultipleMatches) as u:
        h.run(lead.upsert("Esf_Ext_Multi__c", "v.html", {"FirstName": "x"}))
    for err in (g.value, u.value):
        assert err.matches == urls
        assert err.status == 300
        assert err.path == "/sobjects/Lead/Esf_Ext_Multi__c/:value"


def test_plain_values_keep_the_path(h: Harness, mock: respx.MockRouter) -> None:
    """Values that don't end in ``.json``/``.xml``/``.html`` (any case) keep the path."""
    get = mock.get(f"{DATA}/sobjects/Lead/{EXT}/a.csv").respond(json={"Id": LEAD_ID})
    sf = h.client()
    assert h.run(sf.sobject("Lead").get_by_external_id(EXT, "a.csv")) == {"Id": LEAD_ID}
    assert get.called


def test_tooling_fallback_uses_tooling_paths(h: Harness, mock: respx.MockRouter) -> None:
    q = mock.get(f"{DATA}/tooling/query").respond(json={"totalSize": 0, "done": True, "records": []})
    sf = h.client()
    with pytest.raises(esf.NotFound) as ei:
        h.run(sf.tooling.sobject("ApexClass").get_by_external_id("Name", "x.json"))
    assert q.called
    assert ei.value.operation == "tooling.sobject.get_by_external_id"


def test_fallback_unexpected_bodies(h: Harness, mock: respx.MockRouter) -> None:
    mock.get(f"{DATA}/query").respond(json={"oops": True})
    mock.patch(f"{DATA}/composite/sobjects/Lead/{EXT}").respond(json=[{"success": False, "errors": []}])
    sf = h.client()
    with pytest.raises(esf.UnexpectedResponse):
        h.run(sf.sobject("Lead").get_by_external_id(EXT, "x.json"))
    with pytest.raises(esf.UnexpectedResponse):
        h.run(sf.sobject("Lead").upsert(EXT, "x.json", {"FirstName": "x"}))


# --- V24 -------------------------------------------------------------------------------------


@pytest.mark.parametrize("method", ["create", "update", "upsert"])
def test_collections_write_headers(h: Harness, mock: respx.MockRouter, method: str) -> None:
    ok = [{"id": LEAD_ID, "success": True, "errors": [], "created": True}]
    route = mock.route(path__startswith="/services/data/v68.0/composite/sobjects").respond(json=ok)
    sf = h.client()
    rule = "01Qxx0000000001AAA"
    call = {
        "create": lambda: sf.collections.create(
            [{"LastName": "x"}], sobject="Lead", assignment_rule_id=rule, allow_save=True
        ),
        "update": lambda: sf.collections.update(
            "Lead", [{"Id": LEAD_ID}], assignment_rule_id=True, allow_save=True
        ),
        "upsert": lambda: sf.collections.upsert(
            "Lead", EXT, [{EXT: "a"}], assignment_rule_id=True, allow_save=True
        ),
    }[method]
    h.run(call())
    headers = route.calls.last.request.headers
    assert headers["Sforce-Auto-Assign"] == (rule if method == "create" else "TRUE")
    assert headers["Sforce-Duplicate-Rule-Header"] == (
        "allowSave=true; includeRecordDetails=false; runAsCurrentUser=false"
    )
    h.run(sf.collections.create([{"LastName": "x"}], sobject="Lead"))
    assert "Sforce-Auto-Assign" not in route.calls.last.request.headers
    assert "Sforce-Duplicate-Rule-Header" not in route.calls.last.request.headers


def test_collections_duplicate_alert_replayed(h: Harness) -> None:
    sf, transport = _replaying(h, "collections/duplicate_alert.json")
    row = {"LastName": "x", "Company": "esf-sandbox-dup"}
    blocked = h.run(sf.collections.create([row], sobject="Lead"))
    assert [e.error_code for e in blocked.results[0].errors] == ["DUPLICATES_DETECTED"]
    saved = h.run(sf.collections.create([row], sobject="Lead", allow_save=True))
    assert not saved.failed
    assert transport.handler.remaining == 0  # type: ignore[attr-defined]


def test_collections_assignment_rule_id_validated(h: Harness) -> None:
    sf = h.client()
    with pytest.raises(ValueError):
        h.run(sf.collections.create([{"LastName": "x"}], sobject="Lead", assignment_rule_id="../x"))


# --- V25 / V21 -------------------------------------------------------------------------------


def test_delete_with_precondition_is_rejected(h: Harness, mock: respx.MockRouter) -> None:
    """Recorded: Salesforce rejects ``If-Unmodified-Since`` on DELETE (500 or 400), no delete."""
    server, bad = (i["response"] for i in load_interactions("errors/delete_if_unmodified_since.json"))
    mock.delete(f"{DATA}/sobjects/Lead/{LEAD_ID}").mock(
        side_effect=[
            httpx.Response(server["status"], json=server["json"]),
            httpx.Response(bad["status"], json=bad["json"]),
        ]
    )
    sf = h.client(retry=esf.RetryPolicy(max_attempts=1))
    hdr = {"If-Unmodified-Since": "Sun, 13 Nov 2016 23:00:33 GMT"}
    with pytest.raises(esf.ServerError) as e1:
        h.run(sf.request("DELETE", f"sobjects/Lead/{LEAD_ID}", headers=hdr))
    assert e1.value.error_code == "UNKNOWN_EXCEPTION"
    with pytest.raises(esf.RequestError) as e2:
        h.run(sf.request("DELETE", f"sobjects/Lead/{LEAD_ID}", headers=hdr))
    assert e2.value.error_code == "BAD_HEADER"


def test_api_version_not_offered(h: Harness, mock: respx.MockRouter) -> None:
    """Recorded: a version newer than the org's answers 404 NOT_FOUND on every resource."""
    body = load_fixture("errors/api_version_not_offered.json")
    mock.get("https://acme.my.salesforce.com/services/data/v69.0/limits").respond(404, json=body)
    sf = h.client(api_version="69.0")
    with pytest.raises(esf.NotFound) as ei:
        h.run(sf.limits())
    assert ei.value.error_code == "NOT_FOUND"
