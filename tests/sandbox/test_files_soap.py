"""Files (§9.9) and the Partner SOAP calls for lead convert and merge (§9.8) against a real org.

Evidence for big-bang.md §21 V11, V35, V37, V38 and V39, on both clients. Every Lead carries
the run tag in ``LastName`` and ``Company`` (so the Account and Opportunity a conversion
creates carry it too); everything a test creates is deleted by the test and registered with
the session cleanup as a safety net. The Company values that fail on purpose (``ESF_VR``,
``ESF_FLOW``, ``ESF_ADDERROR``, ``ESF_THROW``) are never used here.

Set ``ESF_SANDBOX_RECORD_DIR=<dir>`` to write a redacted recording (``esf.testing.record``) of
each test to ``<dir>/<test>-<async|sync>.json``; ``tests/fixtures/recorded/{soap,files}`` are cut
from such recordings.

Sources:

- *Platform SOAP API login() Retirement* (help, updated 9 Oct 2026): only ``login()`` retires;
  OAuth is the replacement (found by title on help.salesforce.com; search excerpts only).
  *SOAP API End-of-Life Policy*: https://developer.salesforce.com/docs/atlas.en-us.api.meta/api/api_eol_soap.htm
- SOAP API Developer Guide, ``convertLead()``, ``merge()`` and *Error Handling*:
  https://developer.salesforce.com/docs/atlas.en-us.api.meta/api/sforce_api_calls_convertlead.htm,
  https://developer.salesforce.com/docs/atlas.en-us.api.meta/api/sforce_api_calls_merge.htm,
  https://developer.salesforce.com/docs/atlas.en-us.api.meta/api/sforce_api_concepts_errorhandling.htm
- REST API Developer Guide, *Insert or Update Blob Data* and *Get Blob Data*:
  https://developer.salesforce.com/docs/atlas.en-us.api_rest.meta/api_rest/dome_sobject_insert_update_blob.htm
- Object Reference, *ContentVersion* (``FirstPublishLocationId``) and *ContentDocumentLink*:
  https://developer.salesforce.com/docs/atlas.en-us.object_reference.meta/object_reference/sforce_api_objects_contentversion.htm
"""

from __future__ import annotations

import base64
import os
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

import even_simpler_salesforce as esf
from even_simpler_salesforce._async.api import soap as async_soap
from even_simpler_salesforce._sync.api import soap as sync_soap
from even_simpler_salesforce.core.soap import _envelope, merge_envelope  # pyright: ignore[reportPrivateUsage]

from .conftest import Cleanup, Driver

pytestmark = pytest.mark.sandbox

CONVERTED = "Closed - Converted"  # the converted LeadStatus of a fresh Developer Edition org
NOT_CONVERTED = "Open - Not Contacted"
SOAP_HEADERS = {"Content-Type": "text/xml; charset=UTF-8", "SOAPAction": '""', "Accept": "text/xml"}
FILE = b"esf sandbox file\n" * 40  # 680 bytes, compressible text
# A caller's own httpx client keeps httpx's 5 s default; use the client's defaults instead.
TIMEOUT = httpx.Timeout(connect=5.0, read=60.0, write=60.0, pool=5.0)


class Wire:
    """Response headers of every exchange, captured with an httpx event hook."""

    def __init__(self) -> None:
        self.exchanges: list[tuple[httpx.Request, httpx.Response]] = []

    def last(self, path_end: str) -> tuple[httpx.Request, httpx.Response]:
        return next(x for x in reversed(self.exchanges) if x[0].url.path.endswith(path_end))


