"""``core.soap`` and ``core.events``: envelopes, hardened XML parsing, faults, publish results.

Response shapes are **synthetic**, from the SOAP API Developer Guide (convertLead(),
LeadConvertResult, merge(), Error Handling) and the Platform Events Developer Guide; URLs in
``core/soap.py`` and ``core/events.py``.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET

import pytest

import even_simpler_salesforce as esf
from even_simpler_salesforce.core import events, soap
from even_simpler_salesforce.core.classify import classify
from even_simpler_salesforce.core.http import RawResponse, RequestSpec
from even_simpler_salesforce.errors import ErrorDetail

from ..fixtures import load_fixture

P = "{urn:partner.soap.sforce.com}"
ENV = "{http://schemas.xmlsoap.org/soap/envelope/}"
LEAD = "00Qxx0000000001AAA"
ACC = "001xx0000000001AAA"
HEAD = (
    '<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" '
    'xmlns="urn:partner.soap.sforce.com">'
)


def env(body: str) -> bytes:
    return f"{HEAD}<soapenv:Body>{body}</soapenv:Body></soapenv:Envelope>".encode()


def fixture(name: str) -> bytes:
    return load_fixture(f"soap/{name}").encode()


# --- envelopes ---------------------------------------------------------------------------------


def test_soap_path_and_detection() -> None:
    assert soap.soap_path("68.0") == "/services/Soap/u/68.0"
    assert soap.is_soap_path("/services/Soap/u/68.0?x=1")
    assert not soap.is_soap_path("sobjects/Lead")


def test_convert_envelope_minimal_and_escaped() -> None:
    root = ET.fromstring(soap.convert_lead_envelope([esf.LeadConvert(LEAD, "A & <B>")], "tok&<>"))
    assert root.findtext(f"{ENV}Header/{P}SessionHeader/{P}sessionId") == "tok&<>"
    conv = root.find(f"{ENV}Body/{P}convertLead/{P}leadConverts")
    assert conv is not None
    assert [(c.tag.removeprefix(P), c.text) for c in conv] == [
        ("convertedStatus", "A & <B>"),
        ("doNotCreateOpportunity", "false"),
        ("leadId", LEAD),
        ("overwriteLeadSource", "false"),
        ("sendNotificationEmail", "false"),
    ]


def test_convert_envelope_all_fields_in_wsdl_order() -> None:
    c = esf.LeadConvert(
        LEAD,
        "Closed",
        account_id=ACC,
        contact_id="003xx0000000001AAA",
        opportunity_name="Deal",
        do_not_create_opportunity=False,
        owner_id="005xx0000000001AAA",
        send_email=True,
        overwrite_lead_source=True,
    )
    root = ET.fromstring(soap.convert_lead_envelope([c, c], "t"))
    convs = root.findall(f"{ENV}Body/{P}convertLead/{P}leadConverts")
    assert len(convs) == 2
    names = [e.tag.removeprefix(P) for e in convs[0]]
    assert names == sorted(names)
    assert names == [
        "accountId",
        "contactId",
        "convertedStatus",
        "doNotCreateOpportunity",
        "leadId",
        "opportunityName",
        "overwriteLeadSource",
        "ownerId",
        "sendNotificationEmail",
    ]


def test_merge_envelope() -> None:
    root = ET.fromstring(soap.merge_envelope("Account", ACC, ["001xx0000000002AAA"], "t"))
    req = root.find(f"{ENV}Body/{P}merge/{P}request")
    assert req is not None
    assert req.findtext(f"{P}masterRecord/{{urn:sobject.partner.soap.sforce.com}}type") == "Account"
    assert [e.text for e in req.findall(f"{P}recordToMergeIds")] == ["001xx0000000002AAA"]


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"lead_id": "x", "converted_status": "s"}, "invalid Salesforce id"),
        ({"lead_id": LEAD, "converted_status": ""}, "converted_status"),
        ({"lead_id": LEAD, "converted_status": "s", "owner_id": "bad"}, "invalid Salesforce id"),
        ({"lead_id": LEAD, "converted_status": "s", "contact_id": "003xx0000000001AAA"}, "account_id"),
    ],
)
def test_lead_convert_validation(kwargs: dict[str, str], match: str) -> None:
    with pytest.raises(ValueError, match=match):
        esf.LeadConvert(**kwargs)  # type: ignore[arg-type]


def test_lead_converts_rejects_other_types() -> None:
    assert soap.lead_converts(iter([esf.LeadConvert(LEAD, "s")])) == [esf.LeadConvert(LEAD, "s")]
    with pytest.raises(TypeError, match="LeadConvert"):
        soap.lead_converts([LEAD])  # type: ignore[list-item]


# --- hardened parser ---------------------------------------------------------------------------


def test_parse_xml_keeps_namespaces_and_attributes() -> None:
    root = soap.parse_xml(b'<a xmlns="urn:x" xmlns:q="urn:q" q:k="v" plain="p"><b>t</b></a>')
    assert root.tag == "{urn:x}a"
    assert root.get("{urn:q}k") == "v"
    assert root.get("plain") == "p"
    assert root.findtext("{urn:x}b") == "t"


@pytest.mark.parametrize(
    "doc",
    [
        b'<!DOCTYPE a [<!ENTITY x "y">]><a>&x;</a>',
        b'<!DOCTYPE a SYSTEM "http://example.com/a.dtd"><a/>',
        b"<!DOCTYPE a><a/>",
    ],
)
def test_parse_xml_refuses_dtds(doc: bytes) -> None:
    with pytest.raises(esf.UnexpectedResponse, match="DTD"):
        soap.parse_xml(doc)


def test_parse_xml_refuses_entity_declarations_without_doctype_handler() -> None:
    # The entity handlers are a second line of defence; reach them directly.
    with pytest.raises(soap._Refused):  # pyright: ignore[reportPrivateUsage]
        soap._refuse("x")  # pyright: ignore[reportPrivateUsage]


@pytest.mark.parametrize("doc", [b"<a>", b"not xml", b"<a>&undefined;</a>"])
def test_parse_xml_malformed(doc: bytes) -> None:
    with pytest.raises(esf.UnexpectedResponse, match="not valid XML"):
        soap.parse_xml(doc)


# --- results -----------------------------------------------------------------------------------


def test_parse_convert_results() -> None:
    """Synthetic fixture ``soap/convert_lead.json``."""
    ok, bad = soap.parse_convert_results(fixture("convert_lead.json"), 2)
    assert ok == esf.LeadConvertResult(
        ok=True,
        lead_id=LEAD,
        account_id=ACC,
        contact_id="003xx0000000001AAA",
        opportunity_id="006xx0000000001AAA",
    )
    assert not bad.ok
    assert bad.lead_id is None
    assert bad.errors == (ErrorDetail("INVALID_STATUS", "invalid convertedStatus: Qualified", ("Status",)),)


def test_parse_merge_results() -> None:
    """Synthetic fixture ``soap/merge.json``."""
    (res,) = soap.parse_merge_results(fixture("merge.json"))
    assert res.ok
    assert res.id == ACC
    assert res.merged_ids == ("001xx0000000002AAA", "001xx0000000003AAA")
    assert res.updated_related_ids == ("003xx0000000001AAA",)


def test_result_with_empty_values() -> None:
    body = env(
        "<mergeResponse><result><id> </id><mergedRecordIds/><errors><message/><statusCode/>"
        "</errors><success>false</success></result></mergeResponse>"
    )
    (res,) = soap.parse_merge_results(body)
    assert res.id is None
    assert res.merged_ids == ()
    assert res.errors == (ErrorDetail(None, ""),)


@pytest.mark.parametrize(
    ("body", "match"),
    [
        (b"<other/>", "not a SOAP envelope"),
        (HEAD.encode() + b"</soapenv:Envelope>", "no Body"),
        (env("<somethingElse/>"), "no convertLeadResponse"),
        (env("<convertLeadResponse/>"), "expected 1 results"),
    ],
)
def test_parse_results_unexpected(body: bytes, match: str) -> None:
    with pytest.raises(esf.UnexpectedResponse, match=match):
        soap.parse_convert_results(body, 1)


# --- faults ------------------------------------------------------------------------------------


def test_parse_fault_with_detail() -> None:
    """Synthetic fixture ``soap/fault_invalid_session.json``."""
    d = soap.parse_fault(fixture("fault_invalid_session.json"))
    assert d is not None
    assert d.error_code == "INVALID_SESSION_ID"
    assert d.message == "Invalid Session ID found in SessionHeader: Illegal Session"
    assert d.extended == {"fault_code": "sf:INVALID_SESSION_ID", "fault_type": "UnexpectedErrorFault"}


def test_parse_fault_without_detail_uses_faultcode() -> None:
    body = env(
        "<soapenv:Fault><faultcode>soapenv:Client</faultcode><faultstring>bad</faultstring></soapenv:Fault>"
    )
    d = soap.parse_fault(body)
    assert d == ErrorDetail("Client", "bad", extended={"fault_code": "soapenv:Client"})


def test_parse_fault_empty() -> None:
    d = soap.parse_fault(env("<soapenv:Fault><detail/></soapenv:Fault>"))
    assert d == ErrorDetail(None, "", extended={"fault_code": None})


def test_parse_fault_none_when_no_fault() -> None:
    assert soap.parse_fault(fixture("merge.json")) is None


# --- classify ----------------------------------------------------------------------------------

SPEC = RequestSpec("POST", "/services/Soap/u/68.0", operation="merge", sobject="Account", dml=True)


def xml_response(status: int, content: bytes, headers: dict[str, str] | None = None) -> RawResponse:
    return RawResponse(status, {"content-type": "text/xml; charset=utf-8", **(headers or {})}, content)


def fault(code: str, message: str = "m") -> bytes:
    return env(
        f"<soapenv:Fault><faultcode>sf:{code}</faultcode><faultstring>{code}: {message}</faultstring>"
        '<detail><sf:ApiFault xmlns:sf="urn:fault.partner.soap.sforce.com">'
        f"<sf:exceptionCode>{code}</sf:exceptionCode><sf:exceptionMessage>{message}</sf:exceptionMessage>"
        "</sf:ApiFault></detail></soapenv:Fault>"
    )


def test_classify_soap_success_is_none() -> None:
    assert classify(xml_response(200, fixture("merge.json")), SPEC) is None


def test_classify_known_fault_code() -> None:
    err = classify(xml_response(500, fault("INVALID_FIELD", "No such column")), SPEC)
    assert isinstance(err, esf.InvalidField)
    assert err.status == 500
    assert err.operation == "merge"
    assert err.sobject == "Account"
    assert err.retryable is False


def test_classify_session_fault() -> None:
    err = classify(xml_response(500, fixture("fault_invalid_session.json")), SPEC)
    assert isinstance(err, esf.SessionExpired)
    assert err.error_code == "INVALID_SESSION_ID"


def test_classify_unknown_fault_is_soap_fault() -> None:
    err = classify(xml_response(500, fixture("fault_invalid_id.json")), SPEC)
    assert type(err) is esf.SoapFault
    assert err.fault_code == "sf:INVALID_ID_FIELD"


def test_classify_fault_retry_after_and_server_unavailable() -> None:
    err = classify(xml_response(500, fault("REQUEST_LIMIT_EXCEEDED"), {"retry-after": "7"}), SPEC)
    assert isinstance(err, esf.RequestLimitExceeded)
    assert err.retry_after == 7.0
    err = classify(xml_response(500, fault("SERVER_UNAVAILABLE")), SPEC)
    assert isinstance(err, esf.ServerError)
    assert err.retryable is True


def test_classify_fault_tags() -> None:
    class Custom(esf.SoapFault):
        pass

    err = classify(xml_response(500, fault("WEIRD", "[MY_TAG] hello")), SPEC, {"MY_TAG": Custom})
    assert type(err) is Custom
    assert err.tag == "MY_TAG"
    assert err.message == "hello"


def test_classify_soap_without_fault_falls_back() -> None:
    err = classify(xml_response(500, b"<not-an-envelope/>"), SPEC)
    assert isinstance(err, esf.UnexpectedResponse)
    assert err.retryable is True
    err = classify(xml_response(500, fixture("merge.json")), SPEC)
    assert isinstance(err, esf.UnexpectedResponse)
    # A REST path with an XML body is not parsed as SOAP.
    err = classify(xml_response(500, fault("INVALID_FIELD")), RequestSpec("POST", "sobjects/Lead"))
    assert isinstance(err, esf.UnexpectedResponse)


def test_session_body_spec() -> None:
    spec = RequestSpec("POST", "/services/Soap/u/68.0", session_body=lambda t: t.encode())
    assert spec.session_body is not None
    assert spec.session_body("x") == b"x"
    assert "session_body" not in repr(spec)


# --- platform events ---------------------------------------------------------------------------


def test_check_event_name() -> None:
    assert events.check_event_name("Order__e") == "Order__e"
    with pytest.raises(ValueError, match="__e"):
        events.check_event_name("Order__c")
    with pytest.raises(ValueError, match="invalid sObject"):
        events.check_event_name("bad name__e")


def test_publish_result_moves_enqueued_to_uuid() -> None:
    enq = ErrorDetail("OPERATION_ENQUEUED", "u-1")
    other = ErrorDetail("X", "y")
    r = events.publish_result(
        esf.RecordResult(ok=True, id="e00", errors=(enq, ErrorDetail("OPERATION_ENQUEUED", "u-2"), other))
    )
    assert r == esf.PublishResult(
        ok=True, id="e00", uuid="u-1", errors=(ErrorDetail("OPERATION_ENQUEUED", "u-2"), other)
    )
    assert (
        events.publish_result(esf.RecordResult(ok=True, errors=(ErrorDetail("OPERATION_ENQUEUED", ""),))).uuid
        is None
    )


def test_parse_publish() -> None:
    """Synthetic fixture ``platform_events/publish.json``."""
    r = events.parse_publish(load_fixture("platform_events/publish.json"))
    assert r.ok
    assert r.id == "e00xx0000000001AAA"
    assert r.uuid
    assert r.errors == ()
    assert events.parse_publish({"success": True}) == esf.PublishResult(ok=True)
    with pytest.raises(esf.UnexpectedResponse):
        events.parse_publish([])
    with pytest.raises(esf.UnexpectedResponse):
        events.parse_publish({"id": "x"})


def test_event_error_mapping() -> None:
    plain = esf.RecordError("x", error_code="LIMIT_EXCEEDED", status=400, operation="platform_events.publish")
    mapped = events.event_error(plain)
    assert type(mapped) is esf.EventPublishFailed
    assert (mapped.error_code, mapped.status, mapped.operation, mapped.message) == (
        "LIMIT_EXCEEDED",
        400,
        "platform_events.publish",
        "x",
    )
    assert str(mapped).splitlines()[0] == str(plain).splitlines()[0]
    assert str(mapped).endswith("#eventpublishfailed")

    unknown = events.event_error(esf.RecordError("y", error_code="SOMETHING_NEW"))
    assert type(unknown) is esf.EventPublishFailed

    server = esf.ServerError(
        "z", error_code="PLATFORM_EVENT_PUBLISHING_UNAVAILABLE", status=503, retryable=False
    )
    down = events.event_error(server)
    assert type(down) is esf.EventPublishUnavailable
    assert down.retryable is True

    same = esf.EventPublishFailed("w", error_code="LIMIT_EXCEEDED")
    assert events.event_error(same) is same
    keep = esf.RequiredFieldMissing("v", error_code="REQUIRED_FIELD_MISSING")
    assert events.event_error(keep) is keep
    session = esf.SessionExpired("s")
    assert events.event_error(session) is session


def test_event_errors_pickle() -> None:
    import pickle

    err = esf.EventPublishUnavailable("down", error_code="PLATFORM_EVENT_PUBLISHING_UNAVAILABLE")
    back = pickle.loads(pickle.dumps(err))
    assert type(back) is esf.EventPublishUnavailable
    assert back.retryable is True


def test_body_found_after_header() -> None:
    body = (
        HEAD + "<soapenv:Header/><soapenv:Body><mergeResponse><result><id>" + ACC + "</id>"
        "<success>true</success></result></mergeResponse></soapenv:Body></soapenv:Envelope>"
    ).encode()
    assert soap.parse_merge_results(body)[0].id == ACC


def test_event_error_points_at_its_own_docs_entry() -> None:
    from even_simpler_salesforce import errors as E
    from even_simpler_salesforce.core.events import event_error

    mapped = event_error(E.RecordError("x", error_code="PLATFORM_EVENT_PUBLISHING_UNAVAILABLE"))
    assert type(mapped) is E.EventPublishUnavailable
    assert mapped.docs_url == E.docs_url_for(E.EventPublishUnavailable)
    assert "#eventpublishunavailable" in str(mapped)
