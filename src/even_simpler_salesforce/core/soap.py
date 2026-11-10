"""Partner SOAP envelopes and parsers for lead convert and merge (§9.8, D14). No I/O.

REST has no resource for lead convert or merge, so these two calls use the Partner SOAP API
at ``/services/Soap/u/{version}`` with the OAuth access token as ``SessionHeader.sessionId``.
Everything here is pure: envelope builders take the session id as an argument (the transport
supplies it per attempt), and the parser refuses any DTD or entity declaration.

Sources (SOAP API Developer Guide):

- ``convertLead()``: https://developer.salesforce.com/docs/atlas.en-us.api.meta/api/sforce_api_calls_convertlead.htm
- ``LeadConvertResult``: https://developer.salesforce.com/docs/atlas.en-us.api.meta/api/sforce_api_calls_convertlead_leadconvertresult.htm
- ``merge()``: https://developer.salesforce.com/docs/atlas.en-us.api.meta/api/sforce_api_calls_merge.htm
- Error handling and faults: https://developer.salesforce.com/docs/atlas.en-us.api.meta/api/sforce_api_concepts_errorhandling.htm
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from collections.abc import Iterable, Sequence
from typing import Any, NoReturn
from xml.parsers import expat
from xml.sax.saxutils import escape

from ..errors import ErrorDetail, UnexpectedResponse
from ..models import LeadConvert, LeadConvertResult, MergeResult

__all__ = [
    "CONVERT_CHUNK",
    "MERGE_MAX_DUPLICATES",
    "MERGE_SOBJECTS",
    "NS_ENVELOPE",
    "NS_PARTNER",
    "NS_SOBJECT",
    "convert_lead_envelope",
    "is_soap_path",
    "lead_converts",
    "merge_envelope",
    "parse_convert_results",
    "parse_fault",
    "parse_merge_results",
    "parse_xml",
    "soap_path",
]

NS_ENVELOPE = "http://schemas.xmlsoap.org/soap/envelope/"
NS_PARTNER = "urn:partner.soap.sforce.com"
NS_SOBJECT = "urn:sobject.partner.soap.sforce.com"
NS_XSI = "http://www.w3.org/2001/XMLSchema-instance"

#: ``convertLead()`` accepts at most 100 ``LeadConvert`` entries per call.
CONVERT_CHUNK = 100
#: ``merge()`` merges a master record with at most two other records per request.
MERGE_MAX_DUPLICATES = 2
#: Objects the library merges (spec §9.8).
MERGE_SOBJECTS = frozenset({"Lead", "Contact", "Account"})

_RAW_LIMIT = 4096


def soap_path(api_version: str) -> str:
    """The Partner SOAP endpoint path for ``api_version``."""
    return f"/services/Soap/u/{api_version}"


def is_soap_path(path: str) -> bool:
    """Whether ``path`` is a SOAP endpoint (``/services/Soap/...``)."""
    return path.split("?", 1)[0].startswith("/services/Soap/")


# --- envelopes ---------------------------------------------------------------------------------


def _envelope(session_id: str, body: str) -> bytes:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<env:Envelope xmlns:env="{NS_ENVELOPE}" xmlns:p="{NS_PARTNER}" xmlns:o="{NS_SOBJECT}">'
        f"<env:Header><p:SessionHeader><p:sessionId>{escape(session_id)}</p:sessionId>"
        "</p:SessionHeader></env:Header>"
        f"<env:Body>{body}</env:Body></env:Envelope>"
    ).encode()


def _el(name: str, value: str | bool | None) -> str:
    if value is None:
        return ""
    text = ("true" if value else "false") if isinstance(value, bool) else escape(value)
    return f"<p:{name}>{text}</p:{name}>"


def convert_lead_envelope(converts: Sequence[LeadConvert], session_id: str) -> bytes:
    """``convertLead`` request with one ``leadConverts`` element per entry.

    Elements follow the ``LeadConvert`` sequence of the Partner WSDL (alphabetical); unset
    optional values are left out so Salesforce applies its defaults.
    """
    parts: list[str] = []
    for c in converts:
        parts.append(
            "<p:leadConverts>"
            + _el("accountId", c.account_id)
            + _el("contactId", c.contact_id)
            + _el("convertedStatus", c.converted_status)
            + _el("doNotCreateOpportunity", c.do_not_create_opportunity)
            + _el("leadId", c.lead_id)
            + _el("opportunityName", c.opportunity_name)
            + _el("overwriteLeadSource", c.overwrite_lead_source)
            + _el("ownerId", c.owner_id)
            + _el("sendNotificationEmail", c.send_email)
            + "</p:leadConverts>"
        )
    return _envelope(session_id, "<p:convertLead>" + "".join(parts) + "</p:convertLead>")


def merge_envelope(sobject: str, master_id: str, duplicate_ids: Sequence[str], session_id: str) -> bytes:
    """``merge`` request: one ``MergeRequest`` with the master record and the records to merge."""
    victims = "".join(f"<p:recordToMergeIds>{escape(i)}</p:recordToMergeIds>" for i in duplicate_ids)
    body = (
        "<p:merge><p:request><p:masterRecord>"
        f"<o:type>{escape(sobject)}</o:type><o:Id>{escape(master_id)}</o:Id>"
        f"</p:masterRecord>{victims}</p:request></p:merge>"
    )
    return _envelope(session_id, body)


# --- parsing -----------------------------------------------------------------------------------


class _Refused(Exception):
    pass


def _refuse(*_: Any) -> NoReturn:
    raise _Refused


def _qname(name: str) -> str:
    # expat with namespace_separator=" " reports "uri local"; ElementTree uses "{uri}local".
    uri, sep, local = name.rpartition(" ")
    return f"{{{uri}}}{local}" if sep else name


def parse_xml(content: bytes) -> ET.Element:
    """Parse an XML response into an ``Element``, refusing DTDs and entity declarations.

    The parser rejects any ``<!DOCTYPE`` (and so every entity declaration and external
    entity), which blocks entity-expansion and external-entity attacks. Stdlib only.

    Raises:
        UnexpectedResponse: the body is not well-formed XML, or it declares a DTD or entities.
    """
    builder = ET.TreeBuilder()
    parser = expat.ParserCreate(namespace_separator=" ")
    parser.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_NEVER)
    parser.StartDoctypeDeclHandler = _refuse
    parser.EntityDeclHandler = _refuse
    parser.UnparsedEntityDeclHandler = _refuse
    parser.ExternalEntityRefHandler = _refuse
    parser.buffer_text = True

    def start(name: str, attrs: dict[str, str]) -> None:
        builder.start(_qname(name), {_qname(k): v for k, v in attrs.items()})

    def end(name: str) -> None:
        builder.end(_qname(name))

    parser.StartElementHandler = start
    parser.EndElementHandler = end
    parser.CharacterDataHandler = builder.data
    try:
        parser.Parse(content, True)
    except _Refused:
        raise UnexpectedResponse(
            "XML response declares a DTD or entities; refused", raw=content[:_RAW_LIMIT]
        ) from None
    except expat.ExpatError as exc:
        raise UnexpectedResponse(f"response body is not valid XML: {exc}", raw=content[:_RAW_LIMIT]) from None
    return builder.close()


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _children(el: ET.Element, name: str) -> list[ET.Element]:
    return [c for c in el if _local(c.tag) == name]


def _text(el: ET.Element, name: str) -> str | None:
    for c in el:
        if _local(c.tag) == name:
            if c.get(f"{{{NS_XSI}}}nil") == "true":
                return None
            return (c.text or "").strip() or None
    return None


def _body(root: ET.Element) -> ET.Element:
    if root.tag != f"{{{NS_ENVELOPE}}}Envelope":
        raise UnexpectedResponse("not a SOAP envelope", raw=ET.tostring(root)[:_RAW_LIMIT])
    for c in root:
        if c.tag == f"{{{NS_ENVELOPE}}}Body":
            return c
    raise UnexpectedResponse("SOAP envelope has no Body", raw=ET.tostring(root)[:_RAW_LIMIT])


def parse_fault(content: bytes) -> ErrorDetail | None:
    """The fault of a SOAP response, or ``None`` when the body holds no ``Fault``.

    ``error_code`` is the ``exceptionCode`` of the fault detail (for example
    ``INVALID_SESSION_ID``), else the ``faultcode`` without its prefix. ``message`` is the
    ``exceptionMessage``, else the ``faultstring``. ``extended`` keeps ``fault_code`` (the raw
    ``faultcode``) and ``fault_type`` (the detail element, such as ``InvalidIdFault``).

    Raises:
        UnexpectedResponse: the body is not a well-formed SOAP envelope.
    """
    body = _body(parse_xml(content))
    fault = next((c for c in body if c.tag == f"{{{NS_ENVELOPE}}}Fault"), None)
    if fault is None:
        return None
    fault_code = _text(fault, "faultcode")
    fault_string = _text(fault, "faultstring") or ""
    detail_el = next(iter(_children(fault, "detail")), None)
    inner = next(iter(detail_el), None) if detail_el is not None else None
    code = _text(inner, "exceptionCode") if inner is not None else None
    message = (_text(inner, "exceptionMessage") if inner is not None else None) or fault_string
    if code is None and fault_code:
        code = fault_code.rsplit(":", 1)[-1]
    extended: dict[str, Any] = {"fault_code": fault_code}
    if inner is not None:
        extended["fault_type"] = _local(inner.tag)
    return ErrorDetail(error_code=code, message=message, extended=extended)


def _results(content: bytes, response: str, expected: int) -> list[ET.Element]:
    body = _body(parse_xml(content))
    wrapper = next(iter(_children(body, response)), None)
    if wrapper is None:
        raise UnexpectedResponse(f"SOAP response has no {response}", raw=content[:_RAW_LIMIT])
    results = _children(wrapper, "result")
    if len(results) != expected:
        raise UnexpectedResponse(
            f"expected {expected} results in {response}, got {len(results)}", raw=content[:_RAW_LIMIT]
        )
    return results


def _errors(result: ET.Element) -> tuple[ErrorDetail, ...]:
    out: list[ErrorDetail] = []
    for e in _children(result, "errors"):
        fields = tuple((f.text or "").strip() for f in _children(e, "fields") if (f.text or "").strip())
        out.append(
            ErrorDetail(error_code=_text(e, "statusCode"), message=_text(e, "message") or "", fields=fields)
        )
    return tuple(out)


def _ok(result: ET.Element) -> bool:
    return _text(result, "success") == "true"


def _ids(result: ET.Element, name: str) -> tuple[str, ...]:
    return tuple((c.text or "").strip() for c in _children(result, name) if (c.text or "").strip())


def parse_convert_results(content: bytes, expected: int) -> list[LeadConvertResult]:
    """Parse a ``convertLeadResponse`` into results in input order.

    Raises:
        UnexpectedResponse: not a SOAP envelope, or not ``expected`` results.
    """
    return [
        LeadConvertResult(
            ok=_ok(r),
            lead_id=_text(r, "leadId"),
            account_id=_text(r, "accountId"),
            contact_id=_text(r, "contactId"),
            opportunity_id=_text(r, "opportunityId"),
            errors=_errors(r),
        )
        for r in _results(content, "convertLeadResponse", expected)
    ]


def parse_merge_results(content: bytes, expected: int = 1) -> list[MergeResult]:
    """Parse a ``mergeResponse`` into results in request order.

    Raises:
        UnexpectedResponse: not a SOAP envelope, or not ``expected`` results.
    """
    return [
        MergeResult(
            ok=_ok(r),
            id=_text(r, "id"),
            merged_ids=_ids(r, "mergedRecordIds"),
            updated_related_ids=_ids(r, "updatedRelatedIds"),
            errors=_errors(r),
        )
        for r in _results(content, "mergeResponse", expected)
    ]


def lead_converts(items: Iterable[LeadConvert]) -> list[LeadConvert]:
    """Validate ``items`` (each must be a :class:`LeadConvert`) and return them as a list."""
    out: list[LeadConvert] = []
    for c in items:
        if not isinstance(c, LeadConvert):  # pyright: ignore[reportUnnecessaryIsInstance]
            raise TypeError(f"expected LeadConvert, got {type(c).__name__}")
        out.append(c)
    return out