@pytest.fixture
def wired(sb: Driver, request: pytest.FixtureRequest) -> Iterator[Callable[[], tuple[Any, Wire]]]:
    """``wired()`` → (client, Wire): a client on its own httpx client, recorded if asked."""
    opened: list[Any] = []

    def make() -> tuple[Any, Wire]:
        wire = Wire()
        record_dir = os.environ.get("ESF_SANDBOX_RECORD_DIR")
        transport: Any = None
        if record_dir:
            name = request.node.originalname.removeprefix("test_")
            transport = esf.testing.record(Path(record_dir) / f"{name}-{sb.kind}.json")
        if sb.is_async:

            async def ahook(resp: httpx.Response) -> None:
                wire.exchanges.append((resp.request, resp))

            http: Any = httpx.AsyncClient(
                transport=transport, timeout=TIMEOUT, event_hooks={"response": [ahook]}
            )
        else:

            def hook(resp: httpx.Response) -> None:
                wire.exchanges.append((resp.request, resp))

            http = httpx.Client(transport=transport, timeout=TIMEOUT, event_hooks={"response": [hook]})
        opened.append(http)
        return sb.client(http_client=http), wire

    yield make
    for http in opened:
        sb.run(http.aclose() if sb.is_async else http.close())


def _soap(sb: Driver) -> Any:
    return async_soap if sb.is_async else sync_soap


def _lead(sb: Driver, sf: Any, tag: str, cleanup: Cleanup, suffix: str) -> str:
    name = f"{tag}-{suffix}-{sb.kind}"
    lead_id: str = sb.run(sf.sobject("Lead").create({"LastName": name, "Company": f"{name}-co"}))
    cleanup.add(lead_id)
    return lead_id


def _delete(sb: Driver, sf: Any, cleanup: Cleanup, ids: list[str | None]) -> None:
    """Delete in dependency order (children first); ignore records already gone."""
    real = [i for i in ids if i]
    if real:
        sb.run(sf.collections.delete(real))
        cleanup.discard(*real)


# --- V11 / V35: lead convert and merge over Partner SOAP with an OAuth session ----------------


@pytest.mark.usefixtures("writes")
def test_convert_lead(sb: Driver, tag: str, cleanup: Cleanup, wired: Callable[[], tuple[Any, Wire]]) -> None:
    """V11: ``convertLead`` works with the OAuth access token as ``SessionHeader.sessionId``."""
    sf, wire = wired()
    lead = _lead(sb, sf, tag, cleanup, "convert")
    res = sb.run(sf.leads.convert(lead, converted_status=CONVERTED, do_not_create_opportunity=True))
    cleanup.add(res.account_id, res.contact_id)
    try:
        assert res.ok
        assert res.lead_id == lead
        assert res.account_id.startswith("001")
        assert res.contact_id.startswith("003")
        assert res.opportunity_id is None
        got = sb.run(sf.sobject("Lead").get(lead, ["IsConverted", "ConvertedAccountId"]))
        assert got["IsConverted"] is True
        assert got["ConvertedAccountId"] == res.account_id
        req, resp = wire.last("/services/Soap/u/" + sf.api_version)
        assert "authorization" not in req.headers  # the token travels only in the SOAP header
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/xml")
    finally:
        _delete(sb, sf, cleanup, [res.contact_id, res.account_id, lead])  # converted leads can be deleted


@pytest.mark.usefixtures("writes")
def test_convert_many_mixed(
    sb: Driver, tag: str, cleanup: Cleanup, wired: Callable[[], tuple[Any, Wire]]
) -> None:
    """V35: one ``convertLead`` call, results in input order; a bad status fails only its row."""
    sf, _ = wired()
    ok_lead, bad_lead = _lead(sb, sf, tag, cleanup, "many-ok"), _lead(sb, sf, tag, cleanup, "many-bad")
    batch = sb.run(
        sf.leads.convert_many(
            [
                esf.LeadConvert(ok_lead, CONVERTED, opportunity_name=f"{tag}-opp-{sb.kind}"),
                esf.LeadConvert(bad_lead, NOT_CONVERTED),
            ]
        )
    )
    ok, bad = list(batch)
    cleanup.add(ok.account_id, ok.contact_id, ok.opportunity_id)
    try:
        assert ok.ok
        assert ok.lead_id == ok_lead
        assert ok.opportunity_id
        assert ok.opportunity_id.startswith("006")
        assert not bad.ok
        assert bad.lead_id == bad_lead  # Salesforce echoes the lead id also for a failed row
        assert [(e.error_code, e.fields) for e in bad.errors] == [("INVALID_STATUS", ("Status",))]
    finally:
        _delete(sb, sf, cleanup, [ok.opportunity_id, ok.contact_id, ok.account_id, ok_lead, bad_lead])


