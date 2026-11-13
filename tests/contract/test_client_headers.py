"""Client-wide request headers and the ``Warning`` response header (D30, ADR 0012).

- ``update_mru=`` → ``Sforce-Mru`` on the resources the REST API Developer Guide documents it for
  (*MRU Header*: https://developer.salesforce.com/docs/platform/api-rest/guide/headers-mru.html).
- ``default_namespace=`` → ``defaultNamespace=`` in ``Sforce-Call-Options`` (*Call Options Header*:
  https://developer.salesforce.com/docs/atlas.en-us.api_rest.meta/api_rest/headers_calloptions.htm).
- ``Warning: 299 - "<text>"`` → ``esf.SalesforceWarning`` once per text per client family, and
  ``sf_warning`` on the ``sf.request`` event (*Warning Header*:
  https://developer.salesforce.com/docs/platform/api-rest/guide/headers-warning.html). The
  response is **synthetic** (``tests/fixtures/recorded/misc/warning_header.json``): the
  sandbox org sent no ``Warning`` header to record.
"""

from __future__ import annotations

import json
import warnings

import pytest
import respx

import even_simpler_salesforce as esf

from ..conftest import DATA, INSTANCE, Harness, Recorder
from ..fixtures import RECORDED, is_synthetic

LEAD = "00Q000000000001AAA"
WARNING_FIXTURE = "misc/warning_header.json"


def _warning_header() -> str:
    assert is_synthetic(WARNING_FIXTURE)
    return json.loads((RECORDED / WARNING_FIXTURE).read_text(encoding="utf-8"))["headers"]["Warning"]


# --- update_mru ---------------------------------------------------------------------------


@pytest.mark.parametrize(("value", "header"), [(False, "updateMru=false"), (True, "updateMru=true")])
def test_update_mru_on_sobject_rows(h: Harness, mock: respx.MockRouter, value: bool, header: str) -> None:
    get = mock.get(f"{DATA}/sobjects/Lead/{LEAD}").respond(json={"Id": LEAD})
    ext = mock.get(f"{DATA}/sobjects/Lead/Ext__c/A1").respond(json={"Id": LEAD})
    create = mock.post(f"{DATA}/sobjects/Lead/").respond(201, json={"id": LEAD, "success": True})
    update = mock.patch(f"{DATA}/sobjects/Lead/{LEAD}").respond(204)
    upsert = mock.patch(f"{DATA}/sobjects/Lead/Ext__c/A1").respond(201, json={"id": LEAD, "created": True})
    lead = h.client(update_mru=value).sobject("Lead")
    h.run(lead.get(LEAD))
    h.run(lead.get_by_external_id("Ext__c", "A1"))
    h.run(lead.create({"LastName": "x"}))
    h.run(lead.update(LEAD, {"LastName": "y"}))
    h.run(lead.upsert("Ext__c", "A1", {"LastName": "z"}))
    for route in (get, ext, create, update, upsert):
        assert route.calls.last.request.headers["Sforce-Mru"] == header


def test_update_mru_not_sent_on_query_delete_or_by_default(h: Harness, mock: respx.MockRouter) -> None:
    query = mock.get(f"{DATA}/query").respond(json={"totalSize": 0, "done": True, "records": []})
    delete = mock.delete(f"{DATA}/sobjects/Lead/{LEAD}").respond(204)
    get = mock.get(f"{DATA}/sobjects/Lead/{LEAD}").respond(json={"Id": LEAD})
    sf = h.client(update_mru=False)
    h.run(sf.query("SELECT Id FROM Lead"))
    h.run(sf.sobject("Lead").delete(LEAD))
    assert "Sforce-Mru" not in query.calls.last.request.headers
    assert "Sforce-Mru" not in delete.calls.last.request.headers
    h.run(h.client().sobject("Lead").get(LEAD))
    assert "Sforce-Mru" not in get.calls.last.request.headers


def test_update_mru_shared_by_children(h: Harness, mock: respx.MockRouter) -> None:
    route = mock.get(f"{DATA}/sobjects/Lead/{LEAD}").respond(json={"Id": LEAD})
    sf = h.client(update_mru=False)
    h.run(sf.tagged(caller="job").sobject("Lead").get(LEAD))
    assert route.calls.last.request.headers["Sforce-Mru"] == "updateMru=false"


@pytest.mark.parametrize("version", ["59.0", "59.9"])
def test_update_mru_needs_api_60(h: Harness, version: str) -> None:
    with pytest.raises(ValueError, match="update_mru"):
        h.client(update_mru=False, api_version=version)
    h.client(api_version=version)  # without update_mru the old version is fine
    h.client(update_mru=True, api_version="60.0")


