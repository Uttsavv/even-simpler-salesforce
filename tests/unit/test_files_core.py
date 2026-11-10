"""``core/files.py``: multipart envelopes, validation and ``FileLink`` parsing. No I/O.

Multipart layout from *Insert or Update Blob Data* (REST API Developer Guide,
https://developer.salesforce.com/docs/atlas.en-us.api_rest.meta/api_rest/dome_sobject_insert_update_blob.htm);
ShareType and Visibility values from the *ContentDocumentLink* Object Reference
(https://developer.salesforce.com/docs/atlas.en-us.object_reference.meta/object_reference/sforce_api_objects_contentdocumentlink.htm).
"""

from __future__ import annotations

import json
from email.parser import BytesParser
from email.policy import HTTP

import pytest

import even_simpler_salesforce as esf
from even_simpler_salesforce.core import files as F


def test_envelope_is_valid_multipart() -> None:
    entity = json.dumps(F.version_entity("a.pdf", None, None)).encode()
    head, tail, ctype = F.multipart_envelope(entity, 'q"uo\\te.pdf', "BOUNDARY")
    assert ctype == 'multipart/form-data; boundary="BOUNDARY"'
    body = head + b"FILE-BYTES" + tail
    msg = BytesParser(policy=HTTP).parsebytes(b"Content-Type: " + ctype.encode() + b"\r\n\r\n" + body)
    assert msg.is_multipart()
    first, second = msg.iter_parts()  # type: ignore[attr-defined]
    assert first.get_param("name", header="content-disposition") == "entity_content"
    assert first.get_content_type() == "application/json"
    assert json.loads(first.get_payload(decode=True)) == {"PathOnClient": "a.pdf", "Title": "a.pdf"}
    assert second.get_param("name", header="content-disposition") == "VersionData"
    assert second.get_filename() == 'q"uo\\te.pdf'
    assert second.get_content_type() == "application/octet-stream"
    assert second.get_payload(decode=True) == b"FILE-BYTES"


def test_version_entity() -> None:
    assert F.version_entity("a.pdf", "Invoice", "001xx0000000001AAA") == {
        "PathOnClient": "a.pdf",
        "Title": "Invoice",
        "FirstPublishLocationId": "001xx0000000001AAA",
    }
    assert F.version_entity("a.pdf", "", None)["Title"] == ""
    with pytest.raises(ValueError, match="invalid Salesforce id"):
        F.version_entity("a.pdf", None, "x")


def test_boundary_is_random() -> None:
    a, b = F.new_boundary(), F.new_boundary()
    assert a != b
    assert a.startswith("esf-")
    assert len(a) == 36


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("a.pdf", "a.pdf"),
        ("/tmp/x/a.pdf", "a.pdf"),
        ("C:\\Users\\me\\b.txt", "b.txt"),
        ("ü ñ.csv", "ü ñ.csv"),
    ],
)
def test_safe_filename(name: str, expected: str) -> None:
    assert F.safe_filename(name) == expected


@pytest.mark.parametrize("name", ["", "dir/", ".", "..", "a\nb", "a\x00b", "a\x7fb"])
def test_safe_filename_rejects(name: str) -> None:
    with pytest.raises(ValueError, match="filename"):
        F.safe_filename(name)


def test_share_type_and_visibility() -> None:
    for v in ("V", "C", "I"):
        assert F.check_share_type(v) == v
    for v in ("AllUsers", "InternalUsers", "SharedUsers"):
        assert F.check_visibility(v) == v
    with pytest.raises(ValueError, match="share_type"):
        F.check_share_type("v")
    with pytest.raises(ValueError, match="visibility"):
        F.check_visibility("allusers")


def test_parse_created_id() -> None:
    assert (
        F.parse_created_id({"id": "068xx0000000001AAA", "success": True}, "files.upload")
        == "068xx0000000001AAA"
    )
    for body in (None, [], {"success": True}, {"id": ""}):
        with pytest.raises(esf.UnexpectedResponse) as ei:
            F.parse_created_id(body, "files.link")
        assert ei.value.operation == "files.link"


def test_parse_file_link() -> None:
    row = {
        "Id": "06Axx0000000001AAA",
        "ContentDocumentId": "069xx0000000001AAA",
        "ShareType": "V",
        "Visibility": "AllUsers",
        "ContentDocument": {
            "Title": "T",
            "FileExtension": "pdf",
            "ContentSize": 12.0,
            "LatestPublishedVersionId": "068xx0000000001AAA",
        },
    }
    link = F.parse_file_link(row)
    assert link.size == 12
    assert link.latest_version_id == "068xx0000000001AAA"
    sparse = F.parse_file_link({"Id": "06Axx0000000001AAA", "ContentDocumentId": "069xx0000000001AAA"})
    assert sparse == esf.FileLink(
        "06Axx0000000001AAA", "069xx0000000001AAA", None, None, None, None, None, None
    )
    weird = F.parse_file_link({**row, "ContentDocument": {"ContentSize": True}})
    assert weird.size is None
    with pytest.raises(esf.UnexpectedResponse):
        F.parse_file_link({"ContentDocumentId": "069xx0000000001AAA"})


def test_list_for_soql_filters_on_linked_entity() -> None:
    q = esf.soql(F.LIST_FOR_SOQL, "001xx0000000001AAA")
    assert q.endswith("FROM ContentDocumentLink WHERE LinkedEntityId = '001xx0000000001AAA'")