@pytest.mark.usefixtures("writes")
def test_convert_failed(
    sb: Driver, tag: str, cleanup: Cleanup, wired: Callable[[], tuple[Any, Wire]]
) -> None:
    """V35: a refused conversion is ``success=false`` in an HTTP 200 response, raised as a RecordError."""
    sf, wire = wired()
    lead = _lead(sb, sf, tag, cleanup, "convert-bad")
    try:
        with pytest.raises(esf.RecordError) as ei:
            sb.run(sf.leads.convert(lead, converted_status="esf-no-such-status"))
        assert ei.value.error_code == "INVALID_STATUS"
        assert ei.value.fields == ("Status",)
        assert wire.last("/services/Soap/u/" + sf.api_version)[1].status_code == 200
    finally:
        _delete(sb, sf, cleanup, [lead])


@pytest.mark.usefixtures("writes")
def test_convert_element_order(sb: Driver, cleanup: Cleanup, wired: Callable[[], tuple[Any, Wire]]) -> None:
    """V35: ``LeadConvert`` elements must follow the WSDL sequence (alphabetical).

    With ``leadId`` sent before ``convertedStatus`` Salesforce doesn't fault: it fails the row with
    ``UNKNOWN_EXCEPTION`` "invalid parameter value" (org-observed, API v68.0). The id doesn't
    exist, so nothing is converted either way.
    """
    sf, _ = wired()
    body = (
        "<p:convertLead><p:leadConverts><p:leadId>00Q000000000000AAA</p:leadId>"
        f"<p:convertedStatus>{CONVERTED}</p:convertedStatus></p:leadConverts></p:convertLead>"
    )
    raw = sb.run(
        _soap(sb).soap_send(
            sf, lambda s: _envelope(s, body), operation="leads.convert", sobject="Lead", records=1
        )
    )
    assert raw.status == 200
    assert b"<statusCode>UNKNOWN_EXCEPTION</statusCode>" in raw.content
    assert b"invalid parameter value" in raw.content


@pytest.mark.usefixtures("writes")
def test_merge(sb: Driver, tag: str, cleanup: Cleanup, wired: Callable[[], tuple[Any, Wire]]) -> None:
    """V35: ``merge`` with ``request`` / ``masterRecord`` (``urn:sobject``) / ``recordToMergeIds``."""
    sf, _ = wired()
    master, dup1, dup2 = (_lead(sb, sf, tag, cleanup, f"merge{i}") for i in range(3))
    try:
        res = sb.run(sf.merge(master, [dup1, dup2], sobject="Lead"))
        assert res.ok
        assert res.id == master
        assert set(res.merged_ids) == {dup1, dup2}
        with pytest.raises(esf.NotFound):
            sb.run(sf.sobject("Lead").get(dup1, ["Id"]))
        # Merging a record that is already merged (deleted) fails its row, not the call.
        with pytest.raises(esf.NotFound) as ei:
            sb.run(sf.merge(master, [dup1], sobject="Lead"))
        assert ei.value.error_code == "ENTITY_IS_DELETED"
    finally:
        _delete(sb, sf, cleanup, [master])
        cleanup.discard(dup1, dup2)


@pytest.mark.usefixtures("writes")
def test_merge_unsupported_type_is_fault(sb: Driver, wired: Callable[[], tuple[Any, Wire]]) -> None:
    """V35: a call-level error is a SOAP fault: HTTP 500 with ``detail/*Fault/exceptionCode``.

    ``sf.merge`` refuses Opportunity locally, so this sends the envelope directly. Nothing is
    merged (the type check fails first; the ids don't exist).
    """
    sf, wire = wired()
    with pytest.raises(esf.SoapFault) as ei:
        sb.run(
            _soap(sb).soap_send(
                sf,
                lambda s: merge_envelope("Opportunity", "006000000000000AAA", ["006000000000001AAA"], s),
                operation="merge",
                sobject="Opportunity",
                records=2,
            )
        )
    assert ei.value.status == 500
    assert ei.value.error_code == "INVALID_TYPE_FOR_OPERATION"
    assert ei.value.errors[0].extended == {
        "fault_code": "sf:INVALID_TYPE_FOR_OPERATION",
        "fault_type": "UnexpectedErrorFault",
    }
    assert wire.last("/services/Soap/u/" + sf.api_version)[1].status_code == 500