# --- default_namespace --------------------------------------------------------------------


def test_default_namespace_merged_with_client_name(h: Harness, mock: respx.MockRouter) -> None:
    route = mock.get(f"{DATA}/limits").respond(json={})
    h.run(h.client(client_name="acme", default_namespace="battle").limits())
    assert route.calls.last.request.headers["Sforce-Call-Options"] == "client=acme; defaultNamespace=battle"
    h.run(h.client(default_namespace="battle").limits())
    assert route.calls.last.request.headers["Sforce-Call-Options"] == "defaultNamespace=battle"


def test_caller_namespace_wins_for_one_call(h: Harness, mock: respx.MockRouter) -> None:
    route = mock.get(f"{DATA}/limits").respond(json={})
    sf = h.client(client_name="acme", default_namespace="battle")
    h.run(sf.request("GET", "limits", headers={"Sforce-Call-Options": "defaultNamespace=other"}))
    assert route.calls.last.request.headers["Sforce-Call-Options"] == "client=acme; defaultNamespace=other"


@pytest.mark.parametrize("bad", ["", "1bad", "a__b", "x" * 16, "a-b"])
def test_default_namespace_validated(h: Harness, bad: str) -> None:
    with pytest.raises(ValueError, match="default_namespace"):
        h.client(default_namespace=bad)


# --- Warning → SalesforceWarning ----------------------------------------------------------


def test_salesforce_warning_is_a_user_warning() -> None:
    assert issubclass(esf.SalesforceWarning, UserWarning)
    assert "SalesforceWarning" in esf.__all__


def test_warning_header_warns_once_per_family(h: Harness, mock: respx.MockRouter) -> None:
    header = _warning_header()
    mock.get(f"{DATA}/limits").respond(json={}, headers={"Warning": header})
    rec = Recorder()
    sf = h.client(recorder=rec)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        h.run(sf.limits())
        h.run(sf.limits())
        h.run(sf.with_auth(esf.StaticToken("AT-other", instance_url=INSTANCE)).limits())
    ours = [w for w in caught if issubclass(w.category, esf.SalesforceWarning)]
    text = (
        "This API is deprecated and will be removed by Summer '22. "
        "Please see https://help.salesforce.com/articleView?id=000351312 for details."
    )
    assert [str(w.message) for w in ours] == [text]
    assert [e["sf_warning"] for e in rec.requests] == [text, text, text]


def test_warning_dedup_is_per_family_and_per_text(h: Harness, mock: respx.MockRouter) -> None:
    route = mock.get(f"{DATA}/limits")
    route.side_effect = [
        respx.MockResponse(200, json={}, headers={"Warning": '299 - "one"'}),
        respx.MockResponse(200, json={}, headers={"Warning": '299 - "two"'}),
        respx.MockResponse(200, json={}, headers={"Warning": '299 - "one"'}),
    ]
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        h.run(h.client().limits())
        sf = h.client()
        h.run(sf.limits())
        h.run(sf.limits())
    assert [str(w.message) for w in caught if w.category is esf.SalesforceWarning] == ["one", "two", "one"]


def test_warning_on_error_response(h: Harness, mock: respx.MockRouter) -> None:
    mock.get(f"{DATA}/sobjects/Lead/{LEAD}").respond(
        404,
        json=[{"errorCode": "NOT_FOUND", "message": "gone"}],
        headers={"Warning": '299 - "old version"'},
    )
    rec = Recorder()
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        with pytest.raises(esf.NotFound):
            h.run(h.client(recorder=rec).sobject("Lead").get(LEAD))
    assert [str(w.message) for w in caught if w.category is esf.SalesforceWarning] == ["old version"]
    assert rec.requests[-1]["sf_warning"] == "old version"


def test_no_warning_field_without_header(h: Harness, mock: respx.MockRouter) -> None:
    mock.get(f"{DATA}/limits").respond(json={})
    rec = Recorder()
    with warnings.catch_warnings():
        warnings.simplefilter("error", esf.SalesforceWarning)
        h.run(h.client(recorder=rec).limits())
    assert rec.requests[-1]["sf_warning"] is None


def test_warning_can_be_escalated(h: Harness, mock: respx.MockRouter) -> None:
    mock.get(f"{DATA}/limits").respond(json={}, headers={"Warning": '299 - "escalate me"'})
    with warnings.catch_warnings():
        warnings.simplefilter("error", esf.SalesforceWarning)
        with pytest.raises(esf.SalesforceWarning, match="escalate me"):
            h.run(h.client().limits())
