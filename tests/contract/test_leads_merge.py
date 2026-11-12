"""Lead convert and merge over Partner SOAP (§9.8, D14), for both clients.

The responses in ``tests/fixtures/recorded/soap`` are redacted recordings from a Developer
Edition org (API v68.0, 2026-11-12, ``tests/sandbox/test_files_soap.py``; big-bang.md §21 V11,
V35): convert and merge with an OAuth access token as the session id, a call-level fault
(HTTP 500) and a bogus-session fault (HTTP 500, not 401). Envelopes written inline in a test are
shaped after them. Sources:

- convertLead(): https://developer.salesforce.com/docs/atlas.en-us.api.meta/api/sforce_api_calls_convertlead.htm
  (``LeadConvert[]``, 100 maximum; results in input order)
- LeadConvertResult: https://developer.salesforce.com/docs/atlas.en-us.api.meta/api/sforce_api_calls_convertlead_leadconvertresult.htm
- merge(): https://developer.salesforce.com/docs/atlas.en-us.api.meta/api/sforce_api_calls_merge.htm
  (master plus one or two records to merge)
- Error handling: https://developer.salesforce.com/docs/atlas.en-us.api.meta/api/sforce_api_concepts_errorhandling.htm
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from typing import Any

import httpx
import pytest
import respx

import even_simpler_salesforce as esf

from ..conftest import API, INSTANCE, Harness, Recorder, cc_auth, make_token, token_body
from ..fixtures import recorded_response

SOAP = f"{INSTANCE}/services/Soap/u/{API}"
TOKEN_URL = f"{INSTANCE}/services/oauth2/token"
P = "{urn:partner.soap.sforce.com}"
SOBJ = "{urn:sobject.partner.soap.sforce.com}"
ENV = "{http://schemas.xmlsoap.org/soap/envelope/}"
LEAD = "00Q000000000001EAA"  # ids as in the recordings
ACC = "001000000000002AAA"
CONTACT = "003000000000003AAA"


def xml(name: str) -> httpx.Response:
    """The recorded SOAP response ``tests/fixtures/recorded/soap/<name>`` (status as recorded)."""
    status, headers, body = recorded_response(f"soap/{name}")
    return httpx.Response(status, content=body, headers=headers)


def sent(request: httpx.Request) -> ET.Element:
    return ET.fromstring(request.content)


def session_of(request: httpx.Request) -> str | None:
    el = sent(request).find(f"{ENV}Header/{P}SessionHeader/{P}sessionId")
    return el.text if el is not None else None


def convert_echo(request: httpx.Request) -> httpx.Response:
    """Succeed for every LeadConvert in the request, echoing its lead id."""
    leads = [c.findtext(f"{P}leadId") for c in sent(request).iter(f"{P}leadConverts")]
    results = "".join(
        f"<result><accountId>{ACC}</accountId><contactId>003xx0000000001AAA</contactId>"
        f"<leadId>{lead}</leadId><opportunityId/><success>true</success></result>"
        for lead in leads
    )
    body = (
        '<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" '
        f'xmlns="urn:partner.soap.sforce.com"><soapenv:Body><convertLeadResponse>{results}'
        "</convertLeadResponse></soapenv:Body></soapenv:Envelope>"
    )
    return httpx.Response(200, content=body.encode(), headers={"Content-Type": "text/xml; charset=utf-8"})


def lead_id(n: int) -> str:
    return f"00Qxx{n:013d}"


def test_convert_sends_envelope_with_token_and_parses_result(
    h: Harness, mock: respx.MockRouter, rec: Recorder
) -> None:
    """Recorded ``soap/convert_lead_one.json`` (V11: an OAuth access token works as the session id)."""
    route = mock.post(SOAP).mock(return_value=xml("convert_lead_one.json"))
    sf = h.client(recorder=rec)
    res = h.run(
        sf.leads.convert(
            LEAD,
            converted_status="Closed - Converted",
            account_id=ACC,
            do_not_create_opportunity=True,
            send_email=True,
        )
    )
    assert res == esf.LeadConvertResult(
        ok=True, lead_id=LEAD, account_id=ACC, contact_id=CONTACT, opportunity_id=None
    )
    req = route.calls[0].request
    assert "authorization" not in req.headers  # the token travels only in SessionHeader
    assert req.headers["content-type"].startswith("text/xml")
    assert req.headers["soapaction"] == '""'
    assert session_of(req) == make_token().access_token
    conv = sent(req).find(f"{ENV}Body/{P}convertLead/{P}leadConverts")
    assert conv is not None
    assert [(c.tag.removeprefix(P), c.text) for c in conv] == [
        ("accountId", ACC),
        ("convertedStatus", "Closed - Converted"),
        ("doNotCreateOpportunity", "true"),
        ("leadId", LEAD),
        ("overwriteLeadSource", "false"),
        ("sendNotificationEmail", "true"),
    ]
    (event,) = rec.requests
    assert event["operation"] == "leads.convert"
    assert event["outcome"] == "ok"
    assert event["path_template"] == f"/services/Soap/u/{API}"
    assert make_token().access_token not in repr(event.fields)


def test_convert_failure_raises_typed_record_error(h: Harness, mock: respx.MockRouter) -> None:
    """Recorded ``soap/convert_lead_failed.json``: ``success=false`` in an HTTP 200, by statusCode."""
    mock.post(SOAP).mock(return_value=xml("convert_lead_failed.json"))
    sf = h.client()
    with pytest.raises(esf.RecordError) as ei:
        h.run(sf.leads.convert(LEAD, converted_status="esf-no-such-status"))
    assert ei.value.error_code == "INVALID_STATUS"
    assert ei.value.message == "invalid convertedStatus: esf-no-such-status"
    assert ei.value.fields == ("Status",)
    assert ei.value.operation == "leads.convert"
    assert ei.value.sobject == "Lead"


def test_convert_many_keeps_order_and_per_row_errors(h: Harness, mock: respx.MockRouter) -> None:
    """Recorded ``soap/convert_lead.json``: one success (with an opportunity), one failure."""
    mock.post(SOAP).mock(return_value=xml("convert_lead.json"))
    sf = h.client()
    second = "00Q000000000002EAA"
    batch = h.run(
        sf.leads.convert_many(
            [
                esf.LeadConvert(LEAD, "Closed - Converted", opportunity_name="Deal"),
                esf.LeadConvert(second, "Open - Not Contacted"),
            ]
        )
    )
    assert isinstance(batch, esf.BatchResult)
    assert [r.ok for r in batch] == [True, False]
    assert batch[0].opportunity_id == "006000000000005AAA"
    assert batch[1].lead_id == second  # Salesforce echoes the lead id of a failed row too
    assert (batch[1].errors[0].error_code, batch[1].errors[0].fields) == ("INVALID_STATUS", ("Status",))
    with pytest.raises(esf.PartialFailure):
        batch.raise_for_errors()


def test_convert_many_chunks_at_100(h: Harness, mock: respx.MockRouter, rec: Recorder) -> None:
    route = mock.post(SOAP).mock(side_effect=convert_echo)
    sf = h.client(recorder=rec)
    items = [esf.LeadConvert(lead_id(i), "Closed - Converted") for i in range(250)]
    batch = h.run(sf.leads.convert_many(items))
    assert [r.lead_id for r in batch] == [lead_id(i) for i in range(250)]
    sizes = [len(list(sent(c.request).iter(f"{P}leadConverts"))) for c in route.calls]
    assert sizes == [100, 100, 50]
    assert [e["records"] for e in rec.requests] == [100, 100, 50]
    assert h.run(sf.leads.convert_many([])).results == []


def test_convert_many_partial_chunk_failure(h: Harness, mock: respx.MockRouter) -> None:
    calls = {"n": 0}

    def second_fails(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 2:
            return xml("fault_invalid_type.json")
        return convert_echo(request)

    mock.post(SOAP).mock(side_effect=second_fails)
    sf = h.client()
    items = [esf.LeadConvert(lead_id(i), "Closed - Converted") for i in range(150)]
    with pytest.raises(esf.PartialFailure) as ei:
        h.run(sf.leads.convert_many(items))
    results = ei.value.results
    assert results is not None
    assert len(results) == 150
    assert all(r.ok for r in results.results[:100])
    failed = results.results[100]
    assert not failed.ok
    assert failed.lead_id == lead_id(100)
    assert failed.errors[0].extended["outcome"] == "rejected"
    assert isinstance(ei.value.__cause__, esf.SoapFault)


def test_convert_many_all_chunks_failing_raises_first(h: Harness, mock: respx.MockRouter) -> None:
    mock.post(SOAP).mock(return_value=xml("fault_invalid_type.json"))
    sf = h.client()
    with pytest.raises(esf.SoapFault):
        h.run(sf.leads.convert_many([esf.LeadConvert(LEAD, "Closed - Converted")]))


def test_unknown_fault_is_soap_fault_and_not_retried(h: Harness, mock: respx.MockRouter) -> None:
    """Recorded ``soap/fault_invalid_type.json`` (a merge of Opportunity records, sent raw).

    SOAP faults are HTTP 500 with ``detail/*Fault/exceptionCode``; a write is never resent.
    """
    route = mock.post(SOAP).mock(return_value=xml("fault_invalid_type.json"))
    sf = h.client()
    with pytest.raises(esf.SoapFault) as ei:
        h.run(sf.merge(LEAD, ["00Q000000000002EAA"], sobject="Lead"))
    err = ei.value
    assert err.error_code == "INVALID_TYPE_FOR_OPERATION"
    assert err.fault_code == "sf:INVALID_TYPE_FOR_OPERATION"
    assert err.message == "entity type does not support merge"
    assert err.status == 500
    assert err.retryable is False
    assert err.errors[0].extended["fault_type"] == "UnexpectedErrorFault"
    assert route.call_count == 1


def test_invalid_session_fault_refreshes_once_with_new_envelope(h: Harness, mock: respx.MockRouter) -> None:
    """Recorded ``soap/fault_invalid_session.json`` (HTTP 500, not 401): refresh once, resend."""
    route = mock.post(SOAP).mock(
        side_effect=[xml("fault_invalid_session.json"), xml("convert_lead_one.json")]
    )
    mock.post(TOKEN_URL).respond(json=token_body("AT-refreshed-0002"))
    sf = h.client(cc_auth())
    res = h.run(sf.leads.convert(LEAD, converted_status="Closed - Converted"))
    assert res.ok
    assert [session_of(c.request) for c in route.calls] == ["AT-initial-0001", "AT-refreshed-0002"]


def test_invalid_session_with_static_token_raises(h: Harness, mock: respx.MockRouter) -> None:
    mock.post(SOAP).mock(return_value=xml("fault_invalid_session.json"))
    sf = h.client(esf.StaticToken("AT-static-0001", INSTANCE))
    with pytest.raises(esf.SessionExpired) as ei:
        h.run(sf.merge(ACC, ["001xx0000000002AAA"], sobject="Account"))
    assert ei.value.error_code == "INVALID_SESSION_ID"
    assert "AT-static-0001" not in str(ei.value)


def test_session_id_is_escaped_in_envelope(h: Harness, mock: respx.MockRouter) -> None:
    route = mock.post(SOAP).mock(return_value=xml("merge.json"))
    sf = h.client(esf.StaticToken("00Dxx!<&>", INSTANCE))
    h.run(sf.merge(ACC, ["001xx0000000002AAA", "001xx0000000003AAA"], sobject="Account"))
    assert session_of(route.calls[0].request) == "00Dxx!<&>"


def test_merge_sends_master_and_duplicates(h: Harness, mock: respx.MockRouter, rec: Recorder) -> None:
    """Recorded ``soap/merge.json``: a master Lead and two merged Leads."""
    route = mock.post(SOAP).mock(return_value=xml("merge.json"))
    sf = h.client(recorder=rec)
    dups = ["00Q000000000002EAA", "00Q000000000003EAA"]
    res = h.run(sf.merge(LEAD, dups, sobject="Lead"))
    assert res == esf.MergeResult(ok=True, id=LEAD, merged_ids=tuple(dups), updated_related_ids=())
    req = sent(route.calls[0].request).find(f"{ENV}Body/{P}merge/{P}request")
    assert req is not None
    assert req.findtext(f"{P}masterRecord/{SOBJ}type") == "Lead"
    assert req.findtext(f"{P}masterRecord/{SOBJ}Id") == LEAD
    assert [e.text for e in req.findall(f"{P}recordToMergeIds")] == dups
    assert rec.requests[0]["operation"] == "merge"
    assert rec.requests[0]["sobject"] == "Lead"


def test_merge_failure_raises_typed_error(h: Harness, mock: respx.MockRouter) -> None:
    """Recorded ``soap/merge_failed.json``: merging an already merged (deleted) record."""
    mock.post(SOAP).mock(return_value=xml("merge_failed.json"))
    sf = h.client()
    with pytest.raises(esf.NotFound) as ei:
        h.run(sf.merge(LEAD, ["00Q000000000002EAA"], sobject="Lead"))
    assert ei.value.error_code == "ENTITY_IS_DELETED"
    assert ei.value.operation == "merge"


@pytest.mark.parametrize(
    ("master", "dups", "sobject", "exc", "match"),
    [
        (ACC, ["001xx0000000002AAA"], "Opportunity", ValueError, "merge supports"),
        (ACC, [], "Account", ValueError, "1 to 2"),
        (
            ACC,
            ["001xx0000000002AAA", "001xx0000000003AAA", "001xx0000000004AAA"],
            "Account",
            ValueError,
            "1 to 2",
        ),
        (ACC, [ACC], "Account", ValueError, "distinct"),
        (ACC, ["001xx0000000002AAA", "001xx0000000002"], "Account", ValueError, "distinct"),
        (ACC, "001xx0000000002AAA", "Account", TypeError, "list of ids"),
        ("bad", ["001xx0000000002AAA"], "Account", ValueError, "invalid Salesforce id"),
        (ACC, ["001xx0000000002AAA"], "Bad Name", ValueError, "invalid sObject"),
    ],
)
def test_merge_validates_before_sending(
    h: Harness, mock: respx.MockRouter, master: str, dups: Any, sobject: str, exc: type[Exception], match: str
) -> None:
    route = mock.post(SOAP).mock(return_value=xml("merge.json"))
    sf = h.client()
    with pytest.raises(exc, match=match):
        h.run(sf.merge(master, dups, sobject=sobject))
    assert route.call_count == 0


def test_convert_validates_before_sending(h: Harness, mock: respx.MockRouter) -> None:
    route = mock.post(SOAP).mock(return_value=xml("convert_lead_one.json"))
    sf = h.client()
    with pytest.raises(ValueError, match="invalid Salesforce id"):
        h.run(sf.leads.convert("nope", converted_status="Closed - Converted"))
    with pytest.raises(ValueError, match="converted_status"):
        h.run(sf.leads.convert(LEAD, converted_status=""))
    with pytest.raises(ValueError, match="needs account_id"):
        h.run(sf.leads.convert(LEAD, converted_status="x", contact_id="003xx0000000001AAA"))
    with pytest.raises(TypeError, match="LeadConvert"):
        h.run(sf.leads.convert_many([{"lead_id": LEAD}]))  # type: ignore[list-item]
    assert route.call_count == 0


def test_wrong_result_count_is_unexpected_response(h: Harness, mock: respx.MockRouter) -> None:
    mock.post(SOAP).mock(return_value=xml("convert_lead_one.json"))
    sf = h.client()
    items = [esf.LeadConvert(LEAD, "x"), esf.LeadConvert(lead_id(2), "x")]
    with pytest.raises(esf.UnexpectedResponse, match="expected 2 results"):
        h.run(sf.leads.convert_many(items))


def test_html_error_page_is_unexpected_response(h: Harness, mock: respx.MockRouter) -> None:
    mock.post(SOAP).respond(503, html="<html><body>Maintenance</body></html>")
    sf = h.client()
    with pytest.raises(esf.UnexpectedResponse) as ei:
        h.run(sf.merge(ACC, ["001xx0000000002AAA"], sobject="Account"))
    assert ei.value.status == 503


def test_doctype_in_response_is_refused(h: Harness, mock: respx.MockRouter) -> None:
    evil = (
        '<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa"><!ENTITY b "&a;&a;&a;">]>'
        '<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/">'
        "<soapenv:Body>&b;</soapenv:Body></soapenv:Envelope>"
    )
    mock.post(SOAP).respond(200, content=evil.encode(), headers={"Content-Type": "text/xml"})
    sf = h.client()
    with pytest.raises(esf.UnexpectedResponse, match="DTD"):
        h.run(sf.merge(ACC, ["001xx0000000002AAA"], sobject="Account"))


def test_session_id_never_in_debug_events(
    h: Harness, mock: respx.MockRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ESF_DEBUG", "2")
    mock.post(SOAP).mock(return_value=xml("fault_invalid_type.json"))
    rec = Recorder()
    sf = h.client(recorder=rec)
    with pytest.raises(esf.SoapFault) as ei:
        h.run(sf.merge(ACC, ["001xx0000000002AAA"], sobject="Account"))
    token = make_token().access_token
    assert not any(token in repr(e.fields) for e in rec.events)
    assert token not in repr(ei.value.__dict__)
    attempt = rec.named("sf.debug.attempt")[0]
    assert "Authorization" not in attempt["request_headers"]
    assert re.search(r"/services/Soap/u/\d+\.\d", attempt["path_template"])


def test_failed_result_without_details_is_unexpected(h: Harness, mock: respx.MockRouter) -> None:
    body = (
        '<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" '
        'xmlns="urn:partner.soap.sforce.com"><soapenv:Body><mergeResponse><result>'
        "<success>false</success></result></mergeResponse></soapenv:Body></soapenv:Envelope>"
    )
    mock.post(SOAP).respond(200, content=body.encode(), headers={"Content-Type": "text/xml"})
    sf = h.client()
    with pytest.raises(esf.UnexpectedResponse, match="without error details"):
        h.run(sf.merge(ACC, ["001xx0000000002AAA"], sobject="Account"))


def test_server_side_fault_marks_rows_unknown(h: Harness, mock: respx.MockRouter) -> None:
    fault = (
        '<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" '
        'xmlns:sf="urn:fault.partner.soap.sforce.com"><soapenv:Body><soapenv:Fault>'
        "<faultcode>sf:UNKNOWN_EXCEPTION</faultcode><faultstring>UNKNOWN_EXCEPTION: oops</faultstring>"
        "</soapenv:Fault></soapenv:Body></soapenv:Envelope>"
    )
    calls = {"n": 0}

    def second_fails(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 2:
            return httpx.Response(500, content=fault.encode(), headers={"Content-Type": "text/xml"})
        return convert_echo(request)

    mock.post(SOAP).mock(side_effect=second_fails)
    sf = h.client()
    with pytest.raises(esf.PartialFailure) as ei:
        h.run(sf.leads.convert_many([esf.LeadConvert(lead_id(i), "x") for i in range(101)]))
    results = ei.value.results
    assert results is not None
    assert results.results[100].errors[0].extended["outcome"] == "unknown"