def test_invalid_session_is_a_fault(sb: Driver, settings: Any) -> None:
    """V35: a bad session id is an HTTP 500 ``INVALID_SESSION_ID`` fault, not a 401.

    One raw request with an obviously bogus session id (the shared token is never touched or
    invalidated). Sync only, so the run sends exactly one such request.
    """
    if sb.is_async:
        pytest.skip("one raw request is enough; run on the sync client only")
    with sb.client() as sf:
        sf.limits()  # learn the instance URL from the current token (no forced refresh)
        instance, version = sf.instance_url, sf.api_version
    record_dir = os.environ.get("ESF_SANDBOX_RECORD_DIR")
    transport = esf.testing.record(Path(record_dir) / "invalid_session-sync.json") if record_dir else None
    body = merge_envelope("Lead", "00Q000000000000AAA", ["00Q000000000001AAA"], "esf-sandbox-bogus-session")
    with httpx.Client(transport=transport, timeout=TIMEOUT) as http:
        resp = http.post(f"{instance}/services/Soap/u/{version}", content=body, headers=SOAP_HEADERS)
    assert resp.status_code == 500
    assert resp.headers["content-type"].startswith("text/xml")
    assert "<faultcode>sf:INVALID_SESSION_ID</faultcode>" in resp.text
    assert "<sf:exceptionCode>INVALID_SESSION_ID</sf:exceptionCode>" in resp.text


# --- V37 / V38 / V39: files ------------------------------------------------------------------


@pytest.mark.usefixtures("writes")
def test_upload_sized_and_chunked(
    sb: Driver, tag: str, cleanup: Cleanup, wired: Callable[[], tuple[Any, Wire]]
) -> None:
    """V37: multipart upload with ``Content-Length`` (bytes) and with chunked transfer (iterator)."""
    sf, wire = wired()
    docs: list[str] = []
    try:
        sized = sb.run(sf.files.upload(FILE, filename=f"{tag}-sized.txt", title=f"{tag}-sized-{sb.kind}"))
        req, resp = wire.last("/sobjects/ContentVersion")
        assert resp.status_code == 201
        assert req.headers["content-length"]
        assert "transfer-encoding" not in req.headers
        assert req.headers["content-type"].startswith('multipart/form-data; boundary="esf-')
        docs.append(
            sb.run(sf.sobject("ContentVersion").get(sized, ["ContentDocumentId"]))["ContentDocumentId"]
        )

        chunks = [FILE[:300], FILE[300:]]
        if sb.is_async:

            async def agen() -> Any:
                for c in chunks:
                    yield c

            stream: Any = agen()
        else:
            stream = iter(chunks)
        chunked = sb.run(
            sf.files.upload(stream, filename=f"{tag}-chunked.txt", title=f"{tag}-chunked-{sb.kind}")
        )
        req, resp = wire.last("/sobjects/ContentVersion")
        assert resp.status_code == 201
        assert req.headers["transfer-encoding"] == "chunked"
        assert "content-length" not in req.headers
        docs.append(
            sb.run(sf.sobject("ContentVersion").get(chunked, ["ContentDocumentId"]))["ContentDocumentId"]
        )
        assert sb.run(sf.files.download_bytes(chunked)) == FILE
    finally:
        _delete(sb, sf, cleanup, list(docs))


@pytest.mark.usefixtures("writes")
def test_first_publish_location_link(
    sb: Driver, tag: str, cleanup: Cleanup, wired: Callable[[], tuple[Any, Wire]]
) -> None:
    """V39: ``FirstPublishLocationId`` on a record links the file with ``ShareType=V``, ``AllUsers``.

    Org-observed (Developer Edition, API v68.0, Lead): the docs don't state the share type or
    visibility of this link. An explicit ``share_type``/``visibility`` creates the link instead.
    """
    sf, _ = wired()
    lead = _lead(sb, sf, tag, cleanup, "files")
    docs: list[str] = []
    try:
        auto = sb.run(sf.files.upload(FILE, filename=f"{tag}-auto.txt", link_to=lead))
        explicit = sb.run(
            sf.files.upload(
                b"hello", filename=f"{tag}-explicit.txt", link_to=lead, share_type="I", visibility="AllUsers"
            )
        )
        links = {link.latest_version_id: link for link in sb.run(sf.files.list_for(lead))}
        docs.extend(link.content_document_id for link in links.values())
        assert (links[auto].share_type, links[auto].visibility) == ("V", "AllUsers")
        assert (links[explicit].share_type, links[explicit].visibility) == ("I", "AllUsers")
        assert links[auto].size == len(FILE)
        assert links[auto].file_extension == "txt"
    finally:
        _delete(sb, sf, cleanup, [*docs, lead])


@pytest.mark.usefixtures("writes")
def test_download(sb: Driver, tag: str, cleanup: Cleanup, wired: Callable[[], tuple[Any, Wire]]) -> None:
    """V38: VersionData and Attachment Body: content type, length, no gzip; missing ids.

    Org-observed (API v68.0): ``VersionData`` comes as ``application/octetstream`` (sic) with a
    ``Content-Length`` and no ``Content-Encoding`` although the client accepts gzip;
    ``Attachment/{id}/Body`` comes with the attachment's own ``ContentType``. A missing id is a
    JSON ``NOT_FOUND`` 404.
    """
    sf, wire = wired()
    lead = _lead(sb, sf, tag, cleanup, "download")
    cv = sb.run(sf.files.upload(FILE, filename=f"{tag}-dl.txt", link_to=lead))
    doc = sb.run(sf.sobject("ContentVersion").get(cv, ["ContentDocumentId"]))["ContentDocumentId"]
    cleanup.add(doc)
    att = sb.run(
        sf.sobject("Attachment").create(
            {
                "ParentId": lead,
                "Name": f"{tag}.txt",
                "Body": base64.b64encode(FILE).decode(),
                "ContentType": "text/plain",
            }
        )
    )
    try:
        assert sb.run(sf.files.download_bytes(cv)) == FILE
        req, resp = wire.last("/VersionData")
        assert req.headers["accept"] == "*/*"
        assert "gzip" in req.headers["accept-encoding"]
        assert resp.headers["content-type"] == "application/octetstream"
        # Org-observed: usually Content-Length, but one run (2026-11-12) answered without it
        # (chunked). The client doesn't rely on either; the body above is complete.
        assert resp.headers.get("content-length") in (str(len(FILE)), None)
        assert "content-encoding" not in resp.headers

        if sb.is_async:

            async def read() -> bytes:
                async with sf.files.download(cv) as stream:
                    return b"".join([c async for c in stream])

            streamed = sb.run(read())
        else:
            with sf.files.download(cv) as stream:
                streamed = b"".join(stream)
        assert streamed == FILE

        assert sb.run(sf.files.download_attachment(att)) == FILE
        _, resp = wire.last("/Body")
        assert resp.headers["content-type"] == "text/plain"
        assert resp.headers.get("content-length") in (str(len(FILE)), None)
        assert "content-encoding" not in resp.headers
    finally:
        _delete(sb, sf, cleanup, [doc, lead])  # deleting the lead deletes the attachment

    # Missing ids: the deleted file version and attachment.
    with pytest.raises(esf.NotFound) as ei:
        sb.run(sf.files.download_bytes(cv))
    assert ei.value.status == 404
    assert ei.value.error_code == "NOT_FOUND"
    _, resp = wire.last("/VersionData")
    assert resp.headers["content-type"].startswith("application/json")
    with pytest.raises(esf.NotFound) as ei:
        sb.run(sf.files.download_attachment(att))
    assert ei.value.status == 404
    assert ei.value.error_code == "NOT_FOUND"
